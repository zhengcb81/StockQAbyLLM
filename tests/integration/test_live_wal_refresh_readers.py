"""Real SQLite concurrency, isolated temporary ledgers; no model/network calls."""

import sqlite3
from contextlib import closing

import pytest

from src.utils.quick_scan_refresh_admission import (
    AdmissionRejected,
    RefreshAdmissionService,
)
from src.utils.quick_scan_refresh_inspect import ReadOnlyWorkView, inspect_refresh
from tests.unit.test_quick_scan_refresh_inspect import setup


def admission(ledger):
    service = RefreshAdmissionService.__new__(RefreshAdmissionService)
    service.store = ledger
    service._ledger_ready()


def test_actual_wal_allows_inspection_and_admission_without_business_writes(tmp_path):
    ledger, request, item, policy = setup(tmp_path)
    with closing(sqlite3.connect(ledger.path, isolation_level=None)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("UPDATE work_item SET updated_at=1")
        assert ledger.path.with_name(ledger.path.name + "-wal").exists()
        before = writer.execute("SELECT count(*) FROM work_item").fetchone()
        admission(ledger)
        result = inspect_refresh(ledger.path, request, policy=policy)
        assert result["work_items"]["IQS_05"]["work_item_id"] == item["work_item_id"]
        assert result["API_requests"] == 0 and result["quote"]["budget_available"] is True
        assert writer.execute("SELECT count(*) FROM work_item").fetchone() == before


def test_wal_snapshot_excludes_uncommitted_and_later_commits(tmp_path):
    ledger, request, item, policy = setup(tmp_path)
    with closing(sqlite3.connect(ledger.path, isolation_level=None)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE work_item SET updated_at=1")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE work_item SET updated_at=2")
        view = ReadOnlyWorkView(ledger.path)
        with closing(view._connect()) as reader:
            reader.execute("BEGIN")
            assert reader.execute("SELECT updated_at FROM work_item").fetchone()[0] == 1
            writer.commit()
            assert reader.execute("SELECT updated_at FROM work_item").fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError):
                reader.execute("UPDATE work_item SET updated_at=3")
        with closing(view._connect()) as reader:
            assert reader.execute("SELECT updated_at FROM work_item").fetchone()[0] == 2


@pytest.mark.parametrize("reader", ["inspect", "admission"])
def test_exclusive_rollback_writer_still_fails_closed(tmp_path, reader):
    ledger, request, item, policy = setup(tmp_path)
    with closing(sqlite3.connect(ledger.path, isolation_level=None)) as writer:
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.execute("BEGIN EXCLUSIVE")
        writer.execute("UPDATE work_item SET updated_at=2")
        with pytest.raises((sqlite3.Error, ValueError, AdmissionRejected)):
            if reader == "admission":
                admission(ledger)
            else:
                inspect_refresh(ledger.path, request, policy=policy)
        writer.rollback()
    assert inspect_refresh(ledger.path, request, policy=policy)["API_requests"] == 0


@pytest.mark.parametrize("reader", ["inspect", "admission"])
def test_committed_invalid_schema_in_wal_remains_rejected(tmp_path, reader):
    ledger, request, item, policy = setup(tmp_path)
    with closing(sqlite3.connect(ledger.path, isolation_level=None)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA user_version=999")
        with pytest.raises((ValueError, AdmissionRejected)):
            if reader == "admission":
                admission(ledger)
            else:
                inspect_refresh(ledger.path, request, policy=policy)
