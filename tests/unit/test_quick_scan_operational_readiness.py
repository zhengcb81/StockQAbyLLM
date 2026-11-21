"""Operational opens validate layout; explicit audits own unrelated history."""

import copy
import io
import json
import sqlite3
import sys
from contextlib import closing

import pytest

from src.utils.quick_scan_delivery_control_cli import main as delivery_main
from src.utils.quick_scan_refresh_admission import (
    AdmissionRejected,
    RefreshAdmissionService,
)
from src.utils.quick_scan_refresh_admission_cli import main as admission_main
from src.utils.quick_scan_work_store import (
    BudgetAdmissionError,
    LeaseFencedError,
    QuickScanWorkStore,
    WorkConflictError,
)
from tests.unit.test_quick_scan_budget import _policy, _reserve
from tests.unit.test_quick_scan_work_store import (
    _ack_for_package,
    _c06_package_for_checkpoint,
    _checkpointed_work_item,
    _created,
    _prepared,
)


def _config(tmp_path, store):
    config = {
        key: str(tmp_path)
        for key in (
            "stockwiki_code_root",
            "stockwiki_workspace_root",
            "iqs_code_root",
            "iqs_release_root",
            "execution_inputs_root",
            "runs_dir",
        )
    }
    policy = tmp_path / "budget.json"
    policy.write_text("{}", encoding="utf-8")
    config.update(
        protocol="stockqa.refresh_admission_config/1.0.0",
        consumer_id="offline-readiness-test",
        work_store=str(store.path),
        budget_policy_file=str(policy),
    )
    path = tmp_path / "admission.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _trap_answer_reads(monkeypatch):
    original = sqlite3.connect
    blocked = []

    def connect(*args, **kwargs):
        connection = original(*args, **kwargs)

        def authorize(action, table, _column, _database, _origin):
            if action == sqlite3.SQLITE_READ and table in {
                "answer_checkpoint",
                "quick_scan_attempt_response",
            }:
                blocked.append(table)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    return blocked


@pytest.mark.parametrize("entry", ["constructor", "admission"])
def test_operational_open_does_not_read_historical_answer_payloads(tmp_path, monkeypatch, entry):
    store, _, _, _ = _checkpointed_work_item(tmp_path)
    pending = _created(store, question_id="IQS_06")
    config = _config(tmp_path, store)
    before = store.path.read_bytes()
    blocked = _trap_answer_reads(monkeypatch)
    if entry == "constructor":
        opened = QuickScanWorkStore(store.path, create=False)
    else:
        service = RefreshAdmissionService(config)
        service._ledger_ready()  # Permission/replay readiness uses the same boundary.
        opened = service.store
    assert opened.get_item(pending["work_item_id"])["status"] == "pending"
    assert blocked == []
    assert store.path.read_bytes() == before
    # This is a SQL read trap, not an assertion about a keyword argument. The
    # original explicit audit still reaches historical payload tables.
    with closing(sqlite3.connect(store.path)) as connection:
        connection.row_factory = sqlite3.Row
        with pytest.raises(sqlite3.DatabaseError, match="prohibited|authorized"):
            QuickScanWorkStore.validate_schema(connection)
    assert blocked


@pytest.mark.parametrize("entry", ["constructor", "admission"])
@pytest.mark.parametrize("fault", ["missing_table", "unknown_version", "column_type"])
def test_operational_open_still_rejects_incompatible_layout(tmp_path, entry, fault):
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    config = _config(tmp_path, store)
    with closing(sqlite3.connect(store.path)) as connection, connection:
        if fault == "missing_table":
            connection.execute("DROP TABLE quick_scan_research_visible_output_recovery")
        elif fault == "unknown_version":
            connection.execute("PRAGMA user_version=999")
        else:
            connection.execute("PRAGMA writable_schema=ON")
            connection.execute(
                "UPDATE sqlite_master SET sql=replace(sql, 'identity_revision INTEGER NOT NULL', "
                "'identity_revision TEXT NOT NULL') WHERE name='work_item' AND type='table'"
            )
            connection.execute("PRAGMA writable_schema=OFF")
    before = store.path.read_bytes()
    if entry == "constructor":
        with pytest.raises(ValueError, match="schema"):
            QuickScanWorkStore(store.path, create=False)
    else:
        reason = (
            "refresh_admission_explicit_setup_required"
            if fault == "unknown_version"
            else "refresh_admission_ledger_invalid"
        )
        with pytest.raises(AdmissionRejected) as failure:
            RefreshAdmissionService(config)
        assert failure.value.reason == reason
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("locking", ["busy", "locked"])
def test_admission_sqlite_unavailable_is_not_invalid_and_never_starts_work(
    tmp_path, monkeypatch, capsys, locking
):
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    work = _created(store)
    config = _config(tmp_path, store)
    original = sqlite3.connect
    shared = "?cache=shared" if locking == "locked" else ""
    with closing(original(store.path.as_uri() + shared, uri=True, isolation_level=None)) as locker:
        locker.execute("BEGIN EXCLUSIVE")

        def connect(database, *args, **kwargs):
            kwargs["timeout"] = 0.01
            if locking == "locked":
                database += "&cache=shared"
            return original(database, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", connect)
            with pytest.raises(AdmissionRejected) as failure:
                RefreshAdmissionService(config)
            assert failure.value.reason == "refresh_admission_ledger_unavailable"
            assert isinstance(failure.value.__cause__, sqlite3.OperationalError)
            native_name = getattr(failure.value.__cause__, "sqlite_errorname", None)
            if native_name is not None:  # SQLite exposes this from Python 3.11 onward.
                assert native_name.startswith(
                    "SQLITE_BUSY" if locking == "busy" else "SQLITE_LOCKED"
                )
            assert (
                admission_main(["--config", str(config), "--confirmation-id", "rc2_" + "a" * 64])
                == 2
            )
            result = json.loads(capsys.readouterr().out)
            assert result["reason"] == "refresh_admission_ledger_unavailable"
            assert result["API_requests"] == 0 and result["dispatch_started"] is False
        locker.execute("ROLLBACK")
    assert store.list_attempts(work["work_item_id"]) == []
    assert store.get_item(work["work_item_id"])["status"] == "pending"


def test_admission_open_io_failure_remains_unavailable_without_work(tmp_path, monkeypatch):
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    work = _created(store)
    config = _config(tmp_path, store)
    original = sqlite3.connect
    moved = tmp_path / "temporarily-unavailable.sqlite"

    def vanished_before_open(*args, **kwargs):
        # Reproduce a real SQLite CANTOPEN after the path check, using only this
        # disposable ledger; restore it even if the expected failure changes.
        store.path.rename(moved)
        try:
            return original(*args, **kwargs)
        finally:
            moved.rename(store.path)

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", vanished_before_open)
        with pytest.raises(AdmissionRejected) as failure:
            RefreshAdmissionService(config)
        assert failure.value.reason == "refresh_admission_ledger_unavailable"
        assert isinstance(failure.value.__cause__, sqlite3.OperationalError)
    assert store.list_attempts(work["work_item_id"]) == []
    assert store.get_item(work["work_item_id"])["status"] == "pending"


def test_existing_ledger_open_keeps_current_work_fencing_and_budget_limits(tmp_path):
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    work = _created(store)
    lease = store.claim(work["work_item_id"], lease_seconds=60)
    policy = _policy(max_cost=1, max_cost_per_attempt=1)
    _reserve(store, policy, "existing-reservation")
    reopened = QuickScanWorkStore(store.path, create=False)
    with pytest.raises(WorkConflictError):
        _created(reopened, question_fingerprint="0" * 64)
    stale = type(lease)(lease.lease_token, lease.lease_epoch + 1)
    with pytest.raises(LeaseFencedError):
        _prepared(reopened, work["work_item_id"], stale)
    with pytest.raises(BudgetAdmissionError, match="budget_cost_limit"):
        _reserve(reopened, policy, "over-budget", route_id="route-a2")
    assert reopened.list_attempts(work["work_item_id"]) == []
    assert reopened.get_item(work["work_item_id"])["lease_epoch"] == lease.lease_epoch


def test_delivery_uses_current_binding_and_ack_without_rescanning_answer_payloads(
    tmp_path, monkeypatch, capsys
):
    store, work, checkpoint, _ = _checkpointed_work_item(tmp_path)
    other = _created(store, question_id="IQS_06")
    package = _c06_package_for_checkpoint(checkpoint)
    store.prepare_result_delivery(work["work_item_id"], package)
    ack = _ack_for_package(package)
    blocked = _trap_answer_reads(monkeypatch)

    def call(operation, payload):
        request = {
            "protocol": "stockqa.delivery_control_request/1.0.0",
            "operation": operation,
            "payload": payload,
        }
        monkeypatch.setattr(
            sys,
            "stdin",
            io.TextIOWrapper(io.BytesIO(json.dumps(request).encode("utf-8")), encoding="utf-8"),
        )
        code = delivery_main(["--ledger", str(store.path)])
        result = json.loads(capsys.readouterr().out)
        assert result["API_requests"] == result["network_calls"] == 0
        return code, result

    payload = {
        "work_item_ids": {ack["observation_id"]: other["work_item_id"]},
        "target": ack["consumer"],
        "source_ref": "test:original",
    }
    code, result = call("bind", payload)
    assert code == 2 and result["error_code"] == "original_consumer_binding_rejected"
    payload["work_item_ids"] = {ack["observation_id"]: work["work_item_id"]}
    code, result = call("bind", payload)
    assert code == 0 and result["status"] == "bound"
    bad = copy.deepcopy(ack)
    bad["payload_sha256"] = "f" * 64
    code, result = call(
        "acknowledge", {"work_item_ids": payload["work_item_ids"], "receipt": {"acks": [bad]}}
    )
    assert code == 0 and result["items"][0]["state"] == "ack_rejected"
    assert store.get_result_delivery(work["work_item_id"])["state"] == "ready"
    code, result = call(
        "acknowledge", {"work_item_ids": payload["work_item_ids"], "receipt": {"acks": [ack]}}
    )
    assert code == 0 and result["status"] == "delivered"
    assert blocked == []


def test_old_schema_migration_still_audits_history_and_rolls_back_corruption(tmp_path):
    import re

    from src.utils.quick_scan_research_output import api_research_output_ddl_v27

    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    with closing(sqlite3.connect(store.path)) as connection, connection:
        for statement in reversed(api_research_output_ddl_v27):
            kind, name = re.match(r"CREATE (TABLE|TRIGGER) ([A-Za-z_]+)", statement).groups()
            connection.execute("DROP " + kind + " IF EXISTS " + name)
        connection.execute("PRAGMA user_version=26")
        connection.execute(
            "INSERT INTO quick_scan_observation_context VALUES (?,?,?)", ("e" * 64, "{broken", 1.0)
        )
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="stored context is not valid JSON"):
        QuickScanWorkStore(store.path, create=False)
    assert store.path.read_bytes() == before
    with closing(sqlite3.connect(store.path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 26
