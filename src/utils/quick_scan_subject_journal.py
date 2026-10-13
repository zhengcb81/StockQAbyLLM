"""Append-only pre-send receipt in the existing work database; no HTTP."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

from .quick_scan_result_outbox import (
    canonical_bytes,
    canonical_sha256,
    strict_json_loads,
)
from .quick_scan_subject_context import CONTEXT_SCHEMA, validate_subject_binding

DDL_V15_ADDITIONS = (
    """CREATE TABLE quick_scan_subject_send_receipt (
        attempt_id TEXT PRIMARY KEY REFERENCES attempt(attempt_id),
        work_item_id TEXT NOT NULL REFERENCES work_item(work_item_id),
        receipt_id TEXT NOT NULL UNIQUE,
        receipt_sha256 TEXT NOT NULL,
        receipt_json TEXT NOT NULL,
        received_at REAL NOT NULL)""",
    """CREATE TRIGGER quick_scan_subject_send_receipt_no_update
       BEFORE UPDATE ON quick_scan_subject_send_receipt
       BEGIN SELECT RAISE(ABORT,'subject send receipt is immutable'); END""",
    """CREATE TRIGGER quick_scan_subject_send_receipt_no_delete
       BEFORE DELETE ON quick_scan_subject_send_receipt
       BEGIN SELECT RAISE(ABORT,'subject send receipt is immutable'); END""",
)


def bound_context(connection, item):
    row = connection.execute(
        "SELECT c.context_json,c.context_sha256 FROM quick_scan_work_context w "
        "JOIN quick_scan_observation_context c USING(context_sha256) WHERE w.work_item_id=?",
        (item["work_item_id"],),
    ).fetchone()
    if row is None:
        return None
    context = strict_json_loads(row["context_json"])
    if canonical_sha256(context) != row["context_sha256"]:
        raise ValueError("subject_context_hash_mismatch")
    if context.get("schema") != CONTEXT_SCHEMA:
        return None
    from .quick_scan_observation_context import bind_context_to_work_item

    return bind_context_to_work_item(context, work_item=dict(item), question_id=item["question_id"])


def dispatch_request(
    item,
    attempt,
    context,
    *,
    execution_attempt_id,
    provider_actual,
    provider_prompt_sha256,
):
    binding = validate_subject_binding(
        context["subject_binding"],
        context["metadata"],
        identity_revision=item["identity_revision"],
    )
    if (
        not isinstance(execution_attempt_id, str)
        or not execution_attempt_id
        or len(execution_attempt_id) > 160
    ):
        raise ValueError("subject_dispatch_execution_id_required")
    if provider_actual not in {"mimo", "minimax", "deepseek", "openai"}:
        raise ValueError("subject_dispatch_actual_provider_required")
    metadata = context["metadata"]
    request = {
        "protocol": "stockwiki.subject_dispatch_request/1.1.0",
        "subject_ref": copy.deepcopy(binding["subject_ref"]),
        "identity_revision": item["identity_revision"],
        "question_contract": copy.deepcopy(binding["question_contract"]),
        "work_item_id": item["work_item_id"],
        "work_attempt_id": attempt["attempt_id"],
        "work_request_id": attempt["request_cache_key"],
        "execution_attempt_id": execution_attempt_id,
        "provider": provider_actual,
        "route_provider": attempt["provider"],
        "model_requested": attempt["model_requested"],
        "dispatch_identity_snapshot_sha256": context["identity_snapshot_sha256"],
        "dispatch_manifest_sha256": context["manifest_file_sha256"],
        "dispatch_prompt_sha256": provider_prompt_sha256,
        "dispatch_work_prompt_sha256": attempt["prompt_sha256"],
        **{
            k: metadata[k]
            for k in (
                "question_id",
                "field_id",
                "information_cutoff",
                "question_definition_sha256",
                "question_semantic_sha256",
                "module_package_id",
                "module_release_id",
                "question_version",
                "template_version",
                "method_id",
            )
        },
    }

    if metadata["schema_version"] == "1.2.0":
        request.update(
            protocol="stockwiki.subject_dispatch_request/1.2.0",
            execution_profile=dict(
                profile_id="vendor-http-identity-v1",
                observation_schema_version="1.2.0",
                observation_schema_sha256=context["observation_schema_sha256"],
            ),
        )
    return request


def validate_receipt(receipt, expected, *, received_at):
    if (
        not isinstance(receipt, dict)
        or set(receipt)
        != {
            "protocol",
            "receipt_id",
            "store_id",
            "dispatch_request",
            "request_sha256",
            "registered_at",
        }
        or receipt["protocol"] != "stockwiki.subject_dispatch_receipt/1.0.0"
        or not isinstance(receipt["store_id"], str)
        or not receipt["store_id"]
        or len(receipt["store_id"]) > 160
        or receipt["dispatch_request"] != expected
        or receipt["request_sha256"] != canonical_sha256(expected)
        or receipt["receipt_id"]
        != "sdr_" + canonical_sha256({k: v for k, v in receipt.items() if k != "receipt_id"})
        or len(canonical_bytes(receipt)) > 128 * 1024
    ):
        raise ValueError("subject_dispatch_receipt_mismatch")
    try:
        registered = datetime.fromisoformat(receipt["registered_at"].replace("Z", "+00:00"))
        if registered.utcoffset() is None or registered.timestamp() > received_at:
            raise ValueError("subject_dispatch_receipt_after_admission")
    except (TypeError, AttributeError, OverflowError) as error:
        raise ValueError("subject_dispatch_receipt_time_invalid") from error
    return copy.deepcopy(receipt)


def receipt_record(connection, row) -> dict:
    item = connection.execute(
        "SELECT * FROM work_item WHERE work_item_id=?", (row["work_item_id"],)
    ).fetchone()
    attempt = connection.execute(
        "SELECT * FROM attempt WHERE attempt_id=?", (row["attempt_id"],)
    ).fetchone()
    if item is None or attempt is None or attempt["work_item_id"] != item["work_item_id"]:
        raise ValueError("subject_dispatch_receipt_work_mismatch")
    receipt = strict_json_loads(row["receipt_json"])
    context = bound_context(connection, item)
    if context is None:
        raise ValueError("subject_dispatch_receipt_context_missing")
    request = receipt["dispatch_request"]
    expected = dispatch_request(
        item,
        attempt,
        context,
        execution_attempt_id=request["execution_attempt_id"],
        provider_actual=request["provider"],
        provider_prompt_sha256=request["dispatch_prompt_sha256"],
    )
    validate_receipt(receipt, expected, received_at=row["received_at"])
    if (
        receipt["receipt_id"] != row["receipt_id"]
        or canonical_sha256(receipt) != row["receipt_sha256"]
        or attempt["send_intent_at"] is not None
        and row["received_at"] > attempt["send_intent_at"]
    ):
        raise ValueError("subject_dispatch_receipt_columns_mismatch")
    response = connection.execute(
        "SELECT receipt_json FROM quick_scan_attempt_response WHERE attempt_id=?",
        (attempt["attempt_id"],),
    ).fetchone()
    if response is not None:
        guard_response(
            connection,
            attempt["attempt_id"],
            strict_json_loads(response["receipt_json"]),
        )
    return {
        "receipt": receipt,
        "receipt_sha256": row["receipt_sha256"],
        "received_at": row["received_at"],
    }


def guard_send(connection, item, attempt_id):
    from .quick_scan_work_store import WorkConflictError

    context = bound_context(connection, item)
    from .quick_scan_owner_refresh_journal import read as owner_binding

    owner_bound = owner_binding(connection, item["work_item_id"])
    if context is None:
        if (
            owner_bound is not None
            and owner_bound["binding"]["protocol"] == "stockqa.owner_refresh_binding/2.0.0"
        ):
            raise WorkConflictError("subject_dispatch_context2_required")
        return
    row = connection.execute(
        "SELECT * FROM quick_scan_subject_send_receipt WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    if row is None:
        raise WorkConflictError("subject_dispatch_receipt_required")
    receipt_record(connection, row)


def guard_response(connection, attempt_id, receipt):
    row = connection.execute(
        "SELECT receipt_json FROM quick_scan_subject_send_receipt WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    if row is None:
        return
    pre = strict_json_loads(row["receipt_json"])
    request = pre["dispatch_request"]
    if any(
        receipt.get(k) != request[v]
        for k, v in (
            ("attempt_id", "execution_attempt_id"),
            ("provider", "provider"),
            ("prompt_sha256", "dispatch_prompt_sha256"),
            ("requested_model", "model_requested"),
        )
    ):
        raise ValueError("subject_dispatch_response_mismatch")
    try:
        pre_time = datetime.fromisoformat(pre["registered_at"].replace("Z", "+00:00"))
        attempt = connection.execute(
            "SELECT send_intent_at FROM attempt WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if attempt is None or attempt["send_intent_at"] is None:
            raise ValueError("subject_dispatch_send_intent_missing")
        # The released sanitized response intentionally has no started_at.
        # Standard Observation.start comes from this durable pre-POST instant.
        start = datetime.fromtimestamp(attempt["send_intent_at"], tz=timezone.utc)
        end = datetime.fromisoformat(receipt["completed_at"].replace("Z", "+00:00"))
        if start.utcoffset() is None or end.utcoffset() is None or start < pre_time or end < start:
            raise ValueError("subject_dispatch_response_time_mismatch")
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError("subject_dispatch_response_time_missing") from error


def validate_tables(connection):
    for row in connection.execute("SELECT * FROM quick_scan_subject_send_receipt"):
        receipt_record(connection, row)
