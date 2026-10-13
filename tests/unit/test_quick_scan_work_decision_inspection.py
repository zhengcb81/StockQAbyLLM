"""Synthetic original receipts; readonly owner inspection, never financial gold."""

import copy
from contextlib import closing

import pytest

from src.utils.quick_scan_known_outcome_journal import OwnerWorkDecisionAuthority
from src.utils.quick_scan_result_outbox import canonical_sha256
from src.utils.quick_scan_work_store import WorkConflictError
from tests.unit.test_quick_scan_known_outcome import (
    arguments,
    authority,
    fixture,
    resolve,
)


class Capture(OwnerWorkDecisionAuthority):
    def authorize(self, request):
        self.request = copy.deepcopy(request)
        raise PermissionError("synthetic capture, not an approval")


def source(tmp_path, **kwargs):
    store, clock, members, pack, receipt, policy, context = fixture(tmp_path, **kwargs)
    clock[0] += 121
    capture = Capture()
    with pytest.raises(PermissionError, match="synthetic capture"):
        store.resolve_known_response(
            members[0]["work_item_id"],
            **arguments(store, members, pack, receipt, owner_authority=capture),
        )
    return store, clock, members, pack, receipt, policy, capture.request


def test_readonly_exact_original_request_uses_same_preflight_and_changes_no_bytes(tmp_path):
    from src.utils.quick_scan_work_decision_inspect import inspect_work_decision

    store, clock, _, _, _, policy, request = source(tmp_path)
    before = store.path.read_bytes()
    out = inspect_work_decision(store.path, request, policy=policy, clock=lambda: clock[0])
    assert set(out) == {
        "protocol",
        "request",
        "request_sha256",
        "subject_dispatch_receipt",
        "observed_at",
        "API_requests",
        "database_writes",
    }
    assert out["protocol"] == "stockqa.work_decision_inspection/1.0.0"
    assert out["request"] == request
    assert out["request_sha256"] == canonical_sha256(request)
    assert (
        out["subject_dispatch_receipt"]["receipt_sha256"]
        == request["physical"]["subject_dispatch_receipt_sha256"]
    )
    assert out["API_requests"] == out["database_writes"] == 0
    assert store.path.read_bytes() == before
    with closing(store._connect()) as con:
        assert con.execute("SELECT count(*) FROM quick_scan_owner_work_decision").fetchone()[0] == 0


@pytest.mark.parametrize(
    "fault",
    [
        "receipt",
        "payload",
        "physical-cost",
        "lease",
        "subject",
        "attempt",
        "budget-id",
        "reason",
        "extra",
    ],
)
def test_foreign_or_changed_input_not_authenticated_by_matching_caller_hash(tmp_path, fault):
    from src.utils.quick_scan_work_decision_inspect import inspect_work_decision

    store, clock, _, _, _, policy, request = source(tmp_path)
    wrong = copy.deepcopy(request)
    if fault == "receipt":
        wrong["physical"]["receipt_sha256"] = "a" * 64
    if fault == "payload":
        wrong["physical"]["request_payload_sha256"] = "b" * 64
    if fault == "physical-cost":
        wrong["physical"]["actual_cost_micros"] = 1
    if fault == "lease":
        wrong["physical"]["lease_token"] = "wrong"
    if fault == "subject":
        wrong["physical"]["subject_binding"]["subject_ref"]["entity_id"] = "ENT_WRONG"
    if fault == "attempt":
        wrong["attempt_id"] = "wrong"
    if fault == "budget-id":
        wrong["physical"]["budget_policy_id"] = "wrong"
    if fault == "reason":
        wrong["reason"] = "provider_error"
    if fault == "extra":
        wrong["approved"] = True
    before = store.path.read_bytes()
    with pytest.raises((ValueError, RuntimeError, TypeError, KeyError)):
        inspect_work_decision(store.path, wrong, policy=policy, clock=lambda: clock[0])
    assert store.path.read_bytes() == before


def test_active_lease_refused_in_read_and_expired_intent_rederived_from_original_store(tmp_path):
    from src.utils.quick_scan_work_decision_inspect import (
        derive_work_decision,
        inspect_work_decision,
    )

    store, clock, _, _, _, policy, request = source(tmp_path)
    clock[0] -= 121
    with pytest.raises(RuntimeError, match="active_lease"):
        inspect_work_decision(store.path, request, policy=policy, clock=lambda: clock[0])
    clock[0] += 121
    intent = {
        k: request[k]
        for k in ("kind", "work_item_id", "attempt_id", "idempotency_key", "reason", "diagnostic")
    }
    before = store.path.read_bytes()
    derived = derive_work_decision(store.path, intent, policy=policy, clock=lambda: clock[0])
    assert derived["request"] == request
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("fault", ["version", "route-model", "currency", "cap"])
def test_owner_deployment_policy_must_match_original_ledger_snapshot(tmp_path, fault):
    from src.utils.quick_scan_work_decision_inspect import inspect_work_decision

    store, clock, _, _, _, policy, request = source(tmp_path)
    wrong = copy.deepcopy(policy)
    if fault == "version":
        wrong["policy_version"] = 2
    if fault == "route-model":
        wrong["routes"][0]["model"] = "wrong"
    if fault == "currency":
        wrong["budget"]["currency"] = "USD"
    if fault == "cap":
        wrong["budget"]["max_requests"] += 1
    before = store.path.read_bytes()
    with pytest.raises((ValueError, RuntimeError, TypeError, KeyError)):
        inspect_work_decision(store.path, request, policy=wrong, clock=lambda: clock[0])
    assert store.path.read_bytes() == before


def test_retry_intent_reads_original_resolution_but_never_creates_new_generation(tmp_path):
    from src.utils.quick_scan_work_decision_inspect import derive_work_decision

    store, clock, members, pack, receipt, policy, request = source(tmp_path)
    resolve(store, members, pack, receipt)
    intent = dict(
        kind="retry_decision",
        work_item_id=members[0]["work_item_id"],
        idempotency_key="retry-from-real-read",
        purpose="retry_unusable_answer",
        retry_after_seconds=60,
        comparison_controls=None,
    )
    before = store.path.read_bytes()
    out = derive_work_decision(store.path, intent, policy=policy, clock=lambda: clock[0])
    assert out["request"]["target_generation"] == 2
    assert out["request"]["source_resolution_id"].startswith("owd1_")
    assert out["request"]["physical"] == request["physical"]
    assert store.path.read_bytes() == before


def test_unknown_physical_result_cannot_be_promoted_by_a_readonly_intent(tmp_path):
    from src.utils.quick_scan_work_decision_inspect import derive_work_decision

    store, clock, members, pack, receipt, policy, _ = fixture(tmp_path, outcome="unknown")
    clock[0] += 121
    before = store.path.read_bytes()
    intent = dict(
        kind="known_response_resolution",
        work_item_id=members[0]["work_item_id"],
        attempt_id=pack["members"][0]["attempt_id"],
        idempotency_key="unknown-control",
        reason="output_length",
        diagnostic=None,
    )
    with pytest.raises(WorkConflictError):
        derive_work_decision(store.path, intent, policy=policy, clock=lambda: clock[0])
    assert store.path.read_bytes() == before
