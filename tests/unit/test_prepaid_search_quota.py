"""Real Q09 store/MCP journal, no network or credentials beyond synthetic fixtures."""

import copy
import json
from contextlib import closing

import pytest

from src.config.quick_scan_search_policy import (
    SearchPolicyRejected,
    execution_query_plans,
    load_search_policy,
)
from src.utils.quick_scan_external_context import build_shared_external_budget_policy
from src.utils.quick_scan_work_store import BudgetAdmissionError, QuickScanWorkStore
from tests.unit.test_quick_scan_external_context import _coordinator_fixture
from tests.unit.test_quick_scan_mcp import _control_receipt, _mcp_fixture, _stage


def _quota(document):
    document["schema_version"] = "1.3.0"
    r = document["external_routes"][0]
    r.update(kind="zai_mcp_streamable", endpoint="https://api.z.ai/api/mcp/web_search_prime/mcp")
    r["pricing"].update(unit_cost_micros=None, per_request_cap_micros=3_000_000)
    r["metering"].update(charge_policy="prepaid_quota", usage_unit="glm_coding_plan_credits")
    r["prepaid_quota"] = dict(
        schema="stockqa.prepaid_search_quota/1.0.0",
        enabled=True,
        unit="glm_coding_plan_credits",
        account_group="synthetic_glm_account",
        max_quota_micros=2_400_000,
        search_quota_micros=1_200_000,
        source_ref="https://docs.z.ai/guides/tools/web-search",
        entitlement_ref="synthetic-test-entitlement-not-real",
        control_accounting="protocol_category_no_tool_invocation",
        unknown_usage_action="retain_reservation_and_pause",
    )
    for q in document["execution_plan"]["queries"]:
        q.update(operation="search", source_operation_id=None, source_receipt_sha256=None)


def _fixture(tmp_path, monkeypatch):
    args, session, clock = _coordinator_fixture(tmp_path, monkeypatch, mutate=_quota)
    policy = build_shared_external_budget_policy(args["model_policy"], args["policy"])
    args["store"].configure_prepaid_search_quota(policy, args["policy"])
    request = dict(
        policy=policy,
        plan=execution_query_plans(args["policy"])[0],
        route_id="brave-primary",
        provider="zai_mcp_streamable",
        quota_group="search-brave",
        adapter_version="stockqa.external_retrieval/1.0.0",
        search_policy_sha256=args["policy"].policy_sha256,
        endpoint="https://api.z.ai/api/mcp/web_search_prime/mcp",
    )
    return args, request, clock


def _done(args, request, name):
    controls = {
        "initialize": {"protocol_version": "2025-03-26", "session_id": "SYNTHETIC"},
        "initialized": {"initialized": True},
        "discovery": {"tool_name": "webSearchPrime", "input_schema_sha256": "a" * 64},
    }
    store = args["store"]
    stage = _stage(args, request, name)
    store.consume_mcp_stage(stage["stage_id"], budget_attempt_id=stage["budget_attempt_id"])
    receipt = _control_receipt(stage, status=202 if name == "initialized" else 200)
    result = store.record_mcp_stage(stage["stage_id"], receipt=receipt, control=controls[name])
    assert result["budget"]["status"] == "response_unpriced"
    assert result["budget"]["actual_cost_micros"] is None
    assert result["quota_accounting"]["actual_quota_micros"] == 0
    assert result["quota_accounting"]["provider_bill_reconciled"] is False
    assert (
        store.record_mcp_stage(stage["stage_id"], receipt=receipt, control=controls[name]) == result
    )
    return result


def _search(args, request):
    store = args["store"]
    op = store.begin_external_search(args["work_item_id"], args["lease"], **request)
    store.consume_external_search(op["operation_id"], budget_attempt_id=op["budget_attempt_id"])
    receipt = _control_receipt({"budget_attempt_id": op["budget_attempt_id"]})
    receipt.update(
        parse_status="ok",
        provider_result_count=1,
        entries=[
            dict(
                url="https://fixture.example/ir",
                title="Synthetic issuer",
                snippet="Margin disclosure",
                publisher=None,
                published_at="2026-10-01",
                query=request["plan"]["query"],
            )
        ],
    )
    result = store.record_external_search(op["operation_id"], receipt=receipt)
    assert result["quota_accounting"]["actual_quota_micros"] == 1_200_000
    assert result["budget"]["actual_cost_micros"] is None
    return result


def test_complete_protocol_and_search_use_quota_without_fabricated_cash(tmp_path, monkeypatch):
    args, req, clock = _fixture(tmp_path, monkeypatch)
    for name in ("initialize", "initialized", "discovery"):
        _done(args, req, name)
    result = _search(args, req)
    totals = args["store"].get_quick_scan_budget_status("q09-fixture")
    assert totals["spent_micros"] == 0 and totals["reserved_micros"] == 24_000_000
    assert totals["requests"] == 4 and totals["unreconciled_attempts"] == 4
    assert totals["blocking_unreconciled_attempts"] == 0 and totals["cash_actual_complete"] is False
    assert totals["quota_accounts"][0]["used_quota_micros"] == 1_200_000
    args["store"] = QuickScanWorkStore(args["store"].path, clock=lambda: clock[0])
    assert args["store"].get_external_search(result["operation_id"]) == result
    assert (
        args["store"].begin_external_search(args["work_item_id"], args["lease"], **req)[
            "send_required"
        ]
        is False
    )


def test_unknown_control_never_receives_quota_proof_or_replays(tmp_path, monkeypatch):
    args, req, _ = _fixture(tmp_path, monkeypatch)
    store = args["store"]
    stage = _stage(args, req, "initialize")
    store.consume_mcp_stage(stage["stage_id"], budget_attempt_id=stage["budget_attempt_id"])
    result = store.record_mcp_stage(
        stage["stage_id"],
        receipt=_control_receipt(stage, outcome="unknown", status=None),
        control=None,
    )
    assert result["quota_accounting"] is None
    with pytest.raises(BudgetAdmissionError):
        _stage(args, req, "initialized")
    with closing(store._connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM quick_scan_quota_proof").fetchone()[0] == 0


def test_legacy_unpriced_behavior_is_unchanged(tmp_path, monkeypatch):
    args, req, _, _ = _mcp_fixture(tmp_path, monkeypatch)
    store = args["store"]
    stage = _stage(args, req, "initialize")
    store.consume_mcp_stage(stage["stage_id"], budget_attempt_id=stage["budget_attempt_id"])
    store.record_mcp_stage(
        stage["stage_id"],
        receipt=_control_receipt(stage),
        control={"protocol_version": "2025-03-26", "session_id": None},
    )
    with pytest.raises(BudgetAdmissionError):
        _stage(args, req, "initialized")


@pytest.mark.parametrize(
    "field,value",
    [("unit", "USD"), ("search_quota_micros", 0), ("max_quota_micros", True), ("enabled", False)],
)
def test_invalid_quota_configs_send_nothing(tmp_path, monkeypatch, field, value):
    def mutate(d):
        _quota(d)
        d["external_routes"][0]["prepaid_quota"][field] = value

    with pytest.raises((SearchPolicyRejected, ValueError)):
        _coordinator_fixture(tmp_path, monkeypatch, mutate=mutate)


def test_entire_original_coordinator_cold_four_warm_zero_http(tmp_path, monkeypatch):
    from tests.unit.test_quick_scan_external_context import _retrieve
    from tests.unit.test_quick_scan_mcp import _mcp_replies

    args, session, clock = _coordinator_fixture(tmp_path, monkeypatch, mutate=_quota)
    session.request.side_effect = _mcp_replies()
    context = _retrieve(args)
    assert len(context["sources"]) == 1 and session.request.call_count == 4
    args["store"] = QuickScanWorkStore(args["store"].path, clock=lambda: clock[0])
    assert _retrieve(args) == context and session.request.call_count == 4
    totals = args["store"].get_quick_scan_budget_status("q09-fixture")
    assert totals["quota_accounts"][0]["used_quota_micros"] == 1_200_000


def test_cap_is_atomic_and_never_resets_with_reopen_or_new_plan(tmp_path, monkeypatch):
    args, req, clock = _fixture(tmp_path, monkeypatch)
    for index in range(3):
        current = copy.deepcopy(req)
        current["plan"]["query_id"] = "query_" + str(index)
        current["plan"]["query"] = "Synthetic issuer margin " + str(index)
        for name in ("initialize", "initialized", "discovery"):
            _done(args, current, name)
        if index < 2:
            _search(args, current)
            args["store"] = QuickScanWorkStore(args["store"].path, clock=lambda: clock[0])
        else:
            before = args["store"].get_quick_scan_budget_status("q09-fixture")
            with pytest.raises(BudgetAdmissionError, match="prepaid_quota_limit"):
                args["store"].begin_external_search(args["work_item_id"], args["lease"], **current)
            assert args["store"].get_quick_scan_budget_status("q09-fixture") == before


def test_cash_fabrication_rolls_back_result_and_preserves_original_send(tmp_path, monkeypatch):
    args, req, _ = _fixture(tmp_path, monkeypatch)
    store = args["store"]
    stage = _stage(args, req, "initialize")
    store.consume_mcp_stage(stage["stage_id"], budget_attempt_id=stage["budget_attempt_id"])
    with pytest.raises(ValueError, match="manufacture cash"):
        store.record_mcp_stage(
            stage["stage_id"],
            receipt=_control_receipt(stage),
            control={"protocol_version": "2025-03-26", "session_id": None},
            actual_cost=0,
            cost_source_ref="fake-zero",
        )
    saved = store.get_mcp_stage(stage["stage_id"])
    assert saved["result"] is None and saved["budget"]["status"] == "in_flight"


def test_no_native_model_attempt_can_claim_search_quota(tmp_path, monkeypatch):
    import sqlite3

    from src.utils.quick_scan_work_store import _budget_policy_projection

    args, req, _ = _fixture(tmp_path, monkeypatch)
    store = args["store"]
    native = store.reserve_budget_attempt(
        req["policy"],
        budget_attempt_id="NATIVE_ONLY",
        route_id="route-a1",
        provider="provider-a1",
        model_requested="fixture-a",
        quota_group="A",
    )
    with store._transaction() as c:
        with pytest.raises(sqlite3.IntegrityError, match="invalid native quota reservation"):
            c.execute(
                "INSERT INTO quick_scan_quota_reservation VALUES (?,?,?,?)",
                ("NATIVE_ONLY", "synthetic_glm_account", "search_tool", 1_200_000),
            )


def test_mcp_result_cannot_create_proof_without_dispatch(tmp_path, monkeypatch):
    from src.utils.quick_scan_work_store import WorkConflictError

    args, req, _ = _fixture(tmp_path, monkeypatch)
    store = args["store"]
    stage = _stage(args, req, "initialize")
    with pytest.raises(WorkConflictError):
        store.record_mcp_stage(
            stage["stage_id"],
            receipt=_control_receipt(stage),
            control={"protocol_version": "2025-03-26", "session_id": None},
        )
    assert store.get_mcp_stage(stage["stage_id"])["quota_accounting"] is None


def test_concurrent_account_group_reservations_cannot_overspend(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    args, req, _ = _fixture(tmp_path, monkeypatch)
    for name in ("initialize", "initialized", "discovery"):
        _done(args, req, name)
    _search(args, req)
    requests = []
    for index in range(2):
        r = copy.deepcopy(req)
        r["plan"].update(
            query_id="concurrent_" + str(index), query="Synthetic margin " + str(index)
        )
        for name in ("initialize", "initialized", "discovery"):
            _done(args, r, name)
        requests.append(r)
    from src.utils.quick_scan_prepaid_quota import reserve_tx
    from src.utils.quick_scan_work_store import _budget_policy_projection

    normalized = _budget_policy_projection(req["policy"])
    context = {
        "budget": {
            "policy_id": normalized["policy_id"],
            "policy_version": normalized["policy_version"],
        },
        "route": {
            "route_id": req["route_id"],
            "provider": req["provider"],
            "quota_group": req["quota_group"],
        },
        "search_policy_sha256": req["search_policy_sha256"],
    }

    def begin(index):
        try:
            with args["store"]._transaction() as c:
                args["store"]._reserve_budget_attempt_tx(
                    c,
                    normalized_policy=normalized,
                    budget_attempt_id="CONCURRENT_" + str(index),
                    work_attempt_id=None,
                    route_id=req["route_id"],
                    provider=req["provider"],
                    model_requested="external_retrieval_v1",
                    quota_group=req["quota_group"],
                    now=args["store"]._now(),
                )
                reserve_tx(c, "CONCURRENT_" + str(index), context, "search_tool")
            return "reserved"
        except BudgetAdmissionError as e:
            return e.reason

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(begin, range(2))) == ["prepaid_quota_limit", "reserved"]
    with closing(args["store"]._connect()) as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM quick_scan_budget_attempt WHERE budget_attempt_id LIKE 'CONCURRENT_%'"
            ).fetchone()[0]
            == 1
        )


def test_schema20_migration_preserves_every_old_budget_value(tmp_path, monkeypatch):
    from src.utils.quick_scan_work_store import _MCP_GUARD_OLD, SCHEMA_VERSION

    args, req, _, clock = _mcp_fixture(tmp_path, monkeypatch)
    stage = _stage(args, req, "initialize")
    store = args["store"]
    store.consume_mcp_stage(stage["stage_id"], budget_attempt_id=stage["budget_attempt_id"])
    store.record_mcp_stage(
        stage["stage_id"],
        receipt=_control_receipt(stage, outcome="unknown", status=None),
        control=None,
    )
    with store._transaction() as c:
        before = [tuple(row) for row in c.execute("SELECT * FROM quick_scan_budget_attempt")]
        # Build an actual v20 fixture, including removal of later v22 objects.
        c.execute("DROP TRIGGER quick_scan_external_research_admission_no_update")
        c.execute("DROP TRIGGER quick_scan_external_research_admission_no_delete")
        c.execute("DROP TABLE quick_scan_external_research_admission")
        c.execute("DROP TRIGGER quick_scan_mcp_search_insert_guard")
        for table in ("proof", "reservation", "route", "account"):
            c.execute("DROP TABLE quick_scan_quota_" + table)
        c.execute(_MCP_GUARD_OLD)
        c.execute("PRAGMA user_version=20")
    reopened = QuickScanWorkStore(store.path, clock=lambda: clock[0])
    with closing(reopened._connect()) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert [
            tuple(row) for row in c.execute("SELECT * FROM quick_scan_budget_attempt")
        ] == before
        assert c.execute("SELECT COUNT(*) FROM quick_scan_quota_proof").fetchone()[0] == 0
    with pytest.raises(BudgetAdmissionError):
        reopened.begin_mcp_stage(args["work_item_id"], args["lease"], stage="initialized", **req)


def test_configuration_and_proof_are_immutable_after_recording(tmp_path, monkeypatch):
    import sqlite3

    args, req, _ = _fixture(tmp_path, monkeypatch)
    store = args["store"]
    _done(args, req, "initialize")
    for table in ("account", "route", "reservation", "proof"):
        with store._transaction() as c:
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                c.execute("DELETE FROM quick_scan_quota_" + table)


def test_account_alias_cannot_reset_existing_credential_quota(tmp_path, monkeypatch):
    from src.utils.quick_scan_work_store import BudgetPolicyConflict

    args, req, _ = _fixture(tmp_path, monkeypatch)
    raw = json.loads((tmp_path / "execution-policy.json").read_text())
    raw["external_routes"][0]["prepaid_quota"]["account_group"] = "renamed_account"
    path = tmp_path / "renamed.json"
    path.write_text(json.dumps(raw))
    renamed = load_search_policy(path)
    shared = build_shared_external_budget_policy(args["model_policy"], renamed)
    with pytest.raises(BudgetPolicyConflict, match="cannot be renamed"):
        args["store"].configure_prepaid_search_quota(shared, renamed)


def test_unregistered_search_policy_cannot_bypass_frozen_quota_route(tmp_path, monkeypatch):
    args, req, _ = _fixture(tmp_path, monkeypatch)
    req = copy.deepcopy(req)
    req["search_policy_sha256"] = "b" * 64
    before = args["store"].get_quick_scan_budget_status("q09-fixture")
    with pytest.raises(ValueError, match="not frozen"):
        _stage(args, req, "initialize")
    assert args["store"].get_quick_scan_budget_status("q09-fixture") == before


def test_later_cash_bill_reconciliation_preserves_quota_history(tmp_path, monkeypatch):
    from src.utils.quick_scan_work_store import WorkConflictError

    args, req, clock = _fixture(tmp_path, monkeypatch)
    record = _done(args, req, "initialize")
    store = args["store"]
    bid = record["budget_attempt_id"]
    with pytest.raises(WorkConflictError):
        store.reconcile_budget_attempt(
            bid,
            resolved_outcome="confirmed_not_sent",
            actual_cost=0,
            cost_source_ref="synthetic-invalid-not-sent",
        )
    store.reconcile_budget_attempt(
        bid,
        resolved_outcome="completed",
        actual_cost=0.003,
        cost_source_ref="synthetic-later-itemized-bill",
    )
    reopened = QuickScanWorkStore(store.path, clock=lambda: clock[0])
    saved = reopened.get_mcp_stage(record["stage_id"])
    assert saved["quota_accounting"]["state"] == "quota_accounted_cash_settled"
    assert saved["quota_accounting"]["cash_actual"] == "0.003"
    assert saved["quota_accounting"]["actual_quota_micros"] == 0
    assert (
        reopened.get_quick_scan_budget_status("q09-fixture")["blocking_unreconciled_attempts"] == 0
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_checked_at", "2026-99-99"),
        ("source_ref", "   "),
        ("max_usage_units_per_request", 2),
    ],
)
def test_prepaid_admission_keeps_source_and_single_call_bounds(tmp_path, monkeypatch, field, value):
    from tests.unit.test_quick_scan_external_context import _execution_policy

    def mutate(d):
        _quota(d)
        d["external_routes"][0]["metering"][field] = value

    policy = _execution_policy(tmp_path, monkeypatch, mutate)
    assert policy.admitted == ()
