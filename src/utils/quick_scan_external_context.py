"""Link external retrieval to existing Q09 accounting without model rerouting.

The returned composite is a budget projection ONLY. Answer-model dispatch must
retain its original model policy and priority. No scheduler or ledger lives here.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

from src.config.quick_scan_search_policy import SearchPolicy, execution_query_plans
from src.utils.quick_scan_external_journal import (
    MODEL_BUDGET_LABEL,
    _identity_basis,
    _reusable,
)
from src.utils.quick_scan_work_store import (
    Lease,
    QuickScanWorkStore,
    _budget_policy_projection,
    _cost_to_micros,
)


def build_shared_external_budget_policy(
    model_policy: dict[str, Any], search_policy: SearchPolicy
) -> dict[str, Any]:
    """Add admitted retrieval route identities to one unchanged spending owner."""
    original = _budget_policy_projection(model_policy)
    execution_query_plans(search_policy)  # Requires the loader's unchanged 1.1 plan.
    if not search_policy.admitted or not search_policy.requires_external:
        raise ValueError("no executable admitted external route")
    shared = copy.deepcopy(model_policy)
    route_ids = {route["id"] for route in shared["routes"]}
    groups = {group["id"]: group for group in shared["quota_groups"]}
    for route_id in search_policy.admitted:
        route = next(item for item in search_policy.routes if item["route_id"] == route_id)
        pricing, dispatch = route["pricing"], route["dispatch"]
        if route_id in route_ids:
            raise ValueError("external and answer-model route identities overlap")
        if pricing["currency"] != original["currency"]:
            raise ValueError("search and answer-model budget currencies differ")
        if pricing["per_request_cap_micros"] > min(
            original["max_cost_per_attempt_micros"], original["max_cost_micros"]
        ):
            raise ValueError("external request bound exceeds the existing budget cap")
        group_id = dispatch["quota_group"]
        group_limit = dispatch["quota_group_max_in_flight"]
        if group_id in groups:
            if groups[group_id]["max_in_flight"] != group_limit:
                raise ValueError("external quota-group concurrency conflicts with the model policy")
        else:
            group = {"id": group_id, "max_in_flight": group_limit}
            groups[group_id] = group
            shared["quota_groups"].append(group)
        shared["routes"].append(
            {
                "id": route_id,
                "provider_config_ref": route["kind"],
                "model": MODEL_BUDGET_LABEL,
                "quota_group": group_id,
                "max_in_flight": dispatch["max_in_flight"],
                "eligible": True,
                "unavailable_reason": None,
            }
        )
        route_ids.add(route_id)
    # A policy-version change never creates a new policy_id or resets counters.
    # Company/query/manifest/TTL belong to each retrieval operation, not the
    # shared spending owner. Hashing the entire search-policy file here would
    # fence every other company while a paid request is in flight.
    basis = {
        "model_budget_sha256": original["policy_snapshot_sha256"],
        "external_routes": [
            {
                key: copy.deepcopy(route[key])
                for key in ("route_id", "kind", "dispatch", "pricing", "metering")
            }
            for route_id in search_policy.admitted
            for route in search_policy.routes
            if route["route_id"] == route_id
        ],
    }
    shared["policy_version"] = (
        "external_budget/1.0.0:" + hashlib.sha256(_canonical(basis).encode("utf-8")).hexdigest()
    )
    _budget_policy_projection(shared)  # Validate through the original Q09 owner.
    return shared


def _canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def _publication_date(value: Any) -> str | None:
    """Accept actual ISO/RFC dates, never a guessed first-ten-character prefix."""
    if not isinstance(value, str) or not 1 <= len(value) <= 100 or value != value.strip():
        return None
    try:
        if len(value) == 10:
            parsed = date.fromisoformat(value)
            return parsed.isoformat() if parsed.isoformat() == value else None
        try:
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            instant = parsedate_to_datetime(value)
        if instant.tzinfo is None:
            return None
        return instant.astimezone(timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _origin_binding(url: str, bindings: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Use exact host and canonical path segments, not query target or suffix."""
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.port not in {None, 443}
        ):
            return None
        path = parts.path or "/"
        if (
            any(ord(c) < 33 or c in "\\%" for c in path)
            or "//" in path
            or any(part in {".", ".."} for part in path.split("/"))
        ):
            return None
    except ValueError:
        return None
    for binding in bindings:
        prefix = binding["path_prefix"]
        if parts.hostname == binding["host"] and (
            prefix == "/"
            or path == prefix
            or path.startswith(prefix if prefix.endswith("/") else prefix + "/")
        ):
            return binding
    return None


def _bound_candidate(
    entry: dict[str, Any], plan: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    binding = _origin_binding(entry["url"], plan["entity_domain_bindings"])
    published = _publication_date(entry.get("published_at"))
    if (
        binding is None
        or published is None
        or published > plan["information_as_of"]
        or not isinstance(entry.get("snippet"), str)
        or not entry["snippet"].strip()
    ):
        return None
    return dict(entry, entity_id=plan["entity_id"], published_at=published), binding


def build_external_question_context(
    store: QuickScanWorkStore,
    work_item_id: str,
    lease: Lease,
    policy: SearchPolicy,
    operation_ids: list[str],
    *,
    question_manifest_sha256: str,
) -> dict[str, Any]:
    """Rebuild bounded untrusted data from verified durable retrieval records.

    The manifest hash is required from the producer's independently frozen
    authority, never inferred from the search configuration. No HTTP, answer
    attempt, model/native receipt, refreshed timestamp or extra storage is made.
    The eventual answer dispatch must bind this hash to its actual prompt.
    """
    from src.utils.quick_scan_cost_resolver import ExternalSearchCostResolver
    from src.utils.quick_scan_evidence import build_evidence_package

    plans = execution_query_plans(policy)
    if not isinstance(store, QuickScanWorkStore) or not isinstance(lease, Lease):
        raise ValueError("external context requires a durable work store and lease")
    item = store.get_item(work_item_id)
    now = store._now()
    store._assert_lease(item, lease, now)
    expected = policy.execution_plan
    if (
        expected is None
        or expected["entity_id"] != item["entity_id"]
        or expected["identity_snapshot_sha256"] != item["identity_snapshot_sha256"]
        or expected["question_manifest_sha256"] != question_manifest_sha256
    ):
        raise ValueError("external policy does not match the independently frozen work authority")
    question_id = item["question_id"]
    required = {plan["query_id"]: plan for plan in plans if question_id in plan["question_ids"]}
    if not required:
        raise ValueError("external policy has no frozen queries for this question")
    if (
        not isinstance(operation_ids, list)
        or not operation_ids
        or any(not isinstance(value, str) for value in operation_ids)
    ):
        raise ValueError("external operation identifiers required")
    if len(operation_ids) != len(set(operation_ids)):
        raise ValueError("duplicate external operation identifiers")
    if len(operation_ids) != len(required):
        raise ValueError("external query plan incomplete or ambiguous")
    current_basis = _identity_basis(
        {
            "work_fingerprint": {
                key: item[key]
                for key in (
                    "entity_id",
                    "question_id",
                    "generation",
                    "scope",
                    "scope_id",
                    "identity_revision",
                    "source_binding_version",
                    "identity_state",
                    "source_binding_ref",
                    "source_binding_refs_json",
                    "identity_snapshot_sha256",
                    "question_fingerprint",
                    "routing_fingerprint",
                )
            }
        }
    )
    resolver = ExternalSearchCostResolver(policy)
    records: dict[str, dict[str, Any]] = {}
    for operation_id in operation_ids:
        record = store.get_external_search(
            operation_id
        )  # Rechecks hashes/ledger, not caller dictionaries.
        plan, original = record["plan"], record["context"]
        query_id = plan["query_id"]
        if (
            original["search_policy_sha256"] != policy.policy_sha256
            or original["route"]["route_id"] not in policy.admitted
            or plan != required.get(query_id)
            or _identity_basis(original) != current_basis
            or original["work_fingerprint"]["generation"] != item["generation"]
        ):
            raise ValueError("external policy, query or work scope binding mismatch")
        if query_id in records:
            raise ValueError("duplicate external query result")
        _reusable(record, now)
        result = record["result"]
        if result["recorded_at"] > now:
            raise ValueError("external retrieval time is in the future")
        price = resolver(result["receipt"])
        if (
            price is None
            or result["charge"]["actual_cost_micros"]
            != _cost_to_micros(price["actual_cost"], "actual_cost", allow_zero=True)
            or result["charge"]["cost_source_ref"] != price["source_ref"]
        ):
            raise ValueError("external pricing proof does not match the frozen policy")
        records[query_id] = record
    candidates, retrievals = [], []
    origins: dict[tuple[str, str], dict[str, Any]] = {}
    for query_id, plan in required.items():
        record = records[query_id]
        result = record["result"]
        receipt = result["receipt"]
        retrieved_at = (
            datetime.fromtimestamp(result["recorded_at"], timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        retrievals.append(
            {
                "operation_id": record["operation_id"],
                "query_id": query_id,
                "route_id": receipt["route_id"],
                "route_kind": receipt["route_kind"],
                "receipt_sha256": result["receipt_sha256"],
                "retrieved_at": retrieved_at,
            }
        )
        for entry in receipt["entries"]:
            bound = _bound_candidate(entry, plan)
            if bound is None:
                continue
            candidate, binding = bound
            candidates.append(candidate)
            origins.setdefault(
                (entry["url"], entry["query"]),
                {
                    "binding": binding,
                    "retrieved_at": retrieved_at,
                    "operation_id": record["operation_id"],
                },
            )
    # Normalise each query through the existing evidence owner. Its legacy
    # URL dedup remains unchanged, while a shared page returned by two queries
    # retains BOTH actual snippets/retrieval times instead of losing coverage
    # or attributing the second response to the first paid request.
    by_url: dict[str, dict[str, Any]] = {}
    for query_id, plan in required.items():
        record = records[query_id]
        retrieved_at = next(
            value["retrieved_at"] for value in retrievals if value["query_id"] == query_id
        )
        package = build_evidence_package(
            (candidate for candidate in candidates if candidate["query"] == plan["query"]),
            entity_id=item["entity_id"],
            as_of=expected["information_as_of"],
            retrieved_at=retrieved_at,
            adapter_version="stockqa.external_context/1.0.0",
            request_id=record["operation_id"],
            query_bindings={plan["query"]: plan["question_ids"]},
            question_ids=[question_id],
            snippet_limit=policy.retrieval["max_snippet_unicode_characters"],
            company_limit=policy.retrieval["max_company_evidence_unicode_characters"],
        )
        for entry in package["entries"]:
            if not entry["eligible"]:
                continue
            origin = origins[(entry["url"], entry["query"])]
            provenance = {
                key: entry[key] for key in ("title", "publisher", "published_at", "short_snippet")
            }
            provenance.update(
                retrieved_at=origin["retrieved_at"],
                operation_id=origin["operation_id"],
                query_id=query_id,
            )
            source = by_url.setdefault(
                entry["url"],
                {
                    key: entry[key]
                    for key in (
                        "source_id",
                        "title",
                        "publisher",
                        "url",
                        "published_at",
                        "short_snippet",
                    )
                }
                | {
                    "retrieved_at": origin["retrieved_at"],
                    "operation_id": origin["operation_id"],
                    "query_id": query_id,
                    "identity_binding_kind": origin["binding"]["binding_kind"],
                    "identity_binding_source_ref": origin["binding"]["source_ref"],
                    "query_ids": [],
                    "retrieval_provenance": [],
                },
            )
            source["query_ids"].append(query_id)
            source["retrieval_provenance"].append(provenance)
    sources = list(by_url.values())
    if not sources:
        raise ValueError("no eligible company evidence for this question")
    context = {
        "schema": "stockqa.external_question_context/1.0.0",
        "content_trust": "untrusted_source_data",
        "claim_verification": "not_automatic",
        "entity_id": item["entity_id"],
        "work_item_id": work_item_id,
        "scope": item["scope"],
        "scope_id": item["scope_id"],
        "question_id": question_id,
        "identity_snapshot_sha256": item["identity_snapshot_sha256"],
        "question_manifest_sha256": question_manifest_sha256,
        "information_as_of": expected["information_as_of"],
        "answer_search_mode": expected["answer_search_mode"],
        "retrievals": retrievals,
        "sources": sources,
        "query_coverage": {
            "required": list(required),
            "eligible": [
                query_id
                for query_id in required
                if any(query_id in source["query_ids"] for source in sources)
            ],
            "missing": [
                query_id
                for query_id in required
                if not any(query_id in source["query_ids"] for source in sources)
            ],
        },
    }
    context["context_sha256"] = hashlib.sha256(_canonical(context).encode("utf-8")).hexdigest()
    if len(_canonical(context)) > policy.retrieval["max_company_evidence_unicode_characters"]:
        raise ValueError("external context metadata and sources exceed the company cap")
    return context


class ExternalRetrievalBlocked(RuntimeError):
    """Safe, specific stop reason; never raw provider error text or a key."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _eligible_priced_record(record: dict[str, Any], policy: SearchPolicy, now: float) -> bool:
    from src.utils.quick_scan_cost_resolver import ExternalSearchCostResolver
    from src.utils.quick_scan_work_store import BudgetAdmissionError

    result = record["result"]
    if result is None or record["budget"]["status"] != "settled":
        raise ExternalRetrievalBlocked("external_search_unresolved")
    receipt = result["receipt"]
    price = ExternalSearchCostResolver(policy)(receipt)
    if (
        price is None
        or result["charge"]["actual_cost_micros"]
        != _cost_to_micros(price["actual_cost"], "actual_cost", allow_zero=True)
        or result["charge"]["cost_source_ref"] != price["source_ref"]
    ):
        raise ValueError("external pricing proof does not match the frozen policy")
    if result["recorded_at"] > now:
        raise ValueError("external retrieval time is in the future")
    if receipt["outcome"] == "unknown":
        raise ExternalRetrievalBlocked("external_search_unresolved")
    try:
        _reusable(record, now)
    except BudgetAdmissionError as error:
        if error.reason in {"external_search_failed", "external_evidence_insufficient"}:
            return False  # A paid, terminal failure is never replayed as a new request.
        raise ExternalRetrievalBlocked(error.reason) from None
    return any(_bound_candidate(entry, record["plan"]) is not None for entry in receipt["entries"])


def _settle_mcp_controls(
    store: QuickScanWorkStore,
    work_item_id: str,
    lease: Lease,
    request: dict,
    provider: Any,
    resolver: Any,
    health_store: Any,
    decision: Any,
    *,
    group_id: str,
    unknown_reset_cooldown_seconds: int,
    rate_limit_cooldown_seconds: int,
) -> bool:
    """Journal every control HTTP using Q09; return False only for paid known failure."""
    from src.providers.external_search_provider import ExternalSearchTransportError
    from src.utils.quick_scan_mcp_journal import STAGES, _ready
    from src.utils.quick_scan_work_store import SendPermit
    from src.utils.quick_scan_work_transport import (
        QuickScanBudgetBinding,
        QuickScanSendAttempt,
    )

    for name in STAGES:
        stage = store.begin_mcp_stage(work_item_id, lease, stage=name, **request)
        if stage["send_required"]:
            attempt_id = stage["budget_attempt_id"]
            permit = QuickScanSendAttempt(
                None,
                attempt_id,
                SendPermit(attempt_id, stage["created_at"], 0),
                budget_binding=QuickScanBudgetBinding(store, request["policy"]),
                budget_attempt_id=attempt_id,
                mcp_stage_id=stage["stage_id"],
            )
            try:
                result = provider.fetch_mcp_control(permit)
                receipt, control = result["receipt"], result["control"]
            except ExternalSearchTransportError as error:
                receipt, control = error.receipt, None
            price = resolver(receipt)
            stage = store.record_mcp_stage(
                stage["stage_id"],
                receipt=receipt,
                control=control,
                actual_cost=None if price is None else price["actual_cost"],
                cost_source_ref=None if price is None else price["source_ref"],
            )
            _observe_search_health(
                receipt,
                price,
                health_store,
                decision,
                group_id=group_id,
                route_id=request["route_id"],
                unknown_reset_cooldown_seconds=unknown_reset_cooldown_seconds,
                rate_limit_cooldown_seconds=rate_limit_cooldown_seconds,
            )
        if (
            stage["budget"]["status"] != "settled"
            or stage["result"] is None
            or stage["result"]["receipt"]["outcome"] == "unknown"
        ):
            raise ExternalRetrievalBlocked("mcp_control_unresolved")
        price = resolver(stage["result"]["receipt"])
        from src.utils.quick_scan_work_store import _cost_to_micros

        if price is None or stage["result"]["charge"] != {
            "actual_cost_micros": _cost_to_micros(
                price["actual_cost"], "actual_cost", allow_zero=True
            ),
            "cost_source_ref": price["source_ref"],
        }:
            raise ValueError("MCP control pricing proof differs from frozen policy")
        if not _ready(stage, store._now()):
            return False
    return True


def _observe_search_health(
    receipt: dict,
    price: Any,
    health_store: Any,
    decision: Any,
    *,
    group_id: str,
    route_id: str,
    unknown_reset_cooldown_seconds: int,
    rate_limit_cooldown_seconds: int,
) -> None:
    code, retry_after = receipt.get("provider_error_code"), receipt.get("retry_after_seconds")
    if code in {
        "insufficient_quota",
        "quota_exceeded",
        "billing_hard_limit_reached",
        "account_quota_exceeded",
        "insufficient_funds",
    }:
        health_store.quota_exhausted(
            group_id,
            unknown_reset_cooldown_seconds=unknown_reset_cooldown_seconds,
            retry_after_seconds=retry_after,
        )
    elif receipt["http_status_code"] == 429:
        health_store.rate_limited(
            route_id,
            cooldown_seconds=retry_after or rate_limit_cooldown_seconds,
            source="retry_after" if retry_after is not None else "unknown",
        )
    if receipt["outcome"] == "response_available" and price is not None:
        health_store.probe_succeeded(group_id, decision.probe_token)


def retrieve_external_question_context(
    store: QuickScanWorkStore,
    work_item_id: str,
    lease: Lease,
    policy: SearchPolicy,
    model_policy: dict[str, Any],
    *,
    question_manifest_sha256: str,
    health_store: Any,
    unknown_reset_cooldown_seconds: int,
    rate_limit_cooldown_seconds: int,
    probe_lease_seconds: int = 180,
) -> dict[str, Any]:
    """Execute frozen queries in search priority, reusing Q08/Q09 and the journal.

    Each candidate route sends at most once. A received, priced, known failure
    can move to the next route; an uncertain send/charge or failed persistence
    stops the whole question. No model dispatch or native receipt is made here.
    Only a matching, live-owner intent without a durable dispatch is resumable.
    """
    from src.providers.external_search_provider import (
        ADAPTER_VERSION,
        ExternalSearchProvider,
        ExternalSearchTransportError,
    )
    from src.utils.quick_scan_cost_resolver import ExternalSearchCostResolver
    from src.utils.quick_scan_external_journal import _resumable_unsent
    from src.utils.quick_scan_provider_health import QuickScanProviderHealth
    from src.utils.quick_scan_work_store import BudgetAdmissionError, SendPermit
    from src.utils.quick_scan_work_transport import (
        QuickScanBudgetBinding,
        QuickScanSendAttempt,
    )

    plans = execution_query_plans(policy)
    if not isinstance(store, QuickScanWorkStore) or not isinstance(lease, Lease):
        raise ValueError("external context requires a durable work store and lease")
    if not isinstance(health_store, QuickScanProviderHealth):
        raise ValueError("external retrieval requires the existing durable provider health owner")
    for name, value in (
        ("unknown_reset_cooldown_seconds", unknown_reset_cooldown_seconds),
        ("rate_limit_cooldown_seconds", rate_limit_cooldown_seconds),
        ("probe_lease_seconds", probe_lease_seconds),
    ):
        QuickScanProviderHealth._positive_seconds(value, name)
    item = store.get_item(work_item_id)
    store._assert_lease(item, lease, store._now())
    expected = policy.execution_plan
    if (
        expected is None
        or expected["entity_id"] != item["entity_id"]
        or expected["identity_snapshot_sha256"] != item["identity_snapshot_sha256"]
        or expected["question_manifest_sha256"] != question_manifest_sha256
    ):
        raise ValueError("external policy does not match the independently frozen work authority")
    required = [plan for plan in plans if item["question_id"] in plan["question_ids"]]
    if not required:
        raise ValueError("external policy has no frozen queries for this question")
    shared = build_shared_external_budget_policy(model_policy, policy)
    resolver = ExternalSearchCostResolver(policy)
    operation_ids = []
    for plan in required:
        last_reason = "external_evidence_insufficient"
        for route_id in policy.admitted:  # Loader preserves the user's search priority.
            route = next(route for route in policy.routes if route["route_id"] == route_id)
            group_id = route["dispatch"]["quota_group"]
            request = dict(
                policy=shared,
                plan=plan,
                route_id=route_id,
                provider=route["kind"],
                quota_group=group_id,
                adapter_version=ADAPTER_VERSION,
                search_policy_sha256=policy.policy_sha256,
                endpoint=route["endpoint"],
            )
            try:
                previous = store.lookup_external_search(work_item_id, lease, **request)
            except BudgetAdmissionError as error:
                raise ExternalRetrievalBlocked(error.reason) from None
            if previous is not None and not _resumable_unsent(previous, work_item_id, lease):
                if _eligible_priced_record(previous, policy, store._now()):
                    operation_ids.append(previous["operation_id"])
                    break
                continue
            try:
                provider = ExternalSearchProvider(policy, route_id)
            except ValueError as error:
                if str(error) == "credential_env_unset":
                    last_reason = "credential_env_unset"
                    continue
                raise
            decision = health_store.admit(
                route_id=route_id,
                group_id=group_id,
                unknown_reset_cooldown_seconds=unknown_reset_cooldown_seconds,
                probe_lease_seconds=probe_lease_seconds,
            )
            if not decision.allowed:
                if decision.reason is None:
                    raise ValueError("external provider health refused without a reason")
                last_reason = decision.reason
                continue
            try:
                if route["kind"] == "zai_mcp_streamable" and not _settle_mcp_controls(
                    store,
                    work_item_id,
                    lease,
                    request,
                    provider,
                    resolver,
                    health_store,
                    decision,
                    group_id=group_id,
                    unknown_reset_cooldown_seconds=unknown_reset_cooldown_seconds,
                    rate_limit_cooldown_seconds=rate_limit_cooldown_seconds,
                ):
                    last_reason = "mcp_control_unusable"
                    continue
                operation = store.begin_external_search(work_item_id, lease, **request)
                if operation["send_required"]:
                    attempt_id = operation["budget_attempt_id"]
                    permit = QuickScanSendAttempt(
                        None,
                        attempt_id,
                        SendPermit(attempt_id, operation["created_at"], 0),
                        budget_binding=QuickScanBudgetBinding(store, shared),
                        budget_attempt_id=attempt_id,
                        external_operation_id=operation["operation_id"],
                    )
                    try:
                        receipt = provider.fetch(
                            permit, query=plan["query"], top_k=plan["top_k"], locale=plan["locale"]
                        )
                    except ExternalSearchTransportError as error:
                        receipt = error.receipt
                    price = resolver(receipt)
                    operation = store.record_external_search(
                        operation["operation_id"],
                        receipt=receipt,
                        actual_cost=None if price is None else price["actual_cost"],
                        cost_source_ref=None if price is None else price["source_ref"],
                    )
                    _observe_search_health(
                        receipt,
                        price,
                        health_store,
                        decision,
                        group_id=group_id,
                        route_id=route_id,
                        unknown_reset_cooldown_seconds=unknown_reset_cooldown_seconds,
                        rate_limit_cooldown_seconds=rate_limit_cooldown_seconds,
                    )
                if _eligible_priced_record(operation, policy, store._now()):
                    operation_ids.append(operation["operation_id"])
                    break
            except BudgetAdmissionError as error:
                raise ExternalRetrievalBlocked(error.reason) from None
            finally:
                # Does nothing for a normal route or a probe already settled;
                # on a persistence failure it prevents a free immediate probe.
                health_store.probe_failed(
                    group_id,
                    decision.probe_token,
                    unknown_reset_cooldown_seconds=unknown_reset_cooldown_seconds,
                )
        else:
            raise ExternalRetrievalBlocked(last_reason)
    return build_external_question_context(
        store,
        work_item_id,
        lease,
        policy,
        operation_ids,
        question_manifest_sha256=question_manifest_sha256,
    )
