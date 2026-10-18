"""Actual temporary native SQLite legacy reads; never migrate an owner DB."""

import sqlite3
from contextlib import closing

import pytest

from src.utils.quick_scan_refresh_inspect import ReadOnlyWorkView
from src.utils.quick_scan_work_store import QuickScanWorkStore
from tests.c06_legacy_fixture import remove_c06_additions
from tests.unit.test_quick_scan_budget import _policy, _reserve


def uncertain(tmp_path):
    store = QuickScanWorkStore(tmp_path / "original.sqlite")
    policy = _policy()
    store.configure_quick_scan_budget(policy)
    _reserve(store, policy, "DISPATCH_legacy_unknown")
    store.record_budget_outcome("DISPATCH_legacy_unknown", outcome="unknown", http_status_code=None)
    return store, policy


def test_declared_v18_read_preserves_all_unknown_fees_without_quota_tables(tmp_path):
    store, policy = uncertain(tmp_path)
    prior = store.get_quick_scan_budget_status(policy["policy_id"])
    with closing(sqlite3.connect(store.path)) as connection:
        remove_c06_additions(connection, after_version=18)
        connection.execute("PRAGMA user_version=18")
        connection.commit()
    before = store.path.read_bytes()
    status = ReadOnlyWorkView(store.path).get_quick_scan_budget_status(policy["policy_id"])
    for field in (
        "reserved_micros",
        "spent_micros",
        "requests",
        "in_flight",
        "unreconciled_attempts",
    ):
        assert status[field] == prior[field]
    assert status["reserved_micros"] > 0 and status["unreconciled_attempts"] == 1
    assert status["blocking_unreconciled_attempts"] == 1
    assert status["quota_accounted_attempts"] == 0 and status["quota_accounts"] == []
    assert status["cash_actual_complete"] is False
    assert store.path.read_bytes() == before
    with pytest.raises(ValueError, match="legacy_schema"):
        ReadOnlyWorkView(store.path).get_quick_scan_budget_status(
            policy["policy_id"], include_isolation=True
        )
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("fault", ["missing_current_quota_table", "forged_v18_header"])
def test_old_capability_branch_does_not_accept_broken_current_schema(tmp_path, fault):
    store, policy = uncertain(tmp_path)
    with closing(sqlite3.connect(store.path)) as connection:
        if fault == "missing_current_quota_table":
            connection.execute("DROP TABLE quick_scan_quota_proof")
        else:
            connection.execute("PRAGMA user_version=18")
        connection.commit()
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="schema"):
        ReadOnlyWorkView(store.path).get_quick_scan_budget_status(policy["policy_id"])
    assert store.path.read_bytes() == before
