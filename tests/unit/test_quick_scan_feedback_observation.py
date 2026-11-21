"""Per-question settlement observation beside the public read-only inspect.

Every ledger here is a disposable synthetic SQLite under ``tmp_path``. No
provider is contacted, no production ledger is opened and no paid attempt is
created. The observation must never change the public inspect result.
"""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from src.utils.quick_scan_feedback_observation import (
    SCHEMA_VERSION,
    emit_refresh_observation,
    observe,
    project_question_receipts,
    write_observations,
)
from src.utils.quick_scan_refresh_inspect import ReadOnlyWorkView, inspect_refresh
from tests.unit.test_quick_scan_budget import _policy, _reserve
from tests.unit.test_quick_scan_work_store import (
    _checkpointed_work_item,
    _created,
    _prepared,
    _store,
    _successful_search_attempt,
)

ROOT = Path(__file__).resolve().parents[2]


def _request(store, item, policy, *, entity_id=None, question_id=None):
    normalized = store.configure_quick_scan_budget(policy)
    entity_id = item["entity_id"] if entity_id is None else entity_id
    question_id = item["question_id"] if question_id is None else question_id
    route = policy["routes"][0]
    return {
        "entity_id": entity_id,
        "question_ids": [question_id],
        "scope_bindings": {question_id: {"scope": "entity", "scope_id": entity_id}},
        "policy_id": normalized["policy_id"],
        "route_id": route["id"],
        "provider_config_ref": route["provider_config_ref"],
        "model": route["model"],
    }


def _settled_without_answer(tmp_path):
    clock = [1_800_000_000.0]
    store = _store(tmp_path, clock)
    item = _created(store)
    lease, attempt, _ = _successful_search_attempt(store, item["work_item_id"])
    store.fail_received_without_answer(
        item["work_item_id"],
        lease,
        attempt["attempt_id"],
        reason_code="missing_question",
    )
    return store, item, attempt


def _unknown_send(tmp_path):
    clock = [1_800_000_000.0]
    store = _store(tmp_path, clock)
    item = _created(store)
    lease = store.claim(item["work_item_id"], lease_seconds=60)
    attempt = _prepared(store, item["work_item_id"], lease)
    store.mark_send_intent(item["work_item_id"], lease, attempt["attempt_id"])
    store.record_attempt_outcome(
        item["work_item_id"], lease, attempt["attempt_id"], outcome="unknown"
    )
    return store, item, attempt, lease


def _observed(store, request, policy):
    result = inspect_refresh(store.path, request, policy=policy)
    view = ReadOnlyWorkView(store.path)
    facts = project_question_receipts(view, result, request)
    rows = observe(result, facts)
    return result, facts, rows


def _by_check(rows, check_id):
    return [row for row in rows if row["check_id"] == check_id]


def test_settled_no_answer_is_reported_from_native_receipt_without_ledger_writes(
    tmp_path,
):
    store, item, attempt = _settled_without_answer(tmp_path)
    policy = _policy()
    request = _request(store, item, policy)
    before = store.path.read_bytes()
    result, facts, rows = _observed(store, request, policy)
    assert store.path.read_bytes() == before
    assert result["API_requests"] == 0
    assert facts[item["question_id"]]["settled_reason"] == "missing_question"
    assert facts[item["question_id"]]["has_response"] is True
    # The paid visible journal is consulted; this synthetic attempt has no row.
    assert facts[item["question_id"]]["visible_output"] == "absent"
    settled = _by_check(rows, "settled-disposition")
    assert len(settled) == 1
    assert settled[0]["schema_version"] == SCHEMA_VERSION
    assert settled[0]["owner"] == "stockqa"
    assert settled[0]["expected"] == "settled_no_answer"
    assert settled[0]["actual"] == "failed_settled"
    assert settled[0]["disposition"] == "expected"
    assert settled[0]["evidence_ref"].endswith("/g1")
    assert "issue_key" not in settled[0]
    assert str(store.path) not in json.dumps(rows)
    # A settled item must never invite another paid send.
    assert store.claim(item["work_item_id"], lease_seconds=30) is None


def test_readable_answer_is_reported_as_answer_readable(tmp_path):
    store, item, checkpoint, clock = _checkpointed_work_item(tmp_path)
    policy = _policy()
    request = _request(store, item, policy)
    before = store.path.read_bytes()
    result, facts, rows = _observed(store, request, policy)
    assert store.path.read_bytes() == before
    assert facts[item["question_id"]]["checkpoint"] == "valid"
    settled = _by_check(rows, "settled-disposition")[0]
    assert settled["expected"] == "answer_readable"
    assert settled["actual"] == "answer_ready"
    assert settled["disposition"] == "expected"


def test_unknown_send_is_reported_as_send_unknown_not_as_settled(tmp_path):
    store, item, attempt, lease = _unknown_send(tmp_path)
    policy = _policy()
    request = _request(store, item, policy)
    _, facts, rows = _observed(store, request, policy)
    assert facts[item["question_id"]]["has_response"] is False
    settled = _by_check(rows, "settled-disposition")[0]
    assert settled["expected"] == "send_unknown"
    assert settled["actual"] == "send_uncertain"
    assert settled["disposition"] == "expected"


def test_missing_durable_receipt_stays_unknown(tmp_path):
    store, item, attempt, lease = _unknown_send(tmp_path)
    policy = _policy()
    request = _request(store, item, policy)
    with closing(sqlite3.connect(store.path)) as connection:
        connection.execute(
            "UPDATE attempt SET phase='response_available' WHERE attempt_id=?",
            (attempt["attempt_id"],),
        )
        connection.commit()
    result, facts, rows = _observed(store, request, policy)
    settled = _by_check(rows, "settled-disposition")[0]
    assert facts[item["question_id"]]["has_response"] is False
    assert settled["expected"] == "missing_receipt"
    assert settled["disposition"] == "unknown"


def test_late_receipt_with_a_body_is_observed_from_the_native_journal(tmp_path):
    store, item, attempt, lease = _unknown_send(tmp_path)
    from src.utils.quick_scan_work_store import quick_scan_receipt_sha256
    from tests.unit.test_quick_scan_work_store import _synthetic_search_receipt

    receipt = _synthetic_search_receipt(provider="mimo", model="mimo-v2.6-flash")
    store.note_late_receipt(
        item["work_item_id"],
        lease,
        attempt["attempt_id"],
        receipt_sha256=quick_scan_receipt_sha256(receipt),
        execution_receipt=receipt,
    )
    policy = _policy()
    request = _request(store, item, policy)
    _, facts, rows = _observed(store, request, policy)
    assert facts[item["question_id"]]["late_receipt"] is True
    assert facts[item["question_id"]]["has_response"] is True
    settled = _by_check(rows, "settled-disposition")[0]
    assert settled["expected"] == "late_receipt_observed"
    assert settled["actual"] == "send_uncertain"
    assert settled["disposition"] == "unknown"


def test_unrecognized_provider_label_is_unknown_and_never_settled(tmp_path):
    clock = [1_800_000_000.0]
    store = _store(tmp_path, clock)
    item = _created(store)
    lease = store.claim(item["work_item_id"], lease_seconds=60)
    attempt = _prepared(store, item["work_item_id"], lease)
    store.mark_send_intent(item["work_item_id"], lease, attempt["attempt_id"])
    from src.utils.quick_scan_work_store import quick_scan_receipt_sha256
    from tests.unit.test_quick_scan_work_store import _synthetic_search_receipt

    receipt = _synthetic_search_receipt(provider="mimo", model="mimo-v2.6-flash")
    receipt["response_status"] = "partially_completed"
    store.record_attempt_outcome(
        item["work_item_id"],
        lease,
        attempt["attempt_id"],
        outcome="response_available",
        http_status_code=200,
        receipt_sha256=quick_scan_receipt_sha256(receipt),
        request_id=receipt["request_id"],
        execution_receipt=receipt,
    )
    policy = _policy()
    request = _request(store, item, policy)
    _, facts, rows = _observed(store, request, policy)
    assert facts[item["question_id"]]["unrecognized_label"] is True
    settled = _by_check(rows, "settled-disposition")[0]
    assert settled["expected"] == "unrecognized_label"
    assert settled["actual"] == "retry_scheduled"
    assert settled["disposition"] == "unknown"


def test_newer_generation_is_not_crossed_with_older_settled_generation(tmp_path):
    clock = [1_800_000_000.0]
    store = _store(tmp_path, clock)
    first = _created(store)
    lease, attempt, _ = _successful_search_attempt(store, first["work_item_id"])
    store.fail_received_without_answer(
        first["work_item_id"],
        lease,
        attempt["attempt_id"],
        reason_code="missing_question",
    )
    second = _created(store, generation=2)
    policy = _policy()
    request = _request(store, second, policy)
    result, facts, rows = _observed(store, request, policy)
    assert result["work_items"][second["question_id"]]["generation"] == 2
    assert facts[second["question_id"]]["generation"] == 2
    settled = _by_check(rows, "settled-disposition")[0]
    assert settled["expected"] == "not_attempted"
    assert settled["actual"] == "retry_scheduled"
    assert settled["disposition"] == "expected"
    assert settled["evidence_ref"].endswith("/g2")


def _fact(**overrides):
    """A synthetic fact row; tests override only the field under study."""
    fact = {
        "entity_id": "ENT_BYD",
        "question_id": "IQS_05",
        "work_item_id": "W1",
        "generation": 1,
        "projected": True,
        "reprojected": {"work_item_id": "W1", "generation": 1, "status": "pending"},
        "projected_status": "pending",
        "highest_generation": 1,
        "newer_unresolved_generation": False,
        "attempts": 1,
        "attempt_id": "ATTEMPT_FIXTURE",
        "attempt_ordinal": 1,
        "phase": "response_available",
        "has_response": True,
        "receipt_readable": True,
        "late_receipt": False,
        "unrecognized_label": False,
        "checkpoint": "absent",
        "settled_reason": None,
        "visible_output": "absent",
        "budget_status": None,
        "terminal_outcome": None,
        "actual_cost_micros": None,
        "cost_source_ref": None,
        "delivery_state": None,
        "status": "failed",
    }
    fact.update(overrides)
    return fact


def _rows_for(**overrides):
    values = dict(overrides)
    values.setdefault("projected_status", "pending")
    values.setdefault(
        "reprojected",
        {
            "work_item_id": "W1",
            "generation": 1,
            "status": values["projected_status"],
        },
    )
    result = {
        "protocol": "stockqa.refresh_inspection/1.0.0",
        "work_items": {
            "IQS_05": {
                "work_item_id": "W1",
                "generation": 1,
                "status": values["projected_status"],
            }
        },
    }
    facts = {"IQS_05": _fact(**values)}
    return observe(result, facts, run_id="run-fixture", attempt_id="attempt-fixture")


def test_projection_that_reoffers_a_settled_question_is_a_deviation():
    rows = _rows_for(settled_reason="missing_question", status="failed")
    settled = _by_check(rows, "settled-disposition")[0]
    assert settled["expected"] == "settled_no_answer"
    assert settled["actual"] == "retry_scheduled"
    assert settled["disposition"] == "deviation"
    assert settled["issue_key"] == "settled-answer-replayed"
    assert settled["run_id"] == "run-fixture"
    assert settled["attempt_id"] == "attempt-fixture"
    assert settled["step_id"] == "ENT_BYD/question-IQS_05-inspect"


def test_unusable_checkpoint_is_never_reported_as_a_readable_answer():
    rows = _rows_for(checkpoint="invalid", projected_status="result_ready", status="result_ready")
    settled = _by_check(rows, "settled-disposition")[0]
    assert settled["expected"] == "answer_unusable"
    assert settled["actual"] == "answer_ready"
    assert settled["disposition"] == "deviation"
    assert settled["issue_key"] == "unusable-answer-presented-ready"
    # A settled item with an unusable checkpoint is consistent.
    consistent = _by_check(
        _rows_for(checkpoint="not_usable", status="failed", projected_status="failed"),
        "settled-disposition",
    )[0]
    assert consistent["expected"] == "answer_unusable"
    assert consistent["actual"] == "failed_settled"
    assert consistent["disposition"] == "expected"


def test_late_receipt_with_a_body_is_reported_separately():
    rows = _rows_for(
        late_receipt=True,
        phase="uncertain",
        status="uncertain",
        projected_status="uncertain",
    )
    settled = _by_check(rows, "settled-disposition")[0]
    assert settled["expected"] == "late_receipt_observed"
    assert settled["actual"] == "send_uncertain"
    assert settled["disposition"] == "unknown"
    unresolved = _by_check(
        _rows_for(late_receipt=True, has_response=False, receipt_readable=False),
        "settled-disposition",
    )[0]
    assert unresolved["expected"] == "late_receipt_unresolved"
    assert unresolved["disposition"] == "unknown"


def test_confirmed_refusal_and_cancelled_states_are_named():
    refused = _by_check(
        _rows_for(phase="confirmed_failure", has_response=False, status="leased"),
        "settled-disposition",
    )[0]
    assert refused["expected"] == "refused_settled"
    assert refused["actual"] == "retry_scheduled"
    assert refused["disposition"] == "expected"
    cancelled = _by_check(
        _rows_for(status="cancelled", projected_status="cancelled"), "settled-disposition"
    )[0]
    assert cancelled["expected"] == "cancelled"
    assert cancelled["actual"] == "cancelled"
    assert cancelled["disposition"] == "expected"


def test_unreadable_receipt_stays_unknown():
    rows = _rows_for(receipt_readable=False)
    settled = _by_check(rows, "settled-disposition")[0]
    assert settled["expected"] == "unreadable_receipt"
    assert settled["disposition"] == "unknown"


def test_changed_ledger_between_reads_is_unknown_not_a_deviation():
    rows = _rows_for(
        settled_reason="missing_question",
        reprojected={"work_item_id": "W1", "generation": 1, "status": "failed"},
        status="failed",
        budget_status="response_unpriced",
    )
    assert _by_check(rows, "settled-disposition") == []
    drift = _by_check(rows, "snapshot-consistency")[0]
    assert drift["expected"] == "single_snapshot"
    assert drift["actual"] == "ledger_changed_between_reads"
    assert drift["disposition"] == "unknown"
    assert "issue_key" not in drift
    # The unknown cost is independent of the projection drift.
    assert _by_check(rows, "cost-state")[0]["actual"] == "cost_response_unpriced"


def test_stale_generation_projection_is_reported():
    rows = _rows_for(
        newer_unresolved_generation=True, status="failed", settled_reason="missing_question"
    )
    generation = _by_check(rows, "generation-binding")[0]
    assert generation["expected"] == "latest_unresolved_generation"
    assert generation["actual"] == "stale_generation"
    assert generation["disposition"] == "deviation"
    assert generation["issue_key"] == "stale-generation-projection"


def test_late_receipt_with_a_new_label_reports_both_facts():
    rows = _rows_for(
        late_receipt=True,
        unrecognized_label=True,
        phase="uncertain",
        status="uncertain",
        projected_status="uncertain",
    )
    assert _by_check(rows, "settled-disposition")[0]["expected"] == "late_receipt_observed"
    label = _by_check(rows, "provider-label")[0]
    assert label["expected"] == "recognized_label"
    assert label["actual"] == "unrecognized_label"
    assert label["disposition"] == "unknown"
    assert "issue_key" not in label
    # A non-late unrecognized label is already named by the settlement row.
    plain = _rows_for(unrecognized_label=True)
    assert _by_check(plain, "provider-label") == []
    assert _by_check(plain, "settled-disposition")[0]["expected"] == "unrecognized_label"


def test_retained_paid_body_marks_a_recovery_candidate():
    rows = _rows_for(
        settled_reason="missing_question",
        status="failed",
        projected_status="failed",
        visible_output="present",
    )
    retained = _by_check(rows, "retained-body")[0]
    assert retained["expected"] == "no_retained_body"
    assert retained["actual"] == "visible_body_present"
    assert retained["disposition"] == "unknown"
    assert "issue_key" not in retained
    unreadable = _by_check(
        _rows_for(
            settled_reason="missing_question",
            status="failed",
            projected_status="failed",
            visible_output="unreadable",
        ),
        "retained-body",
    )[0]
    assert unreadable["actual"] == "visible_body_unreadable"
    # A readable answer is not a recovery candidate and gets no such row.
    assert (
        _by_check(
            _rows_for(
                checkpoint="valid",
                status="result_ready",
                projected_status="result_ready",
                visible_output="present",
            ),
            "retained-body",
        )
        == []
    )


def test_blocked_outbox_is_reported_without_blaming_the_settlement(tmp_path):
    store, item, checkpoint, clock = _checkpointed_work_item(tmp_path)
    store.mark_result_delivery_blocked(item["work_item_id"], "c06_standard_answer_unavailable")
    policy = _policy()
    request = _request(store, item, policy)
    _, facts, rows = _observed(store, request, policy)
    assert facts[item["question_id"]]["delivery_state"] == "blocked"
    settled = _by_check(rows, "settled-disposition")[0]
    assert settled["expected"] == "answer_readable"
    assert settled["disposition"] == "expected"
    delivery = _by_check(rows, "delivery-state")[0]
    assert delivery["expected"] == "delivery_available"
    assert delivery["actual"] == "delivery_blocked"
    assert delivery["disposition"] == "unknown"
    assert "issue_key" not in delivery


def test_in_flight_budget_is_not_reported_as_unknown_cost():
    rows = _rows_for(budget_status="in_flight")
    assert _by_check(rows, "cost-state") == []
    unpriced = _by_check(_rows_for(budget_status="response_unpriced"), "cost-state")[0]
    assert unpriced["actual"] == "cost_response_unpriced"
    assert unpriced["disposition"] == "unknown"


def test_settled_zero_cost_without_source_stays_unknown(tmp_path):
    store, item, attempt = _settled_without_answer(tmp_path)
    policy = _policy()
    request = _request(store, item, policy)
    _reserve(store, policy, "fake-zero-budget")
    with closing(sqlite3.connect(store.path)) as connection:
        connection.execute(
            "UPDATE quick_scan_budget_attempt SET work_attempt_id=?,status='settled',"
            "actual_cost_micros=0,cost_source_ref=NULL,in_flight=0,reconciled_at=1.0 "
            "WHERE budget_attempt_id='fake-zero-budget'",
            (attempt["attempt_id"],),
        )
        connection.commit()
    _, facts, rows = _observed(store, request, policy)
    assert facts[item["question_id"]]["actual_cost_micros"] == 0
    cost = _by_check(rows, "cost-state")[0]
    assert cost["expected"] == "cost_unknown"
    assert cost["actual"] == "cost_zero_unverified"
    assert cost["disposition"] == "unknown"


def test_stable_identity_is_reused_for_repeated_export(tmp_path):
    store, item, attempt = _settled_without_answer(tmp_path)
    policy = _policy()
    request = _request(store, item, policy)
    first = _observed(store, request, policy)[2]
    second = _observed(store, request, policy)[2]
    assert first == second
    identity = [
        (row["run_id"], row["attempt_id"], row["step_id"], row["check_id"]) for row in first
    ]
    assert len(set(identity)) == len(identity)
    # The native attempt identity is kept; a real retry gets a new ordinal.
    assert all(row["attempt_id"] == attempt["attempt_id"] for row in first)
    retry = _fact(attempt_id="ATTEMPT_RETRY", attempt_ordinal=2)
    result = {"protocol": "stockqa.refresh_inspection/1.0.0", "work_items": {}}
    retried = observe(result, {"IQS_05": retry})
    assert retried[0]["attempt_id"] == "ATTEMPT_RETRY"
    unattempted = observe(result, {"IQS_05": _fact(attempt_id=None, attempts=0, phase=None)})
    assert unattempted[0]["attempt_id"] == "inspect-g1a0"


def test_unhosted_inspection_keeps_original_ledger_and_paid_status(tmp_path, monkeypatch):
    from src.utils import execution_feedback
    from src.utils.quick_scan_feedback_registry import feedback_ledger

    inbox = tmp_path / "observations"
    monkeypatch.setattr(execution_feedback, "DEFAULT_INBOX", inbox)
    store, item, attempt, _ = _unknown_send(tmp_path)
    policy = _policy()
    request = _request(store, item, policy)
    result = inspect_refresh(store.path, request, policy=policy)
    before = store.path.read_bytes()
    rows = emit_refresh_observation(result, request, store.path, environ={})
    assert store.path.read_bytes() == before and result["API_requests"] == 0
    captured = [
        json.loads(marker.with_suffix(".jsonl").read_bytes())
        for marker in inbox.glob("feedback-*.ready")
    ]
    assert sorted(rows, key=str) == sorted(captured, key=str)
    row = next(row for row in captured if row["actual"] == "send_uncertain")
    assert row["disposition"] == "expected" and row["attempt_id"] == attempt["attempt_id"]
    assert (
        feedback_ledger(inbox / "bindings.sqlite", row["evidence_ref"], row["attempt_id"])
        == store.path
    )


def test_emit_writes_jsonl_beside_the_public_result(tmp_path):
    store, item, attempt = _settled_without_answer(tmp_path)
    policy = _policy()
    request = _request(store, item, policy)
    result = inspect_refresh(store.path, request, policy=policy)
    out = tmp_path / "feedback" / "inspect.jsonl"
    out.parent.mkdir()
    rows = emit_refresh_observation(
        result,
        request,
        store.path,
        environ={
            "RESEARCH_FEEDBACK_OUT": str(out),
            "RESEARCH_FEEDBACK_RUN_ID": "run-fixture",
            "RESEARCH_FEEDBACK_ATTEMPT_ID": "attempt-first",
        },
    )
    assert rows and all(row["run_id"] == "run-fixture" for row in rows)
    assert all(row["attempt_id"] == "attempt-first" for row in rows)
    written = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert written == rows
    assert all(row["schema_version"] == "feedback-observation/1" for row in written)
    for row in written:
        assert isinstance(row["expected"], str) and isinstance(row["actual"], str)


def test_emit_write_failure_keeps_native_business_result(tmp_path):
    store, item, attempt = _settled_without_answer(tmp_path)
    policy = _policy()
    request = _request(store, item, policy)
    result = inspect_refresh(store.path, request, policy=policy)
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    # A directory cannot be opened as the JSONL sidecar; the business result
    # must still be complete and the call must not raise.
    assert (
        emit_refresh_observation(
            result, request, store.path, environ={"RESEARCH_FEEDBACK_OUT": str(blocked)}
        )
        == []
    )
    assert result["protocol"] == "stockqa.refresh_inspection/1.0.0"
    assert result["API_requests"] == 0


def test_corrupt_ledger_observation_is_diagnostic_only(tmp_path):
    store, item, attempt = _settled_without_answer(tmp_path)
    policy = _policy()
    request = _request(store, item, policy)
    result = inspect_refresh(store.path, request, policy=policy)
    out = tmp_path / "inspect.jsonl"
    store.path.unlink()
    assert (
        emit_refresh_observation(
            result, request, store.path, environ={"RESEARCH_FEEDBACK_OUT": str(out)}
        )
        == []
    )
    assert not out.exists()


def test_write_observations_is_utf8_jsonl_and_refuses_non_string_rows(tmp_path):
    out = tmp_path / "rows.jsonl"
    write_observations(out, [{"step_id": "题-1", "expected": "答案", "actual": "已结算"}])
    assert out.read_bytes() == (
        '{"step_id": "题-1", "expected": "答案", "actual": "已结算"}\n'.encode("utf-8")
    )
    with pytest.raises(ValueError):
        write_observations(out, [{"expected": 1}])


def _invoke(ledger, policy_file, request_file, env_extra):
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "USERPROFILE", "TEMP", "TMP"}
    }
    env.update(env_extra)
    return subprocess.run(
        [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            "-m",
            "src.utils.quick_scan_refresh_inspect_cli",
            "--ledger",
            str(ledger),
            "--policy",
            str(policy_file),
            "--request",
            str(request_file),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env=env,
        check=False,
    )


def test_cli_rejection_reports_call_failure_without_inventing_a_settlement(tmp_path):
    store, item, attempt = _settled_without_answer(tmp_path)
    policy = _policy()
    request = _request(store, item, policy)
    request["confirmed"] = True
    policy_file = tmp_path / "policy.json"
    request_file = tmp_path / "request.json"
    policy_file.write_text(json.dumps(policy), encoding="utf-8")
    request_file.write_text(json.dumps(request), encoding="utf-8")
    out = tmp_path / "rejected.jsonl"
    response = _invoke(
        store.path,
        policy_file,
        request_file,
        {"RESEARCH_FEEDBACK_OUT": str(out), "RESEARCH_FEEDBACK_RUN_ID": "rejected-run"},
    )
    assert response.returncode == 2
    assert json.loads(response.stdout)["error_code"] == "refresh_executor_inspection_rejected"
    (row,) = [json.loads(line) for line in out.read_bytes().splitlines()]
    assert (row["check_id"], row["disposition"]) == ("execution-outcome", "deviation")


def test_cli_emits_observation_without_changing_stdout_or_ledger(tmp_path):
    store, item, attempt = _settled_without_answer(tmp_path)
    policy = _policy()
    request = _request(store, item, policy)
    policy_file = tmp_path / "policy.json"
    request_file = tmp_path / "request.json"
    policy_file.write_text(json.dumps(policy), encoding="utf-8")
    request_file.write_text(json.dumps(request), encoding="utf-8")
    before = store.path.read_bytes()
    plain = _invoke(store.path, policy_file, request_file, {})
    assert plain.returncode == 0, plain.stderr
    out = tmp_path / "cli-feedback.jsonl"
    observed = _invoke(
        store.path,
        policy_file,
        request_file,
        {"RESEARCH_FEEDBACK_OUT": str(out), "RESEARCH_FEEDBACK_RUN_ID": "cli-run"},
    )
    assert observed.returncode == 0, observed.stderr
    assert observed.stdout == plain.stdout
    assert store.path.read_bytes() == before
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert rows and all(row["run_id"] == "cli-run" for row in rows)
    assert {row["check_id"] for row in rows} >= {"settled-disposition"}
    assert json.loads(observed.stdout)["API_requests"] == 0
