"""Original Q13 ledger batch/restart tests; synthetic authority callbacks.

No callback fixture here proves native user consent or public admission.
Only the original work/run-ref/owner-binding tables are used.
"""

import copy
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from src.utils.quick_scan_refresh_batch import attach_owner_refresh_batch
from src.utils.quick_scan_work_store import QuickScanWorkStore
from tests.unit.test_quick_scan_work_store import _spec
from tests.unit.test_w15_owner_refresh import _binding


def specs():
    first = dict(
        _spec(generation=2),
        owner_refresh_binding=_binding(
            protocol="stockqa.owner_refresh_binding/2.0.0",
            subject_context_schema="stockqa.quick_scan_observation_context/2.0.0",
        ),
    )
    second = copy.deepcopy(first)
    second["question_id"] = "IQS_06"
    second["owner_refresh_binding"]["question_id"] = "IQS_06"
    return [first, second]


def counts(path):
    with closing(sqlite3.connect(path)) as con:
        return {
            table: con.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            for table in (
                "work_item",
                "work_run_ref",
                "quick_scan_owner_refresh_binding",
                "attempt",
                "quick_scan_budget_attempt",
            )
        }


def test_original_q13_rows_commit_together_and_replay_after_restart(tmp_path):
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    before = counts(store.path)
    checks = []
    result = attach_owner_refresh_batch(
        store,
        specs(),
        run_id="RUN_CONFIRM_SYNTHETIC",
        scan_id="SCAN_SYNTHETIC",
        recheck=lambda: checks.append("synthetic-check"),
    )
    assert len(checks) == 2 and result["dispatch_started"] is False and result["API_requests"] == 0
    assert result["permission_authenticated"] is False
    assert counts(store.path) == dict(
        before, work_item=2, work_run_ref=2, quick_scan_owner_refresh_binding=2
    )
    assert all(store.get_item(i["work_item_id"])["status"] == "pending" for i in result["items"])
    again = attach_owner_refresh_batch(
        QuickScanWorkStore(store.path),
        specs(),
        run_id="RUN_CONFIRM_SYNTHETIC",
        scan_id="SCAN_SYNTHETIC",
        recheck=lambda: None,
    )
    assert again["items"] == result["items"] and counts(store.path) == dict(
        before, work_item=2, work_run_ref=2, quick_scan_owner_refresh_binding=2
    )


@pytest.mark.parametrize("fault", ["second_item", "post_write_check"])
def test_partial_write_or_final_permission_fault_rolls_back_all_original_tables(tmp_path, fault):
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    before = counts(store.path)
    rows = specs()
    checks = []
    if fault == "second_item":
        rows[1]["routing_fingerprint"] = "invalid-hash"

    def recheck():
        checks.append(True)
        if fault == "post_write_check" and len(checks) == 2:
            raise ValueError("synthetic owner changed")

    with pytest.raises(ValueError):
        attach_owner_refresh_batch(store, rows, run_id="RUN_1", scan_id="SCAN_1", recheck=recheck)
    assert counts(store.path) == before


def test_duplicate_question_scope_is_rejected_before_authorization_or_write(tmp_path):
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    before = counts(store.path)
    rows = specs()
    rows[1] = copy.deepcopy(rows[0])
    checks = []
    with pytest.raises(ValueError, match="duplicate"):
        attach_owner_refresh_batch(
            store, rows, run_id="RUN_1", scan_id="SCAN_1", recheck=lambda: checks.append(True)
        )
    assert not checks and counts(store.path) == before


def test_legacy_binding_cannot_be_admitted_as_current_subject_work(tmp_path):
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    before = counts(store.path)
    rows = specs()
    rows[0]["owner_refresh_binding"] = _binding()
    with pytest.raises(ValueError, match="subject_context"):
        attach_owner_refresh_batch(
            store, rows, run_id="RUN_1", scan_id="SCAN_1", recheck=lambda: None
        )
    assert counts(store.path) == before


def test_two_workers_share_original_logical_work_without_second_queue(tmp_path):
    path = tmp_path / "work.sqlite"
    QuickScanWorkStore(path)

    def worker(i):
        return attach_owner_refresh_batch(
            QuickScanWorkStore(path),
            specs(),
            run_id="RUN_CONFIRM_SYNTHETIC",
            scan_id="SCAN_SYNTHETIC",
            recheck=lambda: None,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        out = list(pool.map(worker, [1, 2]))
    assert out[0]["items"] == out[1]["items"] and counts(path)["work_item"] == 2
