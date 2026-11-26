"""Private lookup of native ledgers captured by StockQA's own feedback producer."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path


def separate_feedback_state(state: Path, ledger: Path) -> None:
    if state.resolve() == ledger.resolve() or (
        state.exists() and ledger.exists() and state.samefile(ledger)
    ):
        raise ValueError("feedback state must differ from work ledger")


def bind_feedback_ledger(state: Path, ledger: Path, rows: list[dict]) -> None:
    location = str(ledger.resolve())
    bindings = {
        (row["evidence_ref"], row["details_ref"], location)
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
        con.execute("""
            CREATE TABLE IF NOT EXISTS observed_ledger_bindings (
                evidence TEXT NOT NULL, attempt TEXT NOT NULL, ledger TEXT NOT NULL,
                run_id TEXT NOT NULL, host_attempt_id TEXT NOT NULL,
                PRIMARY KEY (evidence, attempt, ledger, run_id, host_attempt_id)
            )""")
        observed = {
            (row["evidence_ref"], row["details_ref"], location, row["run_id"], row["attempt_id"])
            for row in rows
            if row.get("details_ref")
            and isinstance(row.get("run_id"), str)
            and isinstance(row.get("attempt_id"), str)
        }
        con.executemany(
            "INSERT OR IGNORE INTO observed_ledger_bindings VALUES (?, ?, ?, ?, ?)",
            sorted(observed),
        )


def feedback_ledger(
    state: Path, evidence: str, attempt: str, *, origin: tuple[str, str] | None = None
) -> Path | None:
    if not state.is_file():
        return None
    try:
        with closing(sqlite3.connect(state.resolve().as_uri() + "?mode=ro", uri=True)) as con:
            key = (evidence, "qa-attempt/1/" + attempt)
            if origin is None:
                rows = con.execute(
                    "SELECT ledger FROM ledger_bindings WHERE evidence=? AND attempt=?", key
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT ledger FROM observed_ledger_bindings WHERE evidence=? AND attempt=? "
                    "AND run_id=? AND host_attempt_id=?",
                    (*key, *origin),
                ).fetchall()
    except (OSError, sqlite3.Error):
        return None
    if len(rows) == 1:
        return Path(rows[0][0])
    return None


def native_observation_retained(
    directory: Path, evidence: str, attempt: str, origin: tuple[str, str]
) -> bool:
    """Read a published observation from the pre-binding native inbox format."""
    try:
        for marker in directory.glob("feedback-*.ready"):
            try:
                ready = json.loads(marker.read_bytes())
                if not isinstance(ready, dict) or (
                    ready.get("owner"),
                    ready.get("run_id"),
                    ready.get("attempt_id"),
                ) != ("stockqa", *origin):
                    continue
                with marker.with_suffix(".jsonl").open("rb") as records:
                    for line in records:
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            continue
                        if (
                            row.get("owner"),
                            row.get("run_id"),
                            row.get("attempt_id"),
                            row.get("step_id"),
                            row.get("evidence_ref"),
                            row.get("details_ref"),
                        ) == (
                            "stockqa",
                            *origin,
                            ready.get("step_id"),
                            evidence,
                            "qa-attempt/1/" + attempt,
                        ):
                            return True
            except (OSError, ValueError, RecursionError):
                continue
    except OSError:
        return False
    return False
