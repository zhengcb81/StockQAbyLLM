import asyncio
import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from src.providers.llm_client import AsyncLLMClient, LLMClient
from src.utils.quick_scan_work_store import (
    BudgetAdmissionError,
    BudgetPolicyConflict,
    QuickScanWorkStore,
    WorkConflictError,
)
from src.utils.quick_scan_work_transport import (
    QuickScanBudgetDeferredError,
    bind_quick_scan_budget,
    bind_quick_scan_route,
)


def _policy(
    *,
    policy_version="q09-fixture@v1",
    max_cost=10,
    max_requests=20,
    max_cost_per_attempt=6,
    max_in_flight_total=4,
    model="fixture-a",
):
    return {
        "configured": True,
        "policy_id": "q09-fixture",
        "policy_version": policy_version,
        "budget": {
            "currency": "USD",
            "max_cost": max_cost,
            "max_requests": max_requests,
            "max_cost_per_attempt": max_cost_per_attempt,
        },
        "cost_policy": {
            "pricing_basis": "verified_rate_card",
            "pricing_ref": "fixture-price-v1",
            "reserve_before_dispatch": True,
            "unknown_actual_cost_action": "retain_reservation_and_pause",
        },
        "dispatch": {"max_in_flight_total": max_in_flight_total},
        "quota_groups": [
            {"id": "A", "max_in_flight": 2},
            {"id": "B", "max_in_flight": 2},
        ],
        "routes": [
            {
                "id": "route-a1",
                "provider_config_ref": "provider-a1",
                "model": model,
                "quota_group": "A",
                "max_in_flight": 1,
                "eligible": True,
                "unavailable_reason": None,
            },
            {
                "id": "route-a2",
                "provider_config_ref": "provider-a2",
                "model": "fixture-a2",
                "quota_group": "A",
                "max_in_flight": 1,
                "eligible": True,
                "unavailable_reason": None,
            },
            {
                "id": "route-a3",
                "provider_config_ref": "provider-a3",
                "model": "fixture-a3",
                "quota_group": "A",
                "max_in_flight": 1,
                "eligible": True,
                "unavailable_reason": None,
            },
            {
                "id": "route-b1",
                "provider_config_ref": "provider-b1",
                "model": "fixture-b1",
                "quota_group": "B",
                "max_in_flight": 1,
                "eligible": True,
                "unavailable_reason": None,
            },
            {
                "id": "route-b2",
                "provider_config_ref": "provider-b2",
                "model": "fixture-b2",
                "quota_group": "B",
                "max_in_flight": 1,
                "eligible": True,
                "unavailable_reason": None,
            },
        ],
    }


def _reserve(store, policy, attempt_id, route_id="route-a1"):
    route = next(route for route in policy["routes"] if route["id"] == route_id)
    return store.reserve_budget_attempt(
        policy,
        budget_attempt_id=attempt_id,
        route_id=route_id,
        provider=route["provider_config_ref"],
        model_requested=route["model"],
        quota_group=route["quota_group"],
    )


def _new_work(store, suffix):
    item = store.create_or_attach(
        entity_id=f"ENT_Q09_{suffix}",
        question_id="CORE_01",
        generation=1,
        scope="entity",
        scope_id=f"ENT_Q09_{suffix}",
        identity_revision=1,
        source_binding_version=1,
        identity_state="verified",
        source_binding_ref=f"BND_Q09_{suffix}",
        source_binding_refs=[f"BND_Q09_{suffix}"],
        identity_snapshot_sha256=hashlib.sha256(b"identity").hexdigest(),
        question_fingerprint=hashlib.sha256(b"question").hexdigest(),
        routing_fingerprint=hashlib.sha256(b"routing").hexdigest(),
        run_id=f"RUN_Q09_{suffix}",
        scan_id=f"SCAN_Q09_{suffix}",
    )
    lease = store.claim(item["work_item_id"], lease_seconds=30)
    assert lease is not None
    attempt = store.prepare_attempt(
        item["work_item_id"],
        lease,
        route_id="route-a1",
        provider="provider-a1",
        model_requested="fixture-a",
        request_cache_key="REQ_" + hashlib.sha256(suffix.encode()).hexdigest(),
        prompt_sha256=hashlib.sha256((suffix + "-prompt").encode()).hexdigest(),
    )
    return item["work_item_id"], lease, attempt


def test_two_connections_atomically_admit_only_one_over_budget_reservation(tmp_path):
    path = tmp_path / "budget.sqlite"
    store_a = QuickScanWorkStore(path)
    store_b = QuickScanWorkStore(path)
    policy = _policy(max_cost=10, max_cost_per_attempt=6)

    def reserve(store, attempt_id):
        try:
            _reserve(store, policy, attempt_id)
            return "admitted"
        except BudgetAdmissionError as error:
            return error.reason

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda pair: reserve(*pair),
                [(store_a, "DISPATCH_race_a"), (store_b, "DISPATCH_race_b")],
            )
        )

    assert results.count("admitted") == 1
    assert results.count("budget_cost_limit") == 1
    status = store_a.get_quick_scan_budget_status(policy["policy_id"])
    assert status["reserved_micros"] == 6_000_000
    assert status["spent_micros"] + status["reserved_micros"] <= 10_000_000


def test_over_budget_exact_settlement_blocks_fallback_under_same_policy(tmp_path):
    store = QuickScanWorkStore(tmp_path / "budget.sqlite")
    policy = _policy(max_cost=10, max_cost_per_attempt=3)
    _reserve(store, policy, "DISPATCH_primary")
    store.reconcile_budget_attempt(
        "DISPATCH_primary",
        resolved_outcome="confirmed_failure",
        actual_cost=8,
        cost_source_ref="provider-invoice-fixture",
    )

    with pytest.raises(BudgetAdmissionError, match="budget_cost_limit"):
        _reserve(store, policy, "DISPATCH_backup", route_id="route-a2")

    status = store.get_quick_scan_budget_status(policy["policy_id"])
    assert status["spent_micros"] == 8_000_000
    assert status["reserved_micros"] == 0
    assert status["requests"] == 1


def test_unknown_timeout_retains_cost_and_slot_until_reconciliation(tmp_path):
    store = QuickScanWorkStore(tmp_path / "budget.sqlite")
    policy = _policy(max_cost=10, max_cost_per_attempt=2)
    _reserve(store, policy, "DISPATCH_timeout")
    store.record_budget_outcome("DISPATCH_timeout", outcome="unknown", http_status_code=None)

    status = store.get_quick_scan_budget_status(policy["policy_id"])
    assert status["reserved_micros"] == 2_000_000
    assert status["in_flight"] == 1
    assert status["requests"] == 1
    with pytest.raises(BudgetAdmissionError, match="budget_reconciliation_required"):
        _reserve(store, policy, "DISPATCH_no_blind_retry")

    store.reconcile_budget_attempt(
        "DISPATCH_timeout",
        resolved_outcome="completed",
        actual_cost=0.75,
        cost_source_ref="provider-usage-fixture",
    )
    status = store.get_quick_scan_budget_status(policy["policy_id"])
    assert status["spent_micros"] == 750_000
    assert status["reserved_micros"] == 0
    assert status["in_flight"] == 0
    _reserve(store, policy, "DISPATCH_after_reconciliation")


def test_success_with_unknown_actual_cost_releases_slot_but_pauses_new_dispatch(tmp_path):
    store = QuickScanWorkStore(tmp_path / "budget.sqlite")
    policy = _policy(max_cost=10, max_cost_per_attempt=2)
    _reserve(store, policy, "DISPATCH_unpriced")
    store.record_budget_outcome(
        "DISPATCH_unpriced", outcome="response_available", http_status_code=200
    )

    status = store.get_quick_scan_budget_status(policy["policy_id"])
    assert status["reserved_micros"] == 2_000_000
    assert status["in_flight"] == 0
    assert status["unreconciled_attempts"] == 1
    with pytest.raises(BudgetAdmissionError, match="budget_reconciliation_required"):
        _reserve(store, policy, "DISPATCH_paused")

    store.reconcile_budget_attempt(
        "DISPATCH_unpriced",
        resolved_outcome="completed",
        actual_cost=0,
        cost_source_ref="verified-zero-charge-fixture",
    )
    _reserve(store, policy, "DISPATCH_resumed")


@pytest.mark.parametrize(
    "first_outcome,replay_outcome,expected_first_count",
    [
        ("confirmed_not_sent", "completed", 0),
        ("completed", "confirmed_not_sent", 1),
    ],
)
def test_reconciliation_replay_rejects_changed_request_count_semantics(
    tmp_path, first_outcome, replay_outcome, expected_first_count
):
    store = QuickScanWorkStore(tmp_path / "budget.sqlite")
    policy = _policy(max_cost=10, max_cost_per_attempt=2)
    _reserve(store, policy, "DISPATCH_reconcile_replay")
    first = store.reconcile_budget_attempt(
        "DISPATCH_reconcile_replay",
        resolved_outcome=first_outcome,
        actual_cost=0,
        cost_source_ref="fixture-reconciliation",
    )

    assert first["request_counted"] == expected_first_count
    with pytest.raises(WorkConflictError, match="reconciled differently"):
        store.reconcile_budget_attempt(
            "DISPATCH_reconcile_replay",
            resolved_outcome=replay_outcome,
            actual_cost=0,
            cost_source_ref="fixture-reconciliation",
        )
    assert (
        store.get_quick_scan_budget_status(policy["policy_id"])["requests"] == expected_first_count
    )


@pytest.mark.parametrize(
    "first_outcome,replay_outcome,expected_first_count",
    [
        ("completed", "confirmed_failure", 1),
        ("confirmed_failure", "completed", 1),
    ],
)
def test_reconciliation_replay_rejects_changed_terminal_outcome(
    tmp_path, first_outcome, replay_outcome, expected_first_count
):
    store = QuickScanWorkStore(tmp_path / "budget.sqlite")
    policy = _policy(max_cost=10, max_cost_per_attempt=2)
    _reserve(store, policy, "DISPATCH_terminal_outcome")
    first = store.reconcile_budget_attempt(
        "DISPATCH_terminal_outcome",
        resolved_outcome=first_outcome,
        actual_cost=0,
        cost_source_ref="fixture-reconciliation",
    )

    assert first["request_counted"] == expected_first_count
    assert (
        store.reconcile_budget_attempt(
            "DISPATCH_terminal_outcome",
            resolved_outcome=first_outcome,
            actual_cost=0,
            cost_source_ref="fixture-reconciliation",
        )
        == first
    )
    with pytest.raises(WorkConflictError, match="reconciled differently"):
        store.reconcile_budget_attempt(
            "DISPATCH_terminal_outcome",
            resolved_outcome=replay_outcome,
            actual_cost=0,
            cost_source_ref="fixture-reconciliation",
        )
    assert store.get_quick_scan_budget_status(policy["policy_id"])["requests"] == 1


def test_exact_decimal_cost_ceiling_preserves_subcontext_precision(tmp_path):
    store = QuickScanWorkStore(tmp_path / "budget.sqlite")
    policy = _policy(max_cost=10, max_cost_per_attempt=2)
    _reserve(store, policy, "DISPATCH_exact_decimal")

    settled = store.record_budget_outcome(
        "DISPATCH_exact_decimal",
        outcome="response_available",
        http_status_code=200,
        actual_cost=Decimal("0.0000010000000000000000000000000000000001"),
        cost_source_ref="fixture-exact-decimal",
    )

    assert settled["actual_cost_micros"] == 2


def test_settled_transport_replay_rejects_changed_terminal_outcome(tmp_path):
    store = QuickScanWorkStore(tmp_path / "budget.sqlite")
    policy = _policy(max_cost=10, max_cost_per_attempt=2)
    _reserve(store, policy, "DISPATCH_transport_outcome")
    first = store.record_budget_outcome(
        "DISPATCH_transport_outcome",
        outcome="unknown",
        http_status_code=503,
        actual_cost=0,
        cost_source_ref="fixture-transport-outcome",
    )
    assert (
        store.record_budget_outcome(
            "DISPATCH_transport_outcome",
            outcome="unknown",
            http_status_code=503,
            actual_cost=0,
            cost_source_ref="fixture-transport-outcome",
        )
        == first
    )

    with pytest.raises(WorkConflictError, match="different outcome"):
        store.record_budget_outcome(
            "DISPATCH_transport_outcome",
            outcome="confirmed_failure",
            http_status_code=503,
            actual_cost=0,
            cost_source_ref="fixture-transport-outcome",
        )


def test_v3_migration_preserves_settlement_and_marks_legacy_outcome_unverifiable(tmp_path):
    path = tmp_path / "budget-v3-migration.sqlite"
    store = QuickScanWorkStore(path)
    policy = _policy(max_cost=10, max_cost_per_attempt=2)
    _reserve(store, policy, "DISPATCH_legacy_settled")
    store.reconcile_budget_attempt(
        "DISPATCH_legacy_settled",
        resolved_outcome="completed",
        actual_cost=0.5,
        cost_source_ref="fixture-legacy-reconciliation",
    )
    with closing(sqlite3.connect(path)) as connection, connection:
        before = connection.execute(
            "SELECT status,actual_cost_micros,request_counted FROM quick_scan_budget_attempt "
            "WHERE budget_attempt_id='DISPATCH_legacy_settled'"
        ).fetchone()
        connection.execute("DROP TRIGGER quick_scan_budget_terminal_no_update")
        connection.execute("DROP TRIGGER quick_scan_budget_terminal_no_delete")
        connection.execute("DROP TABLE quick_scan_budget_terminal")
        for trigger in (
            "quick_scan_delivery_package_immutable",
            "quick_scan_delivery_state_transition",
            "quick_scan_delivery_no_delete",
            "quick_scan_delivery_event_no_update",
            "quick_scan_delivery_event_no_delete",
            "quick_scan_delivery_ack_immutable",
        ):
            connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute("DROP TABLE quick_scan_result_delivery_event")
        connection.execute("DROP TABLE quick_scan_result_delivery")
        connection.execute("PRAGMA user_version=3")

    migrated = QuickScanWorkStore(path)

    with closing(migrated._connect()) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert (
            connection.execute("SELECT COUNT(*) FROM quick_scan_result_delivery").fetchone()[0] == 0
        )
        terminal = connection.execute(
            "SELECT terminal_outcome FROM quick_scan_budget_terminal "
            "WHERE budget_attempt_id='DISPATCH_legacy_settled'"
        ).fetchone()
        after = connection.execute(
            "SELECT status,actual_cost_micros,request_counted FROM quick_scan_budget_attempt "
            "WHERE budget_attempt_id='DISPATCH_legacy_settled'"
        ).fetchone()
    assert tuple(after) == tuple(before)
    assert terminal["terminal_outcome"] == "legacy_unverified"
    with pytest.raises(WorkConflictError, match="reconciled differently"):
        migrated.reconcile_budget_attempt(
            "DISPATCH_legacy_settled",
            resolved_outcome="completed",
            actual_cost=0.5,
            cost_source_ref="fixture-legacy-reconciliation",
        )


def test_policy_version_upgrade_keeps_spend_request_count_and_cannot_change_in_flight(tmp_path):
    store = QuickScanWorkStore(tmp_path / "budget.sqlite")
    first = _policy(max_cost=10, max_cost_per_attempt=2)
    _reserve(store, first, "DISPATCH_v1")
    store.record_budget_outcome(
        "DISPATCH_v1",
        outcome="response_available",
        http_status_code=200,
        actual_cost=1.25,
        cost_source_ref="fixture-price-v1",
    )
    upgraded = _policy(
        policy_version="q09-fixture@v2",
        max_cost=20,
        max_cost_per_attempt=2,
        model="fixture-a-v2",
    )
    store.configure_quick_scan_budget(upgraded)

    status = store.get_quick_scan_budget_status(first["policy_id"])
    assert status["policy_version"] == "q09-fixture@v2"
    assert status["spent_micros"] == 1_250_000
    assert status["requests"] == 1
    _reserve(store, upgraded, "DISPATCH_v2")

    with pytest.raises(BudgetPolicyConflict, match="active or unreconciled"):
        store.configure_quick_scan_budget(
            _policy(policy_version="q09-fixture@v3", model="fixture-a-v3")
        )


def test_group_and_route_concurrency_limits_are_enforced_independently(tmp_path):
    store = QuickScanWorkStore(tmp_path / "budget.sqlite")
    policy = _policy(max_cost=100, max_cost_per_attempt=1)
    _reserve(store, policy, "DISPATCH_route_a1")
    with pytest.raises(BudgetAdmissionError, match="dispatch_route_capacity_full"):
        _reserve(store, policy, "DISPATCH_route_a1_again")
    _reserve(store, policy, "DISPATCH_route_a2", route_id="route-a2")
    with pytest.raises(BudgetAdmissionError, match="dispatch_quota_group_capacity_full"):
        _reserve(store, policy, "DISPATCH_route_a3", route_id="route-a3")


def test_budget_denial_rolls_back_work_send_intent_and_linked_reservation(tmp_path):
    store = QuickScanWorkStore(tmp_path / "budget.sqlite")
    policy = _policy(max_cost=6, max_cost_per_attempt=6)
    _reserve(store, policy, "DISPATCH_spends_reservation")
    work_id, lease, attempt = _new_work(store, "atomic")

    with pytest.raises(BudgetAdmissionError, match="budget_cost_limit"):
        store.mark_send_intent(
            work_id,
            lease,
            attempt["attempt_id"],
            budget_policy=policy,
            budget_route={
                "route_id": "route-a1",
                "provider": "provider-a1",
                "model_requested": "fixture-a",
                "quota_group": "A",
            },
        )

    assert store.list_attempts(work_id)[0]["phase"] == "prepared"
    status = store.get_quick_scan_budget_status(policy["policy_id"])
    assert status["requests"] == 1
    assert status["reserved_micros"] == 6_000_000
    with closing(store._connect()) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM quick_scan_budget_attempt WHERE work_attempt_id=?",
                (attempt["attempt_id"],),
            ).fetchone()[0]
            == 0
        )


def test_budget_denial_happens_before_sync_http_post(tmp_path, monkeypatch):
    store = QuickScanWorkStore(tmp_path / "budget.sqlite")
    policy = _policy(max_cost=6, max_cost_per_attempt=6)
    _reserve(store, policy, "DISPATCH_exhausts_budget")
    session = Mock()
    monkeypatch.setattr(
        "src.providers.llm_client.http_client_manager.get_sync_session",
        lambda: session,
    )
    client = LLMClient(
        api_key="fixture-only",
        model="fixture-a",
        base_url="https://api.openai.com/v1/responses",
        provider_name="openai",
    )

    with bind_quick_scan_budget(store, policy):
        with bind_quick_scan_route(
            route_id="route-a1",
            provider="provider-a1",
            model_requested="fixture-a",
            quota_group="A",
        ):
            with pytest.raises(QuickScanBudgetDeferredError, match="budget_cost_limit"):
                client.send_search_request("budget denied before send")

    session.post.assert_not_called()


def test_async_search_settles_the_same_durable_budget_ledger(tmp_path, monkeypatch):
    store = QuickScanWorkStore(tmp_path / "budget.sqlite")
    policy = _policy(max_cost=10, max_cost_per_attempt=2)
    response = Mock()
    response.status_code = 200
    response.headers = {"x-request-id": "async-budget-fixture"}
    response.json.return_value = {
        "id": "async-budget-response",
        "status": "completed",
        "model": "fixture-a",
        "output": [
            {
                "type": "web_search_call",
                "id": "async-budget-search",
                "status": "completed",
                "action": {
                    "type": "search",
                    "sources": [{"type": "url", "url": "https://example.test/async-budget"}],
                },
            },
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "fixture"}],
            },
        ],
    }
    http_client = Mock()
    http_client.post = AsyncMock(return_value=response)
    monkeypatch.setattr(
        "src.providers.llm_client.http_client_manager.get_async_client",
        AsyncMock(return_value=http_client),
    )
    client = AsyncLLMClient(
        api_key="fixture-only",
        model="fixture-a",
        base_url="https://api.openai.com/v1/responses",
        provider_name="openai",
    )

    async def dispatch():
        with bind_quick_scan_budget(
            store,
            policy,
            cost_resolver=lambda _receipt: {
                "actual_cost": 0.75,
                "pricing_ref": "fixture-price-v1",
                "source_ref": "async-provider-usage-fixture",
            },
        ):
            with bind_quick_scan_route(
                route_id="route-a1",
                provider="provider-a1",
                model_requested="fixture-a",
                quota_group="A",
            ):
                return await client.send_search_request_async("fixture question")

    result = asyncio.run(dispatch())
    status = store.get_quick_scan_budget_status(policy["policy_id"])
    assert result.search_verified is True
    assert http_client.post.await_count == 1
    assert status["requests"] == 1
    assert status["spent_micros"] == 750_000
    assert status["reserved_micros"] == 0
    assert status["in_flight"] == 0
