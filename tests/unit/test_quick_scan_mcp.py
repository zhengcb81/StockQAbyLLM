"""MCP control stages over the REAL Q09 store; synthetic data, no HTTP/key.

Not provider interoperability, financial golden or a second budget owner.
Transport/official tool schema tests follow when the adapter is connected.
"""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from decimal import Decimal

import pytest

from src.config.quick_scan_search_policy import execution_query_plans
from src.utils.quick_scan_external_context import build_shared_external_budget_policy
from src.utils.quick_scan_work_store import (
    BudgetAdmissionError,
    QuickScanWorkStore,
    WorkConflictError,
)
from tests.unit.test_quick_scan_external_context import _coordinator_fixture


def _mcp_fixture(tmp_path, monkeypatch, *, second_route=False):
    def route(document):
        document["external_routes"][0].update(
            kind="zai_mcp_streamable", endpoint="https://api.z.ai/api/mcp/web_search_prime/mcp"
        )

    args, session, clock = _coordinator_fixture(
        tmp_path, monkeypatch, mutate=route, second_route=second_route
    )
    request = {
        "policy": build_shared_external_budget_policy(args["model_policy"], args["policy"]),
        "plan": execution_query_plans(args["policy"])[0],
        "route_id": "brave-primary",
        "provider": "zai_mcp_streamable",
        "quota_group": "search-brave",
        "adapter_version": "stockqa.external_retrieval/1.0.0",
        "search_policy_sha256": args["policy"].policy_sha256,
        "endpoint": "https://api.z.ai/api/mcp/web_search_prime/mcp",
    }
    return args, request, session, clock


def _stage(args, request, name):
    return args["store"].begin_mcp_stage(args["work_item_id"], args["lease"], stage=name, **request)


def _control_receipt(stage, *, outcome="response_available", status=200):
    value = {
        "origin": "external",
        "adapter_version": "stockqa.external_retrieval/1.0.0",
        "route_id": "brave-primary",
        "route_kind": "zai_mcp_streamable",
        "attempt_id": stage["budget_attempt_id"],
        "http_request_count": 1,
        "http_status_code": status,
        "request_id": "synthetic-mcp-request",
        "outcome": outcome,
        "response_body_sha256": "f" * 64,
    }
    if outcome == "response_available":
        value.update(parse_status="ok", provider_result_count=0, entries=[])
    return value


def _finish(args, stage, control, *, cost=Decimal("0.003"), consume=True, status=200):
    if consume:
        args["store"].consume_mcp_stage(
            stage["stage_id"], budget_attempt_id=stage["budget_attempt_id"]
        )
    return args["store"].record_mcp_stage(
        stage["stage_id"],
        receipt=_control_receipt(stage, status=status),
        control=control,
        actual_cost=cost,
        cost_source_ref=None if cost is None else "operator-synthetic-pricing/1",
    )


def _initialize(args, request):
    return _finish(
        args,
        _stage(args, request, "initialize"),
        {"protocol_version": "2025-03-26", "session_id": "SESSION_SYNTHETIC_ONLY"},
    )


def test_mcp_control_intent_and_q09_reservation_are_atomic_before_send(tmp_path, monkeypatch):
    args, request, session, _ = _mcp_fixture(tmp_path, monkeypatch)
    stage = _stage(args, request, "initialize")
    saved = args["store"].get_mcp_stage(stage["stage_id"])
    assert stage["send_required"] and saved["dispatch"] is None and saved["result"] is None
    assert (
        saved["budget"]["work_attempt_id"] is None
        and saved["budget"]["model_requested"] == "external_retrieval_v1"
    )
    assert saved["message"]["method"] == "initialize"
    assert saved["message"]["params"]["protocolVersion"] == "2025-03-26"
    assert saved["budget"]["status"] == "in_flight"
    assert args["store"].list_attempts(args["work_item_id"]) == []
    assert args["store"].get_quick_scan_budget_status("q09-fixture")["requests"] == 1
    session.request.assert_not_called()


@pytest.mark.parametrize("name", ["initialized", "discovery"])
def test_mcp_stage_order_cannot_be_forged_before_initialization(tmp_path, monkeypatch, name):
    args, request, _, _ = _mcp_fixture(tmp_path, monkeypatch)
    with pytest.raises(BudgetAdmissionError, match="mcp_parent_not_ready"):
        _stage(args, request, name)
    assert args["store"].list_attempts(args["work_item_id"]) == []
    with closing(args["store"]._connect()) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM quick_scan_budget_attempt").fetchone()[0] == 0
        )


def test_mcp_control_response_cannot_be_accepted_before_actual_dispatch(tmp_path, monkeypatch):
    args, request, _, _ = _mcp_fixture(tmp_path, monkeypatch)
    stage = _stage(args, request, "initialize")
    with pytest.raises(WorkConflictError, match="before.*dispatch"):
        _finish(args, stage, {"protocol_version": "2025-03-26", "session_id": None}, consume=False)
    assert args["store"].get_mcp_stage(stage["stage_id"])["result"] is None


def test_mcp_all_three_controls_are_separately_charged_and_reused_after_reopen(
    tmp_path, monkeypatch
):
    args, request, session, clock = _mcp_fixture(tmp_path, monkeypatch)
    first = _initialize(args, request)
    second = _finish(args, _stage(args, request, "initialized"), {"initialized": True}, status=202)
    third = _finish(
        args,
        _stage(args, request, "discovery"),
        {"tool_name": "webSearchPrime", "input_schema_sha256": "a" * 64},
    )
    totals = args["store"].get_quick_scan_budget_status("q09-fixture")
    assert (
        totals["requests"] == 3
        and totals["spent_micros"] == 9000
        and totals["reserved_micros"] == 0
    )
    args["store"] = QuickScanWorkStore(args["store"].path, clock=lambda: clock[0])
    for name, before in zip(("initialize", "initialized", "discovery"), (first, second, third)):
        after = _stage(args, request, name)
        assert after["send_required"] is False
        assert {k: v for k, v in after.items() if k != "send_required"} == before
    binding = args["store"].get_mcp_binding(first["sequence_key"])
    assert (
        binding["protocol_version"] == "2025-03-26"
        and binding["session_id"] == "SESSION_SYNTHETIC_ONLY"
    )
    assert binding["stage_ids"] == [r["stage_id"] for r in (first, second, third)]
    assert args["store"].get_quick_scan_budget_status("q09-fixture") == totals
    session.request.assert_not_called()


def test_mcp_unknown_control_holds_reservation_and_blocks_next_stage_without_replay(
    tmp_path, monkeypatch
):
    args, request, _, _ = _mcp_fixture(tmp_path, monkeypatch)
    stage = _stage(args, request, "initialize")
    args["store"].consume_mcp_stage(stage["stage_id"], budget_attempt_id=stage["budget_attempt_id"])
    args["store"].record_mcp_stage(
        stage["stage_id"],
        receipt=_control_receipt(stage, outcome="unknown", status=None),
        control=None,
    )
    with pytest.raises(BudgetAdmissionError, match="mcp_control_unresolved"):
        _stage(args, request, "initialize")
    with pytest.raises(BudgetAdmissionError, match="mcp_parent_not_ready"):
        _stage(args, request, "initialized")
    totals = args["store"].get_quick_scan_budget_status("q09-fixture")
    assert totals["requests"] == 1 and totals["reserved_micros"] == 6_000_000


def test_mcp_missing_control_price_cannot_advance_even_with_valid_protocol(tmp_path, monkeypatch):
    args, request, _, _ = _mcp_fixture(tmp_path, monkeypatch)
    _finish(
        args,
        _stage(args, request, "initialize"),
        {"protocol_version": "2025-03-26", "session_id": None},
        cost=None,
    )
    with pytest.raises(BudgetAdmissionError, match="mcp_parent_not_ready"):
        _stage(args, request, "initialized")
    assert args["store"].get_quick_scan_budget_status("q09-fixture")["reserved_micros"] > 0


def test_mcp_two_handles_can_consume_only_one_durable_send_permit(tmp_path, monkeypatch):
    args, request, _, _ = _mcp_fixture(tmp_path, monkeypatch)
    first = _stage(args, request, "initialize")
    second = _stage(args, request, "initialize")
    assert (
        first["stage_id"] == second["stage_id"]
        and first["budget_attempt_id"] == second["budget_attempt_id"]
    )

    def consume():
        try:
            args["store"].consume_mcp_stage(
                first["stage_id"], budget_attempt_id=first["budget_attempt_id"]
            )
            return "sent"
        except WorkConflictError:
            return "fenced"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: consume(), range(2)))
    assert sorted(results) == ["fenced", "sent"]
    assert args["store"].get_quick_scan_budget_status("q09-fixture")["requests"] == 1


def test_mcp_result_and_budget_settlement_roll_back_together_on_save_failure(tmp_path, monkeypatch):
    args, request, _, _ = _mcp_fixture(tmp_path, monkeypatch)
    stage = _stage(args, request, "initialize")
    args["store"].consume_mcp_stage(stage["stage_id"], budget_attempt_id=stage["budget_attempt_id"])
    with closing(args["store"]._connect()) as connection:
        connection.execute(
            "CREATE TRIGGER synthetic_mcp_save_failure BEFORE INSERT ON quick_scan_mcp_result BEGIN SELECT RAISE(ABORT,'synthetic mcp save failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic mcp save failure"):
        _finish(args, stage, {"protocol_version": "2025-03-26", "session_id": None}, consume=False)
    saved = args["store"].get_mcp_stage(stage["stage_id"])
    assert saved["result"] is None and saved["budget"]["status"] == "in_flight"
    assert args["store"].get_quick_scan_budget_status("q09-fixture")["spent_micros"] == 0


@pytest.mark.parametrize(
    "command",
    [
        "UPDATE quick_scan_mcp_result SET control_sha256=control_sha256",
        "DELETE FROM quick_scan_mcp_stage",
    ],
)
def test_mcp_control_rows_are_immutable(tmp_path, monkeypatch, command):
    args, request, _, _ = _mcp_fixture(tmp_path, monkeypatch)
    first = _initialize(args, request)
    with closing(args["store"]._connect()) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(command)
    assert args["store"].get_mcp_stage(first["stage_id"]) == first


def test_mcp_session_and_control_hash_corruption_cannot_create_a_binding(tmp_path, monkeypatch):
    args, request, _, _ = _mcp_fixture(tmp_path, monkeypatch)
    first = _initialize(args, request)
    with closing(args["store"]._connect()) as connection:
        connection.execute("DROP TRIGGER quick_scan_mcp_result_no_update")
        connection.execute(
            "UPDATE quick_scan_mcp_result SET control_json=?",
            (json.dumps({"protocol_version": "2025-03-26", "session_id": "SESSION_FOREIGN"}),),
        )
    with pytest.raises(ValueError, match="MCP.*binding|MCP.*hash"):
        args["store"].get_mcp_stage(first["stage_id"])


def test_observed_but_unknown_control_is_not_reusable_even_when_price_settled(
    tmp_path, monkeypatch
):
    args, request, _, _ = _mcp_fixture(tmp_path, monkeypatch)
    stage = _stage(args, request, "initialize")
    args["store"].consume_mcp_stage(stage["stage_id"], budget_attempt_id=stage["budget_attempt_id"])
    args["store"].record_mcp_stage(
        stage["stage_id"],
        receipt=_control_receipt(stage, outcome="unknown", status=503),
        control=None,
        actual_cost=Decimal("0.003"),
        cost_source_ref="operator-synthetic-pricing/1",
    )
    assert args["store"].get_quick_scan_budget_status("q09-fixture")["spent_micros"] == 3000
    with pytest.raises(BudgetAdmissionError, match="mcp_control_unresolved"):
        _stage(args, request, "initialize")


def test_unknown_mcp_also_blocks_fresh_search_route_before_any_new_reservation(
    tmp_path, monkeypatch
):
    args, request, _, _ = _mcp_fixture(tmp_path, monkeypatch)
    stage = _stage(args, request, "initialize")
    args["store"].consume_mcp_stage(stage["stage_id"], budget_attempt_id=stage["budget_attempt_id"])
    args["store"].record_mcp_stage(
        stage["stage_id"],
        receipt=_control_receipt(stage, outcome="unknown", status=None),
        control=None,
    )
    with pytest.raises(BudgetAdmissionError, match="mcp_control_unresolved"):
        args["store"].begin_external_search(args["work_item_id"], args["lease"], **request)
    assert args["store"].get_quick_scan_budget_status("q09-fixture")["requests"] == 1


@pytest.mark.parametrize(
    "field,sql_value", [("lease_epoch", "lease_epoch+1"), ("created_at", "created_at+1")]
)
def test_mcp_frozen_owner_and_creation_time_are_bound_to_metadata(
    tmp_path, monkeypatch, field, sql_value
):
    args, request, _, _ = _mcp_fixture(tmp_path, monkeypatch)
    stage = _stage(args, request, "initialize")
    with closing(args["store"]._connect()) as connection:
        connection.execute("DROP TRIGGER quick_scan_mcp_stage_no_update")
        connection.execute(f"UPDATE quick_scan_mcp_stage SET {field}={sql_value}")
    with pytest.raises(ValueError, match="MCP.*binding"):
        args["store"].get_mcp_stage(stage["stage_id"])


@pytest.mark.parametrize(
    "control",
    [
        {"protocol_version": "2023-01-01", "session_id": None},
        {"protocol_version": "2025-03-26", "session_id": "bad\r\nheader"},
        {"protocol_version": "2025-03-26", "session_id": ""},
        {"protocol_version": "2025-03-26", "session_id": "x" * 2001},
        {"protocol_version": "2025-03-26", "session_id": "session", "api_key": "synthetic-key"},
    ],
)
def test_mcp_invalid_protocol_or_private_session_is_not_persisted_or_settled(
    tmp_path, monkeypatch, control
):
    args, request, _, _ = _mcp_fixture(tmp_path, monkeypatch)
    stage = _stage(args, request, "initialize")
    args["store"].consume_mcp_stage(stage["stage_id"], budget_attempt_id=stage["budget_attempt_id"])
    with pytest.raises(ValueError):
        _finish(args, stage, control, consume=False)
    record = args["store"].get_mcp_stage(stage["stage_id"])
    assert record["result"] is None and record["budget"]["status"] == "in_flight"


def test_mcp_late_control_keeps_known_cash_but_cannot_advance_protocol(tmp_path, monkeypatch):
    args, request, _, clock = _mcp_fixture(tmp_path, monkeypatch)
    stage = _stage(args, request, "initialize")
    args["store"].consume_mcp_stage(stage["stage_id"], budget_attempt_id=stage["budget_attempt_id"])
    clock[0] += 61
    result = _finish(
        args, stage, {"protocol_version": "2025-03-26", "session_id": None}, consume=False
    )
    assert result["result"]["late"] is True
    assert args["store"].recover_expired(args["work_item_id"]) == "pending"
    args["lease"] = args["store"].claim(args["work_item_id"], lease_seconds=60)
    assert args["lease"] is not None
    with pytest.raises(BudgetAdmissionError, match="mcp_parent_not_ready"):
        _stage(args, request, "initialized")
    assert args["store"].get_quick_scan_budget_status("q09-fixture")["spent_micros"] == 3000


def test_mcp_paid_rejection_is_cached_as_failure_without_resend(tmp_path, monkeypatch):
    args, request, _, _ = _mcp_fixture(tmp_path, monkeypatch)
    stage = _stage(args, request, "initialize")
    args["store"].consume_mcp_stage(stage["stage_id"], budget_attempt_id=stage["budget_attempt_id"])
    first = args["store"].record_mcp_stage(
        stage["stage_id"],
        receipt=_control_receipt(stage, outcome="confirmed_failure", status=401),
        control=None,
        actual_cost=Decimal("0.003"),
        cost_source_ref="operator-synthetic-pricing/1",
    )
    after = _stage(args, request, "initialize")
    assert not after["send_required"] and after["result"] == first["result"]
    with pytest.raises(BudgetAdmissionError, match="mcp_parent_not_ready"):
        _stage(args, request, "initialized")
    assert args["store"].get_quick_scan_budget_status("q09-fixture")["requests"] == 1


def _install_real_v12(store):
    with closing(store._connect()) as connection, connection:
        for table in (
            "quick_scan_mcp_search_binding",
            "quick_scan_mcp_result",
            "quick_scan_mcp_dispatch",
            "quick_scan_mcp_stage",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version=12")


def test_v12_upgrade_keeps_actual_http_events_cash_and_creates_no_control_history(
    tmp_path, monkeypatch
):
    from src.utils.quick_scan_work_store import SCHEMA_VERSION
    from tests.unit.test_quick_scan_external_context import _native_event_answer

    args, _, attempt_id, search, sync, clock = _native_event_answer(tmp_path, monkeypatch)
    _install_real_v12(args["store"])
    before_response = args["store"].get_attempt_response(attempt_id)
    before_cash = args["store"].get_quick_scan_budget_status("q09-fixture")
    with closing(args["store"]._connect()) as connection:
        before_events = [
            tuple(row)
            for row in connection.execute("SELECT * FROM quick_scan_native_search_events")
        ]
    reopened = QuickScanWorkStore(args["store"].path, clock=lambda: clock[0])
    assert reopened.get_attempt_response(attempt_id) == before_response
    assert reopened.get_quick_scan_budget_status("q09-fixture") == before_cash
    with closing(reopened._connect()) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert [
            tuple(row)
            for row in connection.execute("SELECT * FROM quick_scan_native_search_events")
        ] == before_events
        for table in (
            "quick_scan_mcp_search_binding",
            "quick_scan_mcp_result",
            "quick_scan_mcp_dispatch",
            "quick_scan_mcp_stage",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert search.request.call_count == sync.post.call_count == 1


def test_v13_migration_interruption_rolls_back_to_exact_v12_schema_and_rows(tmp_path, monkeypatch):
    from src.utils.quick_scan_work_store import SCHEMA_VERSION

    args, _, _, _ = _mcp_fixture(tmp_path, monkeypatch)
    _install_real_v12(args["store"])
    with closing(args["store"]._connect()) as connection:
        original = tuple(connection.iterdump())
    validate = QuickScanWorkStore._validate_schema

    def reject_final(connection, *, schema_version):
        validate(connection, schema_version=schema_version)
        if schema_version == SCHEMA_VERSION:
            raise sqlite3.OperationalError("synthetic MCP migration interruption")

    monkeypatch.setattr(QuickScanWorkStore, "_validate_schema", staticmethod(reject_final))
    with pytest.raises(sqlite3.OperationalError, match="synthetic MCP migration interruption"):
        QuickScanWorkStore(args["store"].path)
    with closing(args["store"]._connect()) as connection:
        assert tuple(connection.iterdump()) == original
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 12


@pytest.mark.parametrize(
    "protocol,tool", [("2024-11-05", "web_search_prime"), ("2025-03-26", "webSearchPrime")]
)
def test_mcp_negotiated_known_protocol_and_actual_discovered_name_are_retained(
    tmp_path, monkeypatch, protocol, tool
):
    args, request, _, _ = _mcp_fixture(tmp_path, monkeypatch)
    first = _finish(
        args,
        _stage(args, request, "initialize"),
        {"protocol_version": protocol, "session_id": None},
    )
    _finish(args, _stage(args, request, "initialized"), {"initialized": True}, status=202)
    _finish(
        args,
        _stage(args, request, "discovery"),
        {"tool_name": tool, "input_schema_sha256": "a" * 64},
    )
    binding = args["store"].get_mcp_binding(first["sequence_key"])
    assert binding["protocol_version"] == protocol and binding["tool_name"] == tool


def _control_permit(args, request, name):
    from src.utils.quick_scan_work_store import SendPermit
    from src.utils.quick_scan_work_transport import (
        QuickScanBudgetBinding,
        QuickScanSendAttempt,
    )

    stage = _stage(args, request, name)
    handle = QuickScanSendAttempt(
        None,
        stage["budget_attempt_id"],
        SendPermit(stage["budget_attempt_id"], stage["created_at"], 0),
        budget_binding=QuickScanBudgetBinding(args["store"], request["policy"]),
        budget_attempt_id=stage["budget_attempt_id"],
    )
    handle.mcp_stage_id = stage["stage_id"]
    return stage, handle


def _mcp_response(document, *, status=200, headers=None, chunks=None):
    from unittest.mock import Mock

    response = Mock()
    response.status_code = status
    response.headers = {
        "Content-Type": "application/json",
        "x-request-id": "synthetic-header-id",
        **(headers or {}),
    }
    response.iter_content.return_value = (
        chunks
        if chunks is not None
        else [json.dumps(document).encode()] if document is not None else []
    )
    return response


def _mcp_transport(args, session, monkeypatch, response):
    from src.providers.external_search_provider import ExternalSearchProvider

    provider = ExternalSearchProvider(args["policy"], "brave-primary")
    session.request.return_value = response
    monkeypatch.setattr(
        "src.utils.http_client.http_client_manager.get_metered_sync_session", lambda: session
    )
    return provider


def test_mcp_initialize_transport_uses_actual_rpc_id_and_keeps_negotiated_session_private(
    tmp_path, monkeypatch
):
    args, request, session, _ = _mcp_fixture(tmp_path, monkeypatch)
    stage, handle = _control_permit(args, request, "initialize")
    response = _mcp_response(
        {
            "jsonrpc": "2.0",
            "id": stage["stage_id"],
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "synthetic", "version": "1"},
            },
        },
        headers={"Mcp-Session-Id": "SYNTHETIC_SESSION"},
    )
    provider = _mcp_transport(args, session, monkeypatch, response)
    result = provider.fetch_mcp_control(handle)
    assert result["control"] == {
        "protocol_version": "2024-11-05",
        "session_id": "SYNTHETIC_SESSION",
    }
    assert result["receipt"]["parse_status"] == "ok" and result["receipt"]["origin"] == "external"
    assert "SYNTHETIC_SESSION" not in json.dumps(result["receipt"])
    kwargs = session.request.call_args.kwargs
    assert (
        kwargs["json"] == stage["message"]
        and kwargs["headers"]["Accept"] == "application/json, text/event-stream"
    )
    assert "Mcp-Session-Id" not in kwargs["headers"] and kwargs["allow_redirects"] is False
    assert (
        session.request.call_count == 1
        and args["store"].get_mcp_stage(stage["stage_id"])["dispatch"] is not None
    )
    response.close.assert_called_once()


def test_mcp_initialized_notification_sends_private_parent_session_and_accepts_202_empty(
    tmp_path, monkeypatch
):
    args, request, session, _ = _mcp_fixture(tmp_path, monkeypatch)
    _initialize(args, request)
    stage, handle = _control_permit(args, request, "initialized")
    response = _mcp_response(None, status=202)
    result = _mcp_transport(args, session, monkeypatch, response).fetch_mcp_control(handle)
    assert result["control"] == {"initialized": True} and result["receipt"]["parse_status"] == "ok"
    assert session.request.call_args.kwargs["headers"]["Mcp-Session-Id"] == "SESSION_SYNTHETIC_ONLY"
    assert session.request.call_args.kwargs["json"] == {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }
    assert "id" not in stage["message"] and session.request.call_count == 1
    import hashlib

    assert result["receipt"]["response_body_sha256"] == hashlib.sha256(b"").hexdigest()


def test_mcp_discovery_accepts_real_zai_query_only_schema_without_invented_count_argument(
    tmp_path, monkeypatch
):
    args, request, session, _ = _mcp_fixture(tmp_path, monkeypatch)
    _initialize(args, request)
    _finish(args, _stage(args, request, "initialized"), {"initialized": True}, status=202)
    stage, handle = _control_permit(args, request, "discovery")
    schema = {
        "type": "object",
        "properties": {"search_query": {"type": "string"}, "content_size": {"type": "string"}},
        "required": ["search_query"],
        "additionalProperties": False,
    }
    response = _mcp_response(
        {
            "jsonrpc": "2.0",
            "id": stage["stage_id"],
            "result": {"tools": [{"name": "web_search_prime", "inputSchema": schema}]},
        }
    )
    result = _mcp_transport(args, session, monkeypatch, response).fetch_mcp_control(handle)
    assert result["control"]["tool_name"] == "web_search_prime"
    from src.utils.quick_scan_external_journal import _digest

    assert result["control"]["input_schema_sha256"] == _digest(schema)
    assert result["receipt"]["entries"] == [] and result["receipt"]["http_request_count"] == 1


@pytest.mark.parametrize("defect", ["rpc_id", "protocol", "tools_capability", "session_echo"])
def test_mcp_control_bad_binding_is_a_paid_parse_failure_without_private_metadata(
    tmp_path, monkeypatch, defect
):
    args, request, session, _ = _mcp_fixture(tmp_path, monkeypatch)
    stage, handle = _control_permit(args, request, "initialize")
    body = {
        "jsonrpc": "2.0",
        "id": stage["stage_id"],
        "result": {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "synthetic", "version": "1"},
        },
    }
    headers = {}
    if defect == "rpc_id":
        body["id"] = "FOREIGN_ID"
    elif defect == "protocol":
        body["result"]["protocolVersion"] = "unsupported"
    elif defect == "tools_capability":
        body["result"]["capabilities"] = {}
    else:
        headers["Mcp-Session-Id"] = "synthetic-test-only"
    result = _mcp_transport(
        args, session, monkeypatch, _mcp_response(body, headers=headers)
    ).fetch_mcp_control(handle)
    assert result["control"] is None and result["receipt"]["parse_status"] == "parse_failure"
    assert result["receipt"]["outcome"] == "response_available" and session.request.call_count == 1
    assert "synthetic-test-only" not in json.dumps(result) and "FOREIGN_ID" not in json.dumps(
        result
    )


def test_mcp_sse_stops_after_matching_response_and_closes_without_waiting_for_stream_end(
    tmp_path, monkeypatch
):
    args, request, session, _ = _mcp_fixture(tmp_path, monkeypatch)
    stage, handle = _control_permit(args, request, "initialize")
    body = {
        "jsonrpc": "2.0",
        "id": stage["stage_id"],
        "result": {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "synthetic", "version": "1"},
        },
    }
    event = b"data: " + json.dumps(body).encode() + b"\n\n"

    def chunks():
        yield b": keepalive\n\n"
        yield event[:19]
        yield event[19:]
        raise AssertionError("must not drain open stream after matching response")

    response = _mcp_response(None, headers={"Content-Type": "text/event-stream"}, chunks=chunks())
    result = _mcp_transport(args, session, monkeypatch, response).fetch_mcp_control(handle)
    assert (
        result["receipt"]["parse_status"] == "ok"
        and result["control"]["protocol_version"] == "2025-03-26"
    )
    import hashlib

    assert (
        result["receipt"]["response_body_sha256"]
        == hashlib.sha256(b": keepalive\n\n" + event).hexdigest()
    )
    response.close.assert_called_once()
    assert session.request.call_count == 1


def _mcp_replies(
    *,
    timeout_method=None,
    wrong_search_id=False,
    tool_error=False,
    extra_required=False,
    reject_init=False,
):
    def respond(method, endpoint, **kwargs):
        message = kwargs.get("json", {})
        rpc_method = message.get("method")
        if timeout_method is not None and rpc_method == timeout_method:
            raise TimeoutError("synthetic transport timeout")
        if not rpc_method:  # Independently priced fallback REST route.
            return _mcp_response(
                {
                    "web": {
                        "results": [
                            {
                                "title": "Synthetic issuer",
                                "url": "https://fixture.example/ir",
                                "description": "Margin disclosure",
                                "page_age": "2026-10-01",
                            }
                        ]
                    }
                }
            )
        if rpc_method == "initialize":
            if reject_init:
                return _mcp_response({"error": {"code": "insufficient_quota"}}, status=401)
            return _mcp_response(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "synthetic", "version": "1"},
                    },
                },
                headers={"Mcp-Session-Id": "SYNTHETIC_SESSION"},
            )
        if rpc_method == "notifications/initialized":
            return _mcp_response(None, status=202)
        if rpc_method == "tools/list":
            schema = {
                "type": "object",
                "properties": {"search_query": {"type": "string"}},
                "required": ["search_query"],
                "additionalProperties": False,
            }
            if extra_required:
                schema["required"].append("unsupported_argument")
            return _mcp_response(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"tools": [{"name": "web_search_prime", "inputSchema": schema}]},
                }
            )
        assert rpc_method == "tools/call"
        return _mcp_response(
            {
                "jsonrpc": "2.0",
                "id": "FOREIGN_ID" if wrong_search_id else message["id"],
                "result": {
                    "isError": tool_error,
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                json.dumps(
                                    [
                                        {
                                            "title": "Synthetic issuer",
                                            "url": "https://fixture.example/ir",
                                            "summary": "Margin disclosure",
                                            "published_date": "2026-10-01",
                                        }
                                    ]
                                )
                            ),
                        }
                    ],
                },
            }
        )

    return respond


def test_mcp_complete_search_cold_and_reopen_warm_have_four_then_zero_http(tmp_path, monkeypatch):
    from tests.unit.test_quick_scan_external_context import _retrieve

    args, _, session, clock = _mcp_fixture(tmp_path, monkeypatch)
    session.request.side_effect = _mcp_replies()
    context = _retrieve(args)
    assert session.request.call_count == 4
    assert [call.kwargs["json"]["method"] for call in session.request.call_args_list] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ]
    search_call = session.request.call_args_list[-1].kwargs
    assert search_call["json"]["params"] == {
        "name": "web_search_prime",
        "arguments": {"search_query": "Synthetic issuer margin"},
    }
    assert search_call["headers"]["Mcp-Session-Id"] == "SYNTHETIC_SESSION"
    assert "SYNTHETIC_SESSION" not in json.dumps(
        context
    ) and "synthetic-test-only" not in json.dumps(context)
    totals = args["store"].get_quick_scan_budget_status("q09-fixture")
    assert (
        totals["requests"] == 4
        and totals["spent_micros"] == 12000
        and totals["reserved_micros"] == 0
    )
    assert args["store"].list_attempts(args["work_item_id"]) == []
    args["store"] = QuickScanWorkStore(args["store"].path, clock=lambda: clock[0])
    monkeypatch.delenv("IQS_SYNTHETIC_SEARCH_TOKEN")
    assert _retrieve(args) == context
    assert (
        session.request.call_count == 4
        and args["store"].get_quick_scan_budget_status("q09-fixture") == totals
    )


@pytest.mark.parametrize(
    "method,index",
    [("initialize", 0), ("notifications/initialized", 1), ("tools/list", 2), ("tools/call", 3)],
)
def test_mcp_unknown_at_each_actual_http_stops_and_reopens_without_any_resend(
    tmp_path, monkeypatch, method, index
):
    from src.utils.quick_scan_external_context import ExternalRetrievalBlocked
    from tests.unit.test_quick_scan_external_context import _retrieve

    args, _, session, clock = _mcp_fixture(tmp_path, monkeypatch)
    session.request.side_effect = _mcp_replies(timeout_method=method)
    with pytest.raises(ExternalRetrievalBlocked, match="unresolved"):
        _retrieve(args)
    assert session.request.call_count == index + 1
    totals = args["store"].get_quick_scan_budget_status("q09-fixture")
    assert (
        totals["requests"] == index + 1
        and totals["spent_micros"] == index * 3000
        and totals["reserved_micros"] == 6_000_000
    )
    args["store"] = QuickScanWorkStore(args["store"].path, clock=lambda: clock[0])
    with pytest.raises(ExternalRetrievalBlocked, match="unresolved"):
        _retrieve(args)
    assert (
        session.request.call_count == index + 1
        and args["store"].get_quick_scan_budget_status("q09-fixture") == totals
    )


@pytest.mark.parametrize("defect", ["wrong_search_id", "tool_error", "extra_required"])
def test_mcp_bad_search_or_discovery_never_becomes_financial_evidence(
    tmp_path, monkeypatch, defect
):
    from src.utils.quick_scan_external_context import ExternalRetrievalBlocked
    from tests.unit.test_quick_scan_external_context import _retrieve

    args, _, session, _ = _mcp_fixture(tmp_path, monkeypatch)
    session.request.side_effect = _mcp_replies(**{defect: True})
    with pytest.raises(ExternalRetrievalBlocked):
        _retrieve(args)
    expected = 3 if defect == "extra_required" else 4
    assert session.request.call_count == expected
    assert (
        args["store"].get_quick_scan_budget_status("q09-fixture")["spent_micros"] == expected * 3000
    )
    with pytest.raises(ExternalRetrievalBlocked):
        _retrieve(args)
    assert session.request.call_count == expected


@pytest.mark.parametrize(
    "defect",
    ["missing", "non_object", "missing_name", "name_type", "missing_version", "version_type"],
)
def test_mcp_invalid_server_info_stops_after_one_paid_http_and_reopens_without_resend(
    tmp_path, monkeypatch, defect
):
    from src.utils.quick_scan_external_context import ExternalRetrievalBlocked
    from tests.unit.test_quick_scan_external_context import _retrieve

    args, _, session, clock = _mcp_fixture(tmp_path, monkeypatch)
    ordinary = _mcp_replies()

    def reply(method, endpoint, **kwargs):
        message = kwargs["json"]
        if message["method"] != "initialize":
            return ordinary(method, endpoint, **kwargs)
        info = {"name": "synthetic", "version": "1"}
        if defect == "missing_name":
            info.pop("name")
        elif defect == "name_type":
            info["name"] = 17
        elif defect == "missing_version":
            info.pop("version")
        elif defect == "version_type":
            info["version"] = None
        result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}}
        if defect != "missing":
            result["serverInfo"] = [] if defect == "non_object" else info
        return _mcp_response({"jsonrpc": "2.0", "id": message["id"], "result": result})

    session.request.side_effect = reply
    with pytest.raises(ExternalRetrievalBlocked, match="mcp_control_unusable"):
        _retrieve(args)
    totals = args["store"].get_quick_scan_budget_status("q09-fixture")
    assert totals["requests"] == 1 and totals["spent_micros"] == 3000
    assert totals["reserved_micros"] == 0 and session.request.call_count == 1
    assert args["store"].list_attempts(args["work_item_id"]) == []
    args["store"] = QuickScanWorkStore(args["store"].path, clock=lambda: clock[0])
    with pytest.raises(ExternalRetrievalBlocked, match="mcp_control_unusable"):
        _retrieve(args)
    assert args["store"].get_quick_scan_budget_status("q09-fixture") == totals
    assert session.request.call_count == 1


def test_mcp_priced_control_rejection_can_use_next_search_route_without_repeating_handshake(
    tmp_path, monkeypatch
):
    from tests.unit.test_quick_scan_external_context import _retrieve

    args, _, session, _ = _mcp_fixture(tmp_path, monkeypatch, second_route=True)
    session.request.side_effect = _mcp_replies(reject_init=True)
    context = _retrieve(args)
    assert session.request.call_count == 2
    assert session.request.call_args_list[0].kwargs["json"]["method"] == "initialize"
    assert session.request.call_args_list[1].args[0] == "GET"
    assert context["retrievals"][0]["route_id"] == "brave-secondary"
    totals = args["store"].get_quick_scan_budget_status("q09-fixture")
    assert totals["requests"] == 2 and totals["spent_micros"] == 6000


def test_mcp_final_search_cannot_be_admitted_without_three_actual_controls(tmp_path, monkeypatch):
    args, request, session, _ = _mcp_fixture(tmp_path, monkeypatch)
    with pytest.raises(BudgetAdmissionError, match="mcp_parent_not_ready"):
        args["store"].begin_external_search(args["work_item_id"], args["lease"], **request)
    with closing(args["store"]._connect()) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM quick_scan_budget_attempt").fetchone()[0] == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM quick_scan_external_operation").fetchone()[0]
            == 0
        )
    session.request.assert_not_called()


def test_mcp_final_search_binding_cannot_be_rehashed_to_foreign_control_history(
    tmp_path, monkeypatch
):
    from src.utils.quick_scan_external_journal import _canonical, _digest
    from tests.unit.test_quick_scan_external_context import _retrieve

    args, _, session, _ = _mcp_fixture(tmp_path, monkeypatch)
    session.request.side_effect = _mcp_replies()
    _retrieve(args)
    with closing(args["store"]._connect()) as connection:
        row = connection.execute("SELECT * FROM quick_scan_mcp_search_binding").fetchone()
        metadata = json.loads(row["metadata_json"])
        assert "SYNTHETIC_SESSION" not in row["metadata_json"]
        metadata["stage_ids"][0] = "MCP_" + "f" * 64
        connection.execute("DROP TRIGGER quick_scan_mcp_search_no_update")
        connection.execute(
            "UPDATE quick_scan_mcp_search_binding SET metadata_json=?,metadata_sha256=?",
            (_canonical(metadata), _digest(metadata)),
        )
    monkeypatch.delenv("IQS_SYNTHETIC_SEARCH_TOKEN")
    with pytest.raises(ValueError, match="MCP.*binding"):
        _retrieve(args)
    assert session.request.call_count == 4


def test_mcp_search_only_pricing_does_not_hide_control_http_cost(tmp_path, monkeypatch):
    from src.config.quick_scan_search_policy import _admit
    from tests.unit.test_quick_scan_external_context import _execution_policy

    def invalid(document):
        route = document["external_routes"][0]
        route.update(
            kind="zai_mcp_streamable", endpoint="https://api.z.ai/api/mcp/web_search_prime/mcp"
        )
        route["pricing"]["basis"] = "per_search"
        route["metering"].update(charge_policy="successful_search_only", usage_unit="search_calls")

    policy = _execution_policy(tmp_path, monkeypatch, invalid)
    assert policy.admitted == ()
    assert _admit(policy.routes[0], require_storage_rights=True) == (
        False,
        "mcp_control_pricing_required",
    )
