"""Real legacy data backup migration only; not a native owner positive."""

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from src.utils.quick_scan_work_store import QuickScanWorkStore

pytestmark = pytest.mark.skipif(
    not os.environ.get("IQS_CONTINUATION_OWN"),
    reason="opt-in retained schema20 private snapshot acceptance; not ordinary unit fixture",
)


def test_real_legacy_backup_migration_preserves_every_old_row(tmp_path):
    own = Path(os.environ["IQS_CONTINUATION_OWN"])
    project = own.parents[2]
    source = Path(
        os.environ.get(
            "IQS_CONTINUATION_LEGACY_SOURCE",
            str(
                project / "runs/company-agent-owner-2026-10-15-01/execution/quick_scan_work.sqlite"
            ),
        )
    )
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    assert before == "79ae4b84e293a63c762025e326753d78d998e6a5a2d54b5d83245923f03ae85e"
    target = tmp_path / "legacy-copy.sqlite"
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as original:
        original.execute("PRAGMA query_only=ON")
        names = [
            v[0]
            for v in original.execute(
                "select name from sqlite_master where type='table' order by name"
            )
        ]

        def rows(db):
            return {
                table: sorted(list(db.execute('select * from "' + table + '"')), key=repr)
                for table in names
            }

        old = rows(original)
        with closing(sqlite3.connect(target)) as copy:
            original.backup(copy)
    QuickScanWorkStore(target)
    with closing(sqlite3.connect(target)) as migrated:
        assert rows(migrated) == old
        assert migrated.execute("pragma user_version").fetchone()[0] == 24
        assert migrated.execute("pragma integrity_check").fetchone()[0] == "ok"
        assert migrated.execute("pragma foreign_key_check").fetchall() == []
        assert (
            migrated.execute("select count(*) from quick_scan_budget_attempt").fetchone()[0] == 100
        )
        assert migrated.execute("select count(*) from work_item").fetchone()[0] == 0
        new = ("quick_scan_owner_continuation_cell",)
        assert all(
            migrated.execute("select count(*) from " + table).fetchone()[0] == 0 for table in new
        )
        row_hashes = {
            name: hashlib.sha256(
                json.dumps(old[name], ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
            for name in names
        }
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    (own / "evidence/real-migration.json").write_text(
        json.dumps(
            {
                "source_sha256_before": before,
                "source_sha256_after": before,
                "old_table_row_sha256": row_hashes,
                "old_tables": len(names),
                "old_attempts": 100,
                "new_tables_empty": True,
                "native_owner_positive": False,
                "API_requests": 0,
                "source_database_writes": 0,
            },
            indent=2,
        )
        + "\n",
        encoding="utf8",
    )
