"""Late pack send-intent proof: real SDK/SQLite, synthetic wire/owner, no network."""

import copy
from contextlib import closing
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

import tests.unit.test_known_pack_continuation as fixtures
from src.core.qa_engine import QAEngine
from src.providers.base_llm_provider import standard_answer_transport
from src.utils import quick_scan_work_transport as native_transport
from src.utils.quick_scan_work_decision_inspect import derive_work_decision
from src.utils.quick_scan_work_store import QuickScanWorkStore, WorkConflictError
from tests.unit.test_known_outcome_singleton import single, transport
from tests.unit.test_quick_scan_known_outcome import authority, immutable


def late_pack(tmp_path, monkeypatch, *, unknown=False, event_fault=None):
    from src.providers import llm_client

    store, provider, lifecycle, questions, session, policy = fixtures.fixture(
        tmp_path, monkeypatch, "unknown" if unknown else "answer_generator"
    )
    clock = [1800000000.0]
    monkeypatch.setattr(store, "clock", lambda: clock[0])
    monkeypatch.setattr(
        lifecycle._owner_refresh,
        "register_subject_dispatch",
        lambda request: fixtures._owner_receipt(request, clock[0]),
    )
    monkeypatch.setattr(
        llm_client,
        "_utc_now",
        lambda: datetime.fromtimestamp(clock[0], timezone.utc).isoformat().replace("+00:00", "Z"),
    )
    if event_fault:
        original_event = store._event

        def event(con, wid, kind, epoch, now, **kwargs):
            if kind == "late_receipt":
                if event_fault == "missing_event":
                    return None
                if event_fault == "foreign_event":
                    epoch += 1
            return original_event(con, wid, kind, epoch, now, **kwargs)

        monkeypatch.setattr(store, "_event", event)
    handles = {}
    before = lifecycle.before_question

    def capture(question):
        result = before(question)
        if result.get("claimed"):
            handles[question.question_id] = result
        return result

    monkeypatch.setattr(lifecycle, "before_question", capture)
    original = session.post.side_effect

    def post(*args, **kwargs):
        clock[0] += 650
        return original(*args, **kwargs)

    session.post.side_effect = post
    with standard_answer_transport(True), native_transport.bind_quick_scan_budget(
        store,
        policy,
        cost_resolver=lambda _: {
            "actual_cost": 0.03,
            "pricing_ref": "fixture-price",
            "source_ref": "synthetic-rate",
        },
    ):
        QAEngine(
            provider, progress_reporter=Mock(), work_item_lifecycle=lifecycle
        ).process_questions(questions[:2], question_pack_size=2)
    assert session.post.call_count == 1
    assert all(store.get_item(h["work_item_id"])["status"] == "uncertain" for h in handles.values())
    return store, clock, handles, questions, session, policy, lifecycle


def intent_for(store, wid, index):
    attempt = store.list_attempts(wid)[-1]
    response = store.get_attempt_response(attempt["attempt_id"])
    return dict(
        kind="known_response_resolution",
        work_item_id=wid,
        attempt_id=attempt["attempt_id"],
        idempotency_key="late-known-" + str(index),
        reason="answer_validation_rejected",
        diagnostic={
            "code": "standard_answer_invalid",
            "artifact_ref": "synthetic-only-validation.json",
            "artifact_sha256": "a" * 64,
            "response_sha256": response["response_sha256"],
        },
    )


def test_actual_late_send_intent_pack_derive_close_reopen_and_explicit_retry(tmp_path, monkeypatch):
    store, clock, handles, questions, session, policy, lifecycle = late_pack(tmp_path, monkeypatch)
    physical_before = immutable(store)
    intents = []
    saved = []
    for index, q in enumerate(questions[:2]):
        wid = handles[q.question_id]["work_item_id"]
        attempt = store.list_attempts(wid)[-1]
        assert (
            attempt["phase"] == "send_intent"
            and attempt["late_receipt_sha256"]
            and not attempt["receipt_sha256"]
        )
        intent = intent_for(store, wid, index)
        intents.append(intent)
        before = store.path.read_bytes()
        result = derive_work_decision(store.path, intent, policy=policy, clock=lambda: clock[0])
        assert store.path.read_bytes() == before
        proof = result["request"]["physical"]
        response = store.get_attempt_response(attempt["attempt_id"])
        decision = store.resolve_known_response(
            wid,
            attempt_id=attempt["attempt_id"],
            expected_lease=handles[q.question_id]["lease"],
            receipt_sha256=proof["receipt_sha256"],
            request_payload_sha256=proof["request_payload_sha256"],
            reason=intent["reason"],
            owner_authority=authority(),
            idempotency_key=intent["idempotency_key"],
            diagnostic=intent["diagnostic"],
        )
        saved.append(decision)
        assert (
            store.get_item(wid)["status"] == "failed" and store.get_answer_checkpoint(wid) is None
        )
    assert immutable(store) == physical_before and session.post.call_count == 1
    reopened = QuickScanWorkStore(store.path, clock=lambda: clock[0])
    for index, q in enumerate(questions[:2]):
        wid = handles[q.question_id]["work_item_id"]
        intent = intents[index]
        decision = saved[index]
        proof = decision["request"]["physical"]
        assert (
            reopened.resolve_known_response(
                wid,
                attempt_id=intent["attempt_id"],
                expected_lease=handles[q.question_id]["lease"],
                receipt_sha256=proof["receipt_sha256"],
                request_payload_sha256=proof["request_payload_sha256"],
                reason=intent["reason"],
                owner_authority=authority(),
                idempotency_key=intent["idempotency_key"],
                diagnostic=intent["diagnostic"],
            )
            == decision
        )
    q = questions[0]
    wid = handles[q.question_id]["work_item_id"]
    retry = reopened.record_owner_retry_decision(
        wid,
        purpose="retry_unusable_answer",
        retry_after_seconds=0,
        owner_authority=authority(),
        idempotency_key="late-explicit-retry",
    )
    assert (
        retry["request"]["target_generation"] == 2
        and retry["request"]["source_resolution_id"] == saved[0]["decision_id"]
    )
    assert reopened.claim(wid, lease_seconds=600) is None
    assert immutable(reopened) == physical_before and session.post.call_count == 1
    from src.runners.llm_runner import QuickScanWorkLifecycle
    from src.utils.quick_scan_owner_refresh import (
        OwnerRefreshSession,
        routing_fingerprints,
    )

    old_owner = lifecycle._owner_refresh
    new_plan = copy.deepcopy(old_owner.plan)
    selected = next(row for row in new_plan["fields"] if row["field_id"] == q.question_id)
    selected.update(
        decision="dispatch_new_generation",
        generation=retry["request"]["target_generation"],
        request_identity_key="explicit-authorized-" + retry["decision_id"],
    )
    next_owner = OwnerRefreshSession(
        old_owner.client,
        old_owner.current,
        new_plan,
        old_owner.scope_bindings,
        {},
        old_owner.identity,
        reopened,
        observation_context=lifecycle._observation_context,
    )
    next_lifecycle = QuickScanWorkLifecycle(
        reopened,
        entity_id=lifecycle._entity_id,
        run_id="RUN_EXPLICIT_LATE_RETRY",
        scan_id="SCAN_EXPLICIT_LATE_RETRY",
        identity=lifecycle._identity,
        transport_managed=True,
        owner_refresh_session=next_owner,
        observation_context=lifecycle._observation_context,
        scope_by_question=lifecycle._scope_by_question,
        generation_by_question={q.question_id: 2},
        routing_fingerprint_by_question=routing_fingerprints(next_owner),
        c06_authority=lifecycle._c06_authority,
    )
    fresh = next_lifecycle.before_question(q)
    assert fresh["claimed"] is True and fresh["work_item_id"] != wid
    assert reopened.get_item(fresh["work_item_id"])["generation"] == 2
    assert reopened.get_item(wid)["status"] == "failed" and session.post.call_count == 1
    assert immutable(reopened) == physical_before


@pytest.mark.parametrize(
    "fault", ["missing_late", "foreign_late", "missing_event", "foreign_event", "unknown"]
)
def test_late_pack_requires_original_late_hash_and_event_and_known_physical(
    tmp_path, monkeypatch, fault
):
    store, clock, handles, questions, session, policy, _ = late_pack(
        tmp_path,
        monkeypatch,
        unknown=fault == "unknown",
        event_fault=fault if fault in {"missing_event", "foreign_event"} else None,
    )
    wid = handles[questions[0].question_id]["work_item_id"]
    if fault == "unknown":
        attempt = store.list_attempts(wid)[-1]
        intent = dict(
            kind="known_response_resolution",
            work_item_id=wid,
            attempt_id=attempt["attempt_id"],
            idempotency_key="unknown-late",
            reason="answer_validation_rejected",
            diagnostic={
                "code": "standard_answer_invalid",
                "artifact_ref": "synthetic-only-validation.json",
                "artifact_sha256": "a" * 64,
                "response_sha256": "f" * 64,
            },
        )
    else:
        intent = intent_for(store, wid, 0)
        # Negative corruption fixtures, never a manufactured positive owner proof.
        with store._transaction() as con:
            if fault == "missing_late":
                con.execute(
                    "UPDATE attempt SET late_receipt_sha256=NULL WHERE work_item_id=?", (wid,)
                )
            if fault == "foreign_late":
                con.execute(
                    "UPDATE attempt SET late_receipt_sha256=? WHERE work_item_id=?", ("0" * 64, wid)
                )
    before = store.path.read_bytes()
    with pytest.raises((ValueError, WorkConflictError)):
        derive_work_decision(store.path, intent, policy=policy, clock=lambda: clock[0])
    assert store.path.read_bytes() == before and session.post.call_count == 1


def test_singleton_send_intent_still_rejected_even_with_known_receipt(
    tmp_path, transport, monkeypatch
):
    store, clock, item, args, policy, _ = single(tmp_path, transport, monkeypatch)
    clock[0] += 121
    with store._transaction() as con:
        con.execute(
            "UPDATE attempt SET phase='send_intent',late_receipt_sha256=receipt_sha256 WHERE attempt_id=?",
            (args["attempt_id"],),
        )
    before = store.path.read_bytes()
    with pytest.raises(WorkConflictError):
        store.resolve_known_response(item["work_item_id"], **args)
    assert store.path.read_bytes() == before
