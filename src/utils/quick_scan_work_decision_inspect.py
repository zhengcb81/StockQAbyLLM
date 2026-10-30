"""Fixed readonly work-decision proof using original journal preflight.

This read derives evidence, not authority. Only the separately configured owner
confirmation port can authorize a mutation. It reserves no fees and sends no HTTP.
"""

import copy
import time
from contextlib import closing
from datetime import datetime, timezone

from .quick_scan_known_outcome_journal import (
    _existing,
    resolution_request,
    retry_request,
    terminal_received_failure,
)
from .quick_scan_refresh_inspect import ReadOnlyWorkView
from .quick_scan_result_outbox import canonical_sha256
from .quick_scan_subject_journal import receipt_record
from .quick_scan_work_store import Lease, _budget_policy_projection

RESOLUTION_INTENT = {
    "kind",
    "work_item_id",
    "attempt_id",
    "idempotency_key",
    "reason",
    "diagnostic",
}
RETRY_INTENT = {
    "kind",
    "work_item_id",
    "idempotency_key",
    "purpose",
    "retry_after_seconds",
    "comparison_controls",
}
RESOLUTION_REQUEST = RESOLUTION_INTENT | {"physical"}
RETRY_REQUEST = RETRY_INTENT | {
    "physical",
    "attempt_id",
    "source_resolution_id",
    "source_checkpoint_sha256",
    "target_generation",
    "requires_prior_delivery",
}


def _policy_matches(con, policy, physical):
    projected = _budget_policy_projection(policy)
    stored = con.execute(
        "SELECT * FROM quick_scan_budget_policy WHERE policy_id=?",
        (projected["policy_id"],),
    ).fetchone()
    if (
        stored is None
        or stored["policy_snapshot_sha256"] != projected["policy_snapshot_sha256"]
        or stored["policy_version"] != projected["policy_version"]
        or physical["budget_policy_id"] != projected["policy_id"]
        or physical["budget_currency"] != projected["currency"]
    ):
        raise ValueError("owner_work_deployment_policy_changed")
    route = projected["route_identity"].get(physical["route_id"])
    if (
        route is None
        or route["provider_config_ref"] != physical["route_provider"]
        or route["model"] != physical["model_requested"]
    ):
        raise ValueError("owner_work_deployment_route_changed")


def _inspect(path, value, *, policy, clock, derive, comparison_target=None):
    if not isinstance(value, dict) or value.get("kind") not in {
        "known_response_resolution",
        "retry_decision",
    }:
        raise ValueError("owner_work_read_request_invalid")
    resolution = value["kind"] == "known_response_resolution"
    fields = (
        (RESOLUTION_INTENT if resolution else RETRY_INTENT)
        if derive
        else (RESOLUTION_REQUEST if resolution else RETRY_REQUEST)
    )
    if not resolution and value.get("purpose") == "retry_unusable_checkpoint" and not derive:
        fields = fields | {"unusable_checkpoint"}
    if set(value) != fields:
        raise ValueError("owner_work_read_request_invalid")
    view = ReadOnlyWorkView(path)
    view._clock = clock or time.time
    now = view._clock()
    with closing(view._connect()) as con:
        con.execute("BEGIN")
        view._inspection_connection = con
        try:
            item = view._item(con, value["work_item_id"])
            if resolution:
                attempt = con.execute(
                    "SELECT * FROM attempt WHERE attempt_id=? AND work_item_id=?",
                    (value["attempt_id"], value["work_item_id"]),
                ).fetchone()
                if attempt is None:
                    raise ValueError("owner_work_attempt_missing")
                from .quick_scan_known_outcome_journal import _physical

                original = _physical(view, con, item, attempt)
                request, item = resolution_request(
                    view,
                    con,
                    value["work_item_id"],
                    attempt_id=value["attempt_id"],
                    expected_lease=Lease(attempt["lease_token"], attempt["lease_epoch"]),
                    receipt_sha256=original["receipt_sha256"],
                    request_payload_sha256=original["request_payload_sha256"],
                    reason=value["reason"],
                    idempotency_key=value["idempotency_key"],
                    diagnostic=value["diagnostic"],
                    now=now,
                )
                if item["status"] not in {"leased", "uncertain"} and not (
                    item["status"] == "failed"
                    and (
                        _existing(
                            view,
                            con,
                            value["work_item_id"],
                            value["idempotency_key"],
                            request,
                        )
                        or terminal_received_failure(con, item, attempt, value["reason"])
                    )
                ):
                    raise ValueError("owner_work_read_resolution_state_invalid")
            else:
                request, item = retry_request(
                    view,
                    con,
                    value["work_item_id"],
                    purpose=value["purpose"],
                    retry_after_seconds=value["retry_after_seconds"],
                    idempotency_key=value["idempotency_key"],
                    comparison_controls=value["comparison_controls"],
                    now=now,
                )
            if not derive and request != value:
                raise ValueError("owner_work_read_original_request_mismatch")
            _policy_matches(con, policy, request["physical"])
            if comparison_target is not None:
                if (
                    derive
                    or request["kind"] != "retry_decision"
                    or request["purpose"] != "comparison"
                    or not isinstance(comparison_target, dict)
                    or set(comparison_target)
                    != {"policy_id", "route_id", "provider_config_ref", "model"}
                ):
                    raise ValueError("owner_work_target_read_invalid")
                projected = _budget_policy_projection(policy)
                target = projected["route_identity"].get(comparison_target["route_id"])
                if (
                    target is None
                    or comparison_target["policy_id"] != projected["policy_id"]
                    or any(
                        target[k] != comparison_target[k] for k in ("provider_config_ref", "model")
                    )
                ):
                    raise ValueError("owner_work_target_route_changed")
            pre = con.execute(
                "SELECT * FROM quick_scan_subject_send_receipt WHERE attempt_id=?",
                (request["attempt_id"],),
            ).fetchone()
            if pre is None:
                raise ValueError("owner_work_dispatch_proof_missing")
            owner_receipt = receipt_record(con, pre)
            result = dict(
                protocol="stockqa.work_decision_inspection/"
                + (
                    "1.2.0"
                    if request.get("purpose") == "retry_unusable_checkpoint"
                    else "1.1.0" if comparison_target is not None else "1.0.0"
                ),
                request=copy.deepcopy(request),
                request_sha256=canonical_sha256(request),
                subject_dispatch_receipt=owner_receipt,
                observed_at=datetime.fromtimestamp(now, timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                API_requests=0,
                database_writes=0,
            )
            if comparison_target is not None:
                result["target_route"] = copy.deepcopy(comparison_target)
            return result
        finally:
            view._inspection_connection = None
            con.rollback()


def inspect_work_decision(
    path,
    request,
    *,
    policy,
    clock=None,
    comparison_target=None,
    saved_research_task=None,
    saved_research_deployment=None,
):
    if request.get("kind") == "publish_saved_research":
        if comparison_target is not None or saved_research_task is None:
            raise ValueError("saved_research_fixed_task_required")
        from pathlib import Path

        from .quick_scan_saved_research_publication import inspect_publication

        return inspect_publication(
            path,
            request,
            policy=policy,
            task_bundle=Path(saved_research_task).read_bytes(),
            deployment=saved_research_deployment,
        )
    if saved_research_task is not None:
        raise ValueError("saved_research_task_not_applicable_to_legacy_action")
    return _inspect(
        path,
        request,
        policy=policy,
        clock=clock,
        derive=False,
        comparison_target=comparison_target,
    )


def derive_work_decision(
    path, intent, *, policy, clock=None, saved_research_task=None, saved_research_deployment=None
):
    if intent.get("kind") == "publish_saved_research":
        if set(intent) != {"kind", "sdk_id", "idempotency_key"} or saved_research_task is None:
            raise ValueError("saved_research_exact_intent_required")
        from pathlib import Path

        from .quick_scan_saved_research_publication import (
            derive_publication,
            inspect_publication,
            native_dependencies,
        )

        native_dependencies(saved_research_deployment)
        view = ReadOnlyWorkView(path)
        bundle = Path(saved_research_task).read_bytes()
        value = derive_publication(
            view,
            bundle,
            sdk_id=intent["sdk_id"],
            idempotency_key=intent["idempotency_key"],
        )
        return inspect_publication(path, value["request"], policy=policy, task_bundle=bundle)
    if saved_research_task is not None:
        raise ValueError("saved_research_task_not_applicable_to_legacy_action")
    return _inspect(path, intent, policy=policy, clock=clock, derive=True)
