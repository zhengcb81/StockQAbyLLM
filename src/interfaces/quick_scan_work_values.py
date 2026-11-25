"""Canonical quick-scan work values, independent of the ledger and providers.

These values are used both before acquiring work and while saving or reading
durable records. Keep their canonical representation stable for prior records.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")


# Hyphens accepted per owner sign-off (2026-10-05 round-51): the W04
# identity-export package issues ENT_<uuid> entity ids (e.g. the frozen
# golden ENT_1b2a4d3e-0000-4a1b-8c2d-000000000001).
_ENTITY_ID = re.compile(r"^ENT_[A-Za-z0-9_-]+$")


# Original StockWiki G2b source bindings use both UUIDv4 and compact lowercase
# 32-hex opaque IDs (including source-master hashes). Preserve those exact IDs
# and legacy BND spelling; matching this lexical form never grants authority.
_BINDING_ID = re.compile(
    r"^(?:BND_[A-Za-z0-9_]+|BIND_(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}))$"
)
_HEX_SHA256 = re.compile(r"^[a-f0-9]{64}$")


_RESEARCH_VALIDATION_CODES = frozenset(
    {
        "research_actual_model_mismatch",
        "research_choices_invalid",
        "research_role_invalid",
        "research_incomplete_output",
        "research_final_content_invalid",
        "research_tool_calls_invalid",
        "research_tool_identity_invalid",
        "research_reasoning_protocol_invalid",
        "research_tool_arguments_object_required",
        "research_tool_arguments_schema_invalid",
        "research_tool_arguments_json_invalid",
        "research_response_validation_failed",
    }
)


_RECEIPT_FIELDS = (
    "request_payload_sha256",
    "research_visible_output_sha256",
    "research_intent_binding_sha256",
    "endpoint_profile_id",
    "provider",
    "request_id",
    "response_id",
    "actual_model",
    "requested_model",
    "search_protocol",
    "response_sha256",
    "response_json_basis",
    "model_resolution_sha256",
    "search_status",
    "response_status",
    "http_status_code",
    "attempt_id",
    "prompt_sha256",
    "search_receipt_id",
    "failure_type",
    "research_validation_code",
    "provider_error_code",
    "retry_after_seconds",
    "completed_at",
    "source_urls",
    "usage",
)


def _sanitized_receipt(receipt: object) -> dict:
    """Canonical allowlist shared by HTTP persistence and answer checkpointing."""
    if not isinstance(receipt, dict):
        return {}
    result: dict[str, object] = {}
    for key in _RECEIPT_FIELDS:
        if key not in receipt:
            continue
        value = receipt[key]
        if key == "source_urls":
            try:
                result[key] = _canonical_source_urls(value)
            except ValueError:
                continue
        elif key in {
            "request_payload_sha256",
            "research_visible_output_sha256",
            "research_intent_binding_sha256",
        }:
            if isinstance(value, str) and _HEX_SHA256.fullmatch(value):
                result[key] = value
        elif key == "research_validation_code":
            if isinstance(value, str) and value in _RESEARCH_VALIDATION_CODES:
                result[key] = value
        elif key == "endpoint_profile_id":
            if isinstance(value, str) and re.fullmatch(
                r"stockqa\.endpoint-profile/1\.0\.0:[a-f0-9]{64}", value
            ):
                result[key] = value
        elif key == "usage":
            usage = _canonical_usage(value)
            if usage is not None:
                result[key] = usage
        elif value is None or isinstance(value, (str, int, bool)):
            result[key] = value
        elif isinstance(value, float) and math.isfinite(value):
            result[key] = value
    return result


def _canonical_native_events(value: object) -> list[dict]:
    """Bounded event metadata only; no query, prompt, reasoning or raw body."""
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError("invalid native search events")
    events = []
    for original in value:
        if not isinstance(original, dict) or set(original) - {
            "id",
            "status",
            "action_type",
            "source_urls",
            "sources",
            "evidence_basis",
        }:
            raise ValueError("invalid native search event fields")
        event: dict[str, Any] = {}
        for key in ("id", "status", "action_type", "evidence_basis"):
            if key in original:
                event[key] = (
                    None if original[key] is None else _safe_text(original[key], key, maximum=300)
                )
        if "source_urls" in original:
            event["source_urls"] = (
                _canonical_source_urls(original["source_urls"]) if original["source_urls"] else []
            )
        if "sources" in original:
            if not isinstance(original["sources"], list) or len(original["sources"]) > 20:
                raise ValueError("invalid native source metadata")
            sources = []
            for source in original["sources"]:
                if not isinstance(source, dict) or set(source) - {
                    "url",
                    "title",
                    "published_date",
                }:
                    raise ValueError("invalid native source fields")
                entry: dict[str, Any] = {"url": _canonical_source_urls([source["url"]])[0]}
                for key, maximum in (("title", 500), ("published_date", 80)):
                    if key in source:
                        entry[key] = (
                            None
                            if source[key] is None
                            else _safe_text(source[key], key, maximum=maximum)
                        )
                sources.append(entry)
            event["sources"] = sources
        events.append(event)
    encoded = json.dumps(events, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if len(encoded) > 60000:
        raise ValueError("native event metadata exceeds retention bound")
    return events


def _canonical_usage(value: object) -> Optional[dict]:
    """Validate the tiny normalized usage payload; never persist raw provider usage."""
    fields = (
        "input_tokens",
        "cached_input_tokens",
        "cache_creation_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "search_tool_calls",
    )
    if not isinstance(value, dict) or value.get("schema") != "quick-scan-usage-v1":
        return None
    if set(value) != {"schema", *fields}:
        return None
    if any(type(value[field]) is not int or value[field] < 0 for field in fields):
        return None
    if value["reasoning_output_tokens"] > value["output_tokens"]:
        return None
    return {"schema": value["schema"], **{field: value[field] for field in fields}}


def quick_scan_receipt_sha256(receipt: object) -> str:
    encoded = json.dumps(
        _sanitized_receipt(receipt),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_text(value: object, label: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 and character not in "\n\r\t" for character in value)
    ):
        raise ValueError(f"invalid {label}")
    return value.strip()


def _cost_to_micros(value: object, label: str, *, allow_zero: bool = False) -> int:
    if type(value) not in (int, float, Decimal):
        raise ValueError(f"invalid {label}")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or (amount == 0 and not allow_zero):
            raise ValueError
        parts = amount.as_tuple()
        exponent = parts.exponent
        if not isinstance(exponent, int) or len(parts.digits) > 100 or abs(exponent) > 100:
            raise ValueError
        coefficient = int("".join(str(digit) for digit in parts.digits))
        scaled_exponent = int(parts.exponent) + 6
        if coefficient == 0:
            micros = 0
        elif scaled_exponent >= 0:
            if len(str(coefficient)) + scaled_exponent > 19:
                raise ValueError
            micros = coefficient * (10**scaled_exponent)
        else:
            divisor_exponent = -scaled_exponent
            if divisor_exponent > len(parts.digits):
                micros = 1
            else:
                divisor = 10**divisor_exponent
                quotient, remainder = divmod(coefficient, divisor)
                micros = quotient + (1 if remainder else 0)
    except (InvalidOperation, OverflowError, ValueError):
        raise ValueError(f"invalid {label}") from None
    if micros > 9_223_372_036_854_775_807:
        raise ValueError(f"{label} exceeds the durable ledger range")
    return micros


def sanitize_transport_receipt(receipt: object) -> dict:
    """Public sanitized receipt projection for sibling transport components."""
    return _sanitized_receipt(receipt)


def canonical_native_events(value: object) -> list[dict]:
    """Public bounded native-event projection for transport and result adapters."""
    return _canonical_native_events(value)


def validate_transport_text(value: object, label: str, *, maximum: int) -> str:
    """Public bounded-text codec used by transport metadata validation."""
    return _safe_text(value, label, maximum=maximum)


def cost_to_micros(value: object, label: str, *, allow_zero: bool = False) -> int:
    """Public decimal-to-ledger codec shared with the send-boundary module."""
    return _cost_to_micros(value, label, allow_zero=allow_zero)


def _budget_policy_projection(policy: object) -> dict:
    """Validate and canonicalize StockQA's secret-free Q09 policy projection."""
    if not isinstance(policy, dict) or policy.get("configured") is not True:
        raise ValueError("a configured quick-scan budget policy is required")
    policy_id = _safe_text(policy.get("policy_id"), "policy_id", maximum=200)
    policy_version = _safe_text(policy.get("policy_version"), "policy_version", maximum=300)
    budget = policy.get("budget")
    dispatch = policy.get("dispatch")
    cost_policy = policy.get("cost_policy")
    routes = policy.get("routes")
    groups = policy.get("quota_groups")
    if (
        not isinstance(budget, dict)
        or not isinstance(dispatch, dict)
        or not isinstance(cost_policy, dict)
    ):
        raise ValueError("budget policy projection is incomplete")
    if not isinstance(routes, list) or not routes or not isinstance(groups, list) or not groups:
        raise ValueError("budget policy routes and quota groups must be nonempty")
    currency = budget.get("currency")
    if not isinstance(currency, str) or re.fullmatch(r"[A-Z]{3}", currency) is None:
        raise ValueError("budget currency must be an uppercase ISO-style code")
    max_requests = budget.get("max_requests")
    max_in_flight_total = dispatch.get("max_in_flight_total")
    if type(max_requests) is not int or max_requests < 1:
        raise ValueError("max_requests must be a positive integer")
    if type(max_in_flight_total) is not int or not 1 <= max_in_flight_total <= 32:
        raise ValueError("max_in_flight_total must be between 1 and 32")
    pricing_basis = cost_policy.get("pricing_basis")
    pricing_ref = cost_policy.get("pricing_ref")
    if pricing_basis not in {"verified_rate_card", "user_cap"}:
        raise ValueError("unsupported pricing basis")
    if pricing_basis == "verified_rate_card":
        pricing_ref = _safe_text(pricing_ref, "pricing_ref", maximum=300)
    elif pricing_ref is not None:
        pricing_ref = _safe_text(pricing_ref, "pricing_ref", maximum=300)
    if cost_policy.get("reserve_before_dispatch") is not True:
        raise ValueError("budget policy must reserve before dispatch")
    if cost_policy.get("unknown_actual_cost_action") != "retain_reservation_and_pause":
        raise ValueError("unknown actual cost must retain its reservation and pause")

    group_limits = {}
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("invalid quota group projection")
        group_id = _safe_text(group.get("id"), "quota_group", maximum=200)
        limit = group.get("max_in_flight")
        if type(limit) is not int or not 1 <= limit <= 32 or group_id in group_limits:
            raise ValueError("invalid or duplicate quota-group concurrency limit")
        group_limits[group_id] = limit
    route_limits = {}
    route_identity = {}
    for route in routes:
        if not isinstance(route, dict):
            raise ValueError("invalid route projection")
        route_id = _safe_text(route.get("id"), "route_id", maximum=200)
        group_id = _safe_text(route.get("quota_group"), "quota_group", maximum=200)
        route_limit = route.get("max_in_flight")
        if (
            type(route_limit) is not int
            or not 1 <= route_limit <= 32
            or route_id in route_limits
            or group_id not in group_limits
        ):
            raise ValueError("invalid route concurrency limit or quota group")
        route_limits[route_id] = route_limit
        route_identity[route_id] = {
            "quota_group": group_id,
            "provider_config_ref": _safe_text(
                route.get("provider_config_ref"), "provider_config_ref", maximum=200
            ),
            "model": _safe_text(route.get("model"), "model", maximum=200),
        }
    snapshot = {
        "policy_id": policy_id,
        "policy_version": policy_version,
        "budget": {
            "currency": currency,
            "max_cost_micros": _cost_to_micros(budget.get("max_cost"), "max_cost"),
            "max_requests": max_requests,
            "max_cost_per_attempt_micros": _cost_to_micros(
                budget.get("max_cost_per_attempt"), "max_cost_per_attempt"
            ),
        },
        "pricing_basis": pricing_basis,
        "pricing_ref": pricing_ref,
        "max_in_flight_total": max_in_flight_total,
        "quota_groups": [
            {"id": group_id, "max_in_flight": group_limits[group_id]} for group_id in group_limits
        ],
        "routes": [
            {
                "id": route_id,
                **route_identity[route_id],
                "max_in_flight": route_limits[route_id],
            }
            for route_id in route_limits
        ],
    }
    canonical = json.dumps(snapshot, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return {
        "policy_id": policy_id,
        "policy_version": policy_version,
        "currency": currency,
        "pricing_basis": pricing_basis,
        "pricing_ref": pricing_ref,
        "max_cost_micros": _cost_to_micros(budget.get("max_cost"), "max_cost"),
        "max_requests": max_requests,
        "max_cost_per_attempt_micros": _cost_to_micros(
            budget.get("max_cost_per_attempt"), "max_cost_per_attempt"
        ),
        "max_in_flight_total": max_in_flight_total,
        "group_limits_json": json.dumps(group_limits, sort_keys=True, separators=(",", ":")),
        "route_limits_json": json.dumps(route_limits, sort_keys=True, separators=(",", ":")),
        "route_identity": route_identity,
        "policy_snapshot_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def _canonical_source_urls(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)) or not value or len(value) > 20:
        raise ValueError("verified search receipt requires source URLs")
    result = []
    for raw_url in value:
        url = _safe_text(raw_url, "source URL", maximum=2048)
        try:
            parts = urlsplit(url)
            if (
                parts.scheme.lower() not in {"http", "https"}
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
            ):
                raise ValueError
            host = parts.netloc
            safe_query = [
                (key, value)
                for key, value in parse_qsl(parts.query, keep_blank_values=True)
                if not _sensitive_query_parameter(key)
            ]
            canonical = urlunsplit(
                (
                    parts.scheme.lower(),
                    host,
                    parts.path or "/",
                    urlencode(safe_query, doseq=True),
                    "",
                )
            )
        except ValueError:
            raise ValueError("invalid source URL") from None
        if canonical not in result:
            result.append(canonical)
    if not result:
        raise ValueError("verified search receipt requires source URLs")
    return result


def _sensitive_query_parameter(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    exact_names = {
        "key",
        "apikey",
        "accesskey",
        "accesskeyid",
        "authorization",
        "auth",
        "clientsecret",
        "credential",
        "credentials",
        "password",
        "passwd",
        "secret",
        "sig",
        "signature",
        "token",
    }
    return (
        normalized in exact_names
        or normalized.startswith("auth")
        or normalized.endswith(("token", "signature", "secret", "credential", "password", "apikey"))
    )


def _timestamp(value: object, label: str) -> str:
    timestamp = _safe_text(value, label, maximum=80)
    parsed_value = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
    try:
        parsed = datetime.fromisoformat(parsed_value)
    except ValueError:
        raise ValueError(f"invalid {label}") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"invalid {label}")
    return timestamp


def _safe(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not _HEX_SHA256.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _canonical_bindings(source_binding_ref: str, source_binding_refs: Sequence[str]) -> str:
    """Freeze every entity binding; the singular ref is only the prompt anchor."""
    if (
        isinstance(source_binding_refs, (str, bytes))
        or not isinstance(source_binding_refs, (tuple, list))
        or not source_binding_refs
    ):
        raise ValueError("source_binding_refs must be a nonempty sequence")
    refs = []
    for ref in source_binding_refs:
        if not isinstance(ref, str) or not _BINDING_ID.fullmatch(ref):
            raise ValueError("invalid source_binding_refs")
        refs.append(ref)
    if len(set(refs)) != len(refs) or source_binding_ref not in refs:
        raise ValueError("source_binding_refs must be unique and contain prompt anchor")
    return json.dumps(sorted(refs), ensure_ascii=True, separators=(",", ":"))


ENTITY_ID = _ENTITY_ID
BINDING_ID = _BINDING_ID
budget_policy_projection = _budget_policy_projection
canonical_bindings = _canonical_bindings
canonical_source_urls = _canonical_source_urls
validate_safe_identifier = _safe
sensitive_query_parameter = _sensitive_query_parameter
validate_sha256 = _sha256
validate_timestamp = _timestamp
