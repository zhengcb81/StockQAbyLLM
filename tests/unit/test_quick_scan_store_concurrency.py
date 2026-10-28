"""Real SQLite contention and post-lock fencing, isolated and without HTTP."""

import sqlite3
import threading
import time
from contextlib import closing

import pytest

from src.utils.quick_scan_work_store import (
    SCHEMA_VERSION,
    LeaseFencedError,
    QuickScanWorkStore,
)
from tests.unit.test_quick_scan_known_outcome import fixture, immutable
from tests.unit.test_quick_scan_work_store import (
    _created,
    _install_v1_fixture,
    _prepared,
    _store,
)


def _wait_for_writer(store, operation, during_wait):
    """Release a real competing writer only after the tested BEGIN was issued."""
    original = store._connect
    waiting = threading.Event()
    outcome = {}

    def connect():
        connection = original()
        if threading.current_thread().name == "contending-writer":
            connection.set_trace_callback(
                lambda sql: waiting.set() if sql.upper() == "BEGIN IMMEDIATE" else None
            )
        return connection

    def run():
        try:
            outcome["value"] = operation()
        except BaseException as error:
            outcome["error"] = error

    store._connect = connect
    thread = threading.Thread(target=run, name="contending-writer")
    try:
        with closing(original()) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            try:
                thread.start()
                assert waiting.wait(10), "tested operation never reached the write lock"
                during_wait()
            finally:
                blocker.rollback()
                thread.join(20)
        assert not thread.is_alive()
        return outcome
    finally:
        store._connect = original


@pytest.mark.parametrize("operation", ["prepare", "send", "outcome"])
def test_expired_lease_during_write_wait_cannot_advance_attempt(tmp_path, operation):
    clock = [1800000000.0]
    store = _store(tmp_path, clock)
    item = _created(store)
    wid = item["work_item_id"]
    lease = store.claim(wid, lease_seconds=60)
    if operation == "prepare":
        action = lambda: _prepared(store, wid, lease)
    else:
        attempt = _prepared(store, wid, lease)
        if operation == "send":
            action = lambda: store.mark_send_intent(wid, lease, attempt["attempt_id"])
        else:
            store.mark_send_intent(wid, lease, attempt["attempt_id"])
            action = lambda: store.record_attempt_outcome(
                wid, lease, attempt["attempt_id"], outcome="unknown"
            )
    before = immutable(store)
    old_item = store.get_item(wid)
    result = _wait_for_writer(
        store, action, lambda: clock.__setitem__(0, old_item["lease_expires_at"] + 1)
    )
    assert isinstance(result.get("error"), LeaseFencedError), result
    assert immutable(store) == before
    assert store.get_item(wid) == old_item


@pytest.mark.parametrize("operation", ["checkpoint", "freeze"])
def test_expired_settled_pack_cannot_save_checkpoint_or_context_after_wait(tmp_path, operation):
    store, clock, members, pack, receipt, policy, context = fixture(
        tmp_path, response_status="completed"
    )
    member = members[0]
    wid = member["work_item_id"]
    old_item = store.get_item(wid)
    before = immutable(store)
    if operation == "freeze":
        action = lambda: store.freeze_observation_context_before_send(wid, member["lease"], context)
    else:
        action = lambda: store.save_answer_checkpoint(
            wid,
            member["lease"],
            pack["members"][0]["attempt_id"],
            answer={
                "entity_id": old_item["entity_id"],
                "question_id": old_item["question_id"],
                "status": "scored",
                "score": 6,
                "description": "Synthetic checkpoint",
            },
            execution_receipt=receipt,
            observation_context=context,
        )
    result = _wait_for_writer(
        store, action, lambda: clock.__setitem__(0, old_item["lease_expires_at"] + 1)
    )
    assert isinstance(result.get("error"), LeaseFencedError), result
    assert store.get_answer_checkpoint(wid) is None
    assert immutable(store) == before
    assert store.get_item(wid) == old_item


def test_claim_starts_lease_after_transient_lock_released(tmp_path):
    clock = [1800000000.0]
    store = _store(tmp_path, clock)
    wid = _created(store)["work_item_id"]
    result = _wait_for_writer(
        store,
        lambda: store.claim(wid, lease_seconds=60),
        lambda: clock.__setitem__(0, clock[0] + 150),
    )
    assert "error" not in result, result
    assert result["value"] is not None
    assert store.get_item(wid)["lease_expires_at"] == clock[0] + 60


def test_current_constructor_validates_without_reserving_writer_slot(tmp_path, monkeypatch):
    clock = [1800000000.0]
    store = _store(tmp_path, clock)
    before = store.path.read_bytes()
    statements = []
    original = QuickScanWorkStore._connect

    def connect(self):
        connection = original(self)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(QuickScanWorkStore, "_connect", connect)
    with closing(original(store)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        try:
            QuickScanWorkStore(store.path)
        finally:
            writer.rollback()
    assert not any(sql.upper().startswith("BEGIN IMMEDIATE") for sql in statements)
    assert any(sql.upper() == "BEGIN" for sql in statements)
    assert store.path.read_bytes() == before
    with closing(original(store)) as connection:
        QuickScanWorkStore._initialize(connection)
        assert not connection.in_transaction
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 0
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_current_snapshot_rejects_corruption_and_restores_query_only(tmp_path):
    clock = [1800000000.0]
    store = _store(tmp_path, clock)
    with closing(store._connect()) as connection:
        connection.execute("CREATE TABLE unrecognized(data TEXT)")
        for initial in (0, 1):
            connection.execute(f"PRAGMA query_only={initial}")
            with pytest.raises(ValueError, match="schema"):
                QuickScanWorkStore._initialize(connection)
            assert not connection.in_transaction
            assert connection.execute("PRAGMA query_only").fetchone()[0] == initial


def test_reader_longer_than_old_timeout_does_not_abort_writer_commit(tmp_path):
    clock = [1800000000.0]
    store = _store(tmp_path, clock)
    wid = _created(store)["work_item_id"]
    original = store._connect
    committing = threading.Event()
    outcome = {}

    def connect():
        connection = original()
        if threading.current_thread().name == "commit-writer":
            connection.set_trace_callback(
                lambda sql: committing.set() if sql.upper() == "COMMIT" else None
            )
        return connection

    def claim():
        try:
            outcome["lease"] = store.claim(wid, lease_seconds=60)
        except BaseException as error:
            outcome["error"] = error

    store._connect = connect
    thread = threading.Thread(target=claim, name="commit-writer")
    try:
        with closing(original()) as reader:
            reader.execute("BEGIN")
            reader.execute("SELECT * FROM work_item").fetchall()
            try:
                thread.start()
                assert committing.wait(10)
                time.sleep(6.25)
            finally:
                reader.rollback()
                thread.join(20)
        assert not thread.is_alive()
        assert "error" not in outcome, outcome
        assert outcome["lease"] is not None
        assert store.claim(wid, lease_seconds=60) is None
    finally:
        store._connect = original


def test_permanent_contention_rolls_back_without_attempt_or_fee_then_explicit_retry(tmp_path):
    clock = [1800000000.0]
    store = _store(tmp_path, clock)
    wid = _created(store)["work_item_id"]
    before = immutable(store)
    original = store._connect

    def short_connect():
        connection = original()
        connection.execute("PRAGMA busy_timeout=100")
        return connection

    with closing(original()) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        store._connect = short_connect
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                store.claim(wid, lease_seconds=60)
        finally:
            blocker.rollback()
            store._connect = original
    assert store.get_item(wid)["status"] == "pending"
    assert immutable(store) == before
    assert store.claim(wid, lease_seconds=60) is not None
    assert store.claim(wid, lease_seconds=60) is None


def test_two_legacy_constructors_reread_version_under_writer_lock(tmp_path):
    path = _install_v1_fixture(tmp_path)
    with closing(sqlite3.connect(path)) as con:
        con.row_factory = sqlite3.Row
        oldrows = [dict(r) for r in con.execute("SELECT * FROM work_item")]
        assert con.execute("PRAGMA user_version").fetchone()[0] == 1
    barrier = threading.Barrier(2)
    seen = []
    errors = []

    class FirstRow:
        def __init__(self, cursor, row):
            self.cursor = cursor
            self.row = row

        def fetchone(self):
            return self.row

        def __getattr__(self, key):
            return getattr(self.cursor, key)

    class Proxy:
        def __init__(self, con):
            self.con = con
            self.first = True

        def execute(self, sql, *args):
            cursor = self.con.execute(sql, *args)
            if self.first and sql.upper() == "PRAGMA USER_VERSION":
                self.first = False
                row = cursor.fetchone()
                seen.append(row[0])
                barrier.wait(timeout=10)
                return FirstRow(cursor, row)
            return cursor

        def __getattr__(self, key):
            return getattr(self.con, key)

    class RacingStore(QuickScanWorkStore):
        def _connect(self):
            return Proxy(super()._connect())

    def open_store():
        try:
            RacingStore(path, clock=lambda: 1800000000.0)
        except BaseException as error:
            errors.append(error)

    threads = [
        threading.Thread(target=open_store, name="migration-review-" + str(i)) for i in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert all(not t.is_alive() for t in threads) and errors == [], errors
    assert seen == [1, 1]
    with closing(sqlite3.connect(path)) as con:
        con.row_factory = sqlite3.Row
        assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        rows = [dict(r) for r in con.execute("SELECT * FROM work_item")]
        assert len(rows) == len(oldrows)
        for new, old in zip(rows, oldrows):
            assert {k: new[k] for k in old} == old
        QuickScanWorkStore._validate_schema(con, schema_version=SCHEMA_VERSION)


@pytest.mark.parametrize("query_only", [0, 1])
def test_snapshot_cannot_rollback_an_existing_caller_transaction(tmp_path, query_only):
    store = _store(tmp_path, [1800000000.0])
    with closing(store._connect()) as connection:
        connection.execute("CREATE TEMP TABLE caller_marker(value TEXT)")
        connection.execute("BEGIN")
        connection.execute("INSERT INTO caller_marker VALUES ('uncommitted')")
        connection.execute(f"PRAGMA query_only={query_only}")
        try:
            with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
                QuickScanWorkStore._initialize(connection)
            assert connection.in_transaction
            assert connection.execute("PRAGMA query_only").fetchone()[0] == query_only
            assert (
                connection.execute("SELECT value FROM caller_marker").fetchone()[0] == "uncommitted"
            )
        finally:
            connection.rollback()
            connection.execute("PRAGMA query_only=OFF")
        assert connection.execute("SELECT COUNT(*) FROM caller_marker").fetchone()[0] == 0
