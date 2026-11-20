"""Exercise the public StockQA F4 owner against the F2 native ledger fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.utils.quick_scan_work_store import quick_scan_receipt_sha256  # noqa: E402
from tests.unit.test_quick_scan_feedback_observation import (  # noqa: E402
    _invoke,
    _policy,
    _request,
    _settled_without_answer,
)
from tests.unit.test_quick_scan_work_store import (  # noqa: E402
    _created,
    _prepared,
    _store,
    _synthetic_search_receipt,
)

ROOT = Path(__file__).resolve().parents[2]
PORT = "src.utils.quick_scan_feedback_owner"
OLD_PROJECTION = """
from src.utils.quick_scan_refresh_inspect import ReadOnlyWorkView
from src.utils.quick_scan_refresh_inspect_cli import main
import sys

read = ReadOnlyWorkView.owner_refresh_projection
def legacy_status(view, **request):
    rows = read(view, **request)
    for row in rows.values():
        if row["status"] == "failed":
            row["status"] = "pending"
    return rows

ReadOnlyWorkView.owner_refresh_projection = legacy_status
raise SystemExit(main(sys.argv[1:]))
"""
LOST_REPLY = """
import os
from src.utils.quick_scan_feedback_owner import main
import sys
sys.stdout = type("LostStdout", (), {"write": lambda *_: os._exit(47)})()
raise SystemExit(main(sys.argv[1:]))
"""


def fixture(root):
    root.mkdir(parents=True, exist_ok=True)
    store, item, attempt = _settled_without_answer(root)
    policy = _policy()
    request = _request(store, item, policy)
    policy_file = root / "policy.json"
    request_file = root / "request.json"
    policy_file.write_text(json.dumps(policy), encoding="utf-8")
    request_file.write_text(json.dumps(request), encoding="utf-8")
    normal = _invoke(store.path, policy_file, request_file, {})
    assert normal.returncode == 0, normal.stderr
    current = json.loads(normal.stdout)
    assert current["work_items"][item["question_id"]]["status"] == "failed"
    before = store.path.read_bytes()
    sidecar = root / "sample.jsonl"
    state = root / "feedback.sqlite"
    old = subprocess.run(
        [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            "-c",
            OLD_PROJECTION,
            "--ledger",
            str(store.path),
            "--policy",
            str(policy_file),
            "--request",
            str(request_file),
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "RESEARCH_FEEDBACK_OUT": str(sidecar),
            "RESEARCH_FEEDBACK_RUN_ID": "stockqa-f4-reconstructed",
            "RESEARCH_FEEDBACK_ATTEMPT_ID": "wrapper-attempt",
            "STOCKQA_FEEDBACK_STATE": str(state),
        },
        capture_output=True,
        check=True,
        timeout=30,
    )
    result = json.loads(old.stdout)
    rows = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines()]
    observation = next(row for row in rows if row.get("issue_key") == "settled-answer-replayed")
    assert result["API_requests"] == current["API_requests"] == 0
    assert store.path.read_bytes() == before
    return {
        "owner": "stockqa",
        "ledger": str(store.path),
        "policy": str(policy_file),
        "request": str(request_file),
        "sidecar": str(sidecar),
        "state": str(state),
        "attempt_id": attempt["attempt_id"],
        "observation": observation,
    }


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _action(kind, payload):
    key = _digest([kind, payload])
    return {
        "schema_version": "feedback-action/1",
        "method": "lookup" if kind == "reconcile" else "submit",
        "owner": "stockqa",
        "action_id": key,
        "kind": kind,
        "request_digest": _digest(["stockqa", kind, key, payload]),
        "payload": payload,
    }


def _run(ledger, state, kind, action=None, *suffix, lost=False):
    command = ["-c", LOST_REPLY] if lost else ["-m", PORT]
    ledger_args = ["--ledger", str(ledger)] if ledger is not None else []
    return subprocess.run(
        [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            *command,
            *ledger_args,
            "--state",
            str(state),
            kind,
            *suffix,
        ],
        cwd=ROOT,
        input=b"" if action is None else json.dumps(action).encode(),
        capture_output=True,
        check=False,
        timeout=30,
    )


def _apply(ledger, state, kind, payload):
    process = _run(ledger, state, kind, _action(kind, payload))
    assert process.returncode == 0, process.stderr.decode(errors="replace")
    response = json.loads(process.stdout)
    if kind == "reconcile":
        assert response["state"] == "done"
    return response["result"]


def test_republishes_settled_attempt_and_recovers_original_action_without_ledger_writes(tmp_path):
    data = fixture(tmp_path)
    ledger = Path(data["ledger"])
    state = tmp_path / "feedback.sqlite"
    before = ledger.read_bytes()
    observation = data["observation"]
    assert observation["attempt_id"] == "wrapper-attempt"
    assert observation["details_ref"] == f"qa-attempt/1/{data['attempt_id']}"
    occurrence = _digest(observation)
    triage = _apply(
        None, state, "triage", {"occurrence_id": occurrence, "observation": observation}
    )
    assert triage["decision"] == "repair"
    issue = triage["issue_ref"]
    repair_input = {"issue_ref": issue, "generation": triage["generation"]}
    repair = _action("repair", repair_input)
    lost = _run(None, state, "repair", repair, lost=True)
    assert lost.returncode == 47 and lost.stdout == b""
    lookup = _run(None, state, "get-action", None, repair["action_id"])
    receipt = json.loads(lookup.stdout)["receipt"]
    assert receipt["request"] == repair
    publication = receipt["result"]["publication"]
    assert publication["attempt_id"] == data["attempt_id"]
    assert publication["status"] == "failed_settled"
    assert (
        publication["API_requests"]
        == publication["new_attempts"]
        == publication["new_generations"]
        == 0
    )
    version = receipt["result"]["fixed_version"]
    assert _apply(None, state, "repair", repair_input) == receipt["result"]
    replay = _apply(
        None,
        state,
        "replay",
        {
            "issue_ref": issue,
            "issue_owner": "stockqa",
            "fixed_version": version,
            "occurrence_id": occurrence,
            "original_occurrence_id": occurrence,
            "observation": observation,
            "acquisition": "reuse_only",
        },
    )
    assert replay["verdict"] == "verified"
    report = _action(
        "report",
        {
            "occurrence_id": occurrence,
            "original_occurrence_id": occurrence,
            "origin_owner": "stockqa",
            "issue_ref": issue,
            "fixed_version": version,
            "verdict": "verified",
            "evidence_ref": replay["evidence_ref"],
        },
    )
    lost = _run(None, state, "report", report, lost=True)
    assert lost.returncode == 47 and lost.stdout == b""
    lookup = _run(None, state, "get-action", None, report["action_id"])
    assert json.loads(lookup.stdout)["receipt"]["request"] == report
    assert json.loads(lookup.stdout)["receipt"]["result"]["recorded"] is True
    assert ledger.read_bytes() == before


def test_wrong_attempt_is_never_repaired_and_unknown_old_billing_not_zero(tmp_path):
    data = fixture(tmp_path)
    state = Path(data["state"])
    observation = data["observation"]
    bogus = {**observation, "details_ref": "qa-attempt/1/ATTEMPT_OTHER"}
    assert _apply(None, state, "triage", {"occurrence_id": "bad", "observation": bogus}) == {
        "decision": "needs_evidence"
    }
    reconcile = _apply(None, state, "reconcile", {"occurrence_id": "r", "observation": observation})
    assert reconcile == {
        "status": "cost_unknown",
        "job_status": "failed",
        "cost_state": "unknown",
        "evidence_ref": observation["evidence_ref"],
    }


def test_explicit_legacy_read_can_restore_original_ref_and_never_writes_the_ledger(tmp_path):
    data = fixture(tmp_path)
    ledger = Path(data["ledger"])
    before = ledger.read_bytes()
    state = tmp_path / "explicit-feedback.sqlite"
    observation = {**data["observation"], "attempt_id": data["attempt_id"]}
    observation.pop("details_ref")
    payload = {"occurrence_id": "old", "observation": observation}
    assert _apply(ledger, state, "triage", payload)["decision"] == "repair"
    rejected = _run(ledger, ledger, "triage", _action("triage", payload))
    assert rejected.returncode == 2
    assert ledger.read_bytes() == before


def test_reconcile_knows_the_exact_native_paid_cost_without_changing_it(tmp_path):
    store = _store(tmp_path, [1_800_000_000.0])
    item = _created(store)
    work_id = item["work_item_id"]
    lease = store.claim(work_id, lease_seconds=60)
    policy = _policy()
    route = policy["routes"][0]
    route.update(provider_config_ref="mimo", model="mimo-v2.6-flash")
    store.configure_quick_scan_budget(policy)
    attempt = _prepared(store, work_id, lease, route_id=route["id"])["attempt_id"]
    store.mark_send_intent(
        work_id,
        lease,
        attempt,
        budget_policy=policy,
        budget_route={
            "route_id": route["id"],
            "provider": route["provider_config_ref"],
            "model_requested": route["model"],
            "quota_group": route["quota_group"],
        },
    )
    receipt = _synthetic_search_receipt(provider="mimo", model="mimo-v2.6-flash")
    store.record_attempt_outcome(
        work_id,
        lease,
        attempt,
        outcome="response_available",
        http_status_code=200,
        request_id=receipt["request_id"],
        receipt_sha256=quick_scan_receipt_sha256(receipt),
        execution_receipt=receipt,
        actual_cost=0.25,
        cost_source_ref="fixture-original-invoice",
    )
    store.fail_received_without_answer(work_id, lease, attempt, reason_code="missing_question")
    baseline = store.path.read_bytes()
    evidence = f"qa:{work_id}/g1"
    expect = {
        "status": "known",
        "job_status": "failed",
        "cost_state": "known",
        "evidence_ref": evidence,
    }
    state = tmp_path / "feedback.sqlite"
    assert (
        _apply(
            store.path,
            state,
            "reconcile",
            {"observation": {"evidence_ref": evidence, "details_ref": f"qa-attempt/1/{attempt}"}},
        )
        == expect
    )
    assert not state.exists()
    assert store.path.read_bytes() == baseline


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-root", required=True, type=Path)
    print(json.dumps(fixture(parser.parse_args().fixture_root)))
