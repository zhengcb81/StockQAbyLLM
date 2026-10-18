"""Synthetic native SQLite transactions only; no native consent authority claim."""

import copy
import hashlib
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from src.utils import quick_scan_refresh_admission_journal as journal
from src.utils.quick_scan_result_outbox import canonical_sha256
from src.utils.quick_scan_work_store import QuickScanWorkStore, WorkConflictError


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def prepare(tmp_path, count=2):
    clock = [1000.0]
    store = QuickScanWorkStore(tmp_path / "native.sqlite", clock=lambda: clock[0])
    confirmation = "rc2_" + sha("synthetic action")
    specs = []
    items = []
    for ordinal in range(count):
        qid = "Q" + str(ordinal)
        spec = dict(
            entity_id="ENT_fixture",
            question_id=qid,
            generation=1,
            scope="entity",
            scope_id="ENT_fixture",
            identity_revision=1,
            source_binding_version=1,
            identity_state="verified",
            source_binding_ref="BND_US_FIX",
            source_binding_refs=["BND_US_FIX"],
            identity_snapshot_sha256=sha("identity"),
            question_fingerprint=sha(qid),
            routing_fingerprint=sha("route"),
        )
        spec["owner_refresh_binding"] = dict(
            protocol="stockqa.owner_refresh_binding/1.0.0",
            subject_key="ENT_fixture@1",
            perimeter_sha256=sha("perimeter"),
            decision_id="route_" + sha("decision"),
            anchor_version=1,
            manifest_raw_sha256=sha("manifest"),
            provider="mimo",
            model="mimo-v2.6-pro",
            information_cutoff="2026-10-18",
            request_identity_key="REQ_" + sha(qid),
            target_generation=1,
            question_id=qid,
            scope="entity",
            scope_id="ENT_fixture",
            entity_id="ENT_fixture",
            identity_revision=1,
        )
        row = store.create_or_attach(**spec, run_id="ORIGINAL", scan_id="ORIGINAL")
        specs.append(spec)
        items.append({key: row[key] for key in journal.ITEM_FIELDS})
    action = dict(
        confirmation_id=confirmation,
        confirmation_sha256=sha("confirmation"),
        preview_id="rv1_" + sha("preview"),
        scope_sha256=sha("scope"),
        policy_sha256=sha("policy"),
        execution_inputs_sha256=sha("inputs"),
    )
    value = journal.inputs(action, specs, "ORIGINAL", "ORIGINAL")
    with store._transaction() as db:
        journal.publish(db, value, items, registered_at=store._now())
    return store, clock, confirmation, canonical_sha256(value), items


def invoke(prepared, **overrides):
    store, clock, confirmation, input_sha, items = prepared
    options = dict(
        confirmation_id=confirmation,
        expected_input_sha256=input_sha,
        work_item_ids=[item["work_item_id"] for item in items],
        run_id="BATCH",
        scan_id="BATCH",
        lease_seconds=600,
    )
    options.update(overrides)
    return store.claim_refresh_action_batch(**options)


def rows(store):
    with closing(store._connect()) as db:
        return {
            name: [tuple(r) for r in db.execute("SELECT * FROM " + name + " ORDER BY rowid")]
            for name in (
                "work_item",
                "work_run_ref",
                "work_event",
                "attempt",
                "quick_scan_budget_attempt",
            )
        }


def test_batch_claim_uses_original_clock_epoch_and_distinct_tokens(tmp_path):
    prepared = prepare(tmp_path, 3)
    store = prepared[0]
    result = invoke(prepared)
    assert [r["work_item_id"] for r in result] == [i["work_item_id"] for i in prepared[-1]]
    assert len({r["lease"].lease_token for r in result}) == 3
    assert {r["lease"].lease_epoch for r in result} == {1}
    for item in result:
        actual = store.get_item(item["work_item_id"])
        assert actual["status"] == "leased" and actual["lease_expires_at"] == 1600
    with closing(store._connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM attempt").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM quick_scan_budget_attempt").fetchone()[0] == 0
        assert (
            db.execute("SELECT COUNT(*) FROM work_run_ref WHERE run_id='BATCH'").fetchone()[0] == 3
        )


@pytest.mark.parametrize(
    "bad",
    [
        "empty",
        "duplicate",
        "missing",
        "wrong_action",
        "changed_hash",
        "busy",
        "expired",
        "uncertain",
        "result_ready",
        "superseded",
    ],
)
def test_exact_batch_rejection_is_atomic(tmp_path, bad):
    prepared = prepare(tmp_path)
    store = prepared[0]
    ids = [i["work_item_id"] for i in prepared[-1]]
    options = {}
    if bad == "empty":
        options["work_item_ids"] = []
    elif bad == "duplicate":
        options["work_item_ids"] = [ids[0], ids[0]]
    elif bad == "missing":
        options["work_item_ids"] = [ids[0], "WORK_missing"]
    elif bad == "wrong_action":
        options["confirmation_id"] = "rc2_" + sha("another action")
    elif bad == "changed_hash":
        options["expected_input_sha256"] = sha("caller integrity is not authority")
    elif bad in {"busy", "expired"}:
        store.claim(ids[-1], lease_seconds=1 if bad == "expired" else 600)
        if bad == "expired":
            prepared[1][0] += 2
    elif bad in {"uncertain", "result_ready"}:
        with store._transaction() as db:
            db.execute(
                "UPDATE work_item SET status=?,uncertain_attempt_id=? WHERE work_item_id=?",
                (bad, "ATTEMPT_unknown" if bad == "uncertain" else None, ids[-1]),
            )
    else:
        with closing(store._connect()) as db:
            record = journal.read_record(db, prepared[2])
        spec = copy.deepcopy(record["inputs"]["specifications"][-1])
        spec["generation"] = 2
        spec["owner_refresh_binding"]["target_generation"] = 2
        # The original owner journal rightly blocks a normal new generation
        # while old work is unresolved. This adversarial store corruption is
        # synthetic-only, to prove the batch primitive additionally refuses it.
        with store._transaction() as db:
            old = db.execute("SELECT * FROM work_item WHERE work_item_id=?", (ids[-1],)).fetchone()
            columns = list(old.keys())
            values = [old[k] for k in columns]
            values[columns.index("work_item_id")] = "WORK_" + "f" * 32
            values[columns.index("generation")] = 2
            db.execute(
                "INSERT INTO work_item ("
                + ",".join(columns)
                + ") VALUES ("
                + ",".join("?" for _ in columns)
                + ")",
                values,
            )
    before = rows(store)
    with pytest.raises((ValueError, WorkConflictError)):
        invoke(prepared, **options)
    assert rows(store) == before


def test_last_member_failure_rolls_back_all_claims_and_run_refs(tmp_path, monkeypatch):
    prepared = prepare(tmp_path, 3)
    store = prepared[0]
    before = rows(store)
    original = store._event

    def fail_last(db, work_id, kind, *args, **kwargs):
        if kind == "claimed" and work_id == prepared[-1][-1]["work_item_id"]:
            raise RuntimeError("injected transaction failure")
        return original(db, work_id, kind, *args, **kwargs)

    monkeypatch.setattr(store, "_event", fail_last)
    with pytest.raises(RuntimeError, match="injected"):
        invoke(prepared)
    assert rows(store) == before


def test_concurrent_overlapping_groups_have_only_one_winner(tmp_path):
    prepared = prepare(tmp_path, 3)
    barrier = threading.Barrier(2)

    def race(label):
        independent = QuickScanWorkStore(prepared[0].path, clock=lambda: prepared[1][0])
        own = (independent, *prepared[1:])
        barrier.wait()
        try:
            invoke(own, run_id=label, scan_id=label)
            return "claimed"
        except WorkConflictError:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(race, ["A", "B"]))
    assert sorted(outcomes) == ["claimed", "refused"]
    with closing(prepared[0]._connect()) as db:
        assert (
            db.execute("SELECT COUNT(*) FROM work_event WHERE event_type='claimed'").fetchone()[0]
            == 3
        )


def test_expired_dispatch_deadline_does_not_claim(tmp_path):
    prepared = prepare(tmp_path)
    before = rows(prepared[0])
    with pytest.raises(WorkConflictError, match="deadline"):
        invoke(prepared, dispatch_deadline=time.monotonic() - 1)
    assert rows(prepared[0]) == before


def test_original_singleton_default_is_unchanged(tmp_path):
    prepared = prepare(tmp_path)
    store = prepared[0]
    work = prepared[-1][0]["work_item_id"]
    assert store.claim(work, lease_seconds=600) is not None
    assert store.claim(work, lease_seconds=600) is None
    assert store.get_item(prepared[-1][1]["work_item_id"])["status"] == "pending"
