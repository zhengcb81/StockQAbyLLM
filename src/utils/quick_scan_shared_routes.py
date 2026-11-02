"""Ordered company scans on the original owner, clients and durable ledger.

Only explicit v1.6 approvals may select several models. Each model receives
its own immutable task; completed work and unknown sends stay original work.
"""

from __future__ import annotations

import copy

KNOWN_FAILURES = frozenset(
    {"authentication_rejected", "model_or_endpoint_unavailable", "quota_exhausted", "rate_limited"}
)


def next_route_questions(rows, targets, index):
    selected = []
    for row in rows:
        if row["checkpoint_id"] is not None:
            continue
        attempt = row["latest_attempt"]
        if attempt is None:
            selected.append(row["question_id"])
            continue
        matches = [
            i
            for i, t in enumerate(targets)
            if (t["route_id"], t["provider_config_ref"], t["model"])
            == (attempt["route_id"], attempt["provider"], attempt["model_requested"])
        ]
        if len(matches) != 1:
            raise ValueError("shared_company_original_route_mismatch")
        if attempt["phase"] == "abandoned_unsent":
            if index >= matches[0]:
                selected.append(row["question_id"])
            continue
        if attempt["phase"] == "prepared" and index == matches[0]:
            selected.append(row["question_id"])
            continue
        if (
            attempt["phase"] != "confirmed_failure"
            or attempt.get("failure_category") not in KNOWN_FAILURES
            or attempt.get("settled") is not True
        ):
            raise ValueError("shared_company_original_work_unresolved")
        if index > matches[0]:
            selected.append(row["question_id"])
    return selected


def routed_lifecycle(lifecycle, route, client):
    from src.runners.llm_runner import QuickScanWorkLifecycle
    from src.utils.quick_scan_refresh_execution import ApprovedRefreshSession

    if not isinstance(lifecycle, QuickScanWorkLifecycle) or not isinstance(
        lifecycle._owner_refresh, ApprovedRefreshSession
    ):
        raise ValueError("shared_company_original_owner_required")
    owner = lifecycle._owner_refresh
    if owner.ordered_route_targets is None:
        raise ValueError("shared_company_ordered_approval_required")
    approved = owner.execution_routes(lifecycle._budget_policy)
    if route not in approved:
        raise ValueError("shared_company_unapproved_route")
    owner.assert_provider_model(client.provider_name, client.model)
    target = next(t for t in owner.ordered_route_targets if t["route_id"] == route["id"])
    if (client.provider_name, client.model) != (target["provider"], target["model"]):
        raise ValueError("shared_company_original_client_mismatch")
    clone = copy.copy(lifecycle)
    clone._route_id = route["id"]
    clone._provider_name = route["provider_config_ref"]
    clone._model_requested = route["model"]
    from src.providers.model_resolution import normalize_model_resolution

    clone._model_resolution = normalize_model_resolution(route.get("model_resolution"))
    clone._budget_route = dict(
        route_id=route["id"],
        provider=route["provider_config_ref"],
        model_requested=route["model"],
        quota_group=route["quota_group"],
    )
    return clone


def run_ordered_company_scan(lifecycle, route_clients, *, config, questions, **options):
    from src.utils.quick_scan_shared_company import (
        SharedCompanyIncomplete,
        load_original_company_task,
        ordered_task_root,
        original_task_checkpoint,
        run_shared_company_scan,
    )

    owner = lifecycle._owner_refresh
    targets = owner.ordered_route_targets
    if targets is None:
        matching = [
            client
            for route, client in route_clients
            if route["id"] == lifecycle._route_id
            and client is not None
            and route["model"] == lifecycle._model_requested
        ]
        if len(matching) != 1:
            raise ValueError("shared_company_original_route_client_required")
        return run_shared_company_scan(
            lifecycle, matching[0], config=config, questions=questions, **options
        )
    owner.check()
    scope = [qid for qid in owner.requested_question_ids if owner.entries[qid]["kind"] == "work"]
    requested = {q.question_id for q in questions}
    if not options.get("recovery_only") and not set(scope) <= requested:
        raise ValueError("shared_company_complete_original_scope_required")
    if not scope:
        return None
    work_ids = [owner.entries[q]["work_item_id"] for q in scope]
    # Recover the original resource ceiling even if current UI config changed.
    cap = None
    for target in targets:
        root = ordered_task_root(config, owner, scope, target["route_id"])
        checkpoint = original_task_checkpoint(root)
        if checkpoint is not None:
            task = load_original_company_task(checkpoint)
            # The first task holds the original company cap; later tasks only
            # consume its remaining quota, never create another full allowance.
            cap = task.limits["max_model_requests_per_session"]
            break
    # A received answer must first return to its ORIGINAL immutable task and
    # zero-send recovery path, including when its route is a fallback target.
    progress = lifecycle._store.company_scan_progress(work_ids)
    received_routes = {
        row["latest_attempt"]["route_id"]
        for row in progress["rows"]
        if row["question_id"] in requested
        and row["checkpoint_id"] is None
        and row["latest_attempt"] is not None
        and row["latest_attempt"]["phase"] == "response_available"
    }
    for target in targets:
        if target["route_id"] not in received_routes:
            continue
        matched = [
            (r, c) for r, c in route_clients if r["id"] == target["route_id"] and c is not None
        ]
        if len(matched) != 1:
            raise ValueError("shared_recovery_original_route_unavailable")
        route, client = matched[0]
        current = routed_lifecycle(lifecycle, route, client)
        checkpoint = original_task_checkpoint(ordered_task_root(config, owner, scope, route["id"]))
        if checkpoint is None:
            raise ValueError("shared_company_original_checkpoint_required")
        task = load_original_company_task(checkpoint)
        from src.core.models import Question

        original_questions = [
            Question(q["question"]["prompt"], question_id=q["question_id"]) for q in task.questions
        ]
        recovery_options = {**options, "recovery_only": True}
        run_shared_company_scan(
            current,
            client,
            config=config,
            questions=original_questions,
            ordered_scope=scope,
            **recovery_options,
        )
    if options.get("recovery_only"):
        final = lifecycle._store.company_scan_progress(work_ids)
        if any(r["question_id"] in requested and r["checkpoint_id"] is None for r in final["rows"]):
            raise ValueError("shared_company_original_saved_response_required")
        return None
    for index, target in enumerate(targets):
        progress = lifecycle._store.company_scan_progress(work_ids)
        if all(row["checkpoint_id"] is not None for row in progress["rows"]):
            return None
        selected = next_route_questions(progress["rows"], targets, index)
        if not selected:
            continue
        if cap is not None and progress["model_requests"] >= cap:
            raise ValueError("shared_company_model_request_cap_reached")
        matched = [(r, c) for r, c in route_clients if r["id"] == target["route_id"]]
        if len(matched) != 1 or matched[0][1] is None or matched[0][0].get("eligible") is not True:
            # Missing local credentials/capabilities have not sent anything.
            # They never turn an unresolved existing send into another request.
            continue
        route, client = matched[0]
        from src.providers.llm_research_client import research_channel_capabilities

        native_search = lifecycle._external_retrieval is None
        capabilities = research_channel_capabilities(client)
        # Every shared scan, including one question, needs the final_json
        # channel. Generic vendor search alone is insufficient.
        if (
            not capabilities["native_final_json" if native_search else "external_final_json"]
            or options.get("max_output_tokens", 8192) > capabilities["final_json_max_output_tokens"]
        ):
            continue  # Pure pre-send incompatibility, not a paid failure or reask.
        current = routed_lifecycle(lifecycle, route, client)
        route_config = copy.deepcopy(config)
        if cap is not None:
            route_config.setdefault("limits", {})["max_model_requests_per_session"] = cap
        try:
            run_shared_company_scan(
                current,
                client,
                config=route_config,
                questions=[q for q in questions if q.question_id in selected],
                ordered_scope=scope,
                request_scope=(work_ids, cap),
                **options,
            )
        except SharedCompanyIncomplete:
            # The native ledger, not a model answer or broad exception, decides
            # whether another approved model can continue the remaining scope.
            pass
        root = ordered_task_root(config, owner, scope, route["id"])
        checkpoint = original_task_checkpoint(root)
        if cap is None and checkpoint is not None:
            cap = load_original_company_task(checkpoint).limits["max_model_requests_per_session"]
    progress = lifecycle._store.company_scan_progress(work_ids)
    if any(row["checkpoint_id"] is None for row in progress["rows"]):
        # Also surface an uncertain/received failure at the final route.
        next_route_questions(progress["rows"], targets, len(targets))
        raise ValueError("shared_company_approved_routes_exhausted")
    return None
