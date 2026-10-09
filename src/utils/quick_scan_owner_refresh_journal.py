"""Registered v14 immutable owner refresh bindings in the existing work ledger.

This is request lineage, not a second fee ledger or source authentication.
Only a configured first-party owner transport may construct a binding for run.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any

DDL_V14_ADDITIONS = (
    """CREATE TABLE quick_scan_owner_refresh_binding (
        work_item_id TEXT PRIMARY KEY REFERENCES work_item(work_item_id),
        binding_key TEXT NOT NULL UNIQUE,
        binding_json TEXT NOT NULL,
        binding_sha256 TEXT NOT NULL
    )""",
    """CREATE TRIGGER quick_scan_owner_refresh_no_update BEFORE UPDATE ON quick_scan_owner_refresh_binding
        BEGIN SELECT RAISE(ABORT,'owner refresh binding is immutable'); END""",
    """CREATE TRIGGER quick_scan_owner_refresh_no_delete BEFORE DELETE ON quick_scan_owner_refresh_binding
        BEGIN SELECT RAISE(ABORT,'owner refresh binding is immutable'); END""",
)
_KEYS = {
    "protocol",
    "subject_key",
    "perimeter_sha256",
    "decision_id",
    "anchor_version",
    "manifest_raw_sha256",
    "provider",
    "model",
    "information_cutoff",
    "request_identity_key",
    "target_generation",
    "question_id",
    "scope",
    "scope_id",
    "entity_id",
    "identity_revision",
}


def canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize(binding: dict, item: dict) -> tuple[str, str]:
    if not isinstance(binding, dict) or set(binding) != _KEYS:
        raise ValueError("owner_refresh_binding_invalid")
    if binding["protocol"] != "stockqa.owner_refresh_binding/1.0.0":
        raise ValueError("owner_refresh_binding_invalid")
    if any(
        not isinstance(binding[k], str) or not binding[k] or len(binding[k]) > 256
        for k in _KEYS - {"anchor_version", "target_generation", "identity_revision"}
    ):
        raise ValueError("owner_refresh_binding_invalid")
    for key in ("anchor_version", "target_generation", "identity_revision"):
        if type(binding[key]) is not int or binding[key] < 1:
            raise ValueError("owner_refresh_binding_invalid")
    for key in ("perimeter_sha256", "manifest_raw_sha256"):
        if not re.fullmatch(r"[a-f0-9]{64}", binding[key]):
            raise ValueError("owner_refresh_binding_invalid")
    if not re.fullmatch(r"route_[a-f0-9]{64}", binding["decision_id"]):
        raise ValueError("owner_refresh_binding_invalid")
    from datetime import date

    date.fromisoformat(binding["information_cutoff"])
    for key in ("entity_id", "question_id", "scope", "scope_id", "identity_revision"):
        if binding[key] != item[key]:
            raise ValueError("owner_refresh_binding_item_mismatch")
    if binding["target_generation"] != item["generation"]:
        raise ValueError("owner_refresh_binding_item_mismatch")
    text = canonical(binding)
    return text, sha(text)


def read(connection: sqlite3.Connection, work_item_id: str) -> dict | None:
    row = connection.execute(
        "SELECT * FROM quick_scan_owner_refresh_binding WHERE work_item_id=?", (work_item_id,)
    ).fetchone()
    if row is None:
        return None
    try:
        binding = json.loads(row["binding_json"])
        item = dict(
            connection.execute(
                "SELECT * FROM work_item WHERE work_item_id=?", (work_item_id,)
            ).fetchone()
        )
        text, digest = normalize(binding, item)
    except (ValueError, TypeError, KeyError) as error:
        raise ValueError("owner_refresh_binding_corrupted") from error
    if (
        text != row["binding_json"]
        or digest != row["binding_sha256"]
        or row["binding_key"] != "ORF_" + digest
    ):
        raise ValueError("owner_refresh_binding_corrupted")
    return {"binding_key": row["binding_key"], "binding": binding, "binding_sha256": digest}


def guard_unresolved(
    connection: sqlite3.Connection, item: dict, *, exclude_work_id: str | None = None
) -> None:
    # No LIMIT and no dependency on a read-only planner or current generation.
    # Legacy rows have no subject attestation: conservatively reconcile them
    # rather than guessing they belong to another reporting perimeter.
    row = connection.execute(
        "SELECT work_item_id FROM work_item WHERE entity_id=? AND question_id=? AND scope=? AND scope_id=? "
        "AND status IN ('pending','leased','uncertain','result_ready') AND (? IS NULL OR work_item_id!=?) LIMIT 1",
        (
            item["entity_id"],
            item["question_id"],
            item["scope"],
            item["scope_id"],
            exclude_work_id,
            exclude_work_id,
        ),
    ).fetchone()
    if row is not None:
        from src.utils.quick_scan_work_store import WorkConflictError

        raise WorkConflictError("owner_refresh_prior_work_unresolved")


def bind(connection: sqlite3.Connection, item: dict, binding: dict, *, new_item: bool) -> None:
    from src.utils.quick_scan_work_store import WorkConflictError

    text, digest = normalize(binding, item)
    old = read(connection, item["work_item_id"])
    if old is not None:
        if old["binding_sha256"] != digest:
            raise WorkConflictError("owner_refresh_binding_conflict")
        return
    # Do not attach owner authority to a previously created legacy work item.
    if not new_item:
        raise WorkConflictError("owner_refresh_legacy_work_requires_reconciliation")
    guard_unresolved(connection, item, exclude_work_id=item["work_item_id"])
    connection.execute(
        "INSERT INTO quick_scan_owner_refresh_binding VALUES (?,?,?,?)",
        (item["work_item_id"], "ORF_" + digest, text, digest),
    )


def guard_send(connection: sqlite3.Connection, item: dict) -> None:
    if read(connection, item["work_item_id"]) is not None:
        guard_unresolved(connection, item, exclude_work_id=item["work_item_id"])
    else:
        # A legacy worker must not undercut a bound owner's send in the same
        # logical scope. Legacy-only databases retain their historical behavior.
        bound = connection.execute(
            "SELECT 1 FROM quick_scan_owner_refresh_binding b JOIN work_item w USING(work_item_id) "
            "WHERE w.entity_id=? AND w.question_id=? AND w.scope=? AND w.scope_id=? "
            "AND w.work_item_id!=? AND w.status IN ('pending','leased','uncertain','result_ready') LIMIT 1",
            (
                item["entity_id"],
                item["question_id"],
                item["scope"],
                item["scope_id"],
                item["work_item_id"],
            ),
        ).fetchone()
        if bound is not None:
            from src.utils.quick_scan_work_store import WorkConflictError

            raise WorkConflictError("owner_refresh_prior_work_unresolved")


def validate_tables(connection: sqlite3.Connection) -> None:
    for row in connection.execute("SELECT work_item_id FROM quick_scan_owner_refresh_binding"):
        read(connection, row["work_item_id"])
