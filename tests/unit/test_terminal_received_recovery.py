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


def test_expired_original_paid_lease_saves_without_new_claim_or_budget(tmp_path, monkeypatch):
    from src.utils.quick_scan_refresh_execution import ApprovedRefreshSession

    values = fixture(tmp_path, response_status="completed", currency="USD")
    store, clock, members, pack, receipt, _, context = values
    member = pack["members"][0]
    wid = member["work_item_id"]
    original = store.get_attempt_response(member["attempt_id"])
    budget_before = store.get_quick_scan_budget_status("pack-policy")
    clock[0] += 1000
    owner = ApprovedRefreshSession.__new__(ApprovedRefreshSession)
    owner.recovery_only = True
    seen = []
    # Explicit component permission seam. Actual actor/grant checks are exercised
    # separately; the real SQLite/physical receipt/CAS path is never replaced.
    owner.assert_received_checkpoint_recovery = lambda work, attempt, **kw: seen.append(
        (work, attempt)
    )
    real_save = store.save_answer_checkpoint
    monkeypatch.setattr(
        store,
        "save_answer_checkpoint",
        lambda *a, **kw: real_save(*a, **kw, _recovery_session=owner),
    )
    cp = save_synthetic_standard(store, members, pack, receipt, context)
    assert cp["attempt_id"] == member["attempt_id"] and seen
    assert store.get_item(wid)["status"] == "result_ready"
    assert store.get_item(wid)["lease_epoch"] == members[0]["lease"].lease_epoch
    assert store.get_attempt_response(member["attempt_id"]) == original
    assert store.get_quick_scan_budget_status("pack-policy") == budget_before
    assert len(store.list_attempts(wid)) == 1
    events = store.list_events(wid)
    saved_event = next(e for e in reversed(events) if e["to_status"] == "result_ready")
    assert saved_event["from_status"] == "leased"
    assert not any(e["to_status"] == "failed" for e in events)


@pytest.mark.parametrize("journal", [False, True])
@pytest.mark.parametrize("member_fault", [None, "missing", "invalid", "semantic_invalid"])
@pytest.mark.parametrize("description_object", [False, True])
def test_captured_leased_paid_pack_recovers_original_final_json_after_expiry(
    tmp_path, monkeypatch, description_object, member_fault, journal
):
    """Real parser/capture/SQLite/physical-save chain; current actor is a seam."""
    import hashlib
    import json
    from types import SimpleNamespace as NS

    from src.utils.quick_scan_refresh_execution import ApprovedRefreshSession

    values = fixture(tmp_path, response_status="completed", currency="USD")
    store, clock, members, pack, receipt, _, context = values
    pack = store.get_question_pack(pack["pack_id"])
    real_save = store.save_answer_checkpoint
    supplied = {}

    def get_body(*args, **kwargs):
        supplied.update(kwargs)
        return {}

    monkeypatch.setattr(store, "save_answer_checkpoint", get_body)
    save_synthetic_standard(store, members, pack, receipt, context, status="scored")
    monkeypatch.setattr(store, "save_answer_checkpoint", real_save)
    template = supplied["standard_answer"]
    company_name = "Synthetic captured issuer"
    payloads = []
    entries = {}
    for member in pack["members"]:
        qid = member["question_id"]
        body = copy.deepcopy(template)
        body["question_id"] = qid
        entity = context["questions"][qid]["metadata"]["entity_id"]
        payloads.append(
            dict(
                entity_id=entity,
                company_name=company_name,
                question_id=qid,
                score=body["score"],
                description=body if description_object else json.dumps(body),
            )
        )
        entries[qid] = dict(
            kind="work", work_item_id=member["work_item_id"], item={"question_id": qid}
        )
    if member_fault == "missing":
        payloads.pop()
    elif member_fault == "invalid":
        payloads[-1]["score"] = 0
    if member_fault == "semantic_invalid":
        bad_body = payloads[-1]["description"]
        bad_body = bad_body if isinstance(bad_body, dict) else json.loads(bad_body)
        bad_body["items"] = [{"name": "invalid score-fact mixture"}]
        payloads[-1]["description"] = bad_body if description_object else json.dumps(bad_body)
    text = json.dumps({"answers": payloads})
    if journal:
        from src.utils.quick_scan_research_output import persist, visible_output_sha256
        from tests.unit import test_quick_scan_known_outcome as fixtures

        visible = dict(content=text, tool_calls=[], finish_reason="stop")
        original_receipt = fixtures._receipt

        def anchored_receipt(**kwargs):
            return {
                **original_receipt(**kwargs),
                "research_visible_output_sha256": visible_output_sha256(visible),
            }

        monkeypatch.setattr(fixtures, "_receipt", anchored_receipt)
        journal_root = tmp_path / "journal"
        journal_root.mkdir()
        store, clock, members, pack, receipt, _, context = fixture(
            journal_root, response_status="completed", currency="USD"
        )
        pack = store.get_question_pack(pack["pack_id"])
        entries = {
            m["question_id"]: dict(
                kind="work", work_item_id=m["work_item_id"], item={"question_id": m["question_id"]}
            )
            for m in pack["members"]
        }
        with store._transaction() as connection:
            persist(store, connection, pack["budget_attempt_id"], visible)
        from src.utils.quick_scan_work_store import QuickScanWorkStore

        store = QuickScanWorkStore(store.path, clock=lambda: clock[0])
    original = store.get_attempt_response(pack["members"][0]["attempt_id"])
    capture = dict(
        record_kind="final_answer_diagnostic",
        final_answer_retained=True,
        final_answer=text,
        final_answer_sha256=hashlib.sha256(text.encode()).hexdigest(),
        final_answer_bytes=len(text.encode()),
        response_sha256=original["response_sha256"],
        original_transport=dict(
            question_pack_id=pack["pack_id"],
            execution_attempt_id=pack["execution_attempt_id"],
            physical_budget_attempt_id=pack["budget_attempt_id"],
            members=[
                dict(
                    question_id=m["question_id"],
                    work_item_id=m["work_item_id"],
                    work_attempt_id=m["attempt_id"],
                )
                for m in pack["members"]
            ],
        ),
    )
    capture.update(
        {
            k: receipt.get(v)
            for k, v in (
                ("provider", "provider"),
                ("actual_model", "actual_model"),
                ("response_id", "response_id"),
                ("response_status", "response_status"),
            )
        }
    )
    directory = tmp_path / "captured"
    directory.mkdir()
    (directory / ("a" * 64 + ".final.json")).write_text(json.dumps(capture), encoding="utf8")
    owner = ApprovedRefreshSession.__new__(ApprovedRefreshSession)
    owner.recovery_only = True
    owner.entries = entries
    owner.service = NS(store=store)
    from tests.unit.test_quick_scan_observation_context import MANIFEST

    owner.bundle = {
        "authority": {},
        "identity": {"entity_id": entity},
        "manifest": copy.deepcopy(MANIFEST),
    }
    owner.input_raw = {
        "identity.json": json.dumps({"payload": {"canonical_name": company_name}}).encode()
    }
    owner.check = lambda: None
    owner.assert_received_checkpoint_recovery = lambda *args, **kwargs: None
    clock[0] += 1000
    original_responses = [store.get_attempt_response(m["attempt_id"]) for m in pack["members"]]
    budget = store.get_quick_scan_budget_status("pack-policy")
    policy = None if journal else {"directory": directory}
    report = owner.recover_received_checkpoints(capture_policy=policy)
    valid_members = pack["members"][:-1] if member_fault else pack["members"]
    assert set(report["recovered_work_item_ids"]) == {m["work_item_id"] for m in valid_members}
    assert report["status"] == ("partial" if member_fault else "completed")
    again = owner.recover_received_checkpoints(capture_policy=policy)
    assert again["recovered_work_item_ids"] == []
    assert set(again["attached_work_item_ids"]) == {m["work_item_id"] for m in valid_members}
    assert report["API_requests"] == report["new_attempts"] == report["model_calls"] == 0
    assert report["dispatch_started"] is False
    assert [
        store.get_attempt_response(m["attempt_id"]) for m in pack["members"]
    ] == original_responses
    assert store.get_quick_scan_budget_status("pack-policy") == budget
    for member in valid_members:
        assert (
            store.get_answer_checkpoint(member["work_item_id"])["attempt_id"]
            == member["attempt_id"]
        )
        assert len(store.list_attempts(member["work_item_id"])) == 1
