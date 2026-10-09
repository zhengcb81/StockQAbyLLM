import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.providers.llm_client import LLMClient
from src.utils.quick_scan_cost_resolver import QuickScanCostResolver
from src.utils.quick_scan_work_store import QuickScanWorkStore
from src.utils.quick_scan_work_transport import (
    QuickScanBudgetDeferredError,
    bind_quick_scan_budget,
    bind_quick_scan_route,
)


@pytest.mark.parametrize("actual", [None, "", "unpriced-resolved-model"])
def test_q10_missing_actual_rate_card_never_uses_requested_price_or_zero(tmp_path, actual):
    path = tmp_path / "synthetic-rates.json"
    _write_cards(path, [_card(model="requested-model")])
    receipt = {
        "provider": "openai",
        "requested_model": "requested-model",
        "actual_model": actual,
        "usage": _usage(),
        "answer": {"actual_model": "requested-model"},
        "execution": {"actual_model": "requested-model"},
    }
    assert QuickScanCostResolver(path, _policy())(receipt) is None


def test_q10_resolved_rate_card_wins_when_requested_and_actual_have_different_prices(tmp_path):
    path = tmp_path / "synthetic-rates.json"
    requested = _card(model="requested-model")
    resolved = _card(model="resolved-model", source_ref="https://provider.example/pricing#resolved")
    resolved["rates"]["input_per_million_tokens"] = 7
    _write_cards(path, [requested, resolved])
    receipt = {
        "provider": "openai",
        "requested_model": "requested-model",
        "actual_model": "resolved-model",
        "usage": _usage(
            input_tokens=1_000_000,
            cached_input_tokens=0,
            cache_creation_input_tokens=0,
            output_tokens=0,
            reasoning_output_tokens=0,
            search_tool_calls=0,
        ),
        "answer": {"actual_model": "requested-model"},
    }
    priced = QuickScanCostResolver(path, _policy())(receipt)
    assert priced["actual_cost"] == Decimal("7")
    assert priced["source_ref"] == "https://provider.example/pricing#resolved"


def test_q10_unpriced_alias_actual_retains_budget_reservation_and_pauses_dispatch(
    tmp_path, monkeypatch
):
    path = tmp_path / "synthetic-rates.json"
    _write_cards(path, [_card(model="requested-model")])
    resolution = {
        "schema_version": "1.0.0",
        "aliases": [
            {
                "provider": "openai",
                "protocol": "responses",
                "requested_model": "requested-model",
                "resolved_model": "unpriced-resolved-model",
            }
        ],
    }
    route = {
        "id": "q10-cost-route",
        "provider_config_ref": "openai",
        "model": "requested-model",
        "quota_group": "q10-cost-account",
        "max_in_flight": 1,
        "eligible": True,
        "unavailable_reason": None,
        "model_resolution": resolution,
    }
    policy = {
        "configured": True,
        "policy_id": "q10-unpriced",
        "policy_version": "q10-unpriced@alias-v1",
        "budget": {"currency": "USD", "max_cost": 20, "max_requests": 5, "max_cost_per_attempt": 2},
        "cost_policy": {
            "pricing_basis": "verified_rate_card",
            "pricing_ref": "fixture-rates-v1",
            "reserve_before_dispatch": True,
            "unknown_actual_cost_action": "retain_reservation_and_pause",
        },
        "dispatch": {"max_in_flight_total": 1},
        "quota_groups": [{"id": "q10-cost-account", "max_in_flight": 1}],
        "routes": [route],
    }
    resolver = QuickScanCostResolver(path, policy)
    store = QuickScanWorkStore(tmp_path / "q10-cost.sqlite")
    response = Mock()
    response.status_code = 200
    response.headers = {"x-request-id": "synthetic-cost-request"}
    response.json.return_value = {
        "id": "synthetic-cost-response",
        "status": "completed",
        "model": "unpriced-resolved-model",
        "usage": {
            "input_tokens": 1_000_000,
            "output_tokens": 0,
            "input_tokens_details": {"cached_tokens": 0},
        },
        "output": [
            {
                "type": "web_search_call",
                "id": "synthetic-cost-search",
                "status": "completed",
                "action": {
                    "type": "search",
                    "sources": [{"type": "url", "url": "https://example.org/source"}],
                },
            },
            {
                "type": "message",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": '{"score":8,"description":"synthetic"}'}
                ],
            },
        ],
    }
    session = Mock()
    session.post.return_value = response
    monkeypatch.setattr(
        "src.providers.llm_client.http_client_manager.get_sync_session", lambda: session
    )
    client = LLMClient(
        api_key="synthetic-key",
        model="requested-model",
        base_url="https://api.openai.com/v1/responses",
        provider_name="openai",
        model_resolution=resolution,
    )
    with bind_quick_scan_budget(store, policy, cost_resolver=resolver), bind_quick_scan_route(
        route_id=route["id"],
        provider="openai",
        model_requested="requested-model",
        quota_group=route["quota_group"],
        model_resolution=resolution,
    ):
        received = client.send_search_request("Synthetic initial cost question")
        assert received.search_verified
        assert received.actual_model == "unpriced-resolved-model"
        assert resolver(received.execution_metadata) is None
        status = store.get_quick_scan_budget_status(policy["policy_id"])
        assert status["reserved_micros"] == 2_000_000
        assert status["spent_micros"] == 0
        assert status["requests"] == 1
        assert status["unreconciled_attempts"] == 1
        with pytest.raises(QuickScanBudgetDeferredError):
            client.send_search_request("Synthetic later cost question")
    session.post.assert_called_once()


def _policy(*, pricing_ref="fixture-rates-v1", currency="USD"):
    return {
        "cost_policy": {"pricing_basis": "verified_rate_card", "pricing_ref": pricing_ref},
        "budget": {"currency": currency},
    }


def _card(**overrides):
    card = {
        "pricing_ref": "fixture-rates-v1",
        "provider": "openai",
        "model": "fixture-model",
        "currency": "USD",
        "source_ref": "https://provider.example/pricing#fixture",
        "source_checked_at": "2026-09-27",
        "rates": {
            "input_per_million_tokens": 2,
            "cached_input_per_million_tokens": 0.5,
            "cache_creation_input_per_million_tokens": 4,
            "output_per_million_tokens": 10,
            "search_tool_call": 0.25,
        },
    }
    card.update(overrides)
    return card


def _usage(**overrides):
    usage = {
        "schema": "quick-scan-usage-v1",
        "input_tokens": 900,
        "cached_input_tokens": 100,
        "cache_creation_input_tokens": 20,
        "output_tokens": 500,
        "reasoning_output_tokens": 50,
        "search_tool_calls": 2,
    }
    usage.update(overrides)
    return usage


def _write_cards(path, cards):
    path.write_text(json.dumps({"schema_version": "1.0.0", "rate_cards": cards}), encoding="utf-8")


def test_cost_resolver_prices_only_normalized_provider_usage(tmp_path):
    path = tmp_path / "quick_scan_rate_cards.json"
    _write_cards(path, [_card()])
    resolver = QuickScanCostResolver(path, _policy())

    result = resolver({"provider": "openai", "actual_model": "fixture-model", "usage": _usage()})

    assert result == {
        "actual_cost": Decimal("0.50693"),
        "pricing_ref": "fixture-rates-v1",
        "source_ref": "https://provider.example/pricing#fixture",
    }


@pytest.mark.parametrize(
    "receipt",
    [
        {"provider": "openai", "actual_model": "fixture-model"},
        {
            "provider": "unknown-provider",
            "actual_model": "fixture-model",
            "usage": _usage(),
        },
        {"provider": "openai", "actual_model": "unpriced-model", "usage": _usage()},
        {
            "provider": "openai",
            "actual_model": "fixture-model",
            "usage": _usage(search_tool_calls=True),
        },
    ],
)
def test_cost_resolver_returns_unknown_for_missing_or_untrusted_receipt_fields(tmp_path, receipt):
    path = tmp_path / "quick_scan_rate_cards.json"
    _write_cards(path, [_card()])

    assert QuickScanCostResolver(path, _policy())(receipt) is None


def test_cost_resolver_requires_matching_policy_reference_and_currency(tmp_path):
    path = tmp_path / "quick_scan_rate_cards.json"
    _write_cards(path, [_card()])
    receipt = {"provider": "openai", "actual_model": "fixture-model", "usage": _usage()}

    assert QuickScanCostResolver(path, _policy(pricing_ref="other"))(receipt) is None
    assert QuickScanCostResolver(path, _policy(currency="CNY"))(receipt) is None


def test_missing_rate_card_is_unknown_and_invalid_file_fails_closed(tmp_path):
    missing = tmp_path / "missing.json"
    assert QuickScanCostResolver(missing, _policy()).has_pricing_reference is False

    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"schema_version":"9.0.0","rate_cards":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported shape"):
        QuickScanCostResolver(invalid, _policy())


def test_empty_example_template_is_non_operational_and_loadable():
    template = (
        Path(__file__).resolve().parents[2] / "examples" / "quick_scan_rate_cards.template.json"
    )
    resolver = QuickScanCostResolver(template, _policy())

    assert resolver.has_pricing_reference is False


def test_duplicate_rate_card_identity_is_rejected(tmp_path):
    path = tmp_path / "quick_scan_rate_cards.json"
    _write_cards(path, [_card(), _card()])

    with pytest.raises(ValueError, match="duplicate"):
        QuickScanCostResolver(path, _policy())


def test_json_rate_precision_survives_parse_and_token_pricing(tmp_path):
    path = tmp_path / "quick_scan_rate_cards.json"
    card = _card()
    card["rates"] = {field: 0 for field in card["rates"]}
    payload = json.dumps({"schema_version": "1.0.0", "rate_cards": [card]})
    payload = payload.replace(
        '"input_per_million_tokens": 0',
        '"input_per_million_tokens": 1.0000000000000000000000000000000001',
        1,
    )
    path.write_text(payload, encoding="utf-8")
    usage = _usage(
        input_tokens=1,
        cached_input_tokens=0,
        cache_creation_input_tokens=0,
        output_tokens=0,
        reasoning_output_tokens=0,
        search_tool_calls=0,
    )

    result = QuickScanCostResolver(path, _policy())(
        {"provider": "openai", "actual_model": "fixture-model", "usage": usage}
    )

    assert result is not None
    assert result["actual_cost"] == Decimal("0.0000010000000000000000000000000000000001")


def test_rate_card_rejects_decimal_exponents_outside_exact_arithmetic_bound(tmp_path):
    path = tmp_path / "quick_scan_rate_cards.json"
    card = _card()
    card["rates"] = {field: 0 for field in card["rates"]}
    payload = json.dumps({"schema_version": "1.0.0", "rate_cards": [card]})
    payload = payload.replace(
        '"input_per_million_tokens": 0', '"input_per_million_tokens": 1e101', 1
    )
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match="exact decimal precision limit"):
        QuickScanCostResolver(path, _policy())
