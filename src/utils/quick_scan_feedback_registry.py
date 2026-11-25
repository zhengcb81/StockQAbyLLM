"""Private lookup of native ledgers captured by StockQA's own feedback producer."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path


def separate_feedback_state(state: Path, ledger: Path) -> None:
    if state.resolve() == ledger.resolve() or (
        state.exists() and ledger.exists() and state.samefile(ledger)
    ):
        raise ValueError("feedback state must differ from work ledger")


def bind_feedback_ledger(state: Path, ledger: Path, rows: list[dict]) -> None:
    bindings = {
        (row["evidence_ref"], row["details_ref"], str(ledger.resolve()))
        for row in rows
        if row.get("details_ref")
    }
    if not bindings:
        return
    separate_feedback_state(state, ledger)
    state.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(state, timeout=5)) as con, con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS ledger_bindings (
                evidence TEXT NOT NULL, attempt TEXT NOT NULL, ledger TEXT NOT NULL,
                PRIMARY KEY (evidence, attempt, ledger)
            )""")
        con.executemany("INSERT OR IGNORE INTO ledger_bindings VALUES (?, ?, ?)", sorted(bindings))


def feedback_ledger(state: Path, evidence: str, attempt: str) -> Path | None:
    if not state.is_file():
        return None
    with closing(sqlite3.connect(state.resolve().as_uri() + "?mode=ro", uri=True)) as con:
        if (
            con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger_bindings'"
            ).fetchone()
            is None
        ):
            return None
        rows = con.execute(
            "SELECT ledger FROM ledger_bindings WHERE evidence = ? AND attempt = ?",
            (evidence, "qa-attempt/1/" + attempt),
        ).fetchall()
    if len(rows) == 1:
        return Path(rows[0][0])
    return None
