"""Q07: per-question checkpoints, partial replies, cancel-resume.

Binds Q07 cases JOB-03/JOB-04/JOB-05/LLM-07/PAR-04/PAR-09 (invariants
I05/I12/I17): persist-on-return with a trustworthy receipt, hydrate saved
answers instead of re-dispatching, crash-recovery partitioning, cancel as a
state change, repair-budget honesty, pack resume that keeps originals, and
late receipts that settle once without repeat POSTs.

RED phase: the Q07 wiring (hydrate/persist/recovery) does not exist yet.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from src.core.models import Answer, Question
from src.core.qa_engine import QAEngine
from src.utils.quick_scan_work_store import QuickScanWorkStore

UUID_ENTITY = "ENT_1b2a4d3e-0000-4a1b-8c2d-000000000001"
IDENTITY = {
    "identity_revision": 1,
    "source_binding_version": 1,
    "identity_state": "verified",
    "source_binding_ref": "BND_TEST_1",
    "source_binding_refs": ["BND_TEST_1"],
    "identity_snapshot_sha256": "a" * 64,
}


def _store(tmp_path: Path, clock=None) -> QuickScanWorkStore:
    if clock is None:
        return QuickScanWorkStore(tmp_path / "work.sqlite")
    return QuickScanWorkStore(tmp_path / "work.sqlite", clock=clock)


def _lifecycle(store: QuickScanWorkStore, run_id: str = "RUN_1", **over):
    from src.runners.llm_runner import QuickScanWorkLifecycle

    kwargs = dict(
        entity_id=UUID_ENTITY,
        run_id=run_id,
        scan_id="SCAN_1",
        identity=dict(IDENTITY),
        model_requested="mimo-v2.6-flash",  # must equal the receipt's actual_model
    )
    kwargs.update(over)
    return QuickScanWorkLifecycle(store, **kwargs)


def _q(qid: str, text: str) -> Question:
    return Question(text=text, question_id=qid)


def _answer(qid: str, score: int = 8) -> dict:
    return {
        "entity_id": UUID_ENTITY,
        "question_id": qid,
        "status": "scored",
        "score": score,
        "description": "基于公开来源的判断",
    }


def _receipt(**over) -> dict:
    from tests.unit.test_quick_scan_work_store import _synthetic_search_receipt

    receipt = {
        **_synthetic_search_receipt(
            model=over.get("actual_model", "mimo-v2.6-flash")
        ),  # Explicit synthetic HTTP JSON, never a live receipt.
        "search_status": "executed",
        "provider": "mimo",
        "actual_model": "mimo-v2.6-flash",
        "response_id": "resp_q07_01",
        "attempt_id": "provider_attempt_q07_01",
        "search_receipt_id": "ws_q07_01",
        "response_status": "completed",
        "http_status_code": 200,
        "completed_at": "2026-10-06T00:00:00Z",
        "source_urls": ["https://example.com/x"],
        "request_id": "req_q07_01",
        "prompt_sha256": "e" * 64,
    }
    receipt.update(over)
    return receipt


def _seed_checkpoint(store, lifecycle, qid: str, score: int = 8, **receipt_over) -> dict:
    from src.utils.quick_scan_work_store import quick_scan_receipt_sha256

    handle = lifecycle.before_question(_q(qid, f"问题 {qid}"))
    assert handle["claimed"] is True, handle
    receipt = _receipt(**receipt_over)
    # the store contract requires the attempt to reach response_available first
    store.record_attempt_outcome(
        handle["work_item_id"],
        handle["lease"],
        handle["attempt_id"],
        outcome="response_available",
        receipt_sha256=quick_scan_receipt_sha256(receipt),
        http_status_code=receipt["http_status_code"],
        request_id=receipt.get("request_id"),
        execution_receipt=receipt,
    )
    record = store.save_answer_checkpoint(
        handle["work_item_id"],
        handle["lease"],
        handle["attempt_id"],
        answer=_answer(qid, score),
        execution_receipt=receipt,
    )
    return {"handle": handle, "record": record}


class CountingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def get_provider_name(self) -> str:
        return "fake"

    def search_question(self, question):
        self.calls += 1
        return []


class CountingGenerator:
    def __init__(self) -> None:
        self.calls = 0

    def generate_answer(self, question, search_results):
        self.calls += 1
        return Answer(text=f"fresh answer {self.calls}", score=7, status="scored", source="fake")


def _raw_attach(store, qid: str, run_id: str = "RUN_1"):
    """Raw attach with the SAME fingerprints the lifecycle derives."""
    text = f"问题 {qid}"
    return store.create_or_attach(
        entity_id=UUID_ENTITY,
        question_id=qid,
        generation=1,
        scope="entity",
        scope_id=UUID_ENTITY,
        run_id=run_id,
        scan_id="SCAN_1",
        question_fingerprint=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        routing_fingerprint="d" * 64,
        **dict(IDENTITY),
    )


def test_job_03_hydrates_saved_and_only_refills_missing(tmp_path: Path) -> None:
    """JOB-03: a 6-question pack with 4 persisted, 1 missing, 1 structurally
    failed — after interruption+resume the 4 stored results are hydrated (no
    new dispatch), only the 2 missing are refilled, the pack is neither
    discarded nor faked as fully successful."""
    store = _store(tmp_path)
    lifecycle = _lifecycle(store)
    for score, qid in zip((7, 8, 9, 10), ("IQS_05", "IQS_06", "IQS_07", "IQS_08")):
        _seed_checkpoint(store, lifecycle, qid, score=score)

    provider = CountingProvider()
    engine = QAEngine(provider, CountingGenerator(), work_item_lifecycle=_lifecycle(store))
    questions = [
        _q(qid, f"问题 {qid}")
        for qid in ("IQS_05", "IQS_06", "IQS_07", "IQS_08", "IQS_09", "IQS_10")
    ]
    batch = engine.process_questions(questions)

    # only the 2 missing questions are dispatched
    assert provider.calls == 2, provider.calls
    assert batch.processed_count == 6
    hydrated = [r for r in batch.results if r.metadata.get("answer_checkpoint", {}).get("hydrated")]
    assert len(hydrated) == 4
    # hydrated results carry the stored score (persisted, not re-asked)
    assert sorted(r.answer.score for r in hydrated) == [7, 8, 9, 10]
    fresh = [r for r in batch.results if r not in hydrated]
    # the two refilled answers are real dispatches, NOT hydrated fabrications
    assert len(fresh) == 2
    assert all(r.metadata.get("answer_checkpoint") is None for r in fresh)
    assert all(r.answer.score == 7 for r in fresh)


def test_par_04_resume_keeps_original_checkpoints_and_provenance(tmp_path: Path) -> None:
    """PAR-04: pack returned q1-q4, then crash; resume with the NEXT model.
    q1-q4 keep their unique success checkpoints with original execution time
    and model; only q5/q6 are refilled; the original four gain no new sends;
    no new pack id mints new logical questions; attempt/prompt hashes stay."""
    store = _store(tmp_path)
    lifecycle_a = _lifecycle(store, run_id="RUN_1", model_requested="model-a")
    seeded = {}
    for qid in ("IQS_05", "IQS_06", "IQS_07", "IQS_08"):
        seeded[qid] = _seed_checkpoint(
            store,
            lifecycle_a,
            qid,
            actual_model="model-a",
            completed_at="2026-10-06T01:00:00Z",
        )
    before_items = {
        row[0] for row in sqlite3.connect(store.path).execute("SELECT work_item_id FROM work_item")
    }
    before_attempts = {
        row[0]: (row[1], row[2])
        for row in sqlite3.connect(store.path).execute(
            "SELECT attempt_id, work_item_id, prompt_sha256 FROM attempt"
        )
    }

    provider = CountingProvider()
    engine = QAEngine(
        provider, CountingGenerator(), work_item_lifecycle=_lifecycle(store, run_id="RUN_1")
    )
    questions = [
        _q(qid, f"问题 {qid}")
        for qid in ("IQS_05", "IQS_06", "IQS_07", "IQS_08", "IQS_09", "IQS_10")
    ]
    batch = engine.process_questions(questions)

    assert provider.calls == 2, provider.calls  # only q5/q6, model-b run
    con = sqlite3.connect(store.path)
    try:
        after_items = {row[0] for row in con.execute("SELECT work_item_id FROM work_item")}
        # no ORIGINAL logical question is replaced or re-keyed by the resume;
        # the universe grows only by the two missing questions (6, not more)
        assert before_items <= after_items
        assert len(after_items) == 6
        after_attempts = {
            row[0]: (row[1], row[2])
            for row in con.execute("SELECT attempt_id, work_item_id, prompt_sha256 FROM attempt")
        }
        # original four attempts unchanged (id + prompt hash)
        for attempt_id, (work_item_id, prompt_sha) in before_attempts.items():
            assert after_attempts[attempt_id] == (work_item_id, prompt_sha)
        # original checkpoints keep model-a and their execution time
        rows = con.execute("SELECT payload_json FROM answer_checkpoint").fetchall()
    finally:
        con.close()
    assert len(rows) == 4, len(rows)
    import json as _json

    payloads = [_json.loads(r[0]) for r in rows]
    assert all(p["provenance"]["actual_model"] == "model-a" for p in payloads)
    assert all(p["provenance"]["response_completed_at"] == "2026-10-06T01:00:00Z" for p in payloads)
    # per-field provenance is visible on the hydrated results
    hydrated = [r for r in batch.results if r.metadata.get("answer_checkpoint", {}).get("hydrated")]
    assert len(hydrated) == 4
    for result in hydrated:
        prov = result.metadata["answer_checkpoint"]["provenance"]
        assert prov["actual_model"] == "model-a"


def test_job_04_recovery_partitions_all_states_no_silent_loss(tmp_path: Path) -> None:
    """JOB-04: crash at four points — pre-dispatch (no cost), post-send
    (unknown, listed separately), post-persist (never re-asked), pre-import
    ACK (Q10 domain, annotated). Every item lands in exactly one bucket:
    no silent task loss."""
    from src.runners.llm_runner import recovery_report

    clock = {"now": 1000.0}
    store = _store(tmp_path, clock=lambda: clock["now"])
    lifecycle = _lifecycle(store)

    # A: pre-dispatch — created only
    row_a = _raw_attach(store, "IQS_05")
    # B: post-send — claimed, intent marked, response lost, lease expired
    short_lifecycle = _lifecycle(store, lease_seconds=10.0)
    handle_b = short_lifecycle.before_question(_q("IQS_06", "问题 IQS_06"))
    assert handle_b["claimed"] is True
    clock["now"] += 11.0
    assert store.recover_expired(handle_b["work_item_id"]) == "uncertain"
    # C: post-persist — checkpoint saved
    seeded_c = _seed_checkpoint(store, lifecycle, "IQS_07")
    wid_c = seeded_c["handle"]["work_item_id"]
    # D: pre-ACK — a result_ready item WITHOUT checkpoint (delivery/import
    # domain is Q10's): modeled by state, never by fabricating a checkpoint
    wid_d = _raw_attach(store, "IQS_08")["work_item_id"]
    con_d = sqlite3.connect(store.path)
    try:
        con_d.execute("UPDATE work_item SET status='result_ready' WHERE work_item_id=?", (wid_d,))
        con_d.commit()
    finally:
        con_d.close()
    # E: cancelled bucket instance (JOB-05 vocabulary inside the partition)
    from src.runners.llm_runner import cancel_pending_work as _cancel_work

    wid_e = _raw_attach(store, "IQS_09")["work_item_id"]
    assert _cancel_work(store, [wid_e])[wid_e] == "cancelled"
    # "pre-dispatch carries no cost": no budget attempt rows exist at all
    con_b = sqlite3.connect(store.path)
    try:
        budget_rows = con_b.execute("SELECT COUNT(*) FROM quick_scan_budget_attempt").fetchone()[0]
    finally:
        con_b.close()
    assert budget_rows == 0, budget_rows

    report = recovery_report(store, run_id="RUN_1", scan_id="SCAN_1")

    assert set(report) >= {"pre_dispatch", "unknown_in_flight", "persisted", "import_ack_pending"}
    assert row_a["work_item_id"] in report["pre_dispatch"]
    assert handle_b["work_item_id"] in report["unknown_in_flight"]
    # exact memberships (r1 P2-1): checkpointed -> persisted; the pre-ACK
    # crash point -> Q10's import domain; cancelled -> its own bucket
    assert wid_c in report["persisted"]
    assert wid_d in report["import_ack_pending"]
    assert wid_e in report["cancelled"]
    # partition: every listed item appears exactly once across buckets
    all_ids = []
    for key in (
        "pre_dispatch",
        "unknown_in_flight",
        "persisted",
        "import_ack_pending",
        "cancelled",
    ):
        all_ids.extend(report[key])
    assert len(all_ids) == len(set(all_ids)), "an item landed in two buckets (silent loss)"
    assert len(all_ids) == len(
        store.list_run_items("RUN_1", "SCAN_1")
    ), "an item vanished from the report (silent loss)"
    assert report.get("q10_import_ack_note"), "pre-ACK crash point must be annotated toward Q10"


def test_job_05_cancel_is_state_change_and_history_survives(tmp_path: Path) -> None:
    """JOB-05: at cap with in-flight work, cancelling a not-yet-run object is
    a state change — pending stays visible as cancelled, history is not
    deleted, and the cancelled object is never dispatched."""
    store = _store(tmp_path)
    from src.runners.llm_runner import cancel_pending_work

    row = _raw_attach(store, "IQS_05")
    wid = row["work_item_id"]
    outcomes = cancel_pending_work(store, [wid])
    assert outcomes[wid] == "cancelled"
    assert store.cancel_pending(wid) == "cancelled"  # idempotent

    # history retained: creation event still present after cancel
    events = store.list_events(wid)
    assert any(e["event_type"] == "created" for e in events)
    assert any(e["event_type"] == "cancelled" for e in events)

    # cancelled work never dispatches (claim refuses)
    assert store.claim(wid, lease_seconds=60.0) is None
    lifecycle = _lifecycle(store)
    handle = lifecycle.before_question(_q("IQS_05", "问题 IQS_05"))
    assert handle["claimed"] is False
    assert handle.get("reason"), "refusal must carry a named reason (never dispatched)"

    # other pending items are untouched (待办保留)
    other = _raw_attach(store, "IQS_06")
    con = sqlite3.connect(store.path)
    try:
        status = con.execute(
            "SELECT status FROM work_item WHERE work_item_id=?", (other["work_item_id"],)
        ).fetchone()[0]
    finally:
        con.close()
    assert status == "pending"
    # cancelling a leased item is refused (not silently cancelled)
    handle2 = lifecycle.before_question(_q("IQS_06", "问题 IQS_06"))
    assert handle2["claimed"] is True
    try:
        store.cancel_pending(handle2["work_item_id"])
        raised = None
    except Exception as exc:  # noqa: BLE001
        raised = exc
    assert raised is not None and type(raised).__name__ == "WorkConflictError"


def test_llm_07_unfixable_results_never_become_checkpoints(tmp_path: Path) -> None:
    """LLM-07/I05: answers without a trustworthy receipt (search not
    executed, failed HTTP) and score-shape violations never persist — the
    question stays refillable and no fabricated success exists."""
    store = _store(tmp_path)
    lifecycle = _lifecycle(store)

    # no search executed -> refuse
    handle = lifecycle.before_question(_q("IQS_05", "问题 IQS_05"))
    try:
        store.save_answer_checkpoint(
            handle["work_item_id"],
            handle["lease"],
            handle["attempt_id"],
            answer=_answer("IQS_05"),
            execution_receipt=_receipt(search_status="not_attempted"),
        )
        first = None
    except ValueError as exc:
        first = str(exc)
    assert first, "receipt without verified search must be refused"

    # failed HTTP -> refuse
    try:
        store.save_answer_checkpoint(
            handle["work_item_id"],
            handle["lease"],
            handle["attempt_id"],
            answer=_answer("IQS_05"),
            execution_receipt=_receipt(http_status_code=500),
        )
        second = None
    except ValueError as exc:
        second = str(exc)
    assert second, "non-2xx receipt must be refused"

    # I05: score shape violations refused
    for bad_answer in (
        {**_answer("IQS_05"), "score": "8"},
        {**_answer("IQS_05"), "score": 11},
        {**_answer("IQS_05"), "score": True},
        {**_answer("IQS_05"), "status": "scored", "score": None},
        {**_answer("IQS_05"), "status": "unknown", "score": 5},
    ):
        try:
            store.save_answer_checkpoint(
                handle["work_item_id"],
                handle["lease"],
                handle["attempt_id"],
                answer=bad_answer,
                execution_receipt=_receipt(),
            )
            raise AssertionError(f"score/status violation accepted: {bad_answer}")
        except ValueError:
            pass

    assert store.get_answer_checkpoint(handle["work_item_id"]) is None
    # the question remains refillable: the lifecycle refuses nothing here and
    # no success was fabricated — work item still has its live lease/attempt


def test_par_09_late_receipt_settles_once_and_no_repeat_post(tmp_path: Path) -> None:
    """PAR-09: budget-reserved durable task, provider result unknown, then a
    late receipt — no parallel/blind repeat POST, budget never double
    reserved or settled, the late receipt idempotently settles the ORIGINAL
    attempt only."""
    clock = {"now": 1000.0}
    store = _store(tmp_path, clock=lambda: clock["now"])
    policy = {
        "configured": True,
        "policy_id": "POL_test",
        "policy_version": "v1",
        "budget": {
            "currency": "USD",
            "max_cost": 100,
            "max_requests": 100,
            "max_cost_per_attempt": 10,
        },
        "dispatch": {"max_in_flight_total": 2},
        "cost_policy": {
            "pricing_basis": "user_cap",
            "reserve_before_dispatch": True,
            "unknown_actual_cost_action": "retain_reservation_and_pause",
        },
        "routes": [
            {
                "id": "route_a",
                "quota_group": "g1",
                "max_in_flight": 2,
                "provider_config_ref": "mimo",
                "model": "mimo-v2.6-flash",
            }
        ],
        "quota_groups": [{"id": "g1", "max_in_flight": 2}],
    }
    route = {
        "route_id": "route_a",
        "provider": "mimo",
        "model_requested": "mimo-v2.6-flash",
        "quota_group": "g1",
    }
    # the budget policy must be configured in the store before any reserve
    store.configure_quick_scan_budget(policy)
    # the lifecycle marks send intent WITH the policy when provided (Q09 wiring
    # seam): reservation happens exactly once, inside before_question
    lifecycle = _lifecycle(store, lease_seconds=10.0, budget_policy=policy, budget_route=route)
    handle = lifecycle.before_question(_q("IQS_05", "问题 IQS_05"))
    assert handle["claimed"] is True

    con = sqlite3.connect(store.path)
    try:
        reserves = con.execute("SELECT COUNT(*) FROM quick_scan_budget_attempt").fetchone()[0]
    finally:
        con.close()
    assert reserves == 1, reserves

    # response lost -> unknown, then lease recovery -> uncertain
    clock["now"] += 11.0
    assert store.recover_expired(handle["work_item_id"]) == "uncertain"

    # blind repeat POST is impossible: uncertain refuses a fresh claim
    assert store.claim(handle["work_item_id"], lease_seconds=60.0) is None

    # late receipt settles the ORIGINAL attempt (idempotent)
    import hashlib

    receipt_sha = hashlib.sha256(b"late-receipt-body").hexdigest()
    store.note_late_receipt(
        handle["work_item_id"],
        handle["lease"],
        handle["attempt_id"],
        receipt_sha256=receipt_sha,
        budget_outcome="response_available",
        http_status_code=200,
    )
    try:
        store.note_late_receipt(
            handle["work_item_id"],
            handle["lease"],
            handle["attempt_id"],
            receipt_sha256=receipt_sha,
            budget_outcome="response_available",
            http_status_code=200,
        )
    except Exception:  # noqa: BLE001 - reject-or-noop, but never double settle
        pass

    con = sqlite3.connect(store.path)
    try:
        reserves_after = con.execute("SELECT COUNT(*) FROM quick_scan_budget_attempt").fetchone()[0]
        settle_events = con.execute(
            "SELECT COUNT(*) FROM work_event WHERE event_type LIKE '%budget%' "
            "OR event_type LIKE '%late%'"
        ).fetchone()[0]
    finally:
        con.close()
    assert reserves_after == 1, "budget must never be double reserved"
    assert settle_events == 1, "late receipt must settle exactly once"


def test_p1_1_precheck_falls_back_without_strand(tmp_path: Path) -> None:
    """r1 P1-1 regression: answers that would fail save's validation surface
    (over-long description / control character / empty source_urls) are
    pre-checked BEFORE any state is recorded — the attempt ends honest-unknown
    (uncertain, listed for recovery), never stranded at response_available
    with no checkpoint; and the REAL save rejects the same inputs (the drift
    detector between pre-flight and save)."""

    def _result_metadata(source_urls):
        return {
            "search_status": "executed",
            "provider": "mimo",
            "actual_model": "mimo-v2.6-flash",
            "response_id": "resp_probe_01",
            "request_id": "req_probe_01",
            "source_urls": source_urls,
            "execution": {
                "response_status": "completed",
                "http_status_code": 200,
                "completed_at": "2026-10-06T02:00:00Z",
                "attempt_id": "provider_attempt_probe_01",
                "prompt_sha256": "e" * 64,
                "search_receipt_id": "ws_probe_01",
            },
        }

    probes = (
        ("overlong", "x" * 6001, ["https://example.com/x"]),
        ("control", "a\x0cb", ["https://example.com/x"]),
        ("empty_urls", "正常长度描述", []),
    )
    for name, text, urls in probes:
        store = _store(tmp_path / name)
        lifecycle = _lifecycle(store)
        handle = lifecycle.before_question(_q("IQS_05", f"问题 {name}"))
        assert handle["claimed"] is True, name
        from src.core.models import QAResult as _QAResult

        result = _QAResult(
            question=_q("IQS_05", f"问题 {name}"),
            answer=Answer(text=text, score=8, status="scored", metadata=_result_metadata(urls)),
        )
        lifecycle.after_question(handle, result)

        con = sqlite3.connect(store.path)
        try:
            phase = con.execute("SELECT phase FROM attempt").fetchone()[0]
            status = con.execute("SELECT status FROM work_item").fetchone()[0]
            checkpoints = con.execute("SELECT COUNT(*) FROM answer_checkpoint").fetchone()[0]
        finally:
            con.close()
        # zero strand: honest-unknown state, no checkpoint, no response_available
        assert phase == "uncertain", (name, phase)
        assert status == "uncertain", (name, status)
        assert checkpoints == 0, (name, checkpoints)
        assert store.claim(handle["work_item_id"], lease_seconds=60.0) is None, name

        # drift detector: the REAL save rejects the same inputs too
        fresh = lifecycle.before_question(_q("IQS_06", f"问题 {name} 备份"))
        assert fresh["claimed"] is True, name
        bad_receipt = {
            "search_status": "executed",
            "provider": "mimo",
            "actual_model": "mimo-v2.6-flash",
            "response_id": "resp_probe_01",
            "attempt_id": "provider_attempt_probe_01",
            "search_receipt_id": "ws_probe_01",
            "response_status": "completed",
            "http_status_code": 200,
            "completed_at": "2026-10-06T02:00:00Z",
            "source_urls": urls,
            "request_id": "req_probe_01",
            "prompt_sha256": "e" * 64,
        }
        try:
            store.save_answer_checkpoint(
                fresh["work_item_id"],
                fresh["lease"],
                fresh["attempt_id"],
                answer=_answer("IQS_06").copy() | {"description": text},
                execution_receipt=bad_receipt,
            )
            raise AssertionError(f"save accepted the {name} input (preflight/save drift)")
        except ValueError:
            pass
        # the record for the drifted attempt was never made: safe unknown state
        phase2 = None
        con = sqlite3.connect(store.path)
        try:
            phase2 = con.execute("SELECT phase FROM attempt").fetchone()[0]
        finally:
            con.close()
        assert phase2 in {"send_intent", "uncertain"}, (name, phase2)


def test_p1_2_hydration_keeps_original_time_and_envelope_receipt(
    tmp_path: Path,
) -> None:
    """r1 P1-2 regression: hydrated answers carry the ORIGINAL execution time
    (not the hydration wall clock) and the output envelope keeps the original
    receipt — provider / search_status / answered_at survive hydration."""
    from datetime import datetime as _dt

    store = _store(tmp_path)
    lifecycle = _lifecycle(store)
    seeded = _seed_checkpoint(store, lifecycle, "IQS_05", completed_at="2026-10-06T01:00:00Z")
    assert seeded["record"]["payload"]["provenance"]["actual_model"] == "mimo-v2.6-flash"

    provider = CountingProvider()
    engine = QAEngine(provider, CountingGenerator(), work_item_lifecycle=_lifecycle(store))
    batch = engine.process_questions([_q("IQS_05", "问题 IQS_05")])
    assert provider.calls == 0
    result = batch.results[0]
    # original time, not wall clock
    assert result.answer.created_at == _dt.fromisoformat("2026-10-06T01:00:00+00:00")
    assert result.answer.metadata["search_status"] == "executed"

    envelope = batch.to_quick_scan_dict(
        entity_id=UUID_ENTITY,
        company_name="Fixture",
        provider_name=None,
        requested_model=None,
    )
    receipt = envelope["execution_receipts"]["IQS_05"]
    assert receipt["provider"] == "mimo"
    assert receipt["search_status"] == "executed"
    assert receipt["answered_at"] == "2026-10-06T01:00:00Z"
    assert receipt["http_status_code"] == 200
    assert envelope["answers"]["IQS_05"]["score"] == 8
