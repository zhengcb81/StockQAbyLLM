"""Q06: public question path bound to real work items (claim/lease lifecycle).

Binds Q06 cases JOB-01/02/PAR-12/JOB-10/11 (invariants I02/I12/I54): atomic
create-or-attach + claim per question, issuer-level dedup with listing-level
distinction, lease-expiry recovery with late-worker fencing, and the
``--identity-snapshot`` production activation path (owner option 1).

RED phase: the work-item lifecycle does not exist yet.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.core.models import Answer, Question
from src.core.qa_engine import QAEngine
from src.utils.quick_scan_work_store import QuickScanWorkStore

IDENTITY = {
    "identity_revision": 1,
    "source_binding_version": 1,
    "identity_state": "provisional",
    "source_binding_ref": "BND_TEST_1",
    "source_binding_refs": ["BND_TEST_1"],
    "identity_snapshot_sha256": "a" * 64,
}


class FakeProvider:
    def get_provider_name(self) -> str:
        return "fake"

    def search_question(self, question):
        return []


class FakeGenerator:
    def __init__(self) -> None:
        self.calls = 0

    def generate_answer(self, question, search_results):
        self.calls += 1
        return Answer(text=f"ans {self.calls}", score=8, status="scored", source="fake")


def _q(qid: str, text: str) -> Question:
    return Question(text=text, question_id=qid)


def _store(tmp_path: Path) -> QuickScanWorkStore:
    return QuickScanWorkStore(tmp_path / "work.sqlite")


def _lifecycle(
    store: QuickScanWorkStore,
    tmp_path: Path,
    *,
    run_id: str = "RUN_1",
    entity_id: str = "ENT_test",
    generation: int = 1,
    lease_seconds: float = 600.0,
):
    from src.runners.llm_runner import QuickScanWorkLifecycle

    return QuickScanWorkLifecycle(
        store,
        entity_id=entity_id,
        run_id=run_id,
        scan_id="SCAN_1",
        identity=dict(IDENTITY),
        generation=generation,
        lease_seconds=lease_seconds,
    )


def _engine(
    store: QuickScanWorkStore,
    tmp_path: Path,
    *,
    run_id: str = "RUN_1",
    entity_id: str = "ENT_test",
    with_lifecycle: bool = True,
):
    lifecycle = None
    if with_lifecycle:
        lifecycle = _lifecycle(store, tmp_path, run_id=run_id, entity_id=entity_id)
    return QAEngine(FakeProvider(), FakeGenerator(), work_item_lifecycle=lifecycle)


def _db_counts(db_path) -> dict:
    con = sqlite3.connect(db_path)
    try:
        out = {
            "work_items": con.execute("SELECT COUNT(*) FROM work_item").fetchone()[0],
            "statuses": dict(
                con.execute("SELECT status, COUNT(*) FROM work_item GROUP BY status").fetchall()
            ),
        }
        return out
    finally:
        con.close()


def test_r1_default_engine_has_no_lifecycle(tmp_path: Path) -> None:
    """R1: no lifecycle injected — engine works and no work-item machinery
    is required (probe-style runs stay byte-identical in behavior)."""
    store = _store(tmp_path)
    engine = QAEngine(FakeProvider(), FakeGenerator())
    assert engine.work_item_lifecycle is None
    batch = engine.process_questions([_q("IQS_01", "问题一"), _q("IQS_02", "问题二")])
    assert batch.processed_count == 2
    counts = _db_counts(store.path)
    assert counts["work_items"] == 0


def test_r2_claim_dispatch_terminal_then_idempotent_replay(tmp_path: Path) -> None:
    """R2: claim per question, dispatch, terminal outcome; a replay run
    attaches to the SAME work items and is refused re-claim (not pending)."""
    store = _store(tmp_path)
    engine = _engine(store, tmp_path)
    batch = engine.process_questions([_q("IQS_01", "问题一")])
    assert batch.processed_count == 1
    counts = _db_counts(store.path)
    assert counts["work_items"] == 1

    # replay: same identity + run → same work item, claim refused (terminal)
    engine2 = _engine(store, tmp_path)
    batch2 = engine2.process_questions([_q("IQS_01", "问题一")])
    assert batch2.processed_count == 1
    result2 = batch2.results[0]
    assert result2.answer.status == "error"
    assert result2.metadata.get("work_claim_reason") == "work_item_not_pending"
    counts2 = _db_counts(store.path)
    assert counts2 == counts


def test_r3_concurrent_double_claim_second_is_refused(tmp_path: Path) -> None:
    """R3 (r1 P1-1): REAL concurrency — two runs, same identity, dispatched
    simultaneously; exactly one claims, the other is refused without
    dispatching (work item not pending / fenced race)."""
    import threading

    store = _store(tmp_path)
    lifecycle_a = _lifecycle(store, tmp_path, run_id="RUN_A")
    lifecycle_b = _lifecycle(store, tmp_path, run_id="RUN_B")
    question = _q("IQS_01", "问题一")
    results: dict = {}
    barrier = threading.Barrier(2)

    def contender(name: str, lc) -> None:
        barrier.wait()
        results[name] = lc.before_question(question)

    t_a = threading.Thread(target=contender, args=("a", lifecycle_a))
    t_b = threading.Thread(target=contender, args=("b", lifecycle_b))
    t_a.start()
    t_b.start()
    t_a.join()
    t_b.join()

    assert set(results) == {"a", "b"}
    claimed = [r for r in results.values() if r.get("claimed")]
    refused = [r for r in results.values() if not r.get("claimed")]
    assert len(claimed) == 1 and len(refused) == 1
    assert refused[0]["work_item_id"] == claimed[0]["work_item_id"]


def test_r4_lease_expiry_recovery_and_late_worker_fenced(tmp_path: Path) -> None:
    """R4: the two honest recovery paths per C04 —
    (a) expired WITHOUT send intent (never dispatched) -> recover to pending,
        a fresh worker re-claims;
    (b) expired AFTER send intent (dispatched, Q06 lifecycle path) -> recover
        to UNCERTAIN (never pending: a possibly-delivered request must not be
        re-dispatched), claim refuses, and the stale worker's late write is
        fenced out (JOB-10: no duplicate LLM calls)."""
    clock = {"now": 1000.0}
    store = QuickScanWorkStore(tmp_path / "work.sqlite", clock=lambda: clock["now"])

    # (a) never-sent path: claim then let the lease lapse
    row = store.create_or_attach(
        entity_id=_REAL_UUID_ENTITY,
        question_id="IQS_01",
        generation=1,
        scope="entity",
        scope_id=_REAL_UUID_ENTITY,
        run_id="RUN_A",
        scan_id="SCAN_1",
        question_fingerprint="b" * 64,
        routing_fingerprint="c" * 64,
        **dict(IDENTITY),
    )
    lease1 = store.claim(row["work_item_id"], lease_seconds=10.0)
    assert lease1 is not None
    clock["now"] += 11.0
    assert store.recover_expired(row["work_item_id"]) in {"pending", "recovered"}
    lease2 = store.claim(row["work_item_id"], lease_seconds=600.0)
    assert lease2 is not None

    # (b) dispatched path via the lifecycle (prepare + send intent marked)
    lifecycle = _lifecycle(store, tmp_path, entity_id=_REAL_UUID_ENTITY, lease_seconds=10.0)
    question = _q("IQS_02", "问题二")
    handle = lifecycle.before_question(question)
    assert handle["claimed"] is True
    clock["now"] += 11.0
    state = store.recover_expired(handle["work_item_id"])
    assert state == "uncertain", state  # dispatched work never goes back to pending
    again = lifecycle.before_question(question)
    assert again["claimed"] is False  # no re-dispatch of possibly-delivered work
    with pytest.raises(Exception):
        store.record_attempt_outcome(
            handle["work_item_id"],
            handle["lease"],
            handle["attempt_id"],
            outcome="unknown",
        )


def test_r5_issuer_dedup_and_listing_distinction(tmp_path: Path) -> None:
    """R5: issuer-level (entity scope) work dedups to one item per logical
    key; listing-level (security scope) work stays distinct per listing."""
    store = _store(tmp_path)
    identity = dict(IDENTITY)
    row_a = store.create_or_attach(
        entity_id="ENT_issuer",
        question_id="IQS_01",
        generation=1,
        scope="entity",
        scope_id="ENT_issuer",
        run_id="RUN_1",
        scan_id="SCAN_1",
        question_fingerprint="b" * 64,
        routing_fingerprint="c" * 64,
        **identity,
    )
    row_b = store.create_or_attach(
        entity_id="ENT_issuer",
        question_id="IQS_01",
        generation=1,
        scope="entity",
        scope_id="ENT_issuer",
        run_id="RUN_2",
        scan_id="SCAN_1",
        question_fingerprint="b" * 64,
        routing_fingerprint="c" * 64,
        **identity,
    )
    assert row_a["work_item_id"] == row_b["work_item_id"]
    sec_a = store.create_or_attach(
        entity_id="ENT_issuer",
        question_id="IQS_01",
        generation=1,
        scope="security",
        scope_id="SEC_aaa",
        run_id="RUN_1",
        scan_id="SCAN_1",
        question_fingerprint="b" * 64,
        routing_fingerprint="c" * 64,
        **identity,
    )
    sec_b = store.create_or_attach(
        entity_id="ENT_issuer",
        question_id="IQS_01",
        generation=1,
        scope="security",
        scope_id="SEC_bbb",
        run_id="RUN_1",
        scan_id="SCAN_1",
        question_fingerprint="b" * 64,
        routing_fingerprint="c" * 64,
        **identity,
    )
    assert sec_a["work_item_id"] != sec_b["work_item_id"]
    assert sec_a["work_item_id"] != row_a["work_item_id"]


def test_r6_identity_snapshot_payload_maps_to_work_item(tmp_path: Path) -> None:
    """R6 (option 1 production activation): an identity snapshot payload
    (as extracted from a W04 export) maps through the lifecycle into the
    work item's frozen identity fields."""
    store = _store(tmp_path)
    lifecycle = _lifecycle(store, tmp_path)
    question = _q("IQS_01", "问题一")
    handle = lifecycle.before_question(question)
    assert handle["claimed"] is True
    con = sqlite3.connect(store.path)
    try:
        row = con.execute(
            "SELECT identity_revision, source_binding_version, identity_state,"
            " source_binding_ref, identity_snapshot_sha256 FROM work_item"
        ).fetchone()
        assert row[0] == IDENTITY["identity_revision"]
        assert row[1] == IDENTITY["source_binding_version"]
        assert row[2] == IDENTITY["identity_state"]
        assert row[3] == IDENTITY["source_binding_ref"]
        assert row[4] == IDENTITY["identity_snapshot_sha256"]
    finally:
        con.close()


# --- r1 P0 fix batch RED tests (2026-10-05) ---

_REAL_UUID_ENTITY = "ENT_1b2a4d3e-0000-4a1b-8c2d-000000000001"


def test_p0_1_routing_fingerprint_derives_from_question_texts() -> None:
    """P0-1: the routing fingerprint derives from question TEXTS (64-hex);
    json.dumps(List[Question]) raised TypeError and the production path
    crashed before the first question."""
    from src.runners.llm_runner import routing_fingerprint_for  # RED: missing fn

    fp = routing_fingerprint_for([_q("IQS_01", "问题一"), _q("IQS_02", "问题二")])
    assert isinstance(fp, str) and len(fp) == 64
    assert all(c in "0123456789abcdef" for c in fp)


def test_p0_2_store_accepts_real_uuid_entity(tmp_path: Path) -> None:
    """P0-2b (owner-signed 2026-10-05): the W04 export's real ENT_<uuid>
    entity id must be accepted by the work store."""
    store = _store(tmp_path)
    row = store.create_or_attach(
        entity_id=_REAL_UUID_ENTITY,
        question_id="IQS_01",
        generation=1,
        scope="entity",
        scope_id=_REAL_UUID_ENTITY,
        run_id="RUN_1",
        scan_id="SCAN_1",
        question_fingerprint="b" * 64,
        routing_fingerprint="c" * 64,
        **dict(IDENTITY),
    )
    assert row["work_item_id"].startswith("WORK_")


def test_p0_2_default_lifecycle_claims_uuid_entity(tmp_path: Path) -> None:
    """P0-2: with default (64-hex) fingerprint and a real uuid entity the
    lifecycle claims instead of 100%-refusing."""
    store = _store(tmp_path)
    lifecycle = _lifecycle(store, tmp_path, entity_id=_REAL_UUID_ENTITY)
    handle = lifecycle.before_question(_q("IQS_01", "问题一"))
    assert handle["claimed"] is True


def test_p0_3_answered_work_is_terminal_and_never_redispatched(tmp_path: Path) -> None:
    """P0-3: after a recorded answer the work item leaves leased (attempt
    outcome recorded, not stuck at prepared) so lease expiry cannot recover
    it into a re-dispatch (JOB-10 / I12: no duplicate LLM calls)."""
    store = _store(tmp_path)
    lifecycle = _lifecycle(store, tmp_path, entity_id=_REAL_UUID_ENTITY)
    question = _q("IQS_01", "问题一")
    handle = lifecycle.before_question(question)
    assert handle["claimed"] is True
    lifecycle.after_question(handle, None)

    con = sqlite3.connect(store.path)
    try:
        status = con.execute("SELECT status FROM work_item").fetchone()[0]
        phase = con.execute("SELECT phase FROM attempt").fetchone()[0]
    finally:
        con.close()
    assert status not in {"pending", "leased"}, status
    assert phase in {"response_available", "uncertain", "confirmed_failure"}, phase

    again = lifecycle.before_question(question)
    assert again["claimed"] is False
    assert again["reason"] == "work_item_not_pending"


def test_loader_golden_shape_and_entity_crosscheck(tmp_path: Path) -> None:
    """P0-1/P1-2: the loader is covered — golden shape maps to identity
    fields with the exact file-bytes sha, wrong --entity-id cross-check and
    unsupported versions fail fast."""
    import hashlib
    import json as _json

    from src.runners.llm_runner import load_identity_snapshot

    package = {
        "object_type": "entity",
        "schema_version": "2.2.0",
        "payload": {
            "entity_id": _REAL_UUID_ENTITY,
            "identity_revision": 1,
            "identity_state": "verified",
            "listings": [
                {"source_binding_ref": "BND_fixture_1", "ticker": "ACME"},
                {"source_binding_ref": "BND_fixture_2", "ticker": "GOOG"},
            ],
        },
    }
    snapshot = tmp_path / "snapshot.json"
    raw = _json.dumps(package, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    snapshot.write_bytes(raw)

    payload = load_identity_snapshot(str(snapshot), expected_entity_id=_REAL_UUID_ENTITY)
    assert payload["entity_id"] == _REAL_UUID_ENTITY
    assert payload["identity_revision"] == 1
    assert payload["identity_state"] == "verified"
    assert payload["source_binding_refs"] == ["BND_fixture_1", "BND_fixture_2"]
    assert payload["source_binding_ref"] == "BND_fixture_1"
    assert payload["identity_snapshot_sha256"] == hashlib.sha256(raw).hexdigest()

    with pytest.raises(ValueError):
        load_identity_snapshot(str(snapshot), expected_entity_id="ENT_other")

    bad = tmp_path / "bad.json"
    bad.write_bytes(_json.dumps({**package, "schema_version": "1.0.0"}).encode("utf-8"))
    with pytest.raises(ValueError):
        load_identity_snapshot(str(bad), expected_entity_id=_REAL_UUID_ENTITY)


# --- r1 P1/P2 fix batch tests (2026-10-05, round 53) ---


def test_r6_public_cli_threads_identity_snapshot(tmp_path: Path, monkeypatch) -> None:
    """P1-1/R6: the public CLI argparse threads --identity-snapshot (with
    --require-search + --entity-id) into LLMRunner.run — the production
    activation path, at wiring level (no LLM call)."""
    import json as _json
    import sys as _sys

    import main_with_llm
    from src.runners import llm_runner as lr

    snapshot = tmp_path / "snap.json"
    snapshot.write_bytes(
        _json.dumps(
            {
                "object_type": "entity",
                "schema_version": "2.2.0",
                "payload": {
                    "entity_id": _REAL_UUID_ENTITY,
                    "identity_revision": 1,
                    "identity_state": "provisional",
                    "listings": [{"source_binding_ref": "BND_fixture_1"}],
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
    )
    captured = {}

    def fake_run(self, **kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(lr.LLMRunner, "run", fake_run)
    monkeypatch.setattr(
        _sys,
        "argv",
        [
            "main_with_llm.py",
            "--company",
            "TEST",
            "--provider",
            "minimax",
            "--require-search",
            "--entity-id",
            _REAL_UUID_ENTITY,
            "--identity-snapshot",
            str(snapshot),
        ],
    )
    rc = main_with_llm.main()
    assert rc == 0
    assert captured["identity_snapshot"] == str(snapshot)
    assert captured["require_search"] is True
    assert captured["entity_id"] == _REAL_UUID_ENTITY


def test_r5b_lifecycle_listing_scope_binding_path(tmp_path: Path) -> None:
    """P1-4/JOB-11: the LIFECYCLE binding path (not only direct store calls)
    proves listing-level work stays distinct from issuer-level work."""
    from src.runners.llm_runner import QuickScanWorkLifecycle

    store = _store(tmp_path)
    entity_lc = _lifecycle(store, tmp_path, entity_id=_REAL_UUID_ENTITY)
    sec_lc = QuickScanWorkLifecycle(
        store,
        entity_id=_REAL_UUID_ENTITY,
        run_id="RUN_1",
        scan_id="SCAN_1",
        identity=dict(IDENTITY),
        scope="security",
        scope_id="SEC_listing_a",
    )
    question = _q("IQS_01", "问题一")
    h_entity = entity_lc.before_question(question)
    h_sec = sec_lc.before_question(question)
    assert h_entity["claimed"] is True and h_sec["claimed"] is True
    assert h_entity["work_item_id"] != h_sec["work_item_id"]


def test_loader_provisional_multi_ref_fails_fast_verified_passes(tmp_path: Path) -> None:
    """P1-3b: provisional with multiple binding refs fails at LOAD (the store
    would refuse per-question); verified with the same refs loads fine."""
    import json as _json

    from src.runners.llm_runner import load_identity_snapshot

    package = {
        "object_type": "entity",
        "schema_version": "2.2.0",
        "payload": {
            "entity_id": _REAL_UUID_ENTITY,
            "identity_revision": 1,
            "identity_state": "provisional",
            "listings": [
                {"source_binding_ref": "BND_a"},
                {"source_binding_ref": "BND_b"},
            ],
        },
    }
    prov = tmp_path / "prov2.json"
    prov.write_bytes(_json.dumps(package).encode("utf-8"))
    with pytest.raises(ValueError):
        load_identity_snapshot(str(prov))

    verified = {**package, "payload": {**package["payload"], "identity_state": "verified"}}
    ok = tmp_path / "verified2.json"
    ok.write_bytes(_json.dumps(verified).encode("utf-8"))
    payload = load_identity_snapshot(str(ok))
    assert payload["source_binding_refs"] == ["BND_a", "BND_b"]
    assert payload["source_binding_ref"] == "BND_a"
