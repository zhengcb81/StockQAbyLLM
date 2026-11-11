"""Versioned visible research output in the original native work SQLite.

This is a receipt-bound replay artifact, never a provider conversation. Private
reasoning and request messages cannot enter the strict visible-only projection.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

DDL_V23_ADDITIONS: tuple[str, ...] = (
    """CREATE TABLE quick_scan_research_visible_output (
        attempt_id TEXT PRIMARY KEY REFERENCES quick_scan_attempt_response(attempt_id),
        schema_version INTEGER NOT NULL CHECK(schema_version=1),
        provider_attempt_id TEXT NOT NULL,
        receipt_sha256 TEXT NOT NULL,
        request_payload_sha256 TEXT NOT NULL,
        response_sha256 TEXT NOT NULL,
        actual_model TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        recorded_at REAL NOT NULL)""",
    """CREATE TRIGGER quick_scan_research_visible_output_no_update BEFORE UPDATE ON quick_scan_research_visible_output
        BEGIN SELECT RAISE(ABORT,'research visible output is immutable'); END""",
    """CREATE TRIGGER quick_scan_research_visible_output_no_delete BEFORE DELETE ON quick_scan_research_visible_output
        BEGIN SELECT RAISE(ABORT,'research visible output is immutable'); END""",
)

DDL_V27_ADDITIONS: tuple[str, ...] = (
    """CREATE TABLE quick_scan_research_visible_output_recovery (
        attempt_id TEXT PRIMARY KEY REFERENCES quick_scan_attempt_response(attempt_id),
        schema_version INTEGER NOT NULL CHECK(schema_version=1),
        receipt_sha256 TEXT NOT NULL CHECK(length(receipt_sha256)=64),
        response_sha256 TEXT NOT NULL CHECK(length(response_sha256)=64),
        payload_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
        recorded_at REAL NOT NULL)""",
    """CREATE TRIGGER quick_scan_research_output_recovery_insert_guard
        BEFORE INSERT ON quick_scan_research_visible_output_recovery
        BEGIN SELECT RAISE(ABORT,'research output recovery is not bound to its receipt')
        WHERE NOT EXISTS (
            SELECT 1 FROM quick_scan_attempt_response r
            WHERE r.attempt_id=NEW.attempt_id
            AND r.receipt_sha256=NEW.receipt_sha256
            AND r.response_sha256=NEW.response_sha256); END""",
    """CREATE TRIGGER quick_scan_research_output_recovery_no_update
        BEFORE UPDATE ON quick_scan_research_visible_output_recovery
        BEGIN SELECT RAISE(ABORT,'research output recovery is immutable'); END""",
    """CREATE TRIGGER quick_scan_research_output_recovery_no_delete
        BEFORE DELETE ON quick_scan_research_visible_output_recovery
        BEGIN SELECT RAISE(ABORT,'research output recovery is immutable'); END""",
)


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _sha(text):
    return hashlib.sha256(text.encode("utf8")).hexdigest()


def normalize(value):
    if not isinstance(value, dict) or set(value) != {"content", "tool_calls", "finish_reason"}:
        raise ValueError("research_visible_output_fields_invalid")
    if not isinstance(value["content"], str) or value["finish_reason"] not in {
        "stop",
        "tool_calls",
    }:
        raise ValueError("research_visible_output_invalid")
    calls = value["tool_calls"]
    if (
        not isinstance(calls, list)
        or len(calls) > 20
        or bool(calls) != (value["finish_reason"] == "tool_calls")
    ):
        raise ValueError("research_visible_tool_calls_invalid")
    ids = set()
    for call in calls:
        if not isinstance(call, dict) or set(call) != {"id", "name", "arguments"}:
            raise ValueError("research_visible_tool_fields_invalid")
        if (
            any(
                not isinstance(call[k], str) or not call[k] or len(call[k]) > 200
                for k in ("id", "name")
            )
            or call["id"] in ids
            or not isinstance(call["arguments"], dict)
        ):
            raise ValueError("research_visible_tool_invalid")
        ids.add(call["id"])

    def private_guard(node):
        if isinstance(node, dict):
            for key, item in node.items():
                if not isinstance(key, str) or key.casefold() in {
                    "reasoning_content",
                    "reflection_content",
                    "private_messages",
                    "api_key",
                    "authorization",
                }:
                    raise ValueError("research_visible_private_field_forbidden")
                private_guard(item)
        elif isinstance(node, list):
            for item in node:
                private_guard(item)
        elif isinstance(node, str):
            if any(
                marker in node.casefold() for marker in ("<think", "</think", "<antml:", "</antml:")
            ):
                raise ValueError("research_visible_hidden_thinking_forbidden")
        elif node is not None and type(node) not in {int, float, bool}:
            raise ValueError("research_visible_json_invalid")

    private_guard(value)
    try:
        from src.providers.llm_response_parser import load_visible_answer_json

        decoded = load_visible_answer_json(value["content"])
    except (ValueError, TypeError) as error:
        if str(error) in {
            "private_channel_field_in_visible_answer",
            "private_thought_tag_in_visible_answer",
        }:
            raise ValueError("research_visible_private_field_forbidden") from error
        decoded = None
    encoded = _canonical(value)
    if len(encoded.encode("utf8")) > 2000000:
        raise ValueError("research_visible_output_too_large")
    return json.loads(encoded)


def public_final_output(content, *, credential=None):
    """Common visible final projection, before answer parsing, for every bound scan.

    Unsafe output keeps its charged native receipt but is never copied into
    the replay journal. This is data retention, not answer validation.
    """
    if not isinstance(content, str) or (credential and credential in content):
        return None
    output = {"content": content, "tool_calls": [], "finish_reason": "stop"}
    try:
        return normalize(output)
    except (ValueError, TypeError, RecursionError):
        return None


def visible_output_sha256(value):
    """Digest the safe canonical output before native receipt persistence."""
    return _sha(_canonical(normalize(value)))


def _source(store, connection, attempt_id):
    row = connection.execute(
        "SELECT * FROM quick_scan_attempt_response WHERE attempt_id=?", (attempt_id,)
    ).fetchone()
    if row is None:
        raise ValueError("research_visible_native_receipt_missing")
    source = store.response_record(connection, row)
    receipt = source["receipt"]
    if (
        not isinstance(receipt.get("search_protocol"), str)
        or receipt.get("actual_model") != source["model_resolved"]
        or not isinstance(receipt.get("http_status_code"), int)
        or not 200 <= receipt["http_status_code"] < 300
    ):
        raise ValueError("research_visible_native_response_invalid")
    from .quick_scan_work_store import api_work_store_sha256 as _sha256

    _sha256(receipt.get("request_payload_sha256"), "research request hash")
    attempt = connection.execute(
        "SELECT phase FROM attempt WHERE attempt_id=?", (attempt_id,)
    ).fetchone()
    budget = connection.execute(
        "SELECT status FROM quick_scan_budget_attempt WHERE budget_attempt_id=?", (attempt_id,)
    ).fetchone()
    terminal = connection.execute(
        "SELECT terminal_outcome FROM quick_scan_budget_terminal WHERE budget_attempt_id=?",
        (attempt_id,),
    ).fetchone()
    if attempt is not None:
        if attempt["phase"] != "response_available":
            raise ValueError("research_visible_native_outcome_unavailable")
    elif budget is None or not (
        budget["status"] == "response_unpriced"
        or (budget["status"] == "settled" and terminal is not None and terminal[0] == "completed")
    ):
        raise ValueError("research_visible_native_outcome_unavailable")
    return source


def persist(store, connection, attempt_id, visible_output):
    from .quick_scan_work_store import WorkConflictError

    output = normalize(visible_output)
    source = _source(store, connection, attempt_id)
    receipt = source["receipt"]
    if receipt.get("research_visible_output_sha256") != visible_output_sha256(output):
        raise WorkConflictError("research visible output differs from native receipt digest")
    payload = {
        "schema": "stockqa.research-visible-output/1.0.0",
        "attempt_id": attempt_id,
        "provider_attempt_id": source["provider_attempt_id"],
        "receipt_sha256": source["receipt_sha256"],
        "request_payload_sha256": receipt["request_payload_sha256"],
        "response_sha256": source["response_sha256"],
        "actual_model": source["model_resolved"],
        "visible_output": output,
    }
    encoded = _canonical(payload)
    prior = connection.execute(
        "SELECT * FROM quick_scan_research_visible_output WHERE attempt_id=?", (attempt_id,)
    ).fetchone()
    if prior is not None:
        read(store, connection, attempt_id)
        if prior["payload_json"] != encoded:
            raise WorkConflictError("research visible output differs from original")
        return
    connection.execute(
        "INSERT INTO quick_scan_research_visible_output VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            attempt_id,
            1,
            source["provider_attempt_id"],
            source["receipt_sha256"],
            receipt["request_payload_sha256"],
            source["response_sha256"],
            source["model_resolved"],
            encoded,
            _sha(encoded),
            store.now(),
        ),
    )


def stage(store, connection, attempt_id, visible_output):
    """Durably retain a safe paid answer before a retryable projection attempt."""
    from .quick_scan_work_store import WorkConflictError

    output = normalize(visible_output)
    source = _source(store, connection, attempt_id)
    output_sha256 = visible_output_sha256(output)
    if source["receipt"].get("research_visible_output_sha256") != output_sha256:
        raise WorkConflictError("research output recovery differs from native receipt digest")
    recovery = {
        "schema": "stockqa.research-visible-output-recovery/1.0.0",
        "attempt_id": attempt_id,
        "provider_attempt_id": source["provider_attempt_id"],
        "receipt_sha256": source["receipt_sha256"],
        "response_sha256": source["response_sha256"],
        "visible_output_sha256": output_sha256,
        "visible_output": output,
    }
    encoded = _canonical(recovery)
    prior = connection.execute(
        "SELECT * FROM quick_scan_research_visible_output_recovery WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    if prior is not None:
        _read_recovery(store, connection, prior)
        if prior["payload_json"] != encoded:
            raise WorkConflictError("research output recovery differs from original")
        return
    connection.execute(
        "INSERT INTO quick_scan_research_visible_output_recovery " "VALUES (?,?,?,?,?,?,?)",
        (
            attempt_id,
            1,
            source["receipt_sha256"],
            source["response_sha256"],
            encoded,
            _sha(encoded),
            store.now(),
        ),
    )


def _public_payload(attempt_id, source, visible_output):
    return {
        "schema": "stockqa.research-visible-output/1.0.0",
        "attempt_id": attempt_id,
        "provider_attempt_id": source["provider_attempt_id"],
        "receipt_sha256": source["receipt_sha256"],
        "request_payload_sha256": source["receipt"]["request_payload_sha256"],
        "response_sha256": source["response_sha256"],
        "actual_model": source["model_resolved"],
        "visible_output": visible_output,
    }


def _read_recovery(store, connection, row) -> dict | None:
    source = _source(store, connection, row["attempt_id"])
    recovery = json.loads(row["payload_json"])
    visible_output = normalize(recovery.get("visible_output"))
    expected = {
        "schema": "stockqa.research-visible-output-recovery/1.0.0",
        "attempt_id": row["attempt_id"],
        "provider_attempt_id": source["provider_attempt_id"],
        "receipt_sha256": source["receipt_sha256"],
        "response_sha256": source["response_sha256"],
        "visible_output_sha256": visible_output_sha256(visible_output),
        "visible_output": visible_output,
    }
    if (
        recovery != expected
        or row["schema_version"] != 1
        or row["receipt_sha256"] != source["receipt_sha256"]
        or row["response_sha256"] != source["response_sha256"]
        or source["receipt"].get("research_visible_output_sha256")
        != expected["visible_output_sha256"]
        or row["payload_json"] != _canonical(recovery)
        or row["payload_sha256"] != _sha(row["payload_json"])
    ):
        raise ValueError("research_visible_output_recovery_provenance_mismatch")
    return dict(_public_payload(row["attempt_id"], source, visible_output))


def read(store, connection, reference) -> dict | None:
    if not isinstance(reference, str) or not reference or len(reference) > 300:
        raise ValueError("research_visible_reference_invalid")
    rows = connection.execute(
        "SELECT * FROM quick_scan_research_visible_output WHERE attempt_id=? OR provider_attempt_id=?",
        (reference, reference),
    ).fetchall()
    recovery_rows = connection.execute(
        "SELECT recovery.* FROM quick_scan_research_visible_output_recovery recovery "
        "JOIN quick_scan_attempt_response source USING(attempt_id) "
        "WHERE recovery.attempt_id=? OR source.provider_attempt_id=?",
        (reference, reference),
    ).fetchall()
    # Resolve uniqueness across both projections. The same attempt may have a
    # canonical projection and its recovery journal; distinct attempts sharing
    # a provider ID remain ambiguous regardless of which projection survived.
    by_attempt: dict[str, dict[str, Any]] = {}
    for row in rows:
        by_attempt.setdefault(row["attempt_id"], {})["output"] = row
    for row in recovery_rows:
        by_attempt.setdefault(row["attempt_id"], {})["recovery"] = row
    if not by_attempt:
        return None
    if len(by_attempt) != 1:
        raise ValueError("research_visible_reference_ambiguous")
    projections: dict[str, Any] = next(iter(by_attempt.values()))
    if "output" in projections and "recovery" in projections:
        visible: dict | None = _read_projected_output(store, connection, projections["output"])
        recovered = _read_recovery(store, connection, projections["recovery"])
        if visible != recovered:
            raise ValueError("research_visible_projection_recovery_mismatch")
        return visible
    if "recovery" in projections:
        return _read_recovery(store, connection, projections["recovery"])
    return _read_projected_output(store, connection, projections["output"])


def _read_projected_output(store, connection, row) -> dict | None:
    source = _source(store, connection, row["attempt_id"])
    payload = json.loads(row["payload_json"])
    if not isinstance(payload, dict):
        raise ValueError("research_visible_output_provenance_mismatch")
    expected = _public_payload(row["attempt_id"], source, normalize(payload.get("visible_output")))
    if (
        payload != expected
        or source["receipt"].get("research_visible_output_sha256")
        != visible_output_sha256(expected["visible_output"])
        or row["schema_version"] != 1
        or row["payload_json"] != _canonical(payload)
        or row["payload_sha256"] != _sha(row["payload_json"])
        or any(
            row[k] != expected[k]
            for k in (
                "provider_attempt_id",
                "receipt_sha256",
                "request_payload_sha256",
                "response_sha256",
                "actual_model",
            )
        )
    ):
        raise ValueError("research_visible_output_provenance_mismatch")
    return payload


from .quick_scan_research_session import api_research_session_ddl_v23

DDL_V23_ADDITIONS += api_research_session_ddl_v23

from .quick_scan_research_intent import api_research_intent_ddl_v23

DDL_V23_ADDITIONS += api_research_intent_ddl_v23


# Versioned persistence-adapter API. These operations run inside the caller's
# original WorkStore transaction so a charged provider receipt and its safe
# visible projection commit or roll back together.
MODULE_API_PROTOCOL = "stockqa.quick-scan-research-output-adapter/1.1.0"
STORE_TRANSACTION_PROTOCOL = "stockqa.quick_scan_work_store/1.4.0"
# Direct SQL + WorkStore.response_record() reads + DDL bundled into the v23 migration.
STORE_TABLE_DEPENDENCIES = frozenset(
    {
        "quick_scan_attempt_response",
        "quick_scan_attempt_resolution",
        "quick_scan_native_search_events",
        "attempt",
        "quick_scan_budget_attempt",
        "quick_scan_budget_terminal",
        "quick_scan_research_visible_output",
        "quick_scan_research_visible_output_recovery",
        "quick_scan_research_session",
        "quick_scan_research_turn",
        "quick_scan_research_intent",
        "work_item",
    }
)
api_research_output_ddl_v23 = DDL_V23_ADDITIONS
api_research_output_ddl_v27 = DDL_V27_ADDITIONS
api_research_output_normalize = normalize
api_research_output_persist = persist
api_research_output_public_final = public_final_output
api_research_output_read = read
api_research_output_stage = stage
api_research_output_visible_sha256 = visible_output_sha256
MODULE_API_EXPORTS = frozenset(
    {
        "api_research_output_ddl_v23",
        "api_research_output_ddl_v27",
        "api_research_output_normalize",
        "api_research_output_persist",
        "api_research_output_public_final",
        "api_research_output_read",
        "api_research_output_stage",
        "api_research_output_visible_sha256",
    }
)
