"""Preview reads the original ledger without creating, upgrading or reserving."""

import hashlib
import sqlite3
from contextlib import closing

import pytest

from src.utils.quick_scan_refresh_inspect import ReadOnlyWorkView, inspect_refresh
from src.utils.quick_scan_work_store import QuickScanWorkStore
from tests.unit.test_quick_scan_budget import _policy, _reserve
from tests.unit.test_quick_scan_work_store import _created


def setup(tmp_path):
    ledger = QuickScanWorkStore(tmp_path / "ledger.sqlite")
    policy = _policy()
    normalized = ledger.configure_quick_scan_budget(policy)
    item = _created(ledger)
    route = policy["routes"][0]
    request = dict(
        entity_id="ENT_BYD",
        question_ids=["IQS_05"],
        scope_bindings={"IQS_05": dict(scope="entity", scope_id="ENT_BYD")},
        policy_id=normalized["policy_id"],
        route_id=route["id"],
        provider_config_ref=route["provider_config_ref"],
        model=route["model"],
    )
    return ledger, request, item, policy


def test_inspection_uses_original_pending_work_and_durable_budget_without_writes(tmp_path):
    ledger, request, item, policy = setup(tmp_path)
    before = ledger.path.read_bytes()
    result = inspect_refresh(ledger.path, request, policy=policy)
    assert result["work_items"]["IQS_05"]["work_item_id"] == item["work_item_id"]
    assert result["work_items"]["IQS_05"]["status"] == "pending"
    assert result["quote"]["max_http_requests"] > 0
    assert result["quote"]["budget_available"] is True
    assert result["API_requests"] == 0
    assert ledger.path.read_bytes() == before


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "unknown-schema",
        "schema-tamper",
        "wrong-model",
        "wrong-scope",
        "policy-change",
        "unknown-policy",
        "unexpected-key",
    ],
)
def test_unknown_or_changed_state_is_refused_without_initialization(tmp_path, fault):
    ledger, request, item, policy = setup(tmp_path)
    if fault == "missing":
        ledger.path.unlink()
    elif fault in {"unknown-schema", "schema-tamper"}:
        with closing(sqlite3.connect(ledger.path)) as con:
            if fault == "unknown-schema":
                con.execute("PRAGMA user_version=999")
            else:
                con.execute("DROP TRIGGER answer_checkpoint_no_update")
            con.commit()
    elif fault == "wrong-model":
        request["model"] = "other-model"
    elif fault == "wrong-scope":
        request["scope_bindings"]["IQS_05"]["scope_id"] = "ENT_OTHER"
    elif fault == "policy-change":
        policy = {**policy, "policy_version": "changed-after-registration"}
    elif fault == "unknown-policy":
        request["policy_id"] = "unknown"
    else:
        request["confirmed"] = True
    before = ledger.path.read_bytes() if ledger.path.exists() else None
    with pytest.raises((ValueError, KeyError)):
        inspect_refresh(ledger.path, request, policy=policy)
    assert (ledger.path.read_bytes() if ledger.path.exists() else None) == before


def test_read_only_connection_refuses_actual_sql_update(tmp_path):
    ledger, request, item, policy = setup(tmp_path)
    view = ReadOnlyWorkView(ledger.path)
    with closing(view._connect()) as con:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("DELETE FROM work_item")


def test_readonly_inspection_does_not_rescan_unrelated_record_tables(tmp_path, monkeypatch):
    ledger, request, item, policy = setup(tmp_path)
    statements = []
    original = sqlite3.connect

    def traced(*args, **kwargs):
        connection = original(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced)
    before = ledger.path.read_bytes()
    result = inspect_refresh(ledger.path, request, policy=policy)
    assert result["work_items"]["IQS_05"]["work_item_id"] == item["work_item_id"]
    normalized = [" ".join(sql.upper().split()) for sql in statements]
    assert not any(
        "PRAGMA QUICK_CHECK" in sql or "PRAGMA FOREIGN_KEY_CHECK" in sql for sql in normalized
    )
    assert not any(
        "FROM QUICK_SCAN_RESULT_DELIVERY" in sql
        or "FROM QUICK_SCAN_STANDARD_ANSWER" in sql
        or "FROM QUICK_SCAN_OBSERVATION_CONTEXT" in sql
        for sql in normalized
    )
    assert any("SQLITE_MASTER" in sql for sql in normalized)
    assert any("QUICK_SCAN_BUDGET_POLICY WHERE POLICY_ID" in sql for sql in normalized)
    assert ledger.path.read_bytes() == before


def test_unrelated_bad_history_is_for_deep_audit_not_every_preview(tmp_path):
    ledger, request, item, policy = setup(tmp_path)
    # Only this disposable database is corrupted. SQL layout is untouched and
    # the unrelated orphan context is not used by the requested pending item.
    with closing(sqlite3.connect(ledger.path)) as connection:
        connection.execute(
            "INSERT INTO quick_scan_observation_context VALUES (?,?,?)", ("e" * 64, "{broken", 1.0)
        )
        connection.commit()
    before = ledger.path.read_bytes()
    result = inspect_refresh(ledger.path, request, policy=policy)
    assert result["work_items"]["IQS_05"]["work_item_id"] == item["work_item_id"]
    # The default writer/explicit audit entry retains the complete historical
    # record validation and still reports the corruption.
    with closing(sqlite3.connect(ledger.path)) as connection:
        connection.row_factory = sqlite3.Row
        with pytest.raises(ValueError, match="stored context is not valid JSON"):
            QuickScanWorkStore._validate_schema(connection)
    assert ledger.path.read_bytes() == before


def test_preview_reloads_current_reservation_instead_of_caching_budget(tmp_path):
    ledger, request, item, policy = setup(tmp_path)
    before = inspect_refresh(ledger.path, request, policy=policy)
    _reserve(ledger, policy, "fixture-reservation-after-first-preview")
    recorded = ledger.path.read_bytes()
    after = inspect_refresh(ledger.path, request, policy=policy)
    assert after["quote"]["max_cost_micros"] < before["quote"]["max_cost_micros"]
    assert (
        before["quote"]["budget_available"] is True and after["quote"]["budget_available"] is False
    )
    assert after["quote"]["budget_state_sha256"] != before["quote"]["budget_state_sha256"]
    assert ledger.path.read_bytes() == recorded
