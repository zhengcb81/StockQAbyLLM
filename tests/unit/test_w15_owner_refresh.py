"""Owner/executor boundary fixtures; no real identity/financial golden or HTTP."""

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from src.utils.quick_scan_work_store import QuickScanWorkStore, WorkConflictError
from tests.unit.test_quick_scan_work_store import _created, _prepared, _spec


def _binding(**overrides):
    value = dict(
        protocol="stockqa.owner_refresh_binding/1.0.0",
        subject_key="ASUB_TEST:1",
        perimeter_sha256="f" * 64,
        decision_id="route_" + "d" * 64,
        anchor_version=1,
        manifest_raw_sha256="c" * 64,
        provider="P1",
        model="M1",
        information_cutoff="2026-10-08",
        request_identity_key="request-test",
        target_generation=2,
        question_id="IQS_05",
        scope="entity",
        scope_id="ENT_BYD",
        entity_id="ENT_BYD",
        identity_revision=2,
    )
    value.update(overrides)
    return value


def test_owner_binding_is_registered_schema14_and_stable_across_restart(tmp_path):
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    row = store.create_or_attach(
        **_spec(generation=2), run_id="RUN_1", scan_id="SCAN_1", owner_refresh_binding=_binding()
    )
    again = QuickScanWorkStore(store.path).create_or_attach(
        **_spec(generation=2), run_id="RUN_2", scan_id="SCAN_1", owner_refresh_binding=_binding()
    )
    assert row["work_item_id"] == again["work_item_id"]
    assert store.get_owner_refresh_binding(row["work_item_id"])["binding"] == _binding()
    with closing(sqlite3.connect(store.path)) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 14
        assert (
            con.execute("SELECT COUNT(*) FROM quick_scan_owner_refresh_binding").fetchone()[0] == 1
        )
    with pytest.raises(WorkConflictError, match="owner_refresh_binding_conflict"):
        store.create_or_attach(
            **_spec(generation=2),
            run_id="RUN_3",
            scan_id="SCAN_1",
            owner_refresh_binding=_binding(model="M2"),
        )


def test_prior_unknown_is_checked_across_all_generations_without_lookup_limit(tmp_path):
    clock = [1800000000.0]
    store = QuickScanWorkStore(tmp_path / "work.sqlite", clock=lambda: clock[0])
    old = _created(store)
    lease = store.claim(old["work_item_id"], lease_seconds=60)
    attempt = _prepared(store, old["work_item_id"], lease)
    store.mark_send_intent(old["work_item_id"], lease, attempt["attempt_id"])
    clock[0] += 61
    assert store.recover_expired(old["work_item_id"]) == "uncertain"
    with pytest.raises(WorkConflictError, match="owner_refresh_prior_work_unresolved"):
        store.create_or_attach(
            **_spec(generation=2, question_fingerprint="e" * 64),
            run_id="RUN_2",
            scan_id="SCAN_1",
            owner_refresh_binding=_binding(model="M2"),
        )
    with closing(sqlite3.connect(store.path)) as con:
        assert con.execute("SELECT COUNT(*) FROM work_item").fetchone()[0] == 1


def test_two_refresh_workers_share_exact_work_and_only_one_claims(tmp_path):
    path = tmp_path / "work.sqlite"
    QuickScanWorkStore(path)

    def worker(i):
        store = QuickScanWorkStore(path)
        row = store.create_or_attach(
            **_spec(generation=2),
            run_id="RUN_" + str(i),
            scan_id="SCAN_1",
            owner_refresh_binding=_binding(),
        )
        return row["work_item_id"], store.claim(row["work_item_id"], lease_seconds=60)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(worker, (1, 2)))
    assert outcomes[0][0] == outcomes[1][0]
    assert sum(lease is not None for _, lease in outcomes) == 1


def test_late_legacy_work_cannot_undercut_bound_send_before_intent(tmp_path):
    clock = [1800000000.0]
    store = QuickScanWorkStore(tmp_path / "work.sqlite", clock=lambda: clock[0])
    new = store.create_or_attach(
        **_spec(generation=2), run_id="RUN_2", scan_id="SCAN_1", owner_refresh_binding=_binding()
    )
    new_lease = store.claim(new["work_item_id"], lease_seconds=300)
    new_attempt = _prepared(store, new["work_item_id"], new_lease)
    old = _created(store)
    old_lease = store.claim(old["work_item_id"], lease_seconds=60)
    old_attempt = _prepared(store, old["work_item_id"], old_lease)
    with pytest.raises(WorkConflictError, match="owner_refresh_prior_work_unresolved"):
        store.mark_send_intent(old["work_item_id"], old_lease, old_attempt["attempt_id"])
    with pytest.raises(WorkConflictError, match="owner_refresh_prior_work_unresolved"):
        store.mark_send_intent(new["work_item_id"], new_lease, new_attempt["attempt_id"])
    assert store.list_attempts(new["work_item_id"])[0]["phase"] == "prepared"
    assert store.list_attempts(old["work_item_id"])[0]["phase"] == "prepared"


def test_schema13_migration_is_additive_and_keeps_old_item_bytes(tmp_path):
    from src.utils.quick_scan_owner_refresh_journal import DDL_V14_ADDITIONS

    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    old = _created(store)
    with closing(sqlite3.connect(store.path)) as con:
        # Construct the previous registered schema by removing only v14 DDL.
        for ddl in reversed(DDL_V14_ADDITIONS):
            words = ddl.split()
            con.execute("DROP " + words[1] + " " + words[2])
        con.execute("PRAGMA user_version=13")
        con.commit()
    again = QuickScanWorkStore(store.path)
    assert again.get_item(old["work_item_id"]) == old
