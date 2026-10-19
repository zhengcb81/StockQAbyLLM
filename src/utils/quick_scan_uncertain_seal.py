"""Explicit research-only isolation of unknown costs, never reconciliation.

The caller supplies reviewed immutable task/input and stopped-process evidence.
This API records that review; it neither authenticates an owner nor grants
production dispatch rights. Native cost/request/route guards remain authoritative.
"""

import hashlib
import json
import re
import sqlite3
from typing import Optional

DDL_V20_ADDITIONS = (
    (
        """CREATE TABLE quick_scan_research_task (
        policy_id TEXT NOT NULL REFERENCES quick_scan_budget_policy(policy_id),
        task_id TEXT NOT NULL, task_sha256 TEXT NOT NULL CHECK(length(task_sha256)=64),
        PRIMARY KEY(policy_id,task_id), UNIQUE(policy_id,task_sha256))""",
        """CREATE TABLE quick_scan_research_attempt (
        budget_attempt_id TEXT PRIMARY KEY REFERENCES quick_scan_budget_attempt(budget_attempt_id),
        policy_id TEXT NOT NULL, task_id TEXT NOT NULL,
        FOREIGN KEY(policy_id,task_id) REFERENCES quick_scan_research_task(policy_id,task_id))""",
        """CREATE TABLE quick_scan_uncertain_seal (
        budget_attempt_id TEXT PRIMARY KEY REFERENCES quick_scan_budget_attempt(budget_attempt_id),
        policy_id TEXT NOT NULL, task_id TEXT NOT NULL,
        audit_json TEXT NOT NULL CHECK(json_valid(audit_json)), sealed_at REAL NOT NULL,
        FOREIGN KEY(policy_id,task_id) REFERENCES quick_scan_research_task(policy_id,task_id))""",
        """CREATE TABLE quick_scan_research_budget_amendment (
        amendment_id TEXT PRIMARY KEY,
        policy_id TEXT NOT NULL REFERENCES quick_scan_budget_policy(policy_id),
        old_snapshot_sha256 TEXT NOT NULL, new_snapshot_sha256 TEXT NOT NULL,
        old_projection_json TEXT NOT NULL CHECK(json_valid(old_projection_json)),
        new_projection_json TEXT NOT NULL CHECK(json_valid(new_projection_json)),
        audit_json TEXT NOT NULL CHECK(json_valid(audit_json)), amended_at REAL NOT NULL,
        UNIQUE(policy_id,old_snapshot_sha256))""",
        """CREATE TRIGGER quick_scan_research_attempt_insert_guard
       BEFORE INSERT ON quick_scan_research_attempt
       BEGIN SELECT RAISE(ABORT,'research attempt binding mismatch') WHERE NOT EXISTS (
           SELECT 1 FROM quick_scan_budget_attempt b WHERE b.budget_attempt_id=NEW.budget_attempt_id
           AND b.policy_id=NEW.policy_id AND b.work_attempt_id IS NULL AND b.status='in_flight'); END""",
        """CREATE TRIGGER quick_scan_uncertain_seal_insert_guard
       BEFORE INSERT ON quick_scan_uncertain_seal
       BEGIN SELECT RAISE(ABORT,'uncertain seal requires matching unknown dispatch') WHERE NOT EXISTS (
           SELECT 1 FROM quick_scan_budget_attempt b JOIN quick_scan_budget_policy p USING(policy_id)
           WHERE b.budget_attempt_id=NEW.budget_attempt_id AND b.policy_id=NEW.policy_id
           AND b.work_attempt_id IS NULL AND b.budget_attempt_id GLOB 'DISPATCH_*'
           AND b.status='outcome_uncertain' AND b.in_flight=1 AND b.actual_cost_micros IS NULL
           AND b.reconciled_at IS NULL AND b.http_status_code IS NULL
           AND json_extract(NEW.audit_json,'$.no_retry') IS 1
           AND json_extract(NEW.audit_json,'$.original_process_stopped') IS 1
           AND json_extract(NEW.audit_json,'$.reserved_cost_micros')=b.reserved_cost_micros
           AND json_extract(NEW.audit_json,'$.policy_snapshot_sha256')=p.policy_snapshot_sha256
           AND NOT EXISTS (SELECT 1 FROM quick_scan_research_attempt a
               WHERE a.budget_attempt_id=b.budget_attempt_id AND a.task_id!=NEW.task_id)); END""",
    )
    + tuple(
        f"CREATE TRIGGER {table}_no_{action} BEFORE {action.upper()} ON {table} "
        "BEGIN SELECT RAISE(ABORT,'research isolation records are immutable'); END"
        for table in (
            "quick_scan_research_task",
            "quick_scan_research_attempt",
            "quick_scan_uncertain_seal",
            "quick_scan_research_budget_amendment",
        )
        for action in ("update", "delete")
    )
)


def normalize_independent_task(value: object) -> Optional[dict]:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"task_id", "task_sha256"}:
        raise ValueError("independent_task requires exact task identity")
    if not isinstance(value["task_id"], str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,160}", value["task_id"]
    ):
        raise ValueError("invalid independent task id")
    if not isinstance(value["task_sha256"], str) or not re.fullmatch(
        r"[a-f0-9]{64}", value["task_sha256"]
    ):
        raise ValueError("invalid independent task sha256")
    return dict(value)


def register_task(connection: sqlite3.Connection, policy_id: str, task: dict) -> None:
    previous = connection.execute(
        "SELECT task_id,task_sha256 FROM quick_scan_research_task WHERE policy_id=? "
        "AND (task_id=? OR task_sha256=?)",
        (policy_id, task["task_id"], task["task_sha256"]),
    ).fetchall()
    if previous and (len(previous) != 1 or dict(previous[0]) != task):
        raise ValueError("independent task identity conflicts")
    connection.execute(
        "INSERT OR IGNORE INTO quick_scan_research_task VALUES (?,?,?)",
        (policy_id, task["task_id"], task["task_sha256"]),
    )


def continuation_exclusion(
    connection: sqlite3.Connection, policy_id: str, task: Optional[dict]
) -> str:
    """Validate all unresolved requests; return SQL exclusion for this dispatch."""
    from src.utils.quick_scan_work_store import BudgetAdmissionError

    if task is None:
        return "0"
    register_task(connection, policy_id, task)
    sealed_task = connection.execute(
        "SELECT 1 FROM quick_scan_uncertain_seal WHERE policy_id=? AND task_id=?",
        (policy_id, task["task_id"]),
    ).fetchone()
    if sealed_task:
        raise BudgetAdmissionError("research_task_sealed_no_retry")
    return (
        "budget_attempt_id IN (SELECT s.budget_attempt_id FROM quick_scan_uncertain_seal s "
        "JOIN quick_scan_budget_attempt b USING(budget_attempt_id) "
        "WHERE b.work_attempt_id IS NULL AND b.status='outcome_uncertain' "
        "AND b.actual_cost_micros IS NULL AND b.http_status_code IS NULL)"
    )


def seal_uncertain(store, budget_attempt_id: str, *, source_task: dict, audit: dict) -> dict:
    """Append a review with exact CAS facts; no original ledger row is updated."""
    from src.utils.quick_scan_work_store import WorkConflictError

    task = normalize_independent_task(source_task)
    required = {
        "operator_ref",
        "input_sha256",
        "events_sha256",
        "review_reason",
        "no_retry",
        "original_process_stopped",
        "policy_snapshot_sha256",
        "reserved_cost_micros",
    }
    if task is None or not isinstance(audit, dict) or set(audit) != required:
        raise ValueError("exact source task and audit required")
    if audit["no_retry"] is not True or audit["original_process_stopped"] is not True:
        raise ValueError("explicit no retry and stopped process review required")
    for field in ("input_sha256", "events_sha256", "policy_snapshot_sha256"):
        if not isinstance(audit[field], str) or not re.fullmatch(r"[a-f0-9]{64}", audit[field]):
            raise ValueError("invalid audit sha256")
    for field in ("operator_ref", "review_reason"):
        if (
            not isinstance(audit[field], str)
            or not 1 <= len(audit[field]) <= 500
            or any(ord(c) < 32 for c in audit[field])
        ):
            raise ValueError("invalid bounded audit review")
    if type(audit["reserved_cost_micros"]) is not int or audit["reserved_cost_micros"] <= 0:
        raise ValueError("invalid expected reservation")
    encoded = json.dumps(audit, sort_keys=True, separators=(",", ":"), allow_nan=False)
    with store._transaction() as connection:
        row = connection.execute(
            "SELECT a.*,p.policy_snapshot_sha256 FROM quick_scan_budget_attempt a "
            "JOIN quick_scan_budget_policy p USING(policy_id) WHERE budget_attempt_id=?",
            (budget_attempt_id,),
        ).fetchone()
        if row is None:
            raise KeyError("unknown budget attempt")
        previous = connection.execute(
            "SELECT * FROM quick_scan_uncertain_seal WHERE budget_attempt_id=?",
            (budget_attempt_id,),
        ).fetchone()
        if previous is not None:
            if previous["task_id"] == task["task_id"] and previous["audit_json"] == encoded:
                register_task(connection, row["policy_id"], task)
                return dict(previous)
            raise WorkConflictError("uncertain seal review is immutable")
        if (
            row["work_attempt_id"] is not None
            or not budget_attempt_id.startswith("DISPATCH_")
            or row["status"] != "outcome_uncertain"
            or row["in_flight"] != 1
            or row["actual_cost_micros"] is not None
            or row["reconciled_at"] is not None
            or row["http_status_code"] is not None
            or row["reserved_cost_micros"] != audit["reserved_cost_micros"]
            or row["policy_snapshot_sha256"] != audit["policy_snapshot_sha256"]
        ):
            raise WorkConflictError("only matching budget-only unknown dispatch may be sealed")
        registered = connection.execute(
            "SELECT task_id FROM quick_scan_research_attempt WHERE budget_attempt_id=?",
            (budget_attempt_id,),
        ).fetchone()
        if registered is not None and registered["task_id"] != task["task_id"]:
            raise WorkConflictError("source task does not match admitted dispatch")
        register_task(connection, row["policy_id"], task)
        connection.execute(
            "INSERT INTO quick_scan_uncertain_seal VALUES (?,?,?,?,?)",
            (budget_attempt_id, row["policy_id"], task["task_id"], encoded, store._now()),
        )
        return dict(
            connection.execute(
                "SELECT * FROM quick_scan_uncertain_seal WHERE budget_attempt_id=?",
                (budget_attempt_id,),
            ).fetchone()
        )


def amend_research_budget(
    store, *, old_policy: dict, new_policy: dict, expected_policy_snapshot_sha256: str, audit: dict
) -> dict:
    """Explicit append-only reviewed policy expansion; never edits any attempt.

    Both full policies must reproduce the native projection. Only this isolated
    API can advance the policy over retained sealed unknowns; configure keeps
    its original active/unreconciled guard. Audit is caller-reviewed evidence,
    not a substitute for a production owner authority.
    """
    from src.utils.quick_scan_work_store import (
        BudgetPolicyConflict,
        _budget_policy_projection,
    )

    old = _budget_policy_projection(old_policy)
    new = _budget_policy_projection(new_policy)
    if old["policy_snapshot_sha256"] != expected_policy_snapshot_sha256:
        raise BudgetPolicyConflict("research amendment expected snapshot mismatch")
    if not isinstance(audit, dict) or set(audit) != {
        "operator_ref",
        "review_reason",
        "input_sha256",
    }:
        raise ValueError("exact amendment audit required")
    for field in ("operator_ref", "review_reason"):
        if (
            not isinstance(audit[field], str)
            or not 1 <= len(audit[field]) <= 500
            or any(ord(c) < 32 for c in audit[field])
        ):
            raise ValueError("invalid amendment audit text")
    if not isinstance(audit["input_sha256"], str) or not re.fullmatch(
        r"[a-f0-9]{64}", audit["input_sha256"]
    ):
        raise ValueError("invalid amendment input sha256")
    immutable = {
        "policy_id",
        "currency",
        "pricing_basis",
        "max_cost_per_attempt_micros",
        "max_in_flight_total",
    }
    if (
        any(old[key] != new[key] for key in immutable)
        or old["policy_version"] == new["policy_version"]
    ):
        raise BudgetPolicyConflict("research amendment changes fixed policy identity or dispatch")
    if new["max_cost_micros"] < old["max_cost_micros"] or new["max_requests"] < old["max_requests"]:
        raise BudgetPolicyConflict("research amendment may only increase caps")
    for key in ("group_limits_json", "route_limits_json"):
        before, after = json.loads(old[key]), json.loads(new[key])
        if any(after.get(item) != value for item, value in before.items()):
            raise BudgetPolicyConflict("research amendment changes existing route or group")
    if any(new["route_identity"].get(key) != value for key, value in old["route_identity"].items()):
        raise BudgetPolicyConflict("research amendment changes existing route identity")

    # Extra policy fields ignored by projection must also remain unchanged.
    def without(value, keys):
        return {key: val for key, val in value.items() if key not in keys}

    if (
        without(old_policy, {"policy_version", "budget", "cost_policy", "routes", "quota_groups"})
        != without(
            new_policy, {"policy_version", "budget", "cost_policy", "routes", "quota_groups"}
        )
        or without(old_policy["budget"], {"max_cost", "max_requests"})
        != without(new_policy["budget"], {"max_cost", "max_requests"})
        or without(old_policy["cost_policy"], {"pricing_ref"})
        != without(new_policy["cost_policy"], {"pricing_ref"})
    ):
        raise BudgetPolicyConflict("research amendment changes unapproved policy fields")
    for key in ("routes", "quota_groups"):
        after = {row["id"]: row for row in new_policy[key]}
        if any(after.get(row["id"]) != row for row in old_policy[key]):
            raise BudgetPolicyConflict(
                "research amendment changes existing route or group metadata"
            )

    def canonical(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)

    old_json, new_json, audit_json = canonical(old), canonical(new), canonical(audit)
    amendment_id = "AMEND_" + hashlib.sha256(canonical([old, new, audit]).encode()).hexdigest()
    with store._transaction() as connection:
        current = connection.execute(
            "SELECT * FROM quick_scan_budget_policy WHERE policy_id=?", (old["policy_id"],)
        ).fetchone()
        if current is None:
            raise BudgetPolicyConflict("research amendment requires existing policy")
        previous = connection.execute(
            "SELECT * FROM quick_scan_research_budget_amendment WHERE amendment_id=?",
            (amendment_id,),
        ).fetchone()
        if (
            previous is not None
            and current["policy_snapshot_sha256"] == new["policy_snapshot_sha256"]
        ):
            return dict(previous)
        columns = {
            key: ("quota_group_limits_json" if key == "group_limits_json" else key)
            for key in old
            if key != "route_identity"
        }
        if any(current[column] != old[key] for key, column in columns.items()):
            raise BudgetPolicyConflict("research amendment current snapshot stale")
        if connection.execute("SELECT 1 FROM work_item LIMIT 1").fetchone() is not None:
            raise BudgetPolicyConflict("research amendment cannot affect owner work")
        unresolved = connection.execute(
            "SELECT 1 FROM quick_scan_budget_attempt b LEFT JOIN quick_scan_uncertain_seal s USING(budget_attempt_id) "
            "WHERE b.policy_id=? AND (b.work_attempt_id IS NOT NULL OR "
            "(b.status!='settled' AND NOT (b.status='outcome_uncertain' AND b.in_flight=1 "
            "AND b.actual_cost_micros IS NULL AND b.http_status_code IS NULL AND s.budget_attempt_id IS NOT NULL))) LIMIT 1",
            (old["policy_id"],),
        ).fetchone()
        if unresolved is not None:
            raise BudgetPolicyConflict(
                "research amendment requires all unknown sealed and no active dispatch"
            )
        connection.execute(
            "INSERT INTO quick_scan_research_budget_amendment VALUES (?,?,?,?,?,?,?,?)",
            (
                amendment_id,
                old["policy_id"],
                old["policy_snapshot_sha256"],
                new["policy_snapshot_sha256"],
                old_json,
                new_json,
                audit_json,
                store._now(),
            ),
        )
        assignments = ",".join(column + "=?" for column in columns.values())
        connection.execute(
            "UPDATE quick_scan_budget_policy SET "  # nosec B608
            + assignments
            + ",updated_at=? WHERE policy_id=?",
            tuple(new[key] for key in columns) + (store._now(), old["policy_id"]),
        )
        return dict(
            connection.execute(
                "SELECT * FROM quick_scan_research_budget_amendment WHERE amendment_id=?",
                (amendment_id,),
            ).fetchone()
        )
