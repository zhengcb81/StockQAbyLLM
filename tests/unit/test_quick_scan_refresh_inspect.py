"""Preview reads the original ledger without creating, upgrading or reserving."""

import hashlib
import sqlite3
from contextlib import closing

import pytest

from src.utils.quick_scan_refresh_inspect import ReadOnlyWorkView, inspect_refresh
from src.utils.quick_scan_work_store import QuickScanWorkStore
from tests.unit.test_quick_scan_budget import _policy
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
