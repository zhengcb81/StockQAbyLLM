"""Explicit prepaid search quota components of the original Q09 SQLite owner.

No conversion of credits into money: old cash rows retain NULL actuals and
their full reference-cap reservation. Only authenticated successful, durable
MCP protocol receipts can complete the separate quota component.
"""

from __future__ import annotations

import json

UNIT = "glm_coding_plan_credits"
SEARCH_MICROS = 1_200_000
DDL_V21_ADDITIONS: tuple[str, ...] = (
    """CREATE TABLE quick_scan_quota_account (
      account_group TEXT PRIMARY KEY, config_json TEXT NOT NULL, credential_env TEXT NOT NULL UNIQUE,
      max_quota_micros INTEGER NOT NULL CHECK(max_quota_micros>0))""",
    """CREATE TABLE quick_scan_quota_route (
      policy_id TEXT NOT NULL REFERENCES quick_scan_budget_policy(policy_id),
      policy_version TEXT NOT NULL, route_id TEXT NOT NULL, quota_group TEXT NOT NULL,
      account_group TEXT NOT NULL REFERENCES quick_scan_quota_account(account_group),
      search_policy_sha256 TEXT NOT NULL CHECK(length(search_policy_sha256)=64),
      PRIMARY KEY(policy_id,policy_version,route_id,search_policy_sha256))""",
    """CREATE TABLE quick_scan_quota_reservation (
      budget_attempt_id TEXT PRIMARY KEY REFERENCES quick_scan_budget_attempt(budget_attempt_id),
      account_group TEXT NOT NULL REFERENCES quick_scan_quota_account(account_group),
      category TEXT NOT NULL CHECK(category IN ('search_tool','protocol_control')),
      reserved_quota_micros INTEGER NOT NULL CHECK(reserved_quota_micros>=0))""",
    """CREATE TABLE quick_scan_quota_proof (
      budget_attempt_id TEXT PRIMARY KEY REFERENCES quick_scan_quota_reservation(budget_attempt_id),
      receipt_sha256 TEXT NOT NULL CHECK(length(receipt_sha256)=64),
      actual_quota_micros INTEGER NOT NULL CHECK(actual_quota_micros>=0),
      recorded_at REAL NOT NULL)""",
)
DDL_V21_ADDITIONS += tuple(
    f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
    "BEGIN SELECT RAISE(ABORT,'quota components are immutable'); END"
    for table in (
        "quick_scan_quota_account",
        "quick_scan_quota_route",
        "quick_scan_quota_reservation",
        "quick_scan_quota_proof",
    )
    for action in ("UPDATE", "DELETE")
)
DDL_V21_ADDITIONS += (
    """CREATE TRIGGER quick_scan_quota_reservation_insert_guard BEFORE INSERT ON quick_scan_quota_reservation
    BEGIN SELECT RAISE(ABORT,'invalid native quota reservation') WHERE NOT EXISTS (
      SELECT 1 FROM quick_scan_budget_attempt b JOIN quick_scan_quota_route r
      ON b.policy_id=r.policy_id AND b.policy_version=r.policy_version AND b.route_id=r.route_id
      WHERE b.budget_attempt_id=NEW.budget_attempt_id AND b.status='in_flight'
      AND b.work_attempt_id IS NULL AND b.model_requested='external_retrieval_v1'
      AND b.provider='zai_mcp_streamable' AND b.quota_group=r.quota_group
      AND r.account_group=NEW.account_group)
      OR (NEW.category='protocol_control' AND NEW.reserved_quota_micros!=0)
      OR (NEW.category='search_tool' AND NEW.reserved_quota_micros!=1200000); END""",
    """CREATE TRIGGER quick_scan_quota_proof_insert_guard BEFORE INSERT ON quick_scan_quota_proof
    BEGIN SELECT RAISE(ABORT,'invalid native quota proof') WHERE NOT EXISTS (
      SELECT 1 FROM quick_scan_quota_reservation q JOIN quick_scan_budget_attempt b USING(budget_attempt_id)
      WHERE q.budget_attempt_id=NEW.budget_attempt_id AND b.status='response_unpriced'
      AND b.actual_cost_micros IS NULL AND b.in_flight=0 AND b.http_status_code BETWEEN 200 AND 299
      AND NEW.actual_quota_micros=q.reserved_quota_micros AND (
        (q.category='protocol_control' AND EXISTS (
          SELECT 1 FROM quick_scan_mcp_stage s JOIN quick_scan_mcp_dispatch d USING(stage_id)
          JOIN quick_scan_mcp_result r USING(stage_id) WHERE s.budget_attempt_id=b.budget_attempt_id
          AND r.receipt_sha256=NEW.receipt_sha256 AND json_extract(r.receipt_json,'$.parse_status')='ok'))
        OR (q.category='search_tool' AND EXISTS (
          SELECT 1 FROM quick_scan_external_operation o JOIN quick_scan_external_dispatch d USING(operation_id)
          JOIN quick_scan_external_result r USING(operation_id) JOIN quick_scan_mcp_search_binding m USING(operation_id)
          WHERE o.budget_attempt_id=b.budget_attempt_id AND r.receipt_sha256=NEW.receipt_sha256
          AND json_extract(r.receipt_json,'$.parse_status') IN ('ok','empty'))))); END""",
)


def validate_config(config):
    from src.utils.quick_scan_work_store import _safe, _safe_text

    if not isinstance(config, dict) or set(config) != {
        "schema",
        "enabled",
        "unit",
        "account_group",
        "max_quota_micros",
        "search_quota_micros",
        "source_ref",
        "entitlement_ref",
        "control_accounting",
        "unknown_usage_action",
    }:
        raise ValueError("invalid prepaid quota config fields")
    if (
        config["schema"] != "stockqa.prepaid_search_quota/1.0.0"
        or config["enabled"] is not True
        or config["unit"] != UNIT
        or type(config["max_quota_micros"]) is not int
        or not SEARCH_MICROS <= config["max_quota_micros"] <= 9_000_000_000_000
        or type(config["search_quota_micros"]) is not int
        or config["search_quota_micros"] != SEARCH_MICROS
        or config["control_accounting"] != "protocol_category_no_tool_invocation"
        or config["unknown_usage_action"] != "retain_reservation_and_pause"
    ):
        raise ValueError("invalid prepaid quota semantics")
    _safe(config["account_group"], "account_group")
    for key in ("source_ref", "entitlement_ref"):
        _safe_text(config[key], key, maximum=300)
    return json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def configure(store, budget_policy, search_policy):
    """Freeze opt-in 1.3 route plus entitlement in the existing native owner."""
    from src.config.quick_scan_search_policy import assert_policy_frozen
    from src.utils.quick_scan_work_store import BudgetPolicyConflict

    assert_policy_frozen(search_policy)
    routes = [
        r
        for r in search_policy.routes
        if "prepaid_quota" in r and r["route_id"] in search_policy.admitted
    ]
    if not routes:
        return
    if search_policy.schema_id != "stockqa.quick_scan_search_policy/1.3.0":
        raise ValueError("prepaid quota requires explicit policy 1.3")
    normalized = store.configure_quick_scan_budget(budget_policy)
    with store._transaction() as connection:
        for route in routes:
            config = route["prepaid_quota"]
            encoded = validate_config(config)
            identity = normalized["route_identity"].get(route["route_id"])
            if (
                route["kind"] != "zai_mcp_streamable"
                or identity is None
                or identity["quota_group"] != route["dispatch"]["quota_group"]
                or identity["provider_config_ref"] != route["kind"]
            ):
                raise ValueError("quota route does not match native budget")
            row = connection.execute(
                "SELECT config_json FROM quick_scan_quota_account WHERE account_group=?",
                (config["account_group"],),
            ).fetchone()
            if row is not None and row[0] != encoded:
                raise BudgetPolicyConflict("prepaid account configuration changed")
            credential = connection.execute(
                "SELECT account_group FROM quick_scan_quota_account WHERE credential_env=?",
                (route["credential_env"],),
            ).fetchone()
            if credential is not None and credential[0] != config["account_group"]:
                raise BudgetPolicyConflict(
                    "credential quota account cannot be renamed to reset usage"
                )
            connection.execute(
                "INSERT OR IGNORE INTO quick_scan_quota_account VALUES (?,?,?,?)",
                (
                    config["account_group"],
                    encoded,
                    route["credential_env"],
                    config["max_quota_micros"],
                ),
            )
            values = (
                normalized["policy_id"],
                normalized["policy_version"],
                route["route_id"],
                identity["quota_group"],
                config["account_group"],
                search_policy.policy_sha256,
            )
            before = connection.execute(
                "SELECT * FROM quick_scan_quota_route WHERE policy_id=? AND policy_version=? AND route_id=? AND search_policy_sha256=?",
                (*values[:3], values[5]),
            ).fetchone()
            if before is not None and tuple(before) != values:
                raise BudgetPolicyConflict("prepaid route configuration changed")
            connection.execute(
                "INSERT OR IGNORE INTO quick_scan_quota_route VALUES (?,?,?,?,?,?)", values
            )


def reserve_tx(connection, budget_id, context, category):
    from src.utils.quick_scan_work_store import BudgetAdmissionError

    route = connection.execute(
        "SELECT * FROM quick_scan_quota_route WHERE policy_id=? AND policy_version=? AND route_id=? AND search_policy_sha256=?",
        (
            context["budget"]["policy_id"],
            context["budget"]["policy_version"],
            context["route"]["route_id"],
            context["search_policy_sha256"],
        ),
    ).fetchone()
    if route is None:
        other = connection.execute(
            "SELECT 1 FROM quick_scan_quota_route WHERE policy_id=? AND policy_version=? AND route_id=?",
            (
                context["budget"]["policy_id"],
                context["budget"]["policy_version"],
                context["route"]["route_id"],
            ),
        ).fetchone()
        if other is not None:
            raise ValueError("quota search policy is not frozen for this owner")
        return
    if (
        context["route"]["provider"] != "zai_mcp_streamable"
        or context["route"]["quota_group"] != route["quota_group"]
        or context["search_policy_sha256"] != route["search_policy_sha256"]
    ):
        raise ValueError("quota intent frozen identity mismatch")
    amount = SEARCH_MICROS if category == "search_tool" else 0
    spent = connection.execute(
        "SELECT COALESCE(SUM(COALESCE(p.actual_quota_micros,r.reserved_quota_micros)),0) FROM quick_scan_quota_reservation r LEFT JOIN quick_scan_quota_proof p USING(budget_attempt_id) WHERE r.account_group=?",
        (route["account_group"],),
    ).fetchone()[0]
    maximum = connection.execute(
        "SELECT max_quota_micros FROM quick_scan_quota_account WHERE account_group=?",
        (route["account_group"],),
    ).fetchone()[0]
    if spent + amount > maximum:
        raise BudgetAdmissionError("prepaid_quota_limit")
    connection.execute(
        "INSERT INTO quick_scan_quota_reservation VALUES (?,?,?,?)",
        (budget_id, route["account_group"], category, amount),
    )


def record_tx(connection, budget_id, *, receipt, receipt_sha256, now, category):
    """Called only inside result insertion transaction after canonical validation."""
    reservation = connection.execute(
        "SELECT * FROM quick_scan_quota_reservation WHERE budget_attempt_id=?", (budget_id,)
    ).fetchone()
    if reservation is None:
        return
    if reservation["category"] != category:
        raise ValueError("quota category mismatch")
    budget = connection.execute(
        "SELECT * FROM quick_scan_budget_attempt WHERE budget_attempt_id=?", (budget_id,)
    ).fetchone()
    if budget["actual_cost_micros"] is not None:
        raise ValueError("prepaid quota cannot manufacture cash actual")
    if (
        budget["status"] != "response_unpriced"
        or receipt["outcome"] != "response_available"
        or receipt.get("parse_status") not in {"ok", "empty"}
        or receipt.get("http_request_count") != 1
    ):
        return  # unknown/rejected/malformed requests remain a hard fence
    if category == "search_tool":
        binding = connection.execute(
            "SELECT 1 FROM quick_scan_external_operation o JOIN quick_scan_external_dispatch d USING(operation_id) JOIN quick_scan_mcp_search_binding m USING(operation_id) WHERE o.budget_attempt_id=?",
            (budget_id,),
        ).fetchone()
    else:
        binding = connection.execute(
            "SELECT 1 FROM quick_scan_mcp_stage s JOIN quick_scan_mcp_dispatch d USING(stage_id) WHERE s.budget_attempt_id=?",
            (budget_id,),
        ).fetchone()
    if binding is None:
        raise ValueError("quota proof lacks actual native dispatch and MCP binding")
    connection.execute(
        "INSERT INTO quick_scan_quota_proof VALUES (?,?,?,?)",
        (budget_id, receipt_sha256, reservation["reserved_quota_micros"], now),
    )


def projection(connection, budget_id):
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='quick_scan_quota_reservation'"
        ).fetchone()
        is None
    ):
        if connection.execute("PRAGMA user_version").fetchone()[0] < 21:
            return None  # Reading an exact validated old schema during migration.
        raise ValueError("current quota table missing")
    row = connection.execute(
        "SELECT r.category,r.account_group,p.actual_quota_micros,p.receipt_sha256,b.actual_cost_micros,b.status AS cash_budget_state FROM quick_scan_quota_reservation r JOIN quick_scan_quota_proof p USING(budget_attempt_id) JOIN quick_scan_budget_attempt b USING(budget_attempt_id) WHERE r.budget_attempt_id=? AND b.status IN ('response_unpriced','settled') AND b.in_flight=0",
        (budget_id,),
    ).fetchone()
    if row is None:
        return None
    _validate_proof(connection, budget_id, row)
    settled = row["cash_budget_state"] == "settled"
    amount = row["actual_cost_micros"]
    from decimal import Decimal

    return {
        "state": "quota_accounted_cash_settled" if settled else "quota_accounted_cash_reserved",
        "cash_actual": str(Decimal(amount) / Decimal(1000000)) if settled else None,
        "quota_unit": UNIT,
        **dict(row),
        "provider_bill_reconciled": settled,
    }


def _validate_proof(connection, budget_id, proof):
    """Check original route/work/context/receipt, including after database reopen."""
    from src.utils.quick_scan_external_journal import (
        _canonical,
        _digest,
        strict_search_json,
    )

    control = proof["category"] == "protocol_control"
    if control:
        raw = connection.execute(
            "SELECT s.work_item_id,s.metadata_json,s.stage,r.receipt_json,r.receipt_sha256,r.recorded_at FROM quick_scan_mcp_stage s JOIN quick_scan_mcp_dispatch d USING(stage_id) JOIN quick_scan_mcp_result r USING(stage_id) WHERE s.budget_attempt_id=?",
            (budget_id,),
        ).fetchone()
    else:
        raw = connection.execute(
            "SELECT o.work_item_id,o.context_json AS metadata_json,r.receipt_json,r.receipt_sha256,r.recorded_at FROM quick_scan_external_operation o JOIN quick_scan_external_dispatch d USING(operation_id) JOIN quick_scan_external_result r USING(operation_id) JOIN quick_scan_mcp_search_binding m USING(operation_id) WHERE o.budget_attempt_id=?",
            (budget_id,),
        ).fetchone()
    if raw is None:
        raise ValueError("quota proof lacks original dispatch/result")
    context = strict_search_json(raw["metadata_json"])
    if control:
        context = context["context"]
    receipt = strict_search_json(raw["receipt_json"])
    budget = connection.execute(
        "SELECT * FROM quick_scan_budget_attempt WHERE budget_attempt_id=?", (budget_id,)
    ).fetchone()
    configured = connection.execute(
        "SELECT * FROM quick_scan_quota_route WHERE policy_id=? AND policy_version=? AND route_id=? AND search_policy_sha256=?",
        (
            budget["policy_id"],
            budget["policy_version"],
            budget["route_id"],
            context["search_policy_sha256"],
        ),
    ).fetchone()
    if (
        configured is None
        or configured["account_group"] != proof["account_group"]
        or configured["quota_group"] != budget["quota_group"]
        or context["work_item_id"] != raw["work_item_id"]
        or context["route"]["provider"] != "zai_mcp_streamable"
        or context["route"]["quota_group"] != budget["quota_group"]
        or context["route"]["route_id"] != budget["route_id"]
        or budget["provider"] != "zai_mcp_streamable"
        or budget["model_requested"] != "external_retrieval_v1"
        or budget["work_attempt_id"] is not None
        or _canonical(receipt) != raw["receipt_json"]
        or _digest(receipt) != proof["receipt_sha256"]
        or raw["receipt_sha256"] != proof["receipt_sha256"]
        or receipt["attempt_id"] != budget_id
        or receipt["route_kind"] != "zai_mcp_streamable"
        or receipt["route_id"] != budget["route_id"]
        or receipt["outcome"] != "response_available"
        or receipt["http_status_code"] != budget["http_status_code"]
        or receipt.get("parse_status") not in ({"ok"} if control else {"ok", "empty"})
        or proof["actual_quota_micros"] != (0 if control else SEARCH_MICROS)
    ):
        raise ValueError("quota proof frozen owner/receipt mismatch")


def validate_tables(connection):
    for row in connection.execute("SELECT budget_attempt_id FROM quick_scan_quota_proof"):
        if projection(connection, row[0]) is None:
            raise ValueError("quota proof budget state mismatch")


def accounted(record):
    return record["budget"]["status"] == "settled" or record.get("quota_accounting") is not None
