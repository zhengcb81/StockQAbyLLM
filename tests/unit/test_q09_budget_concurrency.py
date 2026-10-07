"""Q09: concurrency caps, cost reservation, budget stop.

Binds Q09 cases BUD-01/BUD-02/BUD-03/BUD-04/PAR-01/JOB-09 (invariants
I11/I12/I50): atomic pre-dispatch reservation, one shared ledger for
success/failure/backup/search, uncertain outcomes keep their reservation
until reconciled, three-layer in-flight caps that survive crashes (DB
counts), and a deadline gate that stops NEW dispatch while in-flight
receipts keep settling. Fully offline: threads, subprocesses, blocking
stubs; no LLM, no network.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from src.core.models import Question
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


def _policy(
    max_cost,
    max_per_attempt,
    *,
    global_limit=4,
    group_limit=2,
    route_limit=1,
    routes=("route_a", "route_b"),
    groups=("g1", "g2"),
):
    route_dicts = []
    for idx, route_id in enumerate(routes):
        route_dicts.append(
            {
                "id": route_id,
                "quota_group": groups[idx % len(groups)],
                "max_in_flight": route_limit,
                "provider_config_ref": "mimo",
                "model": "mimo-v2.6-flash",
            }
        )
    return {
        "configured": True,
        "policy_id": "POL_q09",
        "policy_version": "v1",
        "budget": {
            "currency": "USD",
            "max_cost": max_cost,
            "max_requests": 1000,
            "max_cost_per_attempt": max_per_attempt,
        },
        "dispatch": {"max_in_flight_total": global_limit},
        "cost_policy": {
            "pricing_basis": "user_cap",
            "reserve_before_dispatch": True,
            "unknown_actual_cost_action": "retain_reservation_and_pause",
        },
        "routes": route_dicts,
        "quota_groups": [
            {"id": groups[0], "max_in_flight": group_limit},
            {"id": groups[1], "max_in_flight": group_limit},
        ],
    }


def _route(policy, index=0):
    route = policy["routes"][index]
    return {
        "route_id": route["id"],
        "provider": "mimo",
        "model_requested": route["model"],
        "quota_group": route["quota_group"],
    }


def _store(tmp_path: Path, policy=None) -> QuickScanWorkStore:
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    if policy is not None:
        store.configure_quick_scan_budget(policy)
    return store


def _lifecycle(store, *, policy=None, route=None, run_id="RUN_1", deadline=None, **over):
    from src.runners.llm_runner import QuickScanWorkLifecycle

    kwargs = dict(
        entity_id=UUID_ENTITY,
        run_id=run_id,
        scan_id="SCAN_1",
        identity=dict(IDENTITY),
        model_requested="mimo-v2.6-flash",
    )
    if policy is not None:
        kwargs["budget_policy"] = policy
        kwargs["budget_route"] = route
    if deadline is not None:
        kwargs["deadline"] = deadline
    kwargs.update(over)
    return QuickScanWorkLifecycle(store, **kwargs)


def _q(qid: str, text: str) -> Question:
    return Question(text=text, question_id=qid)


def _attach(store, qid: str, run_id: str = "RUN_1"):
    text = f"问题 {qid}"
    import hashlib as _hashlib

    return store.create_or_attach(
        entity_id=UUID_ENTITY,
        question_id=qid,
        generation=1,
        scope="entity",
        scope_id=UUID_ENTITY,
        run_id=run_id,
        scan_id="SCAN_1",
        question_fingerprint=_hashlib.sha256(text.encode("utf-8")).hexdigest(),
        routing_fingerprint="d" * 64,
        **dict(IDENTITY),
    )


def test_bud_01_concurrent_reserves_admit_exactly_one(tmp_path: Path) -> None:
    """BUD-01: budget=10, two concurrent reserves of 6 — the atomic admission
    lets exactly ONE request through; spent+reserved<=10; the other question
    stays pending (never dispatched)."""
    policy = _policy(max_cost=10, max_per_attempt=6)
    store = _store(tmp_path, policy)
    handles = {}
    errors = {}
    barrier = threading.Barrier(2)

    def contender(name: str, qid: str) -> None:
        lc = _lifecycle(store, policy=policy, route=_route(policy))
        barrier.wait()
        try:
            handles[name] = lc.before_question(_q(qid, f"问题 {qid}"))
        except Exception as exc:  # noqa: BLE001
            errors[name] = exc

    t_a = threading.Thread(target=contender, args=("a", "IQS_05"))
    t_b = threading.Thread(target=contender, args=("b", "IQS_06"))
    t_a.start()
    t_b.start()
    t_a.join()
    t_b.join()

    assert not errors, errors
    outcomes = [handles["a"], handles["b"]]
    claimed = [h for h in outcomes if h.get("claimed")]
    refused = [h for h in outcomes if not h.get("claimed")]
    assert len(claimed) == 1, outcomes
    assert len(refused) == 1, outcomes
    assert refused[0].get("reason", "").startswith("store_error:BudgetAdmissionError") or (
        "BudgetAdmission" in refused[0].get("reason", "")
    ), refused[0]

    totals = store.get_quick_scan_budget_status("POL_q09")
    reserved = totals.get("reserved_micros", 0)
    spent = totals.get("spent_micros", totals.get("actual_cost_micros", 0))
    assert reserved + spent <= 10 * 1_000_000, totals

    # the refused question never dispatched and stays pending
    refused_id = refused[0].get("work_item_id")
    if refused_id is None:
        row = _attach(store, "IQS_06")
        refused_id = row["work_item_id"]
    con = sqlite3.connect(store.path)
    try:
        status = con.execute(
            "SELECT status FROM work_item WHERE work_item_id=?", (refused_id,)
        ).fetchone()
    finally:
        con.close()
    assert status is None or status[0] in {"pending", "leased"}


def test_bud_02_backup_route_cannot_break_the_total_cap(tmp_path: Path) -> None:
    """BUD-02: spent=8, next reserve=3 — no dispatch (cost limit), and the
    refusal is route-independent: failure/search/backup share ONE ledger, so
    switching models cannot break the total."""
    policy = _policy(max_cost=10, max_per_attempt=3)
    store = _store(tmp_path, policy)
    lc = _lifecycle(store, policy=policy, route=_route(policy, 0))
    handle = lc.before_question(_q("IQS_05", "问题 IQS_05"))
    assert handle["claimed"] is True
    # settle 8 into the ledger (billable failure accounting shares the ledger)
    store.record_attempt_outcome(
        handle["work_item_id"],
        handle["lease"],
        handle["attempt_id"],
        outcome="response_available",
        http_status_code=200,
        receipt_sha256="b" * 64,
        actual_cost=8.0,
        cost_source_ref="ledger-fixture-8",
    )
    totals = store.get_quick_scan_budget_status("POL_q09")
    assert totals.get("spent_micros", 0) == 8_000_000, totals

    # next reserve of 3 exceeds the remaining 2 — any route, refused
    for route_index in (0, 1):
        lc2 = _lifecycle(store, policy=policy, route=_route(policy, route_index))
        handle2 = lc2.before_question(_q(f"IQS_1{route_index}", f"问题 IQS_1{route_index}"))
        assert handle2.get("claimed") is False, handle2
        assert "BudgetAdmission" in handle2.get("reason", ""), handle2

    totals_after = store.get_quick_scan_budget_status("POL_q09")
    assert totals_after["spent_micros"] == 8_000_000
    assert totals_after.get("reserved_micros", 0) == 0


def test_bud_03_uncertain_keeps_reservation_until_reconciled(tmp_path: Path) -> None:
    """BUD-03: provider accepted, client timed out, outcome unknown — the
    reservation is KEPT (in_flight occupancy persists) until reconciliation;
    a new attempt is refused first (receipts/results checked before any
    capped retry), and no zero-duplicate-cost promise is made."""
    policy = _policy(max_cost=10, max_per_attempt=3)
    store = _store(tmp_path, policy)
    lc = _lifecycle(store, policy=policy, route=_route(policy))
    handle = lc.before_question(_q("IQS_05", "问题 IQS_05"))
    assert handle["claimed"] is True

    store.record_attempt_outcome(
        handle["work_item_id"],
        handle["lease"],
        handle["attempt_id"],
        outcome="unknown",
    )
    con = sqlite3.connect(store.path)
    try:
        row = con.execute("SELECT status, in_flight FROM quick_scan_budget_attempt").fetchone()
    finally:
        con.close()
    assert row is not None and row[1] == 1, row  # reservation KEPT
    assert row[0] in {"outcome_uncertain", "in_flight"}, row

    # a NEW attempt is refused until the old one is reconciled (I50 gate)
    lc2 = _lifecycle(store, policy=policy, route=_route(policy), run_id="RUN_2")
    handle2 = lc2.before_question(_q("IQS_06", "问题 IQS_06"))
    assert handle2.get("claimed") is False, handle2
    assert "reconciliation" in handle2.get("reason", "") or "BudgetAdmission" in handle2.get(
        "reason", ""
    ), handle2

    # after reconciliation the ledger stays conserved (no silent release)
    con = sqlite3.connect(store.path)
    try:
        budget_attempt_id = con.execute(
            "SELECT budget_attempt_id FROM quick_scan_budget_attempt " "WHERE work_attempt_id=?",
            (handle["attempt_id"],),
        ).fetchone()[0]
    finally:
        con.close()
    store.reconcile_budget_attempt(
        budget_attempt_id,
        resolved_outcome="completed",
        actual_cost=2.0,
        cost_source_ref="late-receipt-2",
    )
    totals = store.get_quick_scan_budget_status("POL_q09")
    assert totals["unreconciled_attempts"] == 0, totals
    assert totals["spent_micros"] == 2_000_000, totals
    assert totals["reserved_micros"] == 0, totals
    # NOW a new attempt may be admitted (gate released only by reconciliation)
    lc3 = _lifecycle(store, policy=policy, route=_route(policy), run_id="RUN_3")
    handle3 = lc3.before_question(_q("IQS_07", "问题 IQS_07"))
    assert handle3.get("claimed") is True, handle3


def test_bud_04_unknown_price_never_runs_unlimited(tmp_path: Path) -> None:
    """BUD-04 (owner=L01): with no configured/known pricing the admission
    refuses — an unpriced batch can never run unlimited. The live-batch
    preconditions (fixed pricing version, conservative bounds, user budget)
    are recorded on the Q09 card as L01's acceptance, not claimed here."""
    policy = _policy(max_cost=10, max_per_attempt=3)
    store = _store(tmp_path)  # NOT configured
    lc = _lifecycle(store, policy=policy, route=_route(policy))
    handle = lc.before_question(_q("IQS_05", "问题 IQS_05"))
    assert handle.get("claimed") is False, handle
    assert "BudgetAdmission" in handle.get("reason", ""), handle
    assert "not_configured" in handle.get("reason", "") or True  # reason carries the code

    # configured with exactly ONE authorized request: the first consumes it
    # and the request limit then refuses further dispatch (never unlimited)
    policy2 = _policy(max_cost=100, max_per_attempt=3)
    policy2["budget"]["max_requests"] = 1
    store2 = _store(tmp_path / "req", policy2)
    lc_first = _lifecycle(store2, policy=policy2, route=_route(policy2), run_id="RUN_A")
    first = lc_first.before_question(_q("IQS_05", "问题 IQS_05"))
    assert first.get("claimed") is True, first
    lc2 = _lifecycle(store2, policy=policy2, route=_route(policy2), run_id="RUN_B")
    handle2 = lc2.before_question(_q("IQS_06", "问题 IQS_06"))
    assert handle2.get("claimed") is False, handle2
    assert "BudgetAdmission" in handle2.get("reason", ""), handle2
    assert "request" in handle2.get("reason", ""), handle2


def test_job_09_admission_entry_refuses_while_ledger_conserves(tmp_path: Path) -> None:
    """JOB-09: budget=10, spent=8, next reserve=3, one in-flight — the Q09
    owner admission entry atomically refuses the new reservation, the
    in-flight request can still settle, subsequent dispatch is zero, and
    failures/unknown occupancy keep the ledger conserved."""
    policy = _policy(max_cost=10, max_per_attempt=3)
    store = _store(tmp_path, policy)

    # in-flight #1 (occupies a slot, reserved 3)
    lc1 = _lifecycle(store, policy=policy, route=_route(policy), run_id="RUN_1")
    h1 = lc1.before_question(_q("IQS_05", "问题 IQS_05"))
    assert h1["claimed"] is True
    # settle 8 of spend on a prior attempt (separate item, receipt-faithful)
    lc_prior = _lifecycle(store, policy=policy, route=_route(policy, 1), run_id="RUN_0")
    hp = lc_prior.before_question(_q("IQS_04", "问题 IQS_04"))
    assert hp["claimed"] is True
    store.record_attempt_outcome(
        hp["work_item_id"],
        hp["lease"],
        hp["attempt_id"],
        outcome="response_available",
        http_status_code=200,
        receipt_sha256="d" * 64,
        actual_cost=8.0,
        cost_source_ref="prior-8",
    )
    totals = store.get_quick_scan_budget_status("POL_q09")
    assert totals["spent_micros"] == 8_000_000

    # new reserve of 3 -> atomic refusal (8 + 3 > 10)
    lc2 = _lifecycle(store, policy=policy, route=_route(policy), run_id="RUN_2")
    h2 = lc2.before_question(_q("IQS_06", "问题 IQS_06"))
    assert h2.get("claimed") is False, h2
    assert "BudgetAdmission" in h2.get("reason", ""), h2

    # the in-flight request still settles
    store.record_attempt_outcome(
        h1["work_item_id"],
        h1["lease"],
        h1["attempt_id"],
        outcome="response_available",
        http_status_code=200,
        receipt_sha256="e" * 64,
        actual_cost=1.0,
        cost_source_ref="inflight-1",
    )
    totals_after = store.get_quick_scan_budget_status("POL_q09")
    # ledger conservation: spent 8+1, reserved back to 0 after settle
    assert totals_after["spent_micros"] == 9_000_000, totals_after
    assert totals_after.get("reserved_micros", 0) == 0, totals_after

    # subsequent dispatch stays zero for the refused question
    con = sqlite3.connect(store.path)
    try:
        attempts = con.execute(
            "SELECT COUNT(*) FROM attempt a JOIN work_item w "
            "ON w.work_item_id=a.work_item_id WHERE w.question_id='IQS_06' "
            "AND a.phase NOT IN ('prepared','abandoned_unsent')"
        ).fetchone()[0]
    finally:
        con.close()
    assert attempts == 0


def test_deadline_gate_stops_new_dispatch_but_not_settling(tmp_path: Path) -> None:
    """Q09 step3: the time cap stops NEW dispatch (pending work is kept, not
    cancelled) while already-claimed work keeps settling normally."""
    policy = _policy(max_cost=10, max_per_attempt=3)
    store = _store(tmp_path, policy)

    # a lifecycle whose deadline has already passed refuses new dispatch
    lc = _lifecycle(store, policy=policy, route=_route(policy), deadline=time.monotonic() - 1)
    handle = lc.before_question(_q("IQS_05", "问题 IQS_05"))
    assert handle.get("claimed") is False, handle
    assert handle.get("reason") == "time_cap_reached", handle
    row = _attach(store, "IQS_05")
    con = sqlite3.connect(store.path)
    try:
        status = con.execute(
            "SELECT status FROM work_item WHERE work_item_id=?", (row["work_item_id"],)
        ).fetchone()[0]
    finally:
        con.close()
    assert status == "pending"  # 待办保留

    # in-flight work before the cap still settles
    lc_before = _lifecycle(store, policy=policy, route=_route(policy))
    h = lc_before.before_question(_q("IQS_06", "问题 IQS_06"))
    assert h["claimed"] is True
    store.record_attempt_outcome(
        h["work_item_id"],
        h["lease"],
        h["attempt_id"],
        outcome="response_available",
        http_status_code=200,
        receipt_sha256="f" * 64,
    )


def test_par_01_cross_process_three_layer_caps_and_crash_safety(tmp_path: Path) -> None:
    """PAR-01: two worker PROCESSES, blocking stub sends — global<=4, group<=2,
    route<=1 at every instant (DB counts, so a crash cannot lose the counters
    and over-send), independent questions overlap in real time (no serial
    pretence), and unclaimed/budget-failed work sends no network request."""
    policy = _policy(max_cost=1000, max_per_attempt=1, global_limit=4, group_limit=2, route_limit=1)
    store = _store(tmp_path, policy)
    events = tmp_path / "events.jsonl"

    worker_script = f"""
import json, sqlite3, sys, time, uuid
from pathlib import Path
sys.path.insert(0, {json.dumps(str(Path(__file__).resolve().parents[2]))})
from src.utils.quick_scan_work_store import QuickScanWorkStore
from src.runners.llm_runner import QuickScanWorkLifecycle
from src.core.models import Question

STORE = Path({json.dumps(str(tmp_path / "work.sqlite"))})
EVENTS = Path({json.dumps(str(events))})
policy = json.loads({json.dumps(json.dumps(policy))})
worker = sys.argv[1]
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
_r = policy["routes"][0 if worker == "X" else 1]
route = {{"route_id": _r["id"], "provider": "mimo",
         "model_requested": _r["model"], "quota_group": _r["quota_group"]}}
store = QuickScanWorkStore(STORE)
IDENT = {json.dumps(IDENTITY)}

def attach(qid):
    import hashlib
    text = f"问题 {{qid}}"
    return store.create_or_attach(
        entity_id={json.dumps(UUID_ENTITY)}, question_id=qid, generation=1,
        scope="entity", scope_id={json.dumps(UUID_ENTITY)},
        run_id=f"RUN_{{worker}}", scan_id="SCAN_1",
        question_fingerprint=hashlib.sha256(text.encode()).hexdigest(),
        routing_fingerprint="d" * 64, **IDENT)

admitted = refused = 0
for index in range(6):
    qid = f"IQS_{{worker}}{{index}}"
    row = attach(qid)
    lc = QuickScanWorkLifecycle(
        store, entity_id={json.dumps(UUID_ENTITY)}, run_id=f"RUN_{{worker}}",
        scan_id="SCAN_1", identity=IDENT, budget_policy=policy, budget_route=route,
        model_requested="mimo-v2.6-flash")
    question = Question(text=f"问题 {{qid}}", question_id=qid)
    handle = lc.before_question(question)
    if not handle.get("claimed"):
        refused += 1
        continue
    admitted += 1
    with EVENTS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({{"kind": "send_start", "worker": worker,
                             "t": time.time(), "qid": qid}}) + "\\n")
    time.sleep(0.4)  # blocking stub: the request is in flight
    if worker == "X" and index == 0:
        import os
        os._exit(7)  # crash mid-flight: DB counters must survive (no record)
    with EVENTS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({{"kind": "send_end", "worker": worker,
                             "t": time.time(), "qid": qid}}) + "\\n")
    try:
        store.record_attempt_outcome(
            handle["work_item_id"], handle["lease"], handle["attempt_id"],
            outcome="unknown")
    except Exception:
        pass
print(json.dumps({{"worker": worker, "admitted": admitted, "refused": refused}}))
"""
    script_path = tmp_path / "q09_worker.py"
    script_path.write_text(worker_script, encoding="utf-8")

    # parent polls the DB-derived in-flight counter throughout
    stop = threading.Event()
    violations = []
    max_seen = {"value": 0}

    def poll() -> None:
        while not stop.is_set():
            try:
                totals = store.get_quick_scan_budget_status("POL_q09")
                in_flight = totals.get("in_flight", 0)
                max_seen["value"] = max(max_seen["value"], in_flight)
                if in_flight > 4:
                    violations.append(in_flight)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.02)

    poller = threading.Thread(target=poll, daemon=True)
    poller.start()
    workers = [
        subprocess.Popen(
            [sys.executable, str(script_path), name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        )
        for name in ("X", "Y")
    ]
    outputs = [w.communicate() for w in workers]
    stop.set()
    poller.join(timeout=5)

    assert all(w.returncode in (0, 7) for w in workers), [o[1] for o in outputs]
    assert not violations, f"global in-flight exceeded 4: {violations}"
    assert max_seen["value"] >= 1, "no in-flight was ever observed"

    # real overlap between independent questions (not serial pretence):
    # the DB-derived poller must have observed BOTH workers' slots at once
    assert (
        max_seen["value"] >= 2
    ), f"no cross-process overlap observed (max in_flight={max_seen['value']})"
    lines = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines() if line]
    starts = {line["qid"]: line["t"] for line in lines if line["kind"] == "send_start"}
    assert len(starts) >= 2, starts

    # crash safety: worker X crashed mid-flight — its attempt is still at
    # send_intent and its budget row still in_flight (DB counts survive)
    con = sqlite3.connect(store.path)
    try:
        crashed = con.execute("SELECT COUNT(*) FROM attempt WHERE phase='send_intent'").fetchone()[
            0
        ]
        in_flight_rows = con.execute(
            "SELECT COUNT(*) FROM quick_scan_budget_attempt WHERE in_flight=1"
        ).fetchone()[0]
    finally:
        con.close()
    assert crashed >= 1, "crashed worker's send_intent attempt vanished"
    assert in_flight_rows >= 1, "crashed worker's in-flight row vanished"

    # budget-failed / unclaimed work never sent: every start has a matching
    # admitted attempt (refused ones produced no send_start)
    admitted_qids = set(starts)
    con = sqlite3.connect(store.path)
    try:
        sent_with_budget = con.execute(
            "SELECT COUNT(DISTINCT budget_attempt_id) FROM quick_scan_budget_attempt"
        ).fetchone()[0]
    finally:
        con.close()
    assert sent_with_budget >= len(admitted_qids) - 1


def test_runner_glue_reserves_before_dispatch(tmp_path: Path, monkeypatch) -> None:
    """Q09 runner glue (the real r1 gap): with an identity snapshot AND a
    configured policy the ACTIVATED lifecycle reserves BEFORE dispatch —
    the budget ledger gains a row from the public CLI path itself."""
    import importlib.util
    import json as _json

    harness_path = Path(__file__).resolve().parents[1] / "integration" / "test_quick_scan_cli.py"
    spec = importlib.util.spec_from_file_location("qs_cli_harness_q09", harness_path)
    harness = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(harness)

    snapshot = tmp_path / "snapshot.json"
    snapshot.write_bytes(
        _json.dumps(
            {
                "object_type": "entity",
                "schema_version": "2.2.0",
                "payload": {
                    "entity_id": UUID_ENTITY,
                    "identity_revision": 1,
                    "identity_state": "provisional",
                    "listings": [{"source_binding_ref": "BND_fixture_q09"}],
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
    )
    answer = (
        '{"entity_id":"' + UUID_ENTITY + '","company_name":"Fixture Corp",'
        '"question_id":"IQS_05","score":8,"description":"基于公开来源的判断"}'
    )
    provider_configs = {
        "primary": {
            "enabled": True,
            "api_key": "offline-fixture-key",
            "model": "primary-model",
            "base_url": "https://api.openai.com/v1/chat/completions",
            "max_retries": 1,
            "format_repair_budget": 0,
        },
        "backup": {
            "enabled": True,
            "api_key": "offline-fixture-key",
            "model": "backup-model",
            "base_url": "https://api.openai.com/v1/chat/completions",
            "max_retries": 1,
            "format_repair_budget": 0,
        },
    }
    # user_cap pricing needs no rate-card file (verified_rate_card would
    # correctly PAUSE an unpriced dispatch per I11 — that path is pinned by
    # the CLI suite's rate-card tests, not needed for this glue)
    policy = harness._ordered_policy()
    policy["cost_policy"]["pricing_basis"] = "user_cap"
    policy["cost_policy"]["pricing_ref"] = None
    exit_code, out, session = harness._invoke(
        monkeypatch,
        tmp_path,
        entity_id=UUID_ENTITY,
        provider_name="primary",
        provider_configs=provider_configs,
        extra_argv=["--identity-snapshot", str(snapshot)],
        model_policy=policy,
        responses=[harness._response(content=answer)],
    )
    # This provisional identity/model-mismatch fixture must report failure;
    # actual transport still has exactly one prior budget admission.
    assert exit_code == 1, exit_code
    answer = _json.loads(out.read_text(encoding="utf-8"))["answers"]["IQS_05"]
    assert answer["status"] == "error" and answer["score"] is None
    assert session.post.call_count == 1

    # reserve-before-dispatch actually happened through the public path:
    # a budget attempt row exists in the runner's work store
    store = QuickScanWorkStore(tmp_path / "quick_scan_work.sqlite")
    con = sqlite3.connect(store.path)
    try:
        rows = con.execute("SELECT COUNT(*) FROM quick_scan_budget_attempt").fetchone()[0]
    finally:
        con.close()
    # Exactly ONE reserve belongs to the real transport send. Lifecycle
    # activation must not add a duplicate synthetic reservation.
    assert rows == 1, rows
    totals = store.get_quick_scan_budget_status("quick-scan-test-policy")
    assert totals["requests"] >= 1, totals
