"""Ledger-owned reads of immutable transport response and model resolution rows."""

import hashlib
import json
import sqlite3

from src.interfaces.quick_scan_work_values import (
    canonical_native_events as _canonical_native_events,
)
from src.interfaces.quick_scan_work_values import (
    quick_scan_receipt_sha256,
)
from src.interfaces.quick_scan_work_values import (
    sanitize_transport_receipt as _sanitized_receipt,
)
from src.interfaces.quick_scan_work_values import validate_sha256 as _sha256
from src.interfaces.quick_scan_work_values import validate_transport_text as _safe_text


def resolution_record(row: sqlite3.Row) -> dict:
    from src.providers.model_resolution import (
        model_resolution_sha256,
        normalize_model_resolution,
    )

    policy = normalize_model_resolution(json.loads(row["policy_json"]))
    encoded = json.dumps(policy, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if encoded != row["policy_json"] or model_resolution_sha256(policy) != row["policy_sha256"]:
        raise ValueError("frozen model resolution hash mismatch")
    return {**dict(row), "policy": policy}


def response_record(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
    receipt = json.loads(row["receipt_json"])
    encoded = json.dumps(
        _sanitized_receipt(receipt),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    resolution = connection.execute(
        "SELECT * FROM quick_scan_attempt_resolution WHERE attempt_id=?",
        (row["attempt_id"],),
    ).fetchone()
    if resolution is None:
        raise ValueError("durable response has no frozen resolution")
    frozen = resolution_record(resolution)
    mapping = {
        "provider": "provider",
        "protocol": "search_protocol",
        "model_requested": "requested_model",
        "model_resolved": "actual_model",
        "response_id": "response_id",
        "provider_attempt_id": "attempt_id",
        "response_sha256": "response_sha256",
    }
    if (
        encoded != row["receipt_json"]
        or quick_scan_receipt_sha256(receipt) != row["receipt_sha256"]
        or any(row[field] != receipt.get(key) for field, key in mapping.items())
        or receipt.get("model_resolution_sha256") != frozen["policy_sha256"]
        or row["model_requested"] != frozen["model_requested"]
        or receipt.get("response_json_basis") not in {"strict_http_json", "parsed_payload"}
    ):
        raise ValueError("durable response provenance mismatch")
    _sha256(row["response_sha256"], "HTTP canonical JSON hash")
    _safe_text(row["provider_attempt_id"], "provider attempt", maximum=300)
    result = {**dict(row), "receipt": receipt, "model_resolution": frozen["policy"]}
    if connection.execute("PRAGMA user_version").fetchone()[0] >= 12:
        events = connection.execute(
            "SELECT * FROM quick_scan_native_search_events WHERE attempt_id=?",
            (row["attempt_id"],),
        ).fetchone()
        if events is not None:
            canonical = json.dumps(
                _canonical_native_events(json.loads(events["events_json"])),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            if (
                canonical != events["events_json"]
                or hashlib.sha256(canonical.encode()).hexdigest() != events["events_sha256"]
                or events["native_receipt_sha256"] != row["receipt_sha256"]
                or events["response_sha256"] != row["response_sha256"]
            ):
                raise ValueError("durable native event binding mismatch")
            result["native_search_events"] = json.loads(canonical)
    return result
