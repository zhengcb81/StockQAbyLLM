"""Private Z.ai MCP control journal, using the existing Q09 budget owner.

Each control POST is independently admitted and consumed exactly once. This is
not a generic MCP client or a second ledger. Session data remains private runtime
state; no control response is financial evidence or an LLM answer attempt.
"""

from __future__ import annotations

from contextlib import closing
from typing import TYPE_CHECKING, Any

from src.utils.quick_scan_external_journal import (
    _WORK_KEYS,
    ENDPOINTS,
    MODEL_BUDGET_LABEL,
    _assert_no_unknown_scope,
    _cache_key,
    _canonical,
    _digest,
    _hash,
    _plan,
    _receipt,
    _request_context,
    _resumable_unsent,
    strict_search_json,
)

if TYPE_CHECKING:
    import sqlite3

    from src.utils.quick_scan_work_store import Lease, QuickScanWorkStore

PROTOCOL_VERSION = "2025-03-26"
SUPPORTED_PROTOCOLS = frozenset({"2025-03-26", "2024-11-05"})
APPROVED_SEARCH_TOOLS = frozenset({"webSearchPrime", "web_search_prime"})
STAGES = ("initialize", "initialized", "discovery")
DDL_V13_ADDITIONS = (
    """CREATE TABLE quick_scan_mcp_stage (
        stage_id TEXT PRIMARY KEY,
        sequence_key TEXT NOT NULL CHECK(length(sequence_key)=64),
        stage TEXT NOT NULL CHECK(stage IN ('initialize','initialized','discovery')),
        work_item_id TEXT NOT NULL REFERENCES work_item(work_item_id),
        budget_attempt_id TEXT NOT NULL UNIQUE REFERENCES quick_scan_budget_attempt(budget_attempt_id),
        lease_epoch INTEGER NOT NULL CHECK(lease_epoch>=1),
        lease_token TEXT NOT NULL,
        metadata_json TEXT NOT NULL CHECK(length(metadata_json)<=30000),
        metadata_sha256 TEXT NOT NULL CHECK(length(metadata_sha256)=64),
        created_at REAL NOT NULL,
        UNIQUE(sequence_key,stage)
    )""",
    """CREATE TRIGGER quick_scan_mcp_stage_insert_guard BEFORE INSERT ON quick_scan_mcp_stage
        BEGIN SELECT RAISE(ABORT,'invalid MCP control intent') WHERE NOT EXISTS (
            SELECT 1 FROM work_item w,quick_scan_budget_attempt b
            WHERE w.work_item_id=NEW.work_item_id AND w.status='leased'
            AND w.lease_epoch=NEW.lease_epoch AND w.lease_token=NEW.lease_token
            AND w.lease_expires_at>NEW.created_at
            AND b.budget_attempt_id=NEW.budget_attempt_id AND b.work_attempt_id IS NULL
            AND b.model_requested='external_retrieval_v1' AND b.status='in_flight'); END""",
    """CREATE TRIGGER quick_scan_mcp_stage_no_update BEFORE UPDATE ON quick_scan_mcp_stage
        BEGIN SELECT RAISE(ABORT,'MCP stages are immutable'); END""",
    """CREATE TRIGGER quick_scan_mcp_stage_no_delete BEFORE DELETE ON quick_scan_mcp_stage
        BEGIN SELECT RAISE(ABORT,'MCP stages are immutable'); END""",
    """CREATE TABLE quick_scan_mcp_dispatch (
        stage_id TEXT PRIMARY KEY REFERENCES quick_scan_mcp_stage(stage_id),
        sent_at REAL NOT NULL
    )""",
    """CREATE TRIGGER quick_scan_mcp_dispatch_insert_guard BEFORE INSERT ON quick_scan_mcp_dispatch
        BEGIN SELECT RAISE(ABORT,'invalid MCP control dispatch') WHERE NOT EXISTS (
            SELECT 1 FROM quick_scan_mcp_stage s JOIN work_item w USING(work_item_id)
            JOIN quick_scan_budget_attempt b USING(budget_attempt_id)
            WHERE s.stage_id=NEW.stage_id AND w.status='leased'
            AND w.lease_epoch=s.lease_epoch AND w.lease_token=s.lease_token
            AND w.lease_expires_at>NEW.sent_at AND b.status='in_flight'); END""",
    """CREATE TRIGGER quick_scan_mcp_dispatch_no_update BEFORE UPDATE ON quick_scan_mcp_dispatch
        BEGIN SELECT RAISE(ABORT,'MCP dispatches are immutable'); END""",
    """CREATE TRIGGER quick_scan_mcp_dispatch_no_delete BEFORE DELETE ON quick_scan_mcp_dispatch
        BEGIN SELECT RAISE(ABORT,'MCP dispatches are immutable'); END""",
    """CREATE TABLE quick_scan_mcp_result (
        stage_id TEXT PRIMARY KEY REFERENCES quick_scan_mcp_stage(stage_id),
        receipt_json TEXT NOT NULL CHECK(length(receipt_json)<=60000),
        receipt_sha256 TEXT NOT NULL CHECK(length(receipt_sha256)=64),
        control_json TEXT NOT NULL CHECK(length(control_json)<=2500),
        control_sha256 TEXT NOT NULL CHECK(length(control_sha256)=64),
        charge_json TEXT NOT NULL,
        recorded_at REAL NOT NULL,
        late INTEGER NOT NULL CHECK(late IN (0,1))
    )""",
    """CREATE TRIGGER quick_scan_mcp_result_insert_guard BEFORE INSERT ON quick_scan_mcp_result
        BEGIN SELECT RAISE(ABORT,'MCP result without dispatch and outcome') WHERE NOT EXISTS (
            SELECT 1 FROM quick_scan_mcp_stage s JOIN quick_scan_mcp_dispatch d USING(stage_id)
            JOIN quick_scan_budget_attempt b USING(budget_attempt_id)
            WHERE s.stage_id=NEW.stage_id AND b.status!='in_flight'); END""",
    """CREATE TRIGGER quick_scan_mcp_result_no_update BEFORE UPDATE ON quick_scan_mcp_result
        BEGIN SELECT RAISE(ABORT,'MCP results are immutable'); END""",
    """CREATE TRIGGER quick_scan_mcp_result_no_delete BEFORE DELETE ON quick_scan_mcp_result
        BEGIN SELECT RAISE(ABORT,'MCP results are immutable'); END""",
    """CREATE TABLE quick_scan_mcp_search_binding (
        operation_id TEXT PRIMARY KEY REFERENCES quick_scan_external_operation(operation_id),
        sequence_key TEXT NOT NULL CHECK(length(sequence_key)=64),
        metadata_json TEXT NOT NULL CHECK(length(metadata_json)<=2000),
        metadata_sha256 TEXT NOT NULL CHECK(length(metadata_sha256)=64),
        created_at REAL NOT NULL
    )""",
    """CREATE TRIGGER quick_scan_mcp_search_insert_guard BEFORE INSERT ON quick_scan_mcp_search_binding
        BEGIN SELECT RAISE(ABORT,'invalid MCP search binding') WHERE NOT EXISTS (
            SELECT 1 FROM quick_scan_external_operation o JOIN quick_scan_budget_attempt b USING(budget_attempt_id)
            WHERE o.operation_id=NEW.operation_id AND o.cache_key=NEW.sequence_key AND b.status='in_flight'
            AND NOT EXISTS(SELECT 1 FROM quick_scan_external_dispatch WHERE operation_id=NEW.operation_id))
            OR (SELECT COUNT(*) FROM quick_scan_mcp_stage s JOIN quick_scan_mcp_result r USING(stage_id)
                JOIN quick_scan_budget_attempt b USING(budget_attempt_id) WHERE s.sequence_key=NEW.sequence_key
                AND b.status='settled' AND r.late=0 AND json_extract(r.receipt_json,'$.parse_status')='ok'
                AND json_extract(r.receipt_json,'$.outcome')='response_available')!=3; END""",
    """CREATE TRIGGER quick_scan_mcp_search_no_update BEFORE UPDATE ON quick_scan_mcp_search_binding
        BEGIN SELECT RAISE(ABORT,'MCP search bindings are immutable'); END""",
    """CREATE TRIGGER quick_scan_mcp_search_no_delete BEFORE DELETE ON quick_scan_mcp_search_binding
        BEGIN SELECT RAISE(ABORT,'MCP search bindings are immutable'); END""",
)


def _message(stage: str, stage_id: str) -> dict:
    if stage == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": stage_id,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "StockQAbyLLM", "version": "1.0.0"},
            },
        }
    if stage == "initialized":
        return {"jsonrpc": "2.0", "method": "notifications/initialized"}
    if stage == "discovery":
        return {"jsonrpc": "2.0", "id": stage_id, "method": "tools/list"}
    raise ValueError("unsupported MCP control stage")


def _control(value: Any, stage: str, receipt: dict) -> dict | None:
    success = receipt["outcome"] == "response_available" and receipt.get("parse_status") == "ok"
    if receipt.get("entries") or receipt.get("provider_result_count", 0) != 0:
        raise ValueError("MCP control cannot contain search evidence")
    if not success:
        if value is not None:
            raise ValueError("failed MCP control cannot bind a session")
        return None
    if not isinstance(value, dict):
        raise ValueError("MCP control binding missing")
    safe: dict = strict_search_json(_canonical(value))
    if stage == "initialize":
        if (
            set(safe) != {"protocol_version", "session_id"}
            or not isinstance(safe["protocol_version"], str)
            or safe["protocol_version"] not in SUPPORTED_PROTOCOLS
        ):
            raise ValueError("MCP protocol binding mismatch")
        session = safe["session_id"]
        if session is not None and (
            not isinstance(session, str)
            or not 1 <= len(session) <= 2000
            or any(not 0x21 <= ord(char) <= 0x7E for char in session)
        ):
            raise ValueError("invalid private MCP session")
    elif stage == "initialized":
        if (
            set(safe) != {"initialized"}
            or safe["initialized"] is not True
            or receipt["http_status_code"] != 202
        ):
            raise ValueError("MCP initialized notification binding mismatch")
    elif stage == "discovery":
        if (
            set(safe) != {"tool_name", "input_schema_sha256"}
            or not isinstance(safe["tool_name"], str)
            or safe["tool_name"] not in APPROVED_SEARCH_TOOLS
        ):
            raise ValueError("MCP approved tool binding mismatch")
        _hash(safe["input_schema_sha256"])
    else:
        raise ValueError("unsupported MCP control stage")
    if len(_canonical(safe).encode("utf-8")) > 2500:
        raise ValueError("MCP control size exceeded")
    return safe


def _ready(record: dict, now: float) -> bool:
    result = record["result"]
    return bool(
        record["budget"]["status"] == "settled"
        and result is not None
        and not result["late"]
        and result["control"] is not None
        and result["receipt"]["outcome"] == "response_available"
        and result["receipt"].get("parse_status") == "ok"
        and result["recorded_at"] <= now < result["recorded_at"] + record["plan"]["ttl_seconds"]
    )


def _result_binding(record: dict) -> str:
    result = record["result"]
    return _digest(
        {
            "stage_id": record["stage_id"],
            "metadata_sha256": record["metadata_sha256"],
            "receipt_sha256": result["receipt_sha256"],
            "control_sha256": result["control_sha256"],
            "charge": result["charge"],
            "recorded_at": result["recorded_at"],
            "late": result["late"],
        }
    )


def read_record(connection: "sqlite3.Connection", row: "sqlite3.Row") -> dict:
    meta = strict_search_json(row["metadata_json"])
    keys = {
        "schema",
        "context",
        "stage",
        "parent_stage_id",
        "parent_result_sha256",
        "message_sha256",
        "lease_epoch",
        "lease_token",
        "created_at",
    }
    if (
        not isinstance(meta, dict)
        or set(meta) != keys
        or meta["schema"] != "stockqa.mcp_control_intent/1.0.0"
        or _canonical(meta) != row["metadata_json"]
        or _digest(meta) != row["metadata_sha256"]
        or row["stage"] not in STAGES
        or meta["stage"] != row["stage"]
    ):
        raise ValueError("MCP metadata hash/binding mismatch")
    if any(meta[key] != row[key] for key in ("lease_epoch", "lease_token", "created_at")):
        raise ValueError("MCP frozen owner/time binding mismatch")
    context = meta["context"]
    if not isinstance(context, dict) or set(context) != {
        "work_item_id",
        "work_fingerprint",
        "plan",
        "route",
        "search_policy_sha256",
        "budget",
    }:
        raise ValueError("MCP context binding mismatch")
    _plan(context["plan"])
    _hash(context["search_policy_sha256"])
    route = context["route"]
    if (
        route["provider"] != "zai_mcp_streamable"
        or route["endpoint"] != ENDPOINTS["zai_mcp_streamable"]
    ):
        raise ValueError("MCP endpoint binding mismatch")
    sequence_key = _cache_key(context)
    stage_id = "MCP_" + _digest({"sequence_key": sequence_key, "stage": row["stage"]})
    budget_id = "DISPATCH_" + _digest({"mcp_stage": stage_id})
    message = _message(row["stage"], stage_id)
    if (
        row["sequence_key"] != sequence_key
        or row["stage_id"] != stage_id
        or row["budget_attempt_id"] != budget_id
        or meta["message_sha256"] != _digest(message)
    ):
        raise ValueError("MCP message/identity binding mismatch")
    work = connection.execute(
        "SELECT * FROM work_item WHERE work_item_id=?", (row["work_item_id"],)
    ).fetchone()
    budget = connection.execute(
        "SELECT * FROM quick_scan_budget_attempt WHERE budget_attempt_id=?", (budget_id,)
    ).fetchone()
    if (
        work is None
        or budget is None
        or context["work_item_id"] != row["work_item_id"]
        or context["work_fingerprint"] != {key: work[key] for key in _WORK_KEYS}
        or budget["work_attempt_id"] is not None
        or budget["model_requested"] != MODEL_BUDGET_LABEL
        or any(budget[key] != context["budget"][key] for key in ("policy_id", "policy_version"))
        or budget["route_id"] != route["route_id"]
        or budget["provider"] != route["provider"]
        or budget["quota_group"] != route["quota_group"]
    ):
        raise ValueError("MCP durable budget/work binding mismatch")
    index = STAGES.index(row["stage"])
    if index == 0:
        if meta["parent_stage_id"] is not None or meta["parent_result_sha256"] is not None:
            raise ValueError("MCP initialization parent binding mismatch")
    else:
        parent_row = connection.execute(
            "SELECT * FROM quick_scan_mcp_stage WHERE sequence_key=? AND stage=?",
            (sequence_key, STAGES[index - 1]),
        ).fetchone()
        if parent_row is None:
            raise ValueError("MCP parent binding missing")
        parent = read_record(connection, parent_row)
        if (
            not _ready(parent, row["created_at"])
            or meta["parent_stage_id"] != parent["stage_id"]
            or meta["parent_result_sha256"] != _result_binding(parent)
        ):
            raise ValueError("MCP parent result binding mismatch")
    dispatch = connection.execute(
        "SELECT * FROM quick_scan_mcp_dispatch WHERE stage_id=?", (stage_id,)
    ).fetchone()
    raw = connection.execute(
        "SELECT * FROM quick_scan_mcp_result WHERE stage_id=?", (stage_id,)
    ).fetchone()
    result = None
    if raw is not None:
        receipt = _receipt(strict_search_json(raw["receipt_json"]), context, budget_id)
        control = _control(strict_search_json(raw["control_json"]), row["stage"], receipt)
        charge = strict_search_json(raw["charge_json"])
        if (
            dispatch is None
            or budget["status"] == "in_flight"
            or raw["recorded_at"] < dispatch["sent_at"]
            or _canonical(receipt) != raw["receipt_json"]
            or _digest(receipt) != raw["receipt_sha256"]
            or _canonical(control) != raw["control_json"]
            or _digest(control) != raw["control_sha256"]
            or _canonical(charge) != raw["charge_json"]
            or set(charge) != {"actual_cost_micros", "cost_source_ref"}
        ):
            raise ValueError("MCP result hash/binding mismatch")
        if budget["status"] == "settled" and (
            charge["actual_cost_micros"] != budget["actual_cost_micros"]
            or charge["cost_source_ref"] != budget["cost_source_ref"]
        ):
            raise ValueError("MCP result ledger binding mismatch")
        result = {
            "receipt": receipt,
            "receipt_sha256": raw["receipt_sha256"],
            "control": control,
            "control_sha256": raw["control_sha256"],
            "charge": charge,
            "recorded_at": raw["recorded_at"],
            "late": bool(raw["late"]),
        }
    return {
        **dict(row),
        "metadata": meta,
        "context": context,
        "plan": context["plan"],
        "message": message,
        "budget": dict(budget),
        "dispatch": None if dispatch is None else dict(dispatch),
        "result": result,
    }


def validate_tables(connection: "sqlite3.Connection") -> None:
    for row in connection.execute("SELECT * FROM quick_scan_mcp_stage"):
        read_record(connection, row)


def assert_no_unknown_scope(connection: "sqlite3.Connection", context: dict) -> None:
    from src.utils.quick_scan_work_store import BudgetAdmissionError

    item = context["work_fingerprint"]
    for row in connection.execute(
        "SELECT s.* FROM quick_scan_mcp_stage s JOIN work_item w USING(work_item_id) WHERE w.entity_id=? AND w.scope=? AND w.scope_id=?",
        (item["entity_id"], item["scope"], item["scope_id"]),
    ):
        record = read_record(connection, row)
        terminal = connection.execute(
            "SELECT terminal_outcome FROM quick_scan_budget_terminal WHERE budget_attempt_id=?",
            (record["budget_attempt_id"],),
        ).fetchone()
        if record["budget"]["status"] != "settled" or terminal is None or terminal[0] == "unknown":
            raise BudgetAdmissionError("mcp_control_unresolved")


def begin(
    store: "QuickScanWorkStore",
    work_item_id: str,
    lease: "Lease",
    *,
    stage: str,
    policy: dict,
    **request: Any,
) -> dict:
    from src.utils.quick_scan_work_store import (
        BudgetAdmissionError,
        _budget_policy_projection,
    )

    if stage not in STAGES or request.get("provider") != "zai_mcp_streamable":
        raise ValueError("unsupported MCP control route/stage")
    normalized = _budget_policy_projection(policy)
    now = store._now()
    with store._transaction() as connection:
        context = _request_context(
            store, connection, work_item_id, lease, normalized=normalized, **request
        )
        sequence_key = _cache_key(context)
        previous = connection.execute(
            "SELECT * FROM quick_scan_mcp_stage WHERE sequence_key=? AND stage=?",
            (sequence_key, stage),
        ).fetchone()
        if previous is not None:
            record = read_record(connection, previous)
            if _resumable_unsent(record, work_item_id, lease):
                return {**record, "send_required": True}
            if (
                record["budget"]["status"] != "settled"
                or record["result"] is None
                or record["result"]["receipt"]["outcome"] == "unknown"
            ):
                raise BudgetAdmissionError("mcp_control_unresolved")
            if now >= record["result"]["recorded_at"] + record["plan"]["ttl_seconds"]:
                raise BudgetAdmissionError("mcp_control_stale_generation_required")
            return {**record, "send_required": False}
        parent = None
        index = STAGES.index(stage)
        if index:
            parent_row = connection.execute(
                "SELECT * FROM quick_scan_mcp_stage WHERE sequence_key=? AND stage=?",
                (sequence_key, STAGES[index - 1]),
            ).fetchone()
            if parent_row is not None:
                parent = read_record(connection, parent_row)
            if parent is None or not _ready(parent, now):
                raise BudgetAdmissionError("mcp_parent_not_ready")
        _assert_no_unknown_scope(connection, context)
        normalized = store._ensure_budget_policy_tx(connection, normalized, now=now)
        stage_id = "MCP_" + _digest({"sequence_key": sequence_key, "stage": stage})
        budget_id = "DISPATCH_" + _digest({"mcp_stage": stage_id})
        meta = {
            "schema": "stockqa.mcp_control_intent/1.0.0",
            "context": context,
            "stage": stage,
            "parent_stage_id": None if parent is None else parent["stage_id"],
            "parent_result_sha256": None if parent is None else _result_binding(parent),
            "message_sha256": _digest(_message(stage, stage_id)),
            "lease_epoch": lease.lease_epoch,
            "lease_token": lease.lease_token,
            "created_at": now,
        }
        if len(_canonical(meta).encode("utf-8")) > 30000:
            raise ValueError("MCP intent exceeds bound")
        store._reserve_budget_attempt_tx(
            connection,
            normalized_policy=normalized,
            budget_attempt_id=budget_id,
            work_attempt_id=None,
            route_id=context["route"]["route_id"],
            provider=context["route"]["provider"],
            model_requested=MODEL_BUDGET_LABEL,
            quota_group=context["route"]["quota_group"],
            now=now,
        )
        connection.execute(
            "INSERT INTO quick_scan_mcp_stage (stage_id,sequence_key,stage,work_item_id,budget_attempt_id,lease_epoch,lease_token,metadata_json,metadata_sha256,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                stage_id,
                sequence_key,
                stage,
                work_item_id,
                budget_id,
                lease.lease_epoch,
                lease.lease_token,
                _canonical(meta),
                _digest(meta),
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM quick_scan_mcp_stage WHERE stage_id=?", (stage_id,)
        ).fetchone()
        return {**read_record(connection, row), "send_required": True}


def consume(store: "QuickScanWorkStore", stage_id: str, *, budget_attempt_id: str) -> None:
    from src.utils.quick_scan_work_store import Lease, WorkConflictError

    with store._transaction() as connection:
        row = connection.execute(
            "SELECT * FROM quick_scan_mcp_stage WHERE stage_id=?", (stage_id,)
        ).fetchone()
        if row is None:
            raise KeyError("unknown MCP control stage")
        record = read_record(connection, row)
        if (
            record["budget_attempt_id"] != budget_attempt_id
            or record["dispatch"] is not None
            or record["budget"]["status"] != "in_flight"
        ):
            raise WorkConflictError("MCP send intent already consumed or mismatched")
        store._assert_lease(
            store._item(connection, row["work_item_id"]),
            Lease(row["lease_token"], row["lease_epoch"]),
            store._now(),
        )
        connection.execute(
            "INSERT INTO quick_scan_mcp_dispatch (stage_id,sent_at) VALUES (?,?)",
            (stage_id, store._now()),
        )


def get(store: "QuickScanWorkStore", stage_id: str) -> dict:
    with closing(store._connect()) as connection:
        row = connection.execute(
            "SELECT * FROM quick_scan_mcp_stage WHERE stage_id=?", (stage_id,)
        ).fetchone()
        if row is None:
            raise KeyError("unknown MCP control stage")
        return read_record(connection, row)


def record_result(
    store: "QuickScanWorkStore",
    stage_id: str,
    *,
    receipt: dict,
    control: Any,
    actual_cost: Any = None,
    cost_source_ref: str | None = None,
) -> dict:
    from src.utils.quick_scan_work_store import (
        Lease,
        LeaseFencedError,
        WorkConflictError,
        _cost_to_micros,
        _safe_text,
    )

    micros = (
        None
        if actual_cost is None
        else _cost_to_micros(actual_cost, "actual_cost", allow_zero=True)
    )
    if micros is not None:
        cost_source_ref = _safe_text(cost_source_ref, "cost_source_ref", maximum=300)
    elif cost_source_ref is not None:
        raise ValueError("cost reference without exact MCP cost")
    charge = {"actual_cost_micros": micros, "cost_source_ref": cost_source_ref}
    now = store._now()
    with store._transaction() as connection:
        row = connection.execute(
            "SELECT * FROM quick_scan_mcp_stage WHERE stage_id=?", (stage_id,)
        ).fetchone()
        if row is None:
            raise KeyError("unknown MCP control stage")
        record = read_record(connection, row)
        safe = _receipt(receipt, record["context"], record["budget_attempt_id"])
        safe_control = _control(control, record["stage"], safe)
        if record["result"] is not None:
            before = record["result"]
            if (
                before["receipt_sha256"] != _digest(safe)
                or before["control_sha256"] != _digest(safe_control)
                or before["charge"] != charge
            ):
                raise WorkConflictError("MCP control already has a different result")
            return record
        if record["dispatch"] is None:
            raise WorkConflictError("MCP result arrived before durable dispatch")
        late = False
        try:
            store._assert_lease(
                store._item(connection, row["work_item_id"]),
                Lease(row["lease_token"], row["lease_epoch"]),
                now,
            )
        except LeaseFencedError:
            late = True
        store._record_budget_outcome_tx(
            connection,
            row["budget_attempt_id"],
            outcome=safe["outcome"],
            http_status_code=safe.get("http_status_code"),
            now=now,
            actual_cost_micros=micros,
            cost_source_ref=cost_source_ref,
        )
        connection.execute(
            "INSERT INTO quick_scan_mcp_result (stage_id,receipt_json,receipt_sha256,control_json,control_sha256,charge_json,recorded_at,late) VALUES (?,?,?,?,?,?,?,?)",
            (
                stage_id,
                _canonical(safe),
                _digest(safe),
                _canonical(safe_control),
                _digest(safe_control),
                _canonical(charge),
                now,
                int(late),
            ),
        )
        return read_record(connection, row)


def get_binding(store: "QuickScanWorkStore", sequence_key: str) -> dict:
    with closing(store._connect()) as connection:
        return _binding(connection, sequence_key, now=store._now())


def _binding(connection: "sqlite3.Connection", sequence_key: str, *, now: float) -> dict:
    from src.utils.quick_scan_work_store import BudgetAdmissionError

    _hash(sequence_key)
    records = []
    for stage in STAGES:
        row = connection.execute(
            "SELECT * FROM quick_scan_mcp_stage WHERE sequence_key=? AND stage=?",
            (sequence_key, stage),
        ).fetchone()
        record = None if row is None else read_record(connection, row)
        if record is None or not _ready(record, now):
            raise BudgetAdmissionError("mcp_parent_not_ready")
        records.append(record)
    initial, _, discovery = records
    return {
        "sequence_key": sequence_key,
        **initial["result"]["control"],
        **discovery["result"]["control"],
        "stage_ids": [record["stage_id"] for record in records],
        "stage_binding_sha256s": [_result_binding(record) for record in records],
    }


def bind_search(
    connection: "sqlite3.Connection", operation_id: str, context: dict, *, now: float
) -> None:
    binding = _binding(connection, _cache_key(context), now=now)
    meta = {
        "schema": "stockqa.mcp_search_binding/1.0.0",
        "operation_id": operation_id,
        "sequence_key": binding["sequence_key"],
        "stage_ids": binding["stage_ids"],
        "stage_binding_sha256s": binding["stage_binding_sha256s"],
        "created_at": now,
    }
    connection.execute(
        "INSERT INTO quick_scan_mcp_search_binding (operation_id,sequence_key,metadata_json,metadata_sha256,created_at) VALUES (?,?,?,?,?)",
        (operation_id, binding["sequence_key"], _canonical(meta), _digest(meta), now),
    )


def read_search_binding(
    connection: "sqlite3.Connection", operation: "sqlite3.Row", context: dict
) -> dict:
    row = connection.execute(
        "SELECT * FROM quick_scan_mcp_search_binding WHERE operation_id=?",
        (operation["operation_id"],),
    ).fetchone()
    if row is None:
        raise ValueError("MCP search binding missing")
    meta = strict_search_json(row["metadata_json"])
    binding = _binding(connection, _cache_key(context), now=row["created_at"])
    expected = {
        "schema": "stockqa.mcp_search_binding/1.0.0",
        "operation_id": operation["operation_id"],
        "sequence_key": binding["sequence_key"],
        "stage_ids": binding["stage_ids"],
        "stage_binding_sha256s": binding["stage_binding_sha256s"],
        "created_at": operation["created_at"],
    }
    if (
        meta != expected
        or _canonical(meta) != row["metadata_json"]
        or _digest(meta) != row["metadata_sha256"]
        or row["sequence_key"] != expected["sequence_key"]
        or row["created_at"] != operation["created_at"]
    ):
        raise ValueError("MCP search control/intent binding mismatch")
    return {"metadata": meta, "metadata_sha256": row["metadata_sha256"]}
