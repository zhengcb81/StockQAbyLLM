"""Actual isolated SQLite: logical failure never rewrites physical success."""

import copy
from pathlib import Path

import pytest

from src.providers.llm_client import LLMClient
from src.utils.quick_scan_work_store import (
    LeaseFencedError,
    QuickScanWorkStore,
    WorkConflictError,
)
from src.utils.quick_scan_work_transport import (
    bind_quick_scan_route,
    bind_quick_scan_work,
)
from tests.unit.test_quick_scan_work_transport import (
    _create_claimed_item,
    _http_response,
)


def received(tmp_path, monkeypatch):
    clock = [100.0]
    store, work, lease = _create_claimed_item(tmp_path, clock=lambda: clock[0])
    from unittest.mock import Mock

    session = Mock()
    session.post.return_value = _http_response(200)
    monkeypatch.setattr(
        "src.providers.llm_client.http_client_manager.get_sync_session", lambda: session
    )
    with bind_quick_scan_work(store, work, lease), bind_quick_scan_route(
        route_id="primary", provider="configured", model_requested="fixture-model"
    ):
        LLMClient(
            "synthetic", "fixture-model", "https://api.openai.com/v1/responses"
        ).send_search_request("synthetic")
    attempt = store.list_attempts(work)[0]
    return store, work, lease, attempt, clock


def test_received_missing_answer_is_durable_failed_not_a_new_physical_failure(
    tmp_path, monkeypatch
):
    store, work, lease, attempt, clock = received(tmp_path, monkeypatch)
    original = store.get_attempt_response(attempt["attempt_id"])
    attempts = store.list_attempts(work)
    assert (
        store.fail_received_without_answer(
            work, lease, attempt["attempt_id"], reason_code="missing_question"
        )
        == "failed"
    )
    item = store.get_item(work)
    assert item["status"] == "failed" and item["lease_token"] is item["lease_expires_at"] is None
    assert (
        store.list_attempts(work) == attempts
        and store.get_attempt_response(attempt["attempt_id"]) == original
    )
    assert store.get_answer_checkpoint(work) is store.get_result_delivery(work) is None
    assert store.claim(work, lease_seconds=30) is None
    reopened = QuickScanWorkStore(store.path, clock=lambda: clock[0])
    before = store.path.read_bytes()
    assert (
        reopened.fail_received_without_answer(
            work, lease, attempt["attempt_id"], reason_code="missing_question"
        )
        == "failed"
    )
    assert reopened.path.read_bytes() == before
    assert (
        len(
            [
                e
                for e in reopened.list_events(work)
                if e["event_type"] == "received_without_answer:missing_question"
            ]
        )
        == 1
    )


@pytest.mark.parametrize(
    "fault",
    ["expired", "wrong_attempt", "wrong_reason", "changed_reason", "no_response"],
)
def test_failure_cannot_resolve_uncertain_or_cross_fences(tmp_path, monkeypatch, fault):
    store, work, lease, attempt, clock = received(tmp_path, monkeypatch)
    attempt_id = attempt["attempt_id"]
    reason = "invalid_question"
    if fault == "expired":
        clock[0] = 200.0
    elif fault == "wrong_attempt":
        attempt_id = "ATTEMPT_SYNTHETIC_WRONG"
    elif fault == "wrong_reason":
        reason = "caller_claimed_success"
    elif fault == "changed_reason":
        store.fail_received_without_answer(work, lease, attempt_id, reason_code="missing_question")
    else:
        store, work, lease = _create_claimed_item(tmp_path / "unsent", clock=lambda: clock[0])
        attempt_id = "ATTEMPT_SYNTHETIC_UNSENT"
    before = store.path.read_bytes()
    with pytest.raises((ValueError, WorkConflictError, LeaseFencedError)):
        store.fail_received_without_answer(work, lease, attempt_id, reason_code=reason)
    assert store.path.read_bytes() == before


def test_v17_migration_preserves_rows_and_current_events_reject_sql_mutation(tmp_path, monkeypatch):
    import sqlite3
    from contextlib import closing

    store, work, lease, attempt, clock = received(tmp_path, monkeypatch)
    before_item = store.get_item(work)
    before_attempts = store.list_attempts(work)
    before_response = store.get_attempt_response(attempt["attempt_id"])
    before_events = store.list_events(work)
    # Explicit mutable fixture restores the previous native DDL, never a real
    # old ledger or the immutable source SQL used in inspection regressions.
    with closing(sqlite3.connect(store.path)) as con:
        con.execute("DROP TRIGGER quick_scan_work_event_no_update")
        con.execute("DROP TRIGGER quick_scan_work_event_no_delete")
        con.execute("PRAGMA user_version=17")
        con.commit()
    migrated = QuickScanWorkStore(store.path, clock=lambda: clock[0])
    assert (
        migrated.get_item(work) == before_item and migrated.list_attempts(work) == before_attempts
    )
    assert (
        migrated.get_attempt_response(attempt["attempt_id"]) == before_response
        and migrated.list_events(work) == before_events
    )
    migrated.fail_received_without_answer(
        work, lease, attempt["attempt_id"], reason_code="invalid_question"
    )
    before = migrated.path.read_bytes()
    for sql in ("UPDATE work_event SET event_type='forged'", "DELETE FROM work_event"):
        with closing(sqlite3.connect(migrated.path)) as con:
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                con.execute(sql)
    assert migrated.path.read_bytes() == before


def test_write_lock_wait_cannot_extend_current_lease(tmp_path, monkeypatch):
    import sqlite3
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import closing, contextmanager

    store, work, lease, attempt, clock = received(tmp_path, monkeypatch)
    original_transaction = store._transaction
    entered = threading.Event()

    @contextmanager
    def waiting_transaction():
        entered.set()
        with original_transaction() as con:
            yield con

    monkeypatch.setattr(store, "_transaction", waiting_transaction)
    before = store.path.read_bytes()
    with closing(sqlite3.connect(store.path, isolation_level=None)) as blocker, ThreadPoolExecutor(
        max_workers=1
    ) as pool:
        blocker.execute("BEGIN IMMEDIATE")
        task = pool.submit(
            store.fail_received_without_answer,
            work,
            lease,
            attempt["attempt_id"],
            reason_code="missing_question",
        )
        assert entered.wait(3)
        clock[0] = 200.0
        blocker.execute("ROLLBACK")
        with pytest.raises(LeaseFencedError):
            task.result(timeout=5)
    assert store.path.read_bytes() == before and store.get_item(work)["status"] == "leased"


def test_real_lifecycle_only_finishes_explicit_member_failure(tmp_path, monkeypatch):
    from src.runners.llm_runner import QuickScanWorkLifecycle

    store, work, lease, attempt, clock = received(tmp_path, monkeypatch)
    lifecycle = QuickScanWorkLifecycle(
        store,
        entity_id=store.get_item(work)["entity_id"],
        run_id="RUN_SYNTHETIC",
        scan_id="SCAN_SYNTHETIC",
        identity={"identity_state": "verified"},
        transport_managed=True,
    )
    handle = dict(work_item_id=work, lease=lease, claimed=True)
    before = store.path.read_bytes()
    lifecycle.after_question_failed(handle, "RuntimeError")
    assert store.path.read_bytes() == before
    lifecycle.after_question_failed(handle, "answer_generator_rejected_member")
    assert store.get_item(work)["status"] == "failed"
    assert store.list_attempts(work)[0]["phase"] == "response_available"
