"""Execute fixed recovery/budget queries against disposable SQLite data.

These minimal query fixtures do not grant owner authority or perform dispatch.
Original Store/lifecycle integration is covered by the existing native suites.
"""

import sqlite3
from contextlib import closing

import pytest

from src.utils.quick_scan_known_outcome_journal import terminal_received_failure
from src.utils.quick_scan_work_store import QuickScanWorkStore


@pytest.mark.parametrize(
    "scope, expected",
    [
        ([], 0),
        (["unknown"], 0),
        (["work'quoted-汉字"], 1),
        (["WPACK1", "WPACK2"], 1),
        (["work'quoted-汉字", "WPACK1", "WPACK2", "WPACK1"], 2),
        (["x') OR 1=1 --"], 0),
        ([f"absent-{i}" for i in range(1200)] + ["work'quoted-汉字", "WPACK1"], 2),
    ],
)
def test_company_scope_counts_physical_requests_with_bound_ids(scope, expected):
    with closing(sqlite3.connect(":memory:")) as con:
        con.executescript("""
            CREATE TABLE attempt(attempt_id TEXT, work_item_id TEXT);
            CREATE TABLE quick_scan_budget_attempt(
                budget_attempt_id TEXT, work_attempt_id TEXT, request_counted INTEGER);
            CREATE TABLE quick_scan_question_pack_send(pack_id TEXT, budget_attempt_id TEXT);
            CREATE TABLE quick_scan_question_pack_member(pack_id TEXT, work_item_id TEXT);
        """)
        con.executemany(
            "INSERT INTO attempt VALUES (?,?)",
            [("ordinary", "work'quoted-汉字"), ("member", "WPACK1"), ("other", "WOTHER")],
        )
        con.executemany(
            "INSERT INTO quick_scan_budget_attempt VALUES (?,?,?)",
            [
                ("physical-ordinary", "ordinary", 1),
                ("physical-pack", "member", 1),
                ("not-counted", "ordinary", 0),
                ("physical-other", "other", 1),
            ],
        )
        con.execute(
            "INSERT INTO quick_scan_question_pack_send VALUES (?,?)", ("pack", "physical-pack")
        )
        con.executemany(
            "INSERT INTO quick_scan_question_pack_member VALUES (?,?)",
            [("pack", "WPACK1"), ("pack", "WPACK2")],
        )
        assert QuickScanWorkStore._company_request_count_tx(con, scope) == expected
        assert con.execute("SELECT COUNT(*) FROM attempt").fetchone()[0] == 3


@pytest.mark.parametrize(
    "existing, expected",
    [(None, True), ("checkpoint", False), ("delivery", False), ("other_work", True)],
)
def test_terminal_received_failure_checks_both_saved_targets_without_table_interpolation(
    existing, expected
):
    wid = "work'quoted-汉字"
    item = {
        "work_item_id": wid,
        "status": "failed",
        "lease_token": None,
        "lease_expires_at": None,
        "uncertain_attempt_id": None,
        "lease_epoch": 4,
    }
    attempt = {"attempt_id": "original-attempt", "phase": "response_available", "lease_epoch": 4}
    with closing(sqlite3.connect(":memory:")) as con:
        con.row_factory = sqlite3.Row
        con.executescript("""
            CREATE TABLE answer_checkpoint(work_item_id TEXT);
            CREATE TABLE quick_scan_result_delivery(work_item_id TEXT);
            CREATE TABLE work_event(event_id INTEGER, work_item_id TEXT, event_type TEXT,
                attempt_id TEXT, lease_epoch INTEGER, from_status TEXT, to_status TEXT);
        """)
        con.execute(
            "INSERT INTO work_event VALUES (?,?,?,?,?,?,?)",
            (
                1,
                wid,
                "received_without_answer:invalid_question",
                "original-attempt",
                4,
                "leased",
                "failed",
            ),
        )
        if existing == "checkpoint":
            con.execute("INSERT INTO answer_checkpoint VALUES (?)", (wid,))
        elif existing == "delivery":
            con.execute("INSERT INTO quick_scan_result_delivery VALUES (?)", (wid,))
        elif existing == "other_work":
            con.execute("INSERT INTO answer_checkpoint VALUES (?)", ("another-work",))
            con.execute("INSERT INTO quick_scan_result_delivery VALUES (?)", ("another-work",))
        assert (
            terminal_received_failure(con, item, attempt, "answer_validation_rejected") is expected
        )
        assert con.execute("SELECT COUNT(*) FROM work_event").fetchone()[0] == 1
