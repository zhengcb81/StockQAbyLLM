"""Immutable refresh action metadata in the ORIGINAL Q13 database.

No queue, permission authentication, model dispatch or budget ledger lives here.
The trusted native admission service must authenticate consent independently.
"""

import copy
import math
import re

from .quick_scan_result_outbox import (
    canonical_bytes,
    canonical_sha256,
    strict_json_loads,
)

DDL_V16_ADDITIONS = (
    """CREATE TABLE quick_scan_refresh_admission (
        confirmation_id TEXT PRIMARY KEY,
        input_sha256 TEXT NOT NULL,
        input_json TEXT NOT NULL,
        receipt_sha256 TEXT NOT NULL,
        receipt_json TEXT NOT NULL,
        registered_at REAL NOT NULL)""",
    """CREATE TRIGGER quick_scan_refresh_admission_no_update
       BEFORE UPDATE ON quick_scan_refresh_admission
       BEGIN SELECT RAISE(ABORT,'refresh admission is immutable'); END""",
    """CREATE TRIGGER quick_scan_refresh_admission_no_delete
       BEFORE DELETE ON quick_scan_refresh_admission
       BEGIN SELECT RAISE(ABORT,'refresh admission is immutable'); END""",
)
ACTION_FIELDS = {
    "confirmation_id",
    "confirmation_sha256",
    "preview_id",
    "scope_sha256",
    "policy_sha256",
    "execution_inputs_sha256",
}
ITEM_FIELDS = ("work_item_id", "entity_id", "question_id", "generation", "scope", "scope_id")
MAX_BYTES = 8 * 1024 * 1024


def _references(values):
    from .quick_scan_subject_context import SUBJECT_FIELDS

    if not isinstance(values, list) or len(values) > 20000:
        raise ValueError("refresh_admission_references_invalid")
    seen = set()
    for value in values:
        if not isinstance(value, dict) or set(value) != {
            "subject_ref",
            "question_id",
            "field_id",
            "observation_id",
            "payload_sha256",
        }:
            raise ValueError("refresh_admission_reference_invalid")
        ref = value["subject_ref"]
        if (
            not isinstance(ref, dict)
            or set(ref) != SUBJECT_FIELDS
            or ref["scope"] not in {"entity", "security", "segment"}
            or type(ref["analysis_subject_revision"]) is not int
            or ref["analysis_subject_revision"] < 1
            or not isinstance(ref["perimeter_sha256"], str)
            or not re.fullmatch("[a-f0-9]{64}", ref["perimeter_sha256"])
            or any(
                not isinstance(value[k], str) or not 1 <= len(value[k]) <= 160
                for k in ("question_id", "field_id", "observation_id")
            )
            or not isinstance(value["payload_sha256"], str)
            or not re.fullmatch("[a-f0-9]{64}", value["payload_sha256"])
        ):
            raise ValueError("refresh_admission_reference_invalid")
        key = (canonical_sha256(ref), value["question_id"])
        if key in seen:
            raise ValueError("refresh_admission_duplicate_reference")
        seen.add(key)
    return copy.deepcopy(values)


def inputs(action, specifications, run_id, scan_id, references=None):
    if not isinstance(action, dict) or set(action) != ACTION_FIELDS:
        raise ValueError("refresh_admission_action_invalid")
    for name, prefix in [("confirmation_id", "rc[12]_"), ("preview_id", "rv1_")]:
        if not isinstance(action[name], str) or not re.fullmatch(
            prefix + "[a-f0-9]{64}", action[name]
        ):
            raise ValueError("refresh_admission_action_invalid")
    if any(
        not isinstance(action[k], str) or not re.fullmatch("[a-f0-9]{64}", action[k])
        for k in ACTION_FIELDS - {"confirmation_id", "preview_id"}
    ):
        raise ValueError("refresh_admission_action_invalid")
    if any(
        not isinstance(v, str) or not re.fullmatch("[A-Za-z0-9_.-]{1,160}", v)
        for v in (run_id, scan_id)
    ):
        raise ValueError("refresh_admission_run_invalid")
    value = dict(
        action=copy.deepcopy(action),
        specifications=copy.deepcopy(specifications),
        run_id=run_id,
        scan_id=scan_id,
    )
    if references is not None:
        value["references"] = _references(references)
    if (
        not isinstance(specifications, list)
        or len(specifications) > 20000
        or not specifications
        and not value.get("references")
    ):
        raise ValueError("refresh_admission_empty_or_invalid_scope")
    raw = canonical_bytes(value)
    if len(raw) > MAX_BYTES:
        raise ValueError("refresh_admission_input_too_large")
    # Normalize native tuples through the same JSON boundary used on restart.
    return strict_json_loads(raw.decode())


def receipt_for(value, items):
    action = value["action"]
    receipt = dict(
        protocol="stockqa.refresh_action_registration/1.0.0",
        confirmation_id=action["confirmation_id"],
        scope_sha256=action["scope_sha256"],
        policy_sha256=action["policy_sha256"],
        input_sha256=canonical_sha256(value),
        run_id=value["run_id"],
        scan_id=value["scan_id"],
        items=copy.deepcopy(items),
        dispatch_started=False,
        API_requests=0,
        permission_authenticated=False,
    )
    if "references" in value:
        receipt.update(
            protocol="stockqa.refresh_action_registration/1.1.0",
            observation_references=copy.deepcopy(value["references"]),
        )
    return receipt


def read_record(connection, confirmation_id):
    row = connection.execute(
        "SELECT * FROM quick_scan_refresh_admission WHERE confirmation_id=?", (confirmation_id,)
    ).fetchone()
    if row is None:
        return None
    if (
        len(row["input_json"].encode("utf-8")) > MAX_BYTES
        or len(row["receipt_json"].encode("utf-8")) > MAX_BYTES
        or not math.isfinite(row["registered_at"])
        or row["registered_at"] < 0
    ):
        raise ValueError("refresh_admission_record_invalid")
    value = strict_json_loads(row["input_json"])
    receipt = strict_json_loads(row["receipt_json"])
    if (
        not isinstance(value, dict)
        or set(value)
        not in (
            {"action", "specifications", "run_id", "scan_id"},
            {"action", "specifications", "run_id", "scan_id", "references"},
        )
        or inputs(**value) != value
        or value["action"]["confirmation_id"] != row["confirmation_id"]
        or canonical_sha256(value) != row["input_sha256"]
        or canonical_bytes(value).decode() != row["input_json"]
        or canonical_sha256(receipt) != row["receipt_sha256"]
        or canonical_bytes(receipt).decode() != row["receipt_json"]
    ):
        raise ValueError("refresh_admission_record_integrity_invalid")
    specs = value["specifications"]
    items = receipt.get("items")
    if (
        not isinstance(specs, list)
        or len(specs) > 20000
        or not isinstance(items, list)
        or len(items) != len(specs)
    ):
        raise ValueError("refresh_admission_work_mismatch")
    from .quick_scan_owner_refresh_journal import read as read_binding
    from .quick_scan_refresh_batch import SPEC_FIELDS

    expected = []
    seen = set()
    for spec, item in zip(specs, items):
        if (
            not isinstance(spec, dict)
            or set(spec) != SPEC_FIELDS
            or not isinstance(item, dict)
            or set(item) != set(ITEM_FIELDS)
        ):
            raise ValueError("refresh_admission_work_mismatch")
        work = connection.execute(
            "SELECT * FROM work_item WHERE work_item_id=?", (item["work_item_id"],)
        ).fetchone()
        if work is None or item["work_item_id"] in seen:
            raise ValueError("refresh_admission_work_mismatch")
        seen.add(item["work_item_id"])
        for key in SPEC_FIELDS - {"owner_refresh_binding", "source_binding_refs"}:
            if canonical_bytes(work[key]) != canonical_bytes(spec[key]):
                raise ValueError("refresh_admission_work_mismatch")
        if strict_json_loads(work["source_binding_refs_json"]) != sorted(
            spec["source_binding_refs"]
        ):
            raise ValueError("refresh_admission_work_mismatch")
        binding = read_binding(connection, item["work_item_id"])
        if binding is None or binding["binding"] != spec["owner_refresh_binding"]:
            raise ValueError("refresh_admission_binding_mismatch")
        if (
            connection.execute(
                "SELECT 1 FROM work_run_ref WHERE work_item_id=? AND run_id=? AND scan_id=?",
                (item["work_item_id"], value["run_id"], value["scan_id"]),
            ).fetchone()
            is None
        ):
            raise ValueError("refresh_admission_run_ref_missing")
        expected.append({k: work[k] for k in ITEM_FIELDS})
    if canonical_bytes(receipt) != canonical_bytes(receipt_for(value, expected)):
        raise ValueError("refresh_admission_receipt_mismatch")
    return dict(inputs=value, receipt=receipt, registered_at=row["registered_at"])


def read(connection, confirmation_id):
    record = read_record(connection, confirmation_id)
    return None if record is None else copy.deepcopy(record["receipt"])


def publish(connection, value, items, *, registered_at):
    receipt = receipt_for(value, items)
    connection.execute(
        "INSERT INTO quick_scan_refresh_admission VALUES (?,?,?,?,?,?)",
        (
            value["action"]["confirmation_id"],
            canonical_sha256(value),
            canonical_bytes(value).decode(),
            canonical_sha256(receipt),
            canonical_bytes(receipt).decode(),
            registered_at,
        ),
    )
    if read(connection, value["action"]["confirmation_id"]) != receipt:
        raise ValueError("refresh_admission_publication_failed")
    return receipt


def validate_tables(connection):
    for row in connection.execute("SELECT confirmation_id FROM quick_scan_refresh_admission"):
        read_record(connection, row["confirmation_id"])
