"""Research-only finite continuation admission in the existing Q09 owner.

Never grants a production authority or reconciles cash. Original dispatched
slots (settled OR unknown) remain excluded. Only a reviewed, sealed source task
permits a distinct task to send the exact previously undispatched difference.
MCP stages are deliberately outside this research continuation contract.
"""

from __future__ import annotations

import json
import re
import unicodedata
from contextlib import closing

DDL_V22_ADDITIONS = (
    """CREATE TABLE quick_scan_external_research_admission (
        policy_id TEXT NOT NULL,task_id TEXT NOT NULL,task_sha256 TEXT NOT NULL,
        registration_json TEXT NOT NULL,registration_sha256 TEXT NOT NULL,
        created_at REAL NOT NULL,PRIMARY KEY(policy_id,task_id),
        FOREIGN KEY(policy_id,task_id) REFERENCES quick_scan_research_task(policy_id,task_id))""",
    """CREATE TRIGGER quick_scan_external_research_admission_no_update BEFORE UPDATE ON quick_scan_external_research_admission
        BEGIN SELECT RAISE(ABORT,'external research admission is immutable'); END""",
    """CREATE TRIGGER quick_scan_external_research_admission_no_delete BEFORE DELETE ON quick_scan_external_research_admission
        BEGIN SELECT RAISE(ABORT,'external research admission is immutable'); END""",
)


def _sha(value):
    from src.utils.quick_scan_external_journal import _digest

    return _digest(value)


def _canonical(value):
    from src.utils.quick_scan_external_journal import _canonical as encode

    return encode(value)


def _task(value):
    from src.utils.quick_scan_uncertain_seal import normalize_independent_task

    task = normalize_independent_task(value)
    if task is None:
        raise ValueError("explicit external research task required")
    return task


def slot_key(context):
    """Research semantic search identity, independent of relabellable provenance.

    A new policy hash, work UUID, route ID, query ID, question/routing fingerprint,
    generation, requested count or manifest label cannot make a spent query new.
    This guard is scoped to this same native allocation, not all future research.
    """
    work = context["work_fingerprint"]
    plan = context["plan"]
    query = " ".join(unicodedata.normalize("NFKC", plan["query"]).split()).casefold()
    return _sha(
        {
            "entity_id": work["entity_id"],
            "scope": work["scope"],
            "scope_id": work["scope_id"],
            "query": query,
            "provider": context["route"]["provider"],
            "locale": plan["locale"],
            "policy_id": context["budget"]["policy_id"],
        }
    )


def _row(connection, policy_id, task):
    task = _task(task)
    row = connection.execute(
        "SELECT * FROM quick_scan_external_research_admission WHERE policy_id=? AND task_id=?",
        (policy_id, task["task_id"]),
    ).fetchone()
    if row is None:
        raise ValueError("external research continuation is not registered")
    registration = json.loads(row["registration_json"])
    if (
        row["task_sha256"] != task["task_sha256"]
        or row["registration_sha256"] != _sha(registration)
        or row["registration_json"] != _canonical(registration)
        or task["task_sha256"] != _sha(registration)
    ):
        raise ValueError("external research registration hash mismatch")
    registered = connection.execute(
        "SELECT task_sha256 FROM quick_scan_research_task WHERE policy_id=? AND task_id=?",
        (policy_id, task["task_id"]),
    ).fetchone()
    if registered is None or registered[0] != task["task_sha256"]:
        raise ValueError("external research task binding mismatch")
    return registration


def _sealed_source_ids(connection, policy_id, source_task):
    source_task = _task(source_task)
    rows = connection.execute(
        "SELECT b.*,s.audit_json FROM quick_scan_uncertain_seal s "
        "JOIN quick_scan_budget_attempt b USING(budget_attempt_id) "
        "JOIN quick_scan_research_task t ON t.policy_id=s.policy_id AND t.task_id=s.task_id "
        "WHERE s.policy_id=? AND s.task_id=? AND t.task_sha256=?",
        (policy_id, source_task["task_id"], source_task["task_sha256"]),
    ).fetchall()
    ids = set()
    for row in rows:
        audit = json.loads(row["audit_json"])
        if (
            row["work_attempt_id"] is None
            and row["status"] == "outcome_uncertain"
            and row["actual_cost_micros"] is None
            and row["http_status_code"] is None
            and row["reserved_cost_micros"] == audit["reserved_cost_micros"]
            and audit["no_retry"] is True
            and audit["original_process_stopped"] is True
        ):
            operation = connection.execute(
                "SELECT o.operation_id FROM quick_scan_external_operation o "
                "JOIN quick_scan_external_dispatch d USING(operation_id) WHERE o.budget_attempt_id=?",
                (row["budget_attempt_id"],),
            ).fetchone()
            if operation is not None:
                ids.add(row["budget_attempt_id"])
    return ids


def register(store, *, policy, independent_task, registration):
    from src.utils.quick_scan_external_journal import _request_context
    from src.utils.quick_scan_uncertain_seal import register_task
    from src.utils.quick_scan_work_store import Lease, _budget_policy_projection

    task = _task(independent_task)
    if not isinstance(registration, dict) or set(registration) != {
        "schema",
        "operator_ref",
        "production_owner_verified",
        "source_task",
        "planned_slots",
        "expected_allowed_slot_keys",
        "original_matrix_proof",
    }:
        raise ValueError("external research registration fields invalid")
    if (
        registration["schema"] != "stockqa.external_research_continuation/1.0.0"
        or registration["production_owner_verified"] is not False
    ):
        raise ValueError("external continuation is research only")
    if (
        not isinstance(registration["operator_ref"], str)
        or not 1 <= len(registration["operator_ref"]) <= 300
    ):
        raise ValueError("explicit operator review required")
    source = _task(registration["source_task"])
    proof = registration["original_matrix_proof"]
    if not isinstance(proof, dict) or set(proof) != {
        "classification",
        "source_config_raw_sha256",
        "source_sdk_lock_raw_sha256",
        "research_binding_raw_sha256",
        "source_task_input_sha256",
    }:
        raise ValueError("original research matrix proof required")
    if proof["classification"] != "operator_reviewed_research_not_preexisting_native_matrix":
        raise ValueError("research matrix must not claim historical native registration")
    if any(
        not isinstance(proof[key], str) or re.fullmatch(r"[a-f0-9]{64}", proof[key]) is None
        for key in proof
        if key != "classification"
    ):
        raise ValueError("invalid original matrix proof hash")
    if proof["source_task_input_sha256"] != source["task_sha256"]:
        raise ValueError("original task input proof mismatch")
    if source["task_id"] == task["task_id"] or task["task_sha256"] != _sha(registration):
        raise ValueError("continuation must be a distinct immutable task")
    if (
        not isinstance(registration["planned_slots"], list)
        or not 1 <= len(registration["planned_slots"]) <= 200
    ):
        raise ValueError("bounded original planned slots required")
    normalized = _budget_policy_projection(policy)
    with store._transaction() as connection:
        prior = connection.execute(
            "SELECT registration_sha256 FROM quick_scan_external_research_admission WHERE policy_id=? AND task_id=?",
            (normalized["policy_id"], task["task_id"]),
        ).fetchone()
        if prior is not None:
            old = _row(connection, normalized["policy_id"], task)
            if old != registration:
                raise ValueError("continuation registration conflicts")
            return {
                "task": task,
                "allowed_slot_keys": old["expected_allowed_slot_keys"],
                "production_owner_verified": False,
                "existing": True,
            }
        current = connection.execute(
            "SELECT policy_snapshot_sha256 FROM quick_scan_budget_policy WHERE policy_id=?",
            (normalized["policy_id"],),
        ).fetchone()
        if current is None or current[0] != normalized["policy_snapshot_sha256"]:
            raise ValueError("continuation cannot change the native policy")
        sealed = _sealed_source_ids(connection, normalized["policy_id"], source)
        if not sealed:
            raise ValueError("source task has no eligible sealed external uncertainty")
        for unresolved in connection.execute(
            "SELECT budget_attempt_id FROM quick_scan_budget_attempt WHERE policy_id=? AND status!='settled'",
            (normalized["policy_id"],),
        ):
            if unresolved[0] not in sealed:
                raise ValueError("unsealed, unpriced or native uncertainty blocks registration")
        known = {}
        for row in connection.execute(
            "SELECT o.context_json,o.budget_attempt_id,d.sent_at FROM quick_scan_external_operation o "
            "LEFT JOIN quick_scan_external_dispatch d USING(operation_id) JOIN quick_scan_budget_attempt b USING(budget_attempt_id) WHERE b.policy_id=?",
            (normalized["policy_id"],),
        ):
            context = json.loads(row["context_json"])
            key = slot_key(context)
            if key in known:
                raise ValueError("duplicate physical intent in existing research history")
            known[key] = dict(row)
        planned = {}
        allowed = []
        for slot in registration["planned_slots"]:
            if (
                set(slot) != {"work_item_id", "dispatch_enabled", "request"}
                or type(slot["dispatch_enabled"]) is not bool
            ):
                raise ValueError("planned slot fields invalid")
            item = store._item(connection, slot["work_item_id"])
            context = _request_context(
                store,
                connection,
                slot["work_item_id"],
                Lease(item["lease_token"], item["lease_epoch"]),
                normalized=normalized,
                **slot["request"],
            )
            key = slot_key(context)
            if key in planned:
                raise ValueError("duplicate planned physical slot")
            planned[key] = context
            if key not in known and slot["dispatch_enabled"]:
                if context["route"]["provider"] in {"zai_mcp_streamable", "deepseek_fetch"}:
                    raise ValueError("research continuation only admits direct search slots")
                route = normalized["route_identity"].get(context["route"]["route_id"])
                if route is None or route["provider_config_ref"] != context["route"]["provider"]:
                    raise ValueError("continuation route is not in native policy")
                allowed.append(key)
        if not set(known) <= set(planned):
            raise ValueError("original matrix omits existing operations")
        if sorted(allowed) != sorted(registration["expected_allowed_slot_keys"]) or not allowed:
            raise ValueError("expected continuation difference is not exact")
        for key, row in known.items():
            budget = connection.execute(
                "SELECT status FROM quick_scan_budget_attempt WHERE budget_attempt_id=?",
                (row["budget_attempt_id"],),
            ).fetchone()
            if budget[0] != "settled" and row["budget_attempt_id"] not in sealed:
                raise ValueError("unsealed or unpriced original operation blocks continuation")
        register_task(connection, normalized["policy_id"], task)
        previous = connection.execute(
            "SELECT registration_sha256 FROM quick_scan_external_research_admission WHERE policy_id=? AND task_id=?",
            (normalized["policy_id"], task["task_id"]),
        ).fetchone()
        if previous is not None and previous[0] != task["task_sha256"]:
            raise ValueError("continuation registration conflicts")
        connection.execute(
            "INSERT OR IGNORE INTO quick_scan_external_research_admission VALUES (?,?,?,?,?,?)",
            (
                normalized["policy_id"],
                task["task_id"],
                task["task_sha256"],
                _canonical(registration),
                _sha(registration),
                store._now(),
            ),
        )
    return {
        "task": task,
        "allowed_slot_keys": sorted(allowed),
        "excluded_existing_slots": len(known),
        "production_owner_verified": False,
    }


def validate_context(connection, context, task, *, existing_operation_id=None):
    task = _task(task)
    registration = _row(connection, context["budget"]["policy_id"], task)
    key = slot_key(context)
    if key not in registration["expected_allowed_slot_keys"]:
        raise ValueError("slot is outside exact undispatched difference")
    matched = False
    for planned in registration["planned_slots"]:
        request = planned["request"]
        expected_route = {
            name: request[name]
            for name in ("route_id", "provider", "quota_group", "adapter_version", "endpoint")
        }
        if (
            planned["work_item_id"] == context["work_item_id"]
            and request["plan"] == context["plan"]
            and expected_route == context["route"]
            and request["search_policy_sha256"] == context["search_policy_sha256"]
        ):
            matched = True
            break
    if not matched:
        raise ValueError("continuation request differs from frozen original slot")
    if context["route"]["provider"] in {"zai_mcp_streamable", "deepseek_fetch"}:
        raise ValueError("continuation cannot bypass MCP or Fetch guards")
    for row in connection.execute(
        "SELECT operation_id,context_json FROM quick_scan_external_operation"
    ):
        if (
            row["operation_id"] != existing_operation_id
            and slot_key(json.loads(row["context_json"])) == key
        ):
            raise ValueError("physical search slot already has an operation; no replay")
    return task


def may_exclude(connection, context, budget_attempt_id):
    task = context.get("research_task")
    if task is None:
        return False
    registration = _row(connection, context["budget"]["policy_id"], task)
    return budget_attempt_id in _sealed_source_ids(
        connection, context["budget"]["policy_id"], registration["source_task"]
    )


def validate_tables(connection):
    for row in connection.execute(
        "SELECT policy_id,task_id,task_sha256 FROM quick_scan_external_research_admission"
    ):
        _row(
            connection,
            row["policy_id"],
            {"task_id": row["task_id"], "task_sha256": row["task_sha256"]},
        )
