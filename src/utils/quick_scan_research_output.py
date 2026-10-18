"""Versioned visible research output in the original native work SQLite.

This is a receipt-bound replay artifact, never a provider conversation. Private
reasoning and request messages cannot enter the strict visible-only projection.
"""

from __future__ import annotations

import hashlib
import json

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
    encoded = _canonical(value)
    if len(encoded.encode("utf8")) > 2000000:
        raise ValueError("research_visible_output_too_large")
    return json.loads(encoded)


def visible_output_sha256(value):
    """Digest the safe canonical output before native receipt persistence."""
    return _sha(_canonical(normalize(value)))


def _source(store, connection, attempt_id):
    row = connection.execute(
        "SELECT * FROM quick_scan_attempt_response WHERE attempt_id=?", (attempt_id,)
    ).fetchone()
    if row is None:
        raise ValueError("research_visible_native_receipt_missing")
    source = store._response_record(connection, row)
    receipt = source["receipt"]
    if (
        receipt.get("search_protocol") not in {"deepseek_chat_completions", "mimo_chat_completions"}
        or receipt.get("actual_model") != receipt.get("requested_model")
        or not isinstance(receipt.get("http_status_code"), int)
        or not 200 <= receipt["http_status_code"] < 300
    ):
        raise ValueError("research_visible_native_response_invalid")
    from .quick_scan_work_store import _sha256

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
            store._now(),
        ),
    )


def read(store, connection, reference) -> dict | None:
    if not isinstance(reference, str) or not reference or len(reference) > 300:
        raise ValueError("research_visible_reference_invalid")
    rows = connection.execute(
        "SELECT * FROM quick_scan_research_visible_output WHERE attempt_id=? OR provider_attempt_id=?",
        (reference, reference),
    ).fetchall()
    if not rows:
        return None
    # A shared SDK ID can describe multiple pack members. Only the physical
    # output row is stored; ambiguous IDs across physical sends must not guess.
    if len(rows) != 1:
        raise ValueError("research_visible_reference_ambiguous")
    row = rows[0]
    source = _source(store, connection, row["attempt_id"])
    payload = json.loads(row["payload_json"])
    if not isinstance(payload, dict):
        raise ValueError("research_visible_output_provenance_mismatch")
    expected = {
        "schema": "stockqa.research-visible-output/1.0.0",
        "attempt_id": row["attempt_id"],
        "provider_attempt_id": source["provider_attempt_id"],
        "receipt_sha256": source["receipt_sha256"],
        "request_payload_sha256": source["receipt"]["request_payload_sha256"],
        "response_sha256": source["response_sha256"],
        "actual_model": source["model_resolved"],
        "visible_output": normalize(payload.get("visible_output")),
    }
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


from .quick_scan_research_session import DDL_RESEARCH_SESSION

DDL_V23_ADDITIONS += DDL_RESEARCH_SESSION

from .quick_scan_research_intent import DDL_RESEARCH_INTENT

DDL_V23_ADDITIONS += DDL_RESEARCH_INTENT
