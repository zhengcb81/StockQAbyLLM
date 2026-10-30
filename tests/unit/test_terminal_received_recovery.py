"""Actual Q13 temporary SQLite, synthetic receipts/typed owner-authority seam.

No fixture is real owner permission. These tests never open a production DB or
dispatch a model; actual owner confirmation is still required in production.
"""

import copy

import pytest

from src.utils.quick_scan_work_decision_inspect import derive_work_decision
from src.utils.quick_scan_work_store import Lease
from tests.unit.test_quick_scan_known_outcome import (
    arguments,
    authority,
    fixture,
    immutable,
    save_synthetic_standard,
)


def terminal(tmp_path):
    values = fixture(tmp_path, response_status="completed", currency="USD")
    store, _, members, pack, _, _, _ = values
    member = pack["members"][0]
    assert (
        store.fail_received_without_answer(
            member["work_item_id"],
            members[0]["lease"],
            member["attempt_id"],
            reason_code="invalid_question",
        )
        == "failed"
    )
    return values


def diagnostic(store, pack):
    return {
        "code": "standard_answer_invalid",
        "artifact_ref": "synthetic-schema-rejection.json",
        "artifact_sha256": "a" * 64,
        "response_sha256": store.get_attempt_response(pack["members"][0]["attempt_id"])[
            "response_sha256"
        ],
    }


def kwargs(values, **overrides):
    store, _, members, pack, receipt, _, _ = values
    return arguments(
        store,
        members,
        pack,
        receipt,
        reason="answer_validation_rejected",
        diagnostic=diagnostic(store, pack),
        **overrides,
    )


def test_terminal_received_failure_can_be_readonly_preflighted(tmp_path):
    values = terminal(tmp_path)
    store, clock, _, pack, _, policy, _ = values
    args = kwargs(values)
    intent = {k: args[k] for k in ("attempt_id", "idempotency_key", "reason", "diagnostic")}
    intent.update(kind="known_response_resolution", work_item_id=pack["members"][0]["work_item_id"])
    before = store.path.read_bytes()
    result = derive_work_decision(store.path, intent, policy=policy, clock=lambda: clock[0])
    assert result["request"]["work_item_id"] == intent["work_item_id"]
    assert result["API_requests"] == result["database_writes"] == 0
    assert store.path.read_bytes() == before


def test_owner_resolution_then_retry_targets_next_generation_without_rewriting_old_work(tmp_path):
    values = terminal(tmp_path)
    store, clock, _, pack, _, _, _ = values
    wid = pack["members"][0]["work_item_id"]
    original_work = copy.deepcopy(store.get_item(wid))
    before = immutable(store)
    budget = store.get_quick_scan_budget_status("pack-policy")
    resolution = store.resolve_known_response(wid, **kwargs(values))
    assert resolution["request"]["reason"] == "answer_validation_rejected"
    assert store.get_item(wid) == original_work  # Already failed: no timestamp/state rewrite.
    assert immutable(store) == before
    retry = store.record_owner_retry_decision(
        wid,
        purpose="retry_unusable_answer",
        retry_after_seconds=0,
        owner_authority=authority(),
        idempotency_key="retry-after-native-failure",
    )
    assert retry["request"]["source_resolution_id"] == resolution["decision_id"]
    assert retry["request"]["target_generation"] == original_work["generation"] + 1
    assert retry["request"]["requires_prior_delivery"] is False
    assert store.get_item(wid) == original_work
    assert immutable(store) == before
    assert store.get_quick_scan_budget_status("pack-policy") == budget
    assert store.claim(wid, lease_seconds=120) is None
    # This is a retry decision, not a new execution/admission or permission to POST.
    assert (
        len(
            store.find_work_items(
                entity_id=original_work["entity_id"], question_ids=[original_work["question_id"]]
            )[original_work["question_id"]]
        )
        == 1
    )


@pytest.mark.parametrize("fault", ["receipt", "lease", "unconfirmed_authority"])
def test_terminal_failure_still_refuses_wrong_original_proof_or_unconfirmed_caller(tmp_path, fault):
    values = terminal(tmp_path)
    store, _, members, pack, _, _, _ = values
    args = kwargs(values)
    if fault == "receipt":
        args["receipt_sha256"] = "f" * 64
    if fault == "lease":
        args["expected_lease"] = Lease("wrong-original-lease", members[0]["lease"].lease_epoch)
    if fault == "unconfirmed_authority":
        args["owner_authority"] = {"approved": True}
    before = store.path.read_bytes()
    with pytest.raises((ValueError, TypeError, RuntimeError, PermissionError)):
        store.resolve_known_response(pack["members"][0]["work_item_id"], **args)
    assert store.path.read_bytes() == before


def test_other_failed_event_is_not_deterministic_received_member_failure(tmp_path):
    values = terminal(tmp_path)
    store, clock, members, pack, _, _, _ = values
    wid = pack["members"][0]["work_item_id"]
    # Explicit synthetic bad-history fixture: later failed event is unrelated.
    # No production event/row is touched; original events are not overwritten.
    with store._transaction() as con:
        store._event(
            con,
            wid,
            "unrelated_failure",
            members[0]["lease"].lease_epoch,
            clock[0],
            attempt_id=pack["members"][0]["attempt_id"],
            old="leased",
            new="failed",
        )
    before = store.path.read_bytes()
    with pytest.raises((ValueError, RuntimeError)):
        store.resolve_known_response(wid, **kwargs(values))
    assert store.path.read_bytes() == before


def test_existing_checkpoint_stays_protected(tmp_path):
    values = fixture(tmp_path, response_status="completed", currency="USD")
    store, _, members, pack, receipt, _, context = values
    cp = save_synthetic_standard(store, members, pack, receipt, context)
    before = store.path.read_bytes()
    with pytest.raises((ValueError, RuntimeError)):
        store.resolve_known_response(pack["members"][0]["work_item_id"], **kwargs(values))
    assert store.path.read_bytes() == before
    assert (
        store.get_answer_checkpoint(pack["members"][0]["work_item_id"])["payload_sha256"]
        == cp["payload_sha256"]
    )


def test_without_owner_resolution_failed_member_cannot_retry_or_reclaim(tmp_path):
    values = terminal(tmp_path)
    store, _, _, pack, _, _, _ = values
    wid = pack["members"][0]["work_item_id"]
    before = store.path.read_bytes()
    assert store.claim(wid, lease_seconds=120) is None
    with pytest.raises((ValueError, RuntimeError)):
        store.record_owner_retry_decision(
            wid,
            purpose="retry_unusable_answer",
            retry_after_seconds=0,
            owner_authority=authority(),
            idempotency_key="no-resolution-no-retry",
        )
    assert store.path.read_bytes() == before


def test_validation_failure_cannot_be_relabeled_missing_question(tmp_path):
    values = terminal(tmp_path)
    store, _, _, pack, _, _, _ = values
    args = kwargs(values)
    args["reason"] = "missing_question_answer"
    args["diagnostic"]["code"] = "missing_question_answer"
    before = store.path.read_bytes()
    with pytest.raises((ValueError, RuntimeError)):
        store.resolve_known_response(pack["members"][0]["work_item_id"], **args)
    assert store.path.read_bytes() == before
