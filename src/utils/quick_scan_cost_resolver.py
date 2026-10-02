"""Resolve provider-reported quick-scan usage against an explicit local rate card.

Rate cards are user-maintained, versioned inputs. This module deliberately does
not fetch prices, infer account billing modes, or fall back to generic estimates.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Optional

_RATE_FIELDS = (
    "input_per_million_tokens",
    "cached_input_per_million_tokens",
    "cache_creation_input_per_million_tokens",
    "output_per_million_tokens",
    "search_tool_call",
)
_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "search_tool_calls",
)


def _text(value: Any, field: str, maximum: int = 300) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError(f"invalid rate-card {field}")
    return value.strip()


def _decimal(value: Any, field: str) -> Decimal:
    if type(value) not in (int, str, Decimal):
        raise ValueError(f"invalid rate-card {field}")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"invalid rate-card {field}") from None
    if not result.is_finite() or result < 0:
        raise ValueError(f"invalid rate-card {field}")
    parts = result.as_tuple()
    exponent = parts.exponent
    if not isinstance(exponent, int):
        raise ValueError(f"invalid rate-card {field}")
    if len(parts.digits) > 100 or abs(exponent) > 100:
        raise ValueError(f"rate-card {field} exceeds the exact decimal precision limit")
    return result


def _exact_decimal_sum(terms: list[tuple[int, int]]) -> Decimal:
    """Sum integer-coefficient decimal terms without using the active context."""
    nonzero = [(coefficient, exponent) for coefficient, exponent in terms if coefficient]
    if not nonzero:
        return Decimal(0)
    exponent = min(term_exponent for _, term_exponent in nonzero)
    coefficient = sum(
        term_coefficient * (10 ** (term_exponent - exponent))
        for term_coefficient, term_exponent in nonzero
    )
    digits = tuple(int(character) for character in str(coefficient))
    return Decimal((0, digits, exponent))


def _coefficient(value: Decimal) -> tuple[int, int]:
    parts = value.as_tuple()
    return int("".join(str(digit) for digit in parts.digits)), int(parts.exponent)


class QuickScanCostResolver:
    """Callable cost resolver used by the durable Q09 transport gate."""

    def __init__(self, rate_cards_path: Path | str, policy: dict):
        self.path = Path(rate_cards_path)
        cost_policy = policy.get("cost_policy") if isinstance(policy, dict) else None
        budget = policy.get("budget") if isinstance(policy, dict) else None
        self.pricing_ref = cost_policy.get("pricing_ref") if isinstance(cost_policy, dict) else None
        self.currency = budget.get("currency") if isinstance(budget, dict) else None
        self.pricing_basis = (
            cost_policy.get("pricing_basis") if isinstance(cost_policy, dict) else None
        )
        self._cards = self._load_cards()

    @property
    def has_pricing_reference(self) -> bool:
        return any(card["pricing_ref"] == self.pricing_ref for card in self._cards)

    def _load_cards(self) -> tuple[dict, ...]:
        if not self.path.exists():
            return ()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"), parse_float=Decimal)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("quick-scan rate-card file cannot be read") from error
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema_version", "rate_cards"}
            or payload.get("schema_version") != "1.0.0"
            or not isinstance(payload.get("rate_cards"), list)
        ):
            raise ValueError("quick-scan rate-card file has an unsupported shape")

        cards = []
        seen = set()
        expected_card_fields = {
            "pricing_ref",
            "provider",
            "model",
            "currency",
            "source_ref",
            "source_checked_at",
            "rates",
        }
        for index, raw in enumerate(payload["rate_cards"]):
            if not isinstance(raw, dict) or set(raw) != expected_card_fields:
                raise ValueError(f"rate_cards[{index}] has an unsupported shape")
            card: dict[str, Any] = {
                key: _text(raw[key], f"rate_cards[{index}].{key}")
                for key in (
                    "pricing_ref",
                    "provider",
                    "model",
                    "currency",
                    "source_ref",
                    "source_checked_at",
                )
            }
            if (
                len(card["currency"]) != 3
                or not card["currency"].isalpha()
                or not card["currency"].isupper()
            ):
                raise ValueError(f"rate_cards[{index}].currency must be three uppercase letters")
            try:
                date.fromisoformat(card["source_checked_at"])
            except ValueError:
                raise ValueError(
                    f"rate_cards[{index}].source_checked_at must be an ISO date"
                ) from None
            rates = raw["rates"]
            if not isinstance(rates, dict) or set(rates) != set(_RATE_FIELDS):
                raise ValueError(f"rate_cards[{index}].rates has an unsupported shape")
            card["rates"] = {
                field: _decimal(rates[field], f"rate_cards[{index}].rates.{field}")
                for field in _RATE_FIELDS
            }
            key = (card["pricing_ref"], card["provider"], card["model"])
            if key in seen:
                raise ValueError("duplicate quick-scan rate-card identity")
            seen.add(key)
            cards.append(card)
        return tuple(cards)

    def __call__(self, receipt: Dict[str, Any]) -> Optional[dict]:
        if (
            self.pricing_basis != "verified_rate_card"
            or not isinstance(self.pricing_ref, str)
            or not isinstance(self.currency, str)
            or not isinstance(receipt, dict)
        ):
            return None
        provider = receipt.get("provider")
        model = receipt.get("actual_model")
        if not isinstance(provider, str) or not isinstance(model, str):
            return None
        card = next(
            (
                item
                for item in self._cards
                if item["pricing_ref"] == self.pricing_ref
                and item["provider"] == provider
                and item["model"] == model
                and item["currency"] == self.currency
            ),
            None,
        )
        usage = receipt.get("usage")
        if card is None or not isinstance(usage, dict):
            return None
        if usage.get("schema") != "quick-scan-usage-v1" or set(usage) != {
            "schema",
            *_USAGE_FIELDS,
        }:
            return None
        if any(type(usage[field]) is not int or usage[field] < 0 for field in _USAGE_FIELDS):
            return None
        if usage["reasoning_output_tokens"] > usage["output_tokens"]:
            return None

        rates = card["rates"]
        terms = []
        for usage_field, rate_field in (
            ("input_tokens", "input_per_million_tokens"),
            ("cached_input_tokens", "cached_input_per_million_tokens"),
            ("cache_creation_input_tokens", "cache_creation_input_per_million_tokens"),
            ("output_tokens", "output_per_million_tokens"),
        ):
            coefficient, exponent = _coefficient(rates[rate_field])
            if usage[usage_field] and coefficient:
                # Per-million token prices become currency units by shifting six places.
                terms.append((usage[usage_field] * coefficient, exponent - 6))
        search_coefficient, search_exponent = _coefficient(rates["search_tool_call"])
        if usage["search_tool_calls"] and search_coefficient:
            terms.append((usage["search_tool_calls"] * search_coefficient, search_exponent))
        total = _exact_decimal_sum(terms)
        return {
            "actual_cost": total,
            "pricing_ref": self.pricing_ref,
            "source_ref": card["source_ref"],
        }
