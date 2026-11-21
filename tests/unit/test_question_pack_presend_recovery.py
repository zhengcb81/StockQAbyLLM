"""Offline prepared-pack recovery against isolated native SQLite fixtures."""

import copy
import json
import sqlite3
from contextlib import closing
from unittest.mock import Mock

import pytest

from src.core import qa_engine
from src.utils import quick_scan_work_transport as transport
from src.utils.quick_scan_work_store import LeaseFencedError
from tests.unit.test_qa_question_pack import engine, q
from tests.unit.test_quick_scan_question_pack_transport import (
    PROMPT,
    SYSTEM,
    _begin,
    _bindings,
    _response,
    pack,
)
from tests.unit.test_quick_scan_work_store import (
    _answer_for,
    _created,
    _successful_search_attempt,
)

SECRET = "sk-synthetic-private https://secret.invalid/path?token=private"


def _state(store, members):
    return [
        (store.get_item(wid), store.list_attempts(wid), store.list_events(wid))
        for wid, _ in members
    ]


def _fail_begin(pack, monkeypatch, action):
    store, _members, _clock = pack
    original = store.mark_question_pack_send_intent

    def fail(pack_id, **kwargs):
        action(pack_id, original, kwargs)
        raise RuntimeError(SECRET)

    monkeypatch.setattr(store, "mark_question_pack_send_intent", fail)
    with _bindings(pack), pytest.raises(transport.QuickScanWorkPersistenceError) as caught:
        _begin()
    return caught.value


@pytest.mark.parametrize("kind", ["busy", "locked", "ordinary", "owner_guard", "registration"])
def test_presend_failure_releases_exact_pack_and_fences_old_worker(pack, monkeypatch, kind):
    store, members, _clock = pack
    original_mark = store.mark_question_pack_send_intent
    original_get = store.get_question_pack
    original_persist = store.persist_subject_dispatch_receipt
    observed = {}
    original_prepare = store.prepare_question_pack

    def prepare(*args, **kwargs):
        value = original_prepare(*args, **kwargs)
        observed.setdefault("pack_id", value["pack_id"])
        observed.setdefault("before", copy.deepcopy(value))
        return value

    monkeypatch.setattr(store, "prepare_question_pack", prepare)
    source = RuntimeError(SECRET)
    expected_reason = "admission_failed"
    if kind == "busy":
        original_connect = store._connect

        def short_connect():
            connection = original_connect()
            connection.execute("PRAGMA busy_timeout=1")
            return connection

        monkeypatch.setattr(store, "_connect", short_connect)

        def failure(pack_id, **kwargs):
            with closing(original_connect()) as blocker:
                blocker.execute("BEGIN IMMEDIATE")
                try:
                    original_mark(pack_id, **kwargs)
                finally:
                    blocker.rollback()

        monkeypatch.setattr(store, "mark_question_pack_send_intent", failure)
        expected_reason = "sqlite_busy"
    elif kind == "locked":
        source = sqlite3.OperationalError(SECRET)
        source.sqlite_errorcode = getattr(sqlite3, "SQLITE_LOCKED", 6)
        source.sqlite_errorname = "SQLITE_LOCKED"
        expected_reason = "sqlite_locked"

        def failure(*_args, **_kwargs):
            raise source

        monkeypatch.setattr(store, "mark_question_pack_send_intent", failure)
    elif kind == "owner_guard":
        expected_reason = "owner_guard_rejected"
        calls = []

        def guard():
            assert getattr(store._transaction_state, "connection", None) is None
            calls.append(True)
            if len(calls) == 1:
                raise source

        monkeypatch.setattr(transport, "_check_owner_refresh_guard", guard)
    elif kind == "registration":

        def failure(*_args, **_kwargs):
            raise source

        monkeypatch.setattr(store, "persist_subject_dispatch_receipt", failure)
    else:

        def failure(*_args, **_kwargs):
            raise source

        monkeypatch.setattr(store, "mark_question_pack_send_intent", failure)
    with _bindings(pack), pytest.raises(transport.QuickScanWorkPersistenceError) as caught:
        _begin()
    error = caught.value
    assert all(store.get_item(wid)["status"] == "pending" for wid, _ in members)
    diagnostic = error.diagnostic
    native_details = False
    if kind in {"busy", "locked"}:
        expected_name = "SQLITE_BUSY" if kind == "busy" else "SQLITE_LOCKED"
        expected_code = getattr(sqlite3, expected_name, None)
        native_details = (
            expected_code is not None
            and getattr(error.__cause__, "sqlite_errorcode", None) == expected_code
            and getattr(error.__cause__, "sqlite_errorname", None) == expected_name
        )
        expected_reason = (
            ("sqlite_busy" if kind == "busy" else "sqlite_locked")
            if native_details
            else "sqlite_error"
        )
    assert diagnostic["released_unsent"] is True
    assert diagnostic["reason"] == expected_reason
    assert SECRET not in json.dumps(diagnostic)
    if kind == "busy":
        assert isinstance(error.__cause__, sqlite3.OperationalError)
    else:
        assert error.__cause__ is source
    if kind in {"busy", "locked"}:
        assert diagnostic["sqlite_errorcode"] == (expected_code if native_details else None)
        assert diagnostic["sqlite_errorname"] == (expected_name if native_details else None)
    frozen = original_get(observed["pack_id"])
    assert frozen == observed["before"]  # Immutable pack history is not rewritten.
    fresh_members = []
    for wid, old_lease in members:
        item = store.get_item(wid)
        attempts = store.list_attempts(wid)
        assert item["status"] == "pending" and item["lease_token"] is None
        assert attempts[-1]["phase"] == "abandoned_unsent"
        assert attempts[-1]["send_intent_at"] is None
        assert store.get_attempt_response(attempts[-1]["attempt_id"]) is None
        if kind != "registration":
            assert store.get_subject_dispatch_receipt(attempts[-1]["attempt_id"]) is not None
        with pytest.raises(LeaseFencedError):
            store.release_unsent(wid, old_lease)
        fresh = store.claim(wid, lease_seconds=60)
        assert fresh.lease_epoch == old_lease.lease_epoch + 1
        fresh_members.append((wid, fresh))
    with closing(store.connect()) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM quick_scan_budget_attempt").fetchone()[0] == 0
        )
    monkeypatch.setattr(store, "mark_question_pack_send_intent", original_mark)
    monkeypatch.setattr(store, "persist_subject_dispatch_receipt", original_persist)
    with _bindings((store, tuple(fresh_members), _clock)):
        continued = transport.begin_quick_scan_send(
            PROMPT + " after recovery",
            SYSTEM,
            model_requested="mimo-v2.6-flash",
            provider_actual="mimo",
            execution_attempt_id="22222222-2222-4222-8222-222222222222",
            request_payload_sha256="a" * 64,
        )
    assert continued.question_pack_id != observed["pack_id"]
    assert store.get_question_pack(continued.question_pack_id)["phase"] == "send_intent"


@pytest.mark.parametrize("outcome", ["send_intent", "response_available"])
def test_committed_send_or_response_is_never_released(pack, monkeypatch, outcome):
    store, members, _clock = pack

    def commit(pack_id, mark, kwargs):
        mark(pack_id, **kwargs)
        if outcome == "response_available":
            store.record_question_pack_outcome(
                pack_id,
                outcome=outcome,
                execution_receipt=_response(),
                http_status_code=200,
                actual_cost=0.03,
                cost_source_ref="synthetic-rate",
            )

    error = _fail_begin(pack, monkeypatch, commit)
    assert error.diagnostic["released_unsent"] is False
    for wid, old_lease in members:
        assert store.get_item(wid)["lease_token"] == old_lease.lease_token
        assert store.list_attempts(wid)[-1]["phase"] == outcome
        assert store.claim(wid, lease_seconds=60) is None


@pytest.mark.parametrize("changed", ["foreign_lease", "expired", "second_release"])
def test_one_changed_or_failed_member_prevents_partial_release(pack, monkeypatch, changed):
    store, members, clock = pack
    observed = {}
    original_get = store.get_question_pack
    original_release = store.release_unsent

    def interfere(pack_id, _mark, _kwargs):
        if changed == "foreign_lease":
            wid, lease = members[-1]
            original_release(wid, lease)
            assert store.claim(wid, lease_seconds=300) is not None
        elif changed == "expired":
            clock[0] += 301
        else:
            count = []

            def release(wid, lease):
                count.append(wid)
                if len(count) == 2:
                    raise RuntimeError(SECRET)
                return original_release(wid, lease)

            monkeypatch.setattr(store, "release_unsent", release)
        observed["before"] = _state(store, members)

    error = _fail_begin(pack, monkeypatch, interfere)
    assert error.diagnostic["released_unsent"] is False
    assert _state(store, members) == observed["before"]
    assert SECRET not in json.dumps(error.diagnostic)


@pytest.mark.parametrize(
    "failure", ["before_prepare_commit", "after_prepare_commit", "invalid_payload"]
)
def test_known_unsent_preparation_failure_returns_original_scope_to_pending(
    pack, monkeypatch, failure
):
    store, members, _clock = pack
    original = store.prepare_question_pack
    release = Mock(wraps=store.release_unsent)
    monkeypatch.setattr(store, "release_unsent", release)

    def prepare(*args, **kwargs):
        if failure == "after_prepare_commit":
            original(*args, **kwargs)
        raise RuntimeError(SECRET)

    if failure != "invalid_payload":
        monkeypatch.setattr(store, "prepare_question_pack", prepare)
    with _bindings(pack), pytest.raises(Exception) as caught:
        _begin(**({"request_payload_sha256": "bad"} if failure == "invalid_payload" else {}))
    assert all(store.get_item(wid)["status"] == "pending" for wid, _lease in members)
    assert caught.value.diagnostic["released_unsent"] is True
    assert release.call_count == len(members)  # One cleanup, never a second old-lease release.
    assert store.get_quick_scan_budget_status("pack-transport")["requests"] == 0
    for wid, old_lease in members:
        with pytest.raises(LeaseFencedError):
            store.release_unsent(wid, old_lease)
        assert all(a["phase"] == "abandoned_unsent" for a in store.list_attempts(wid))


def test_cleanup_uses_native_binding_not_a_pack_projection(pack, monkeypatch):
    store, members, _clock = pack
    read = Mock(side_effect=AssertionError("cleanup must delegate native release proof"))
    monkeypatch.setattr(store, "get_question_pack", read)
    error = _fail_begin(pack, monkeypatch, lambda *_args: None)
    assert error.diagnostic["released_unsent"] is True
    assert all(store.get_item(wid)["status"] == "pending" for wid, _lease in members)
    read.assert_not_called()


def test_legacy_sqlite_error_details_do_not_block_native_unsent_release(pack, monkeypatch):
    for name in ("SQLITE_BUSY", "SQLITE_LOCKED"):
        monkeypatch.delattr(sqlite3, name, raising=False)
    source = sqlite3.OperationalError("synthetic Python 3.10-style SQLite error")

    def fail(*_args):
        raise source

    error = _fail_begin(pack, monkeypatch, fail)
    assert error.diagnostic["reason"] == "sqlite_error"
    assert error.diagnostic["sqlite_errorcode"] is None
    assert error.diagnostic["sqlite_errorname"] is None
    assert error.diagnostic["released_unsent"] is True
    assert all(pack[0].get_item(wid)["status"] == "pending" for wid, _lease in pack[1])


def test_release_preserves_an_unrelated_original_paid_checkpoint(pack, monkeypatch):
    store, members, _clock = pack
    paid = _created(store, entity_id="ENT_PAID", question_id="PAID_01", scope_id="ENT_PAID")
    lease, attempt, receipt = _successful_search_attempt(store, paid["work_item_id"])
    before = store.save_answer_checkpoint(
        paid["work_item_id"],
        lease,
        attempt["attempt_id"],
        answer=_answer_for(paid),
        execution_receipt=receipt,
    )
    paid_state = _state(store, [(paid["work_item_id"], lease)])
    error = _fail_begin(pack, monkeypatch, lambda *_: None)
    assert error.diagnostic["released_unsent"] is True
    assert store.get_answer_checkpoint(paid["work_item_id"]) == before
    assert _state(store, [(paid["work_item_id"], lease)]) == paid_state
    assert all(store.get_item(wid)["status"] == "pending" for wid, _ in members)


def test_engine_keeps_only_safe_diagnostics_and_never_retries(pack, monkeypatch):
    error = _fail_begin(pack, monkeypatch, lambda *_: None)
    error.diagnostic["request_url"] = SECRET
    # The core must revalidate the exception's mutable diagnostic dictionary.
    error.diagnostic["source_type"] = SECRET
    e, provider, lifecycle = engine()
    provider.error = error
    warning = Mock()
    monkeypatch.setattr(qa_engine.logger, "warning", warning)
    batch = e.process_questions([q(1), q(2), q(3)], question_pack_size=2)
    assert len(provider.calls) == 1 and len(lifecycle.failed) == 2
    assert batch.results[-1].metadata["question_pack_not_dispatched"] is True
    for result in batch.results[:2]:
        diagnostic = result.metadata["quick_scan_diagnostic"]
        assert diagnostic["released_unsent"] is True
        assert diagnostic["source_type"] == "Exception"
        assert result.answer.metadata["quick_scan_diagnostic"] == diagnostic
    serialized = repr([(r.metadata, r.answer.metadata) for r in batch.results])
    assert SECRET not in serialized and SECRET not in repr(warning.call_args_list)
    assert warning.called


@pytest.mark.parametrize("field", ["stage", "reason", "source_type", "release_reason"])
def test_engine_rejects_non_scalar_untrusted_diagnostic_values(field):
    error = transport.QuickScanWorkPersistenceError("fixture")
    error.diagnostic = {
        "stage": "send_intent",
        "reason": "admission_failed",
        "source_type": "RuntimeError",
        "released_unsent": False,
        "release_reason": "release_check_failed",
        field: {"secret": SECRET},
    }
    safe = qa_engine._safe_quick_scan_diagnostic(error)
    assert SECRET not in json.dumps(safe)


def test_engine_retains_original_execution_alongside_safe_diagnostics():
    error = transport.QuickScanWorkPersistenceError(
        "fixture",
        diagnostic={
            "stage": "permit",
            "reason": "admission_failed",
            "source_type": "RuntimeError",
            "released_unsent": False,
            "release_reason": "original_pack_not_prepared",
        },
    )
    e, provider, lifecycle = engine()
    lifecycle.after_question = Mock(side_effect=error)
    batch = e.process_questions([q(1), q(2)], question_pack_size=2)
    assert len(provider.calls) == 1
    for result in batch.results:
        assert result.answer.metadata["execution"] == {"actual": "physical"}
        assert result.answer.metadata["quick_scan_diagnostic"]["released_unsent"] is False


def test_real_uncertain_outcome_is_never_released_or_reclassified(pack, monkeypatch):
    store, members, _clock = pack
    source = transport.QuickScanWorkUncertainError(
        "physical-original", {"request_id": "original-request", "search_status": "unverified"}
    )

    def fail(*_args, **_kwargs):
        raise source

    monkeypatch.setattr(store, "mark_question_pack_send_intent", fail)
    with _bindings(pack), pytest.raises(transport.QuickScanWorkPersistenceError) as caught:
        _begin()
    assert caught.value.__cause__ is source
    assert caught.value.diagnostic["released_unsent"] is False
    assert all(store.get_item(wid)["status"] == "leased" for wid, _ in members)
    e, provider, _lifecycle = engine()
    provider.error = caught.value
    batch = e.process_questions([q(1), q(2)], question_pack_size=2)
    for result in batch.results:
        metadata = result.answer.metadata
        assert metadata["failure_type"] == "outcome_uncertain"
        assert metadata["execution"]["attempt_id"] == "physical-original"
        assert metadata["execution"]["request_id"] == "original-request"
