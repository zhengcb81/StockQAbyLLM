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
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize(binding: dict, item: dict) -> tuple[str, str]:
    subject_required = isinstance(binding, dict) and binding.get("protocol") in {
        "stockqa.owner_refresh_binding/2.0.0",
        "stockqa.owner_refresh_binding/3.0.0",
        "stockqa.owner_refresh_binding/4.0.0",
    }
    keys = _KEYS | {"subject_context_schema"} if subject_required else _KEYS
    controlled = isinstance(binding, dict) and binding.get("protocol") in {
        "stockqa.owner_refresh_binding/3.0.0",
        "stockqa.owner_refresh_binding/4.0.0",
    }
    if controlled:
        keys = keys | {"owner_control"}
    if not isinstance(binding, dict) or set(binding) != keys:
        raise ValueError("owner_refresh_binding_invalid")
    if binding["protocol"] not in {
        "stockqa.owner_refresh_binding/1.0.0",
        "stockqa.owner_refresh_binding/2.0.0",
        "stockqa.owner_refresh_binding/3.0.0",
        "stockqa.owner_refresh_binding/4.0.0",
    }:
        raise ValueError("owner_refresh_binding_invalid")
    if (
        subject_required
        and binding["subject_context_schema"] != "stockqa.quick_scan_observation_context/2.0.0"
    ):
        raise ValueError("owner_refresh_subject_context_version_invalid")
    if any(
        not isinstance(binding[k], str) or not binding[k] or len(binding[k]) > 256
        for k in keys
        - {"anchor_version", "target_generation", "identity_revision", "owner_control"}
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
    if controlled and (
        not isinstance(binding["owner_control"], dict)
        or ("comparison_target" in binding["owner_control"])
        != (binding["protocol"] == "stockqa.owner_refresh_binding/4.0.0")
    ):
        raise ValueError("owner_refresh_binding_invalid")
    text = canonical(binding)
    return text, sha(text)


def read(connection: sqlite3.Connection, work_item_id: str) -> dict | None:
    row = connection.execute(
        "SELECT * FROM quick_scan_owner_refresh_binding WHERE work_item_id=?",
        (work_item_id,),
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
    if binding["protocol"] in {
        "stockqa.owner_refresh_binding/3.0.0",
        "stockqa.owner_refresh_binding/4.0.0",
    }:
        native_control(connection, item, binding)
    return {
        "binding_key": row["binding_key"],
        "binding": binding,
        "binding_sha256": digest,
    }


def native_control(connection, item, binding, *, now=None, require_ready=False):
    """Integrity/readiness from the original immutable decision, not JSON authority.

    The trusted admission/runner separately rechecks current owner permissions.
    This function runs in their existing ledger transaction and never migrates,
    allocates work, rewrites an old result or grants permission by itself.
    """
    from datetime import datetime

    from .quick_scan_known_outcome_journal import _read
    from .quick_scan_work_control import project_owner_control
    from .quick_scan_work_store import QuickScanWorkStore, WorkConflictError

    control = binding["owner_control"]
    try:
        row = connection.execute(
            "SELECT * FROM quick_scan_owner_work_decision WHERE decision_id=?",
            (control["decision_id"],),
        ).fetchone()
        if row is None:
            raise ValueError("native_decision_missing")
        document = _read(QuickScanWorkStore, connection, row)
        target_version = binding["protocol"] == "stockqa.owner_refresh_binding/4.0.0"
        checkpoint_recovery = document["request"].get("purpose") == "retry_unusable_checkpoint"
        if (
            document["protocol"]
            != "stockqa.owner_work_decision/"
            + ("1.1.0" if target_version else "1.2.0" if checkpoint_recovery else "1.0.0")
            or target_version
            and checkpoint_recovery
        ):
            raise ValueError("native_control_dialect_mismatch")
        source = dict(QuickScanWorkStore._item(connection, row["work_item_id"]))
        subject = document["request"]["physical"]["subject_binding"]["subject_ref"]
        if (
            any(item[k] != source[k] for k in ("entity_id", "question_id", "scope", "scope_id"))
            or binding["subject_key"]
            != subject["analysis_subject_id"] + "@" + str(subject["analysis_subject_revision"])
            or binding["perimeter_sha256"] != subject["perimeter_sha256"]
            or binding["identity_revision"]
            != document["request"]["physical"]["subject_binding"]["identity_revision"]
        ):
            raise ValueError("native_scope_mismatch")
        read_clock = datetime.fromisoformat(
            document["decided_at"].replace("Z", "+00:00")
        ).timestamp()
        projection = project_owner_control(
            document,
            source_work=source,
            subject_ref=subject,
            provider=binding["provider"],
            model=binding["model"],
            now=now if require_ready else read_clock,
            cohort_id=control["cohort_id"],
        )
        if (
            canonical(projection["owner_control"]) != canonical(control)
            or item["generation"] != control["target_generation"]
        ):
            raise ValueError("native_projection_mismatch")
    except (ValueError, TypeError, KeyError) as error:
        raise WorkConflictError("owner_refresh_native_control_mismatch") from error
    if require_ready and (
        projection["decision"] != "dispatch_new_generation"
        or projection["generation"] != item["generation"]
    ):
        raise WorkConflictError("owner_refresh_control_not_ready")
    return source


def checkpoint_successor(
    connection: sqlite3.Connection, source, *, before_generation: int | None = None
) -> dict | None:
    """Follow only an exact native checkpoint-retry and failed-answer-retry chain.

    Each hop verifies the original immutable decision and target binding. A
    failed target is not delivery; exact package/consumer ACK remains separate.
    The write guard may stop before its incoming not-yet-bound generation.
    """
    if source["status"] != "result_ready":
        return None
    target = _native_retry_successor(connection, source, "retry_unusable_checkpoint")
    if target is None or (
        before_generation is not None and target["generation"] >= before_generation
    ):
        return None
    while target["status"] == "failed":
        successor = _native_retry_successor(connection, target, "retry_unusable_answer")
        if successor is None or (
            before_generation is not None and successor["generation"] >= before_generation
        ):
            break
        target = successor
    return target


def _native_retry_successor(connection: sqlite3.Connection, source, purpose: str) -> dict | None:
    """Read the exact native successor of an immutable unusable checkpoint.

    Shared by read projection and write/send guards. This does not claim a
    delivery or grant authority; the caller must separately require exact ACK.
    """
    from .quick_scan_known_outcome_journal import projection
    from .quick_scan_result_outbox import canonical_sha256
    from .quick_scan_work_store import QuickScanWorkStore

    if connection.execute("PRAGMA user_version").fetchone()[0] < 19:
        return None
    decision = projection(QuickScanWorkStore, connection, source["work_item_id"])
    if (
        decision is None
        or decision["request"].get("purpose") != purpose
        or decision["request"]["target_generation"] != source["generation"] + 1
    ):
        return None
    target = connection.execute(
        "SELECT * FROM work_item WHERE entity_id=? AND question_id=? AND scope=? AND scope_id=? AND generation=?",
        (
            source["entity_id"],
            source["question_id"],
            source["scope"],
            source["scope_id"],
            decision["request"]["target_generation"],
        ),
    ).fetchone()
    if target is None:
        return None
    original = read(connection, target["work_item_id"])
    if original is None:
        return None
    control = original["binding"].get("owner_control")
    expected = {
        "decision_id": decision["decision_id"],
        "document_sha256": canonical_sha256(decision),
        "source_work_item_id": source["work_item_id"],
        "source_generation": source["generation"],
        "target_generation": target["generation"],
        "confirmation_id": decision["grant"]["confirmation_id"],
        "purpose": purpose,
    }
    if not isinstance(control, dict) or any(control.get(k) != v for k, v in expected.items()):
        return None
    return dict(target)


def delivered_checkpoint_successor(connection: sqlite3.Connection, source) -> dict | None:
    """A status label alone cannot retire the old source's unresolved guard."""
    from .quick_scan_work_store import QuickScanWorkStore

    target = checkpoint_successor(connection, source)
    if target is None or target["status"] != "delivered":
        return None
    row = QuickScanWorkStore._delivery_row(connection, target["work_item_id"])
    if row is None:
        return None
    delivery = QuickScanWorkStore._result_delivery_record(row, connection=connection)
    if (
        delivery["state"] != "delivered"
        or delivery["ack"] is None
        or delivery["consumer_binding"] is None
        or delivery["package"] is None
    ):
        return None
    return target


def guard_unresolved(
    connection: sqlite3.Connection,
    item: dict,
    *,
    exclude_work_id: str | None = None,
    binding: dict | None = None,
    now=None,
) -> None:
    # No LIMIT and no dependency on a read-only planner or current generation.
    # Legacy rows have no subject attestation: conservatively reconcile them
    # rather than guessing they belong to another reporting perimeter.
    comparison_source = None
    source = None
    if binding is not None and binding["protocol"] in {
        "stockqa.owner_refresh_binding/3.0.0",
        "stockqa.owner_refresh_binding/4.0.0",
    }:
        source = native_control(connection, item, binding, now=now, require_ready=True)
        if binding["owner_control"]["purpose"] in {"comparison", "retry_unusable_checkpoint"}:
            comparison_source = source["work_item_id"]
    rows = connection.execute(
        "SELECT * FROM work_item WHERE entity_id=? AND question_id=? AND scope=? AND scope_id=? "
        "AND status IN ('pending','leased','uncertain','result_ready') AND (? IS NULL OR work_item_id!=?)",
        (
            item["entity_id"],
            item["question_id"],
            item["scope"],
            item["scope_id"],
            exclude_work_id,
            exclude_work_id,
        ),
    ).fetchall()
    for row in rows:
        if row["work_item_id"] == comparison_source and row["status"] == "result_ready":
            continue
        if (
            binding is not None
            and binding["protocol"] == "stockqa.owner_refresh_binding/3.0.0"
            and binding["owner_control"]["purpose"] == "retry_unusable_answer"
            and source is not None
            and source["status"] == "failed"
        ):
            predecessor = checkpoint_successor(
                connection, row, before_generation=item["generation"]
            )
            if predecessor is not None and predecessor["work_item_id"] == source["work_item_id"]:
                # The current owner's exact retry of this failed descendant
                # resolves only its own immutable checkpoint ancestor.
                continue
        if binding is not None and binding["protocol"] in {
            "stockqa.owner_refresh_binding/1.0.0",
            "stockqa.owner_refresh_binding/2.0.0",
        }:
            successor = delivered_checkpoint_successor(connection, row)
            if successor is not None and item["generation"] > successor["generation"]:
                # Preserve the source forever; its exact, genuinely ACKed native
                # successor resolved this one historical block. Current owner
                # consent, freshness, budgets and all other work still apply.
                continue
        from src.utils.quick_scan_work_store import WorkConflictError

        raise WorkConflictError("owner_refresh_prior_work_unresolved")


def bind(
    connection: sqlite3.Connection,
    item: dict,
    binding: dict,
    *,
    new_item: bool,
    now=None,
) -> None:
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
    guard_unresolved(
        connection, item, exclude_work_id=item["work_item_id"], binding=binding, now=now
    )
    connection.execute(
        "INSERT INTO quick_scan_owner_refresh_binding VALUES (?,?,?,?)",
        (item["work_item_id"], "ORF_" + digest, text, digest),
    )


def guard_send(connection: sqlite3.Connection, item: dict, *, now=None) -> None:
    bound = read(connection, item["work_item_id"])
    if bound is not None:
        guard_unresolved(
            connection,
            item,
            exclude_work_id=item["work_item_id"],
            binding=bound["binding"],
            now=now,
        )
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
