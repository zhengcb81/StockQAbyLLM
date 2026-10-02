import json
from decimal import Decimal
from pathlib import Path

import pytest

from src.utils.quick_scan_cost_resolver import QuickScanCostResolver


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
