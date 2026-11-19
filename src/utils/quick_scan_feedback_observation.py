"""Optional per-question settlement observation beside the read-only inspect.

The public inspect result is the consumer contract. This module projects the
native facts behind one question (work item, attempt, durable response,
checkpoint, budget terminal and result outbox), compares them with what the
public result actually says, and writes ``feedback-observation/1`` JSONL to a
caller-owned sidecar.

Boundaries kept here on purpose:

* the owner ledger is opened through :class:`ReadOnlyWorkView` only;
* no provider is contacted and no paid attempt is created;
* a missing, unwritable or corrupt sidecar never changes the native result.

The public result and this projection are two read snapshots of the same
ledger. The adapter reads its facts after the result, so a concurrent writer
can only make the projection newer; ``evidence_ref`` always points at the
native work item so a reviewer can re-read the same question.
"""

import json
import os
from pathlib import Path

SCHEMA_VERSION = "feedback-observation/1"
OWNER = "stockqa"

ENV_OUT = "RESEARCH_FEEDBACK_OUT"
ENV_RUN_ID = "RESEARCH_FEEDBACK_RUN_ID"
ENV_ATTEMPT_ID = "RESEARCH_FEEDBACK_ATTEMPT_ID"

#: Provider labels that the verified-search checkpoint contract accepts.
KNOWN_RESPONSE_STATUS = frozenset({"completed"})
KNOWN_SEARCH_STATUS = frozenset({"executed"})

#: Expected settlement words derived from native rows.
SETTLED_NO_ANSWER = "settled_no_answer"
ANSWER_READABLE = "answer_readable"
ANSWER_UNUSABLE = "answer_unusable"
ANSWER_UNSETTLED = "answer_unsettled"
SEND_UNKNOWN = "send_unknown"
NOT_ATTEMPTED = "not_attempted"
CANCELLED = "cancelled"
REFUSED_SETTLED = "refused_settled"
MISSING_RECEIPT = "missing_receipt"
UNREADABLE_RECEIPT = "unreadable_receipt"
UNRECOGNIZED_LABEL = "unrecognized_label"
LATE_RECEIPT_OBSERVED = "late_receipt_observed"
LATE_RECEIPT_UNRESOLVED = "late_receipt_unresolved"
UNCLASSIFIED = "unclassified"

#: Actual words derived from the public projection.
RETRY_SCHEDULED = "retry_scheduled"
SEND_UNCERTAIN = "send_uncertain"
ANSWER_READY = "answer_ready"
ANSWER_DELIVERED = "answer_delivered"
FAILED_SETTLED = "failed_settled"
NOT_PROJECTED = "not_projected"
ACTUAL_CANCELLED = "cancelled"
ACTUAL_UNCLASSIFIED = "unclassified"

_STATUS_WORDS = {
    "pending": RETRY_SCHEDULED,
    "leased": RETRY_SCHEDULED,
    "uncertain": SEND_UNCERTAIN,
    "result_ready": ANSWER_READY,
    "delivered": ANSWER_DELIVERED,
    "failed": FAILED_SETTLED,
    "cancelled": ACTUAL_CANCELLED,
}

#: An expected settlement is legitimate only for these public words.
_ALLOWED_ACTUALS = {
    NOT_ATTEMPTED: frozenset({RETRY_SCHEDULED, NOT_PROJECTED}),
    SEND_UNKNOWN: frozenset({SEND_UNCERTAIN}),
    SETTLED_NO_ANSWER: frozenset({FAILED_SETTLED}),
    ANSWER_READABLE: frozenset({ANSWER_READY, ANSWER_DELIVERED}),
    ANSWER_UNUSABLE: frozenset({FAILED_SETTLED}),
    ANSWER_UNSETTLED: frozenset({RETRY_SCHEDULED, ANSWER_READY}),
    REFUSED_SETTLED: frozenset({RETRY_SCHEDULED, FAILED_SETTLED}),
    CANCELLED: frozenset({ACTUAL_CANCELLED}),
}

_ISSUE_KEYS = {
    (SETTLED_NO_ANSWER, RETRY_SCHEDULED): "settled-answer-replayed",
    (SETTLED_NO_ANSWER, SEND_UNCERTAIN): "settled-answer-replayed",
    (ANSWER_READABLE, RETRY_SCHEDULED): "readable-answer-replayed",
    (ANSWER_READABLE, FAILED_SETTLED): "readable-answer-marked-settled",
    (ANSWER_UNUSABLE, RETRY_SCHEDULED): "unusable-answer-replayed",
    (ANSWER_UNUSABLE, ANSWER_READY): "unusable-answer-presented-ready",
    (ANSWER_UNUSABLE, ANSWER_DELIVERED): "unusable-answer-presented-ready",
    (SEND_UNKNOWN, FAILED_SETTLED): "unknown-send-classified-settled",
}

_STALE_GENERATION = "stale-generation-projection"


def project_question_receipts(view, result, request):
    """Project native per-question facts inside one read-only transaction.

    The public result was read in an earlier snapshot. The same public
    projection is recomputed here so the comparison can tell a stale read
    (the ledger moved) from a projection that really contradicts the native
    settlement.
    """
    facts = {}
    projected_items = result.get("work_items") or {}
    with view.inspection_transaction() as connection:
        reprojection = view.owner_refresh_projection(
            entity_id=request["entity_id"],
            question_ids=request["question_ids"],
            scope_bindings=request["scope_bindings"],
        )
        for question_id in request["question_ids"]:
            projected = projected_items.get(question_id)
            reprojected = reprojection.get(question_id)
            if not isinstance(projected, dict) or not projected.get("work_item_id"):
                fact = _fb_absent_fact(request["entity_id"], question_id)
                fact["reprojected"] = reprojected
                facts[question_id] = fact
                continue
            facts[question_id] = _fb_fact(
                connection,
                view,
                entity_id=request["entity_id"],
                question_id=question_id,
                projected=projected,
                reprojected=reprojected,
                scope_binding=request["scope_bindings"][question_id],
            )
    return facts


def _fb_absent_fact(entity_id, question_id):
    return {
        "entity_id": entity_id,
        "question_id": question_id,
        "projected": False,
        "reprojected": None,
        "work_item_id": None,
        "generation": 0,
        "status": None,
        "projected_status": None,
        "highest_generation": 0,
        "newer_unresolved_generation": False,
        "attempts": 0,
        "attempt_id": None,
        "attempt_ordinal": 0,
        "phase": None,
        "has_response": False,
        "receipt_readable": False,
        "late_receipt": False,
        "unrecognized_label": False,
        "checkpoint": "absent",
        "settled_reason": None,
        "budget_status": None,
        "terminal_outcome": None,
        "actual_cost_micros": None,
        "cost_source_ref": None,
        "delivery_state": None,
    }


def _fb_fact(connection, view, *, entity_id, question_id, projected, reprojected, scope_binding):
    work_item_id = projected["work_item_id"]
    item = connection.execute(
        "SELECT * FROM work_item WHERE work_item_id=?", (work_item_id,)
    ).fetchone()
    if item is None:
        fact = _fb_absent_fact(entity_id, question_id)
        fact.update(projected=True, reprojected=reprojected, work_item_id=work_item_id)
        return fact
    attempts = connection.execute(
        "SELECT * FROM attempt WHERE work_item_id=? ORDER BY ordinal", (work_item_id,)
    ).fetchall()
    latest = attempts[-1] if attempts else None
    highest = connection.execute(
        "SELECT MAX(generation) FROM work_item WHERE entity_id=? AND question_id=? "
        "AND scope=? AND scope_id=?",
        (entity_id, question_id, scope_binding["scope"], scope_binding["scope_id"]),
    ).fetchone()[0]
    newer_unresolved = (
        connection.execute(
            "SELECT 1 FROM work_item WHERE entity_id=? AND question_id=? AND scope=? "
            "AND scope_id=? AND generation>? AND status IN "
            "('pending','leased','uncertain','result_ready') LIMIT 1",
            (
                entity_id,
                question_id,
                scope_binding["scope"],
                scope_binding["scope_id"],
                item["generation"],
            ),
        ).fetchone()
        is not None
    )
    receipt, has_response, receipt_readable = _fb_response(connection, view, latest)
    settled_reason = _fb_settled_reason(connection, work_item_id)
    budget = _fb_budget(connection, latest)
    fact = {
        "entity_id": entity_id,
        "question_id": question_id,
        "projected": True,
        "reprojected": reprojected,
        "work_item_id": work_item_id,
        "generation": item["generation"],
        "status": item["status"],
        "projected_status": projected.get("status"),
        "highest_generation": highest or item["generation"],
        "newer_unresolved_generation": newer_unresolved,
        "attempts": len(attempts),
        "attempt_id": None if latest is None else latest["attempt_id"],
        "attempt_ordinal": 0 if latest is None else latest["ordinal"],
        "phase": None if latest is None else latest["phase"],
        "has_response": has_response,
        "receipt_readable": receipt_readable,
        "late_receipt": bool(latest is not None and latest["late_receipt_sha256"]),
        "unrecognized_label": _fb_unrecognized(receipt),
        "checkpoint": _fb_checkpoint(connection, view, work_item_id),
        "settled_reason": settled_reason,
        "budget_status": None if budget is None else budget["status"],
        "terminal_outcome": None if budget is None else budget["terminal_outcome"],
        "actual_cost_micros": None if budget is None else budget["actual_cost_micros"],
        "cost_source_ref": None if budget is None else budget["cost_source_ref"],
        "delivery_state": _fb_delivery_state(view, work_item_id),
    }
    return fact


def _fb_response(connection, view, latest):
    """Return (receipt, row-present, row-readable) for the latest attempt."""
    if latest is None:
        return None, False, False
    row = connection.execute(
        "SELECT * FROM quick_scan_attempt_response WHERE attempt_id=?",
        (latest["attempt_id"],),
    ).fetchone()
    if row is None:
        return None, False, False
    try:
        return view.response_record(connection, row)["receipt"], True, True
    except Exception:  # a corrupt receipt is an unknown outcome, not a no-answer
        return None, True, False


def _fb_settled_reason(connection, work_item_id):
    row = connection.execute(
        "SELECT event_type FROM work_event WHERE work_item_id=? "
        "AND event_type LIKE 'received_without_answer:%' ORDER BY event_id DESC LIMIT 1",
        (work_item_id,),
    ).fetchone()
    return None if row is None else row[0].split(":", 1)[1]


def _fb_budget(connection, latest):
    if latest is None:
        return None
    return connection.execute(
        "SELECT b.status,b.actual_cost_micros,b.cost_source_ref,t.terminal_outcome "
        "FROM quick_scan_budget_attempt b "
        "LEFT JOIN quick_scan_budget_terminal t USING(budget_attempt_id) "
        "WHERE b.work_attempt_id=?",
        (latest["attempt_id"],),
    ).fetchone()


def _fb_checkpoint(connection, view, work_item_id):
    row = connection.execute(
        "SELECT c.*,w.*,a.attempt_id AS bound_attempt_id FROM answer_checkpoint c "
        "JOIN work_item w ON w.work_item_id=c.work_item_id "
        "JOIN attempt a ON a.attempt_id=c.attempt_id WHERE c.work_item_id=?",
        (work_item_id,),
    ).fetchone()
    if row is None:
        return "absent"
    if row["checkpoint_schema_version"] == 3:
        # Research-only checkpoints do not satisfy a verified search answer.
        return "not_usable"
    try:
        view.checkpoint_record(connection, row)
    except Exception:  # corruption stays visible as a non-usable checkpoint
        return "invalid"
    return "valid"


def _fb_delivery_state(view, work_item_id):
    try:
        row = view.get_result_delivery(work_item_id)
    except Exception:  # the outbox projection is evidence, never a hard stop
        return "unreadable"
    return None if row is None else row["state"]


def _fb_unrecognized(receipt):
    if not isinstance(receipt, dict):
        return False
    response_status = receipt.get("response_status")
    search_status = receipt.get("search_status")
    return bool(
        (isinstance(response_status, str) and response_status not in KNOWN_RESPONSE_STATUS)
        or (isinstance(search_status, str) and search_status not in KNOWN_SEARCH_STATUS)
    )


def _fb_expected(fact):
    """Classify the native settlement; a forced disposition wins over compare."""
    if fact["status"] == "cancelled":
        return CANCELLED, None
    if fact["checkpoint"] == "valid":
        return ANSWER_READABLE, None
    if fact["late_receipt"]:
        # A late receipt may carry the answer a settled attempt was missing.
        return (
            LATE_RECEIPT_OBSERVED if fact["has_response"] else LATE_RECEIPT_UNRESOLVED
        ), "unknown"
    if fact["unrecognized_label"]:
        return UNRECOGNIZED_LABEL, "unknown"
    if fact["settled_reason"] and fact["has_response"]:
        return SETTLED_NO_ANSWER, None
    if fact["checkpoint"] in {"not_usable", "invalid"}:
        return ANSWER_UNUSABLE, None
    if fact["phase"] == "response_available":
        if not fact["has_response"]:
            return MISSING_RECEIPT, "unknown"
        if not fact["receipt_readable"]:
            return UNREADABLE_RECEIPT, "unknown"
        return ANSWER_UNSETTLED, None
    if fact["phase"] == "confirmed_failure":
        return REFUSED_SETTLED, None
    if fact["phase"] in {"send_intent", "uncertain"} or fact["status"] == "uncertain":
        return SEND_UNKNOWN, None
    if fact["terminal_outcome"] in {"unknown", "legacy_unverified"}:
        return SEND_UNKNOWN, None
    if not fact["attempts"] or fact["phase"] in {"prepared", "abandoned_unsent"}:
        return NOT_ATTEMPTED, None
    return UNCLASSIFIED, "unknown"


def _fb_actual(fact):
    if not fact["projected"]:
        return NOT_PROJECTED
    word = _STATUS_WORDS.get(fact["projected_status"])
    return ACTUAL_UNCLASSIFIED if word is None else word


def _fb_attempt_identity(fact):
    """Keep the native attempt identity; only unattempted work gets a label."""
    if fact["attempt_id"]:
        return fact["attempt_id"]
    return "inspect-g{}a0".format(fact["generation"] or 0)


def _fb_row(
    fact,
    *,
    check_id,
    expected,
    actual,
    disposition,
    run_id,
    attempt_id,
    version,
    issue_key=None,
):
    row = {
        "schema_version": SCHEMA_VERSION,
        "owner": OWNER,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "step_id": "{}/question-{}-inspect".format(fact["entity_id"], fact["question_id"]),
        "check_id": check_id,
        "expected": expected,
        "actual": actual,
        "disposition": disposition,
    }
    issue_key = _ISSUE_KEYS.get((expected, actual), issue_key)
    if issue_key is not None:
        row["issue_key"] = issue_key
    row["evidence_ref"] = "qa:{}/g{}".format(
        fact["work_item_id"] or "absent", fact["generation"] or 0
    )
    row["version"] = version
    return row


def observe(result, facts, *, run_id=None, attempt_id=None):
    """Compare native settlement facts with the public projection."""
    version = str(result.get("protocol") or "stockqa.refresh_inspection/unknown")
    base_run = run_id or "inspect-" + str(result.get("request_sha256") or "unknown")[:16]
    rows = []
    for question_id in sorted(facts):
        fact = facts[question_id]
        identity = attempt_id or _fb_attempt_identity(fact)
        if _fb_snapshot_changed(fact):
            # The ledger moved between the public read and this projection.
            # Comparing across snapshots would manufacture a deviation, so
            # report the drift and leave the question for a fresh read.
            rows.append(
                _fb_row(
                    fact,
                    check_id="snapshot-consistency",
                    expected="single_snapshot",
                    actual="ledger_changed_between_reads",
                    disposition="unknown",
                    run_id=base_run,
                    attempt_id=identity,
                    version=version,
                )
            )
        else:
            expected, forced = _fb_expected(fact)
            actual = _fb_actual(fact)
            if forced is not None:
                disposition = forced
            else:
                allowed = _ALLOWED_ACTUALS.get(expected)
                disposition = (
                    "unknown"
                    if allowed is None
                    else "expected" if actual in allowed else "deviation"
                )
            rows.append(
                _fb_row(
                    fact,
                    check_id="settled-disposition",
                    expected=expected,
                    actual=actual,
                    disposition=disposition,
                    run_id=base_run,
                    attempt_id=identity,
                    version=version,
                )
            )
            if fact["newer_unresolved_generation"]:
                rows.append(
                    _fb_row(
                        fact,
                        check_id="generation-binding",
                        expected="latest_unresolved_generation",
                        actual="stale_generation",
                        disposition="deviation",
                        run_id=base_run,
                        attempt_id=identity,
                        version=version,
                        issue_key=_STALE_GENERATION,
                    )
                )
        delivery = _fb_delivery_word(fact)
        if delivery is not None:
            # The answer may be readable and still never reach the consumer.
            # That is an outbox fact, not a settlement verdict.
            rows.append(
                _fb_row(
                    fact,
                    check_id="delivery-state",
                    expected="delivery_available",
                    actual=delivery,
                    disposition="unknown",
                    run_id=base_run,
                    attempt_id=identity,
                    version=version,
                )
            )
        cost = _fb_cost(fact)
        if cost is not None:
            rows.append(
                _fb_row(
                    fact,
                    check_id="cost-state",
                    expected="cost_unknown",
                    actual=cost,
                    disposition="unknown",
                    run_id=base_run,
                    attempt_id=identity,
                    version=version,
                )
            )
    return rows


def _fb_snapshot_changed(fact):
    """True when the public result and the native reprojection disagree."""
    reprojected = fact.get("reprojected")
    if not isinstance(reprojected, dict):
        return False
    return (
        reprojected.get("work_item_id") != fact["work_item_id"]
        or reprojected.get("generation") != fact["generation"]
        or reprojected.get("status") != fact["projected_status"]
    )


def _fb_delivery_word(fact):
    """Return an outbox word for a delivery that cannot reach the consumer.

    ``ready``, ``delivered`` and the in-progress ``send_uncertain`` are normal
    outbox states; only a terminal refusal is worth a separate observation.
    """
    state = fact["delivery_state"]
    if state in {"blocked", "conflict", "rejected", "unreadable"}:
        return "delivery_" + state
    return None


def _fb_cost(fact):
    """Return an unknown-cost word, or None when the native cost is settled.

    ``in_flight`` is a normal state, not an unknown outcome: the attempt is
    still running and the caller has no reconciliation to do yet.
    """
    status = fact["budget_status"]
    if status is None or status == "in_flight":
        return None
    if status != "settled":
        return "cost_" + status
    if not fact["cost_source_ref"]:
        # A settled amount without a source reference is not a verified cost.
        return "cost_zero_unverified" if fact["actual_cost_micros"] == 0 else "cost_unverified"
    return None


def write_observations(path, rows):
    """Append UTF-8 JSONL; every exported value must already be a string."""
    payload = []
    for row in rows:
        if not isinstance(row, dict) or not row:
            raise ValueError("feedback observation must be a non-empty object")
        for key, value in row.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("feedback observation fields must be strings")
        payload.append(json.dumps(row, ensure_ascii=False) + "\n")
    with Path(path).open("a", encoding="utf-8", newline="\n") as stream:
        stream.write("".join(payload))


def emit_refresh_observation(result, request, ledger_path, *, environ=None):
    """Export one invocation's observations; never raises into the business path."""
    environment = os.environ if environ is None else environ
    out = environment.get(ENV_OUT)
    if not out:
        return []
    try:
        from src.utils.quick_scan_refresh_inspect import ReadOnlyWorkView

        view = ReadOnlyWorkView(ledger_path)
        facts = project_question_receipts(view, result, request)
        rows = observe(
            result,
            facts,
            run_id=environment.get(ENV_RUN_ID),
            attempt_id=environment.get(ENV_ATTEMPT_ID),
        )
        write_observations(out, rows)
        return rows
    except Exception:  # diagnostics are optional; the native contract is not
        return []
