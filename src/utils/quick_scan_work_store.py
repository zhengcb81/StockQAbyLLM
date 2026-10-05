"""Durable quick-scan work, normalized answer checkpoints, and transport attempts.

This SQLite store is separate from StockWiki identity authority and stores no
company documents or raw search pages. Checkpoints contain only the validated
answer contract, immutable identity/question fingerprints, and minimal citation
metadata. A caller must still obtain authoritative identity facts from StockWiki.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SCHEMA_VERSION = 5
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")
# Hyphens accepted per owner sign-off (2026-10-05 round-51): the W04
# identity-export package issues ENT_<uuid> entity ids (e.g. the frozen
# golden ENT_1b2a4d3e-0000-4a1b-8c2d-000000000001).
_ENTITY_ID = re.compile(r"^ENT_[A-Za-z0-9_-]+$")
_SECURITY_ID = re.compile(r"^SEC_[A-Za-z0-9_.]+$")
_SEGMENT_ID = re.compile(r"^SEG_[A-Za-z0-9_]+$")
_BINDING_ID = re.compile(r"^BND_[A-Za-z0-9_]+$")
_HEX_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_REQUEST_KEY = re.compile(r"^REQ_[a-f0-9]{64}$")
_DELIVERY_NOT_SENT_REASONS = {
    "connection_refused_before_write",
    "dns_resolution_failed_before_connect",
    "request_body_not_dispatched",
    "caller_cancelled_before_send",
}


class WorkConflictError(ValueError):
    """An existing logical item or attempt disagrees with immutable inputs."""


class LeaseFencedError(RuntimeError):
    """The supplied worker lease is absent, expired, or superseded."""


class BudgetAdmissionError(RuntimeError):
    """A quick-scan dispatch cannot be admitted under its durable policy."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class BudgetPolicyConflict(ValueError):
    """An immutable budget identity was rebound to an incompatible currency."""


@dataclass(frozen=True)
class Lease:
    lease_token: str
    lease_epoch: int


@dataclass(frozen=True)
class SendPermit:
    """Durable intent receipt, not an HTTP one-shot token by itself.

    The future transport hook must consume this for one POST only; merely
    returning a permit cannot prevent a caller from posting twice.
    """

    attempt_id: str
    committed_at: float
    lease_epoch: int


_DDL_V1 = (
    """CREATE TABLE work_item (
        work_item_id TEXT PRIMARY KEY,
        entity_id TEXT NOT NULL,
        question_id TEXT NOT NULL,
        generation INTEGER NOT NULL CHECK (generation >= 1),
        scope TEXT NOT NULL CHECK (scope IN ('entity','security','segment')),
        scope_id TEXT NOT NULL,
        identity_revision INTEGER NOT NULL CHECK (identity_revision >= 1),
        source_binding_version INTEGER NOT NULL CHECK (source_binding_version >= 1),
        identity_state TEXT NOT NULL CHECK (identity_state IN ('provisional','verified')),
        source_binding_ref TEXT NOT NULL,
        source_binding_refs_json TEXT NOT NULL,
        identity_snapshot_sha256 TEXT NOT NULL,
        question_fingerprint TEXT NOT NULL,
        routing_fingerprint TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN
            ('pending','leased','uncertain','result_ready','delivered','failed','cancelled')),
        lease_epoch INTEGER NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
        lease_token TEXT,
        lease_expires_at REAL,
        uncertain_attempt_id TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE (entity_id,question_id,generation,scope,scope_id,
            identity_revision,source_binding_version,identity_state,source_binding_refs_json),
        CHECK ((status='leased' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
            OR (status!='leased' AND lease_token IS NULL AND lease_expires_at IS NULL))
    )""",
    """CREATE TABLE work_run_ref (
        work_item_id TEXT NOT NULL REFERENCES work_item(work_item_id),
        run_id TEXT NOT NULL,
        scan_id TEXT NOT NULL,
        attached_at REAL NOT NULL,
        PRIMARY KEY (work_item_id,run_id,scan_id)
    )""",
    """CREATE TABLE attempt (
        attempt_id TEXT PRIMARY KEY,
        work_item_id TEXT NOT NULL REFERENCES work_item(work_item_id),
        lease_epoch INTEGER NOT NULL CHECK (lease_epoch >= 1),
        lease_token TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
        route_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        model_requested TEXT NOT NULL,
        request_cache_key TEXT NOT NULL,
        prompt_sha256 TEXT NOT NULL,
        phase TEXT NOT NULL CHECK (phase IN
            ('prepared','abandoned_unsent','send_intent','confirmed_failure',
             'response_available','uncertain')),
        prepared_at REAL NOT NULL,
        send_intent_at REAL,
        completed_at REAL,
        http_status_code INTEGER,
        request_id TEXT,
        receipt_sha256 TEXT,
        failure_category TEXT,
        provider_error_code TEXT,
        late_receipt_sha256 TEXT,
        UNIQUE (work_item_id,ordinal)
    )""",
    """CREATE TABLE work_event (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        work_item_id TEXT NOT NULL REFERENCES work_item(work_item_id),
        attempt_id TEXT REFERENCES attempt(attempt_id),
        event_type TEXT NOT NULL,
        from_status TEXT,
        to_status TEXT,
        lease_epoch INTEGER NOT NULL,
        occurred_at REAL NOT NULL
    )""",
    "CREATE INDEX work_item_status_expiry_idx ON work_item(status,lease_expires_at)",
    "CREATE INDEX attempt_work_phase_idx ON attempt(work_item_id,phase)",
    "CREATE INDEX attempt_request_cache_key_idx ON attempt(request_cache_key)",
)

_DDL_V2_ADDITIONS = (
    """CREATE TABLE answer_checkpoint (
        checkpoint_id TEXT PRIMARY KEY,
        work_item_id TEXT NOT NULL UNIQUE REFERENCES work_item(work_item_id),
        attempt_id TEXT NOT NULL UNIQUE REFERENCES attempt(attempt_id),
        checkpoint_schema_version INTEGER NOT NULL CHECK (checkpoint_schema_version = 1),
        payload_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
        checkpointed_at REAL NOT NULL
    )""",
    """CREATE TRIGGER answer_checkpoint_no_update
        BEFORE UPDATE ON answer_checkpoint
        BEGIN SELECT RAISE(ABORT,'answer checkpoints are immutable'); END""",
    """CREATE TRIGGER answer_checkpoint_no_delete
        BEFORE DELETE ON answer_checkpoint
        BEGIN SELECT RAISE(ABORT,'answer checkpoints are immutable'); END""",
)

_DDL_V3_ADDITIONS = (
    """CREATE TABLE quick_scan_budget_policy (
        policy_id TEXT PRIMARY KEY,
        policy_version TEXT NOT NULL,
        currency TEXT NOT NULL CHECK (length(currency) = 3),
        pricing_basis TEXT NOT NULL CHECK (pricing_basis IN ('verified_rate_card','user_cap')),
        pricing_ref TEXT,
        max_cost_micros INTEGER NOT NULL CHECK (max_cost_micros > 0),
        max_requests INTEGER NOT NULL CHECK (max_requests > 0),
        max_cost_per_attempt_micros INTEGER NOT NULL
            CHECK (max_cost_per_attempt_micros > 0),
        max_in_flight_total INTEGER NOT NULL CHECK (max_in_flight_total BETWEEN 1 AND 32),
        quota_group_limits_json TEXT NOT NULL,
        route_limits_json TEXT NOT NULL,
        policy_snapshot_sha256 TEXT NOT NULL CHECK (length(policy_snapshot_sha256) = 64),
        updated_at REAL NOT NULL
    )""",
    """CREATE TABLE quick_scan_budget_attempt (
        budget_attempt_id TEXT PRIMARY KEY,
        work_attempt_id TEXT UNIQUE REFERENCES attempt(attempt_id),
        policy_id TEXT NOT NULL REFERENCES quick_scan_budget_policy(policy_id),
        policy_version TEXT NOT NULL,
        route_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        model_requested TEXT NOT NULL,
        quota_group TEXT NOT NULL,
        currency TEXT NOT NULL CHECK (length(currency) = 3),
        pricing_basis TEXT NOT NULL CHECK (pricing_basis IN ('verified_rate_card','user_cap')),
        pricing_ref TEXT,
        reserved_cost_micros INTEGER NOT NULL CHECK (reserved_cost_micros > 0),
        actual_cost_micros INTEGER CHECK (actual_cost_micros >= 0),
        request_counted INTEGER NOT NULL CHECK (request_counted IN (0,1)),
        in_flight INTEGER NOT NULL CHECK (in_flight IN (0,1)),
        cost_source_ref TEXT,
        status TEXT NOT NULL CHECK (status IN
            ('in_flight','response_unpriced','failure_unpriced','outcome_uncertain','settled')),
        http_status_code INTEGER CHECK (http_status_code BETWEEN 100 AND 599),
        created_at REAL NOT NULL,
        send_intent_at REAL NOT NULL,
        completed_at REAL,
        reconciled_at REAL,
        CHECK ((status='settled' AND actual_cost_micros IS NOT NULL
                AND in_flight=0 AND reconciled_at IS NOT NULL)
            OR (status!='settled' AND actual_cost_micros IS NULL
                AND reconciled_at IS NULL))
    )""",
    "CREATE INDEX quick_scan_budget_attempt_policy_idx "
    "ON quick_scan_budget_attempt(policy_id,status,actual_cost_micros)",
    "CREATE INDEX quick_scan_budget_attempt_slots_idx "
    "ON quick_scan_budget_attempt(policy_id,in_flight,quota_group,route_id)",
)

_DDL_V4_ADDITIONS = (
    """CREATE TABLE quick_scan_budget_terminal (
        budget_attempt_id TEXT PRIMARY KEY
            REFERENCES quick_scan_budget_attempt(budget_attempt_id),
        terminal_outcome TEXT NOT NULL CHECK (terminal_outcome IN
            ('completed','confirmed_failure','confirmed_not_sent','unknown','legacy_unverified')),
        settled_at REAL NOT NULL
    )""",
    """CREATE TRIGGER quick_scan_budget_terminal_no_update
        BEFORE UPDATE ON quick_scan_budget_terminal
        BEGIN SELECT RAISE(ABORT,'budget terminal outcomes are immutable'); END""",
    """CREATE TRIGGER quick_scan_budget_terminal_no_delete
        BEFORE DELETE ON quick_scan_budget_terminal
        BEGIN SELECT RAISE(ABORT,'budget terminal outcomes are immutable'); END""",
)

_DDL_V5_ADDITIONS = (
    """CREATE TABLE quick_scan_result_delivery (
        delivery_id TEXT PRIMARY KEY,
        work_item_id TEXT NOT NULL UNIQUE REFERENCES work_item(work_item_id),
        state TEXT NOT NULL CHECK (state IN
            ('blocked','ready','send_uncertain','delivered','rejected','conflict')),
        block_code TEXT,
        package_json TEXT,
        package_sha256 TEXT,
        package_bytes_sha256 TEXT,
        package_id TEXT,
        item_id TEXT,
        observation_id TEXT,
        payload_sha256 TEXT,
        delivery_key TEXT,
        ack_json TEXT,
        ack_sha256 TEXT,
        consumer_store_id TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        CHECK (
            (state='blocked' AND block_code IS NOT NULL AND package_json IS NULL
                AND package_sha256 IS NULL AND package_bytes_sha256 IS NULL
                AND package_id IS NULL AND item_id IS NULL
                AND observation_id IS NULL AND payload_sha256 IS NULL AND delivery_key IS NULL
                AND ack_json IS NULL AND ack_sha256 IS NULL AND consumer_store_id IS NULL)
            OR
            (state!='blocked' AND block_code IS NULL AND package_json IS NOT NULL
                AND package_sha256 IS NOT NULL AND length(package_bytes_sha256)=64
                AND package_id IS NOT NULL AND item_id IS NOT NULL
                AND observation_id IS NOT NULL AND payload_sha256 IS NOT NULL
                AND length(delivery_key)=64
                AND ((state IN ('ready','send_uncertain') AND ack_json IS NULL
                        AND ack_sha256 IS NULL AND consumer_store_id IS NULL)
                    OR (state IN ('delivered','rejected','conflict') AND ack_json IS NOT NULL
                        AND length(ack_sha256)=64 AND consumer_store_id IS NOT NULL)))
        )
    )""",
    "CREATE INDEX quick_scan_delivery_state_idx "
    "ON quick_scan_result_delivery(state,updated_at,delivery_id)",
    """CREATE TABLE quick_scan_result_delivery_event (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        delivery_id TEXT NOT NULL REFERENCES quick_scan_result_delivery(delivery_id),
        event_type TEXT NOT NULL CHECK (event_type IN
            ('blocked','package_prepared','send_intent','confirmed_not_sent',
             'accepted','already_present','rejected','conflict')),
        from_state TEXT,
        to_state TEXT NOT NULL CHECK (to_state IN
            ('blocked','ready','send_uncertain','delivered','rejected','conflict')),
        reason_code TEXT,
        ack_sha256 TEXT,
        occurred_at REAL NOT NULL
    )""",
    """CREATE TRIGGER quick_scan_delivery_package_immutable
        BEFORE UPDATE ON quick_scan_result_delivery
        WHEN OLD.package_json IS NOT NULL AND (
            NEW.package_json IS NOT OLD.package_json
            OR NEW.package_sha256 IS NOT OLD.package_sha256
            OR NEW.package_bytes_sha256 IS NOT OLD.package_bytes_sha256
            OR NEW.package_id IS NOT OLD.package_id
            OR NEW.item_id IS NOT OLD.item_id
            OR NEW.observation_id IS NOT OLD.observation_id
            OR NEW.payload_sha256 IS NOT OLD.payload_sha256
            OR NEW.delivery_key IS NOT OLD.delivery_key)
        BEGIN SELECT RAISE(ABORT,'quick-scan delivery package is immutable'); END""",
    """CREATE TRIGGER quick_scan_delivery_state_transition
        BEFORE UPDATE OF state ON quick_scan_result_delivery
        WHEN NOT (
                (OLD.state='blocked' AND NEW.state IN ('blocked','ready'))
                OR (OLD.state='ready' AND NEW.state IN
                    ('send_uncertain','delivered','rejected','conflict'))
                OR (OLD.state='send_uncertain' AND NEW.state IN
                    ('ready','delivered','rejected','conflict')))
            OR (NEW.state IN ('delivered','rejected','conflict') AND NOT COALESCE(
                CASE WHEN json_valid(NEW.ack_json) THEN
                    json_extract(NEW.ack_json,'$.schema_version')='1.0.0'
                    AND json_extract(NEW.ack_json,'$.package_id')=NEW.package_id
                    AND json_extract(NEW.ack_json,'$.item_id')=NEW.item_id
                    AND json_extract(NEW.ack_json,'$.observation_id')=NEW.observation_id
                    AND json_extract(NEW.ack_json,'$.payload_sha256')=NEW.payload_sha256
                    AND json_extract(NEW.ack_json,'$.consumer.component')='StockWiki'
                    AND json_extract(NEW.ack_json,'$.consumer.namespace')='quick_scan'
                    AND json_extract(NEW.ack_json,'$.consumer.store_id')=NEW.consumer_store_id
                    AND (
                        (NEW.state='delivered'
                            AND json_extract(NEW.ack_json,'$.status')
                                IN ('accepted','already_present')
                            AND json_type(NEW.ack_json,'$.error_code')='null')
                        OR (NEW.state='conflict'
                            AND json_extract(NEW.ack_json,'$.status')='conflict'
                            AND json_extract(NEW.ack_json,'$.error_code')
                                ='immutable_key_hash_conflict')
                        OR (NEW.state='rejected'
                            AND json_extract(NEW.ack_json,'$.status')='rejected'
                            AND json_extract(NEW.ack_json,'$.error_code') IN
                                ('unsupported_schema','missing_entity','unknown_question',
                                 'invalid_payload','lineage_violation'))
                    )
                ELSE 0 END,0))
        BEGIN SELECT RAISE(ABORT,'invalid quick-scan delivery state transition'); END""",
    """CREATE TRIGGER quick_scan_delivery_no_delete
        BEFORE DELETE ON quick_scan_result_delivery
        BEGIN SELECT RAISE(ABORT,'quick-scan delivery ledger is immutable'); END""",
    """CREATE TRIGGER quick_scan_delivery_event_no_update
        BEFORE UPDATE ON quick_scan_result_delivery_event
        BEGIN SELECT RAISE(ABORT,'quick-scan delivery events are immutable'); END""",
    """CREATE TRIGGER quick_scan_delivery_event_no_delete
        BEFORE DELETE ON quick_scan_result_delivery_event
        BEGIN SELECT RAISE(ABORT,'quick-scan delivery events are immutable'); END""",
    """CREATE TRIGGER quick_scan_delivery_ack_immutable
        BEFORE UPDATE OF ack_json,ack_sha256,consumer_store_id ON quick_scan_result_delivery
        WHEN OLD.ack_json IS NOT NULL AND (
            NEW.ack_json IS NOT OLD.ack_json
            OR NEW.ack_sha256 IS NOT OLD.ack_sha256
            OR NEW.consumer_store_id IS NOT OLD.consumer_store_id)
        BEGIN SELECT RAISE(ABORT,'quick-scan delivery ACK is immutable'); END""",
)

_DDL = _DDL_V1 + _DDL_V2_ADDITIONS + _DDL_V3_ADDITIONS + _DDL_V4_ADDITIONS + _DDL_V5_ADDITIONS

_RECEIPT_FIELDS = (
    "provider",
    "request_id",
    "response_id",
    "actual_model",
    "search_status",
    "response_status",
    "http_status_code",
    "attempt_id",
    "prompt_sha256",
    "search_receipt_id",
    "failure_type",
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
        elif key == "usage":
            usage = _canonical_usage(value)
            if usage is not None:
                result[key] = usage
        elif value is None or isinstance(value, (str, int, bool)):
            result[key] = value
        elif isinstance(value, float) and math.isfinite(value):
            result[key] = value
    return result


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


def _positive_number(value: object, label: str, *, maximum: float) -> float:
    if type(value) is not int and type(value) is not float:
        raise ValueError(f"invalid {label}")
    if not math.isfinite(value):
        raise ValueError(f"invalid {label}")
    number = float(value)
    if number <= 0 or number > maximum:
        raise ValueError(f"invalid {label}")
    return number


def _normalized_sql(value: str) -> str:
    return " ".join(value.split())


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


class QuickScanWorkStore:
    """SQLite ledger for per-question work, answer checkpoints, and attempts."""

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            self._initialize(connection)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        # A returned SendPermit must survive a power/process crash before POST.
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @classmethod
    def _initialize(cls, connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            objects = cls._schema_objects(connection)
            if version == 0:
                if objects:
                    raise ValueError("quick-scan work database has an unknown schema")
                for statement in _DDL:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif version == 1:
                cls._validate_schema(connection, schema_version=1)
                inconsistent = connection.execute(
                    "SELECT COUNT(*) FROM work_item WHERE status IN ('result_ready','delivered')"
                ).fetchone()[0]
                if inconsistent:
                    raise ValueError("schema v1 contains completed work without answer checkpoints")
                for statement in _DDL_V2_ADDITIONS:
                    connection.execute(statement)
                for statement in _DDL_V3_ADDITIONS:
                    connection.execute(statement)
                cls._apply_v4_migration(connection)
                cls._apply_v5_migration(connection)
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif version == 2:
                cls._validate_schema(connection, schema_version=2)
                for statement in _DDL_V3_ADDITIONS:
                    connection.execute(statement)
                cls._apply_v4_migration(connection)
                cls._apply_v5_migration(connection)
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif version == 3:
                cls._validate_schema(connection, schema_version=3)
                cls._apply_v4_migration(connection)
                cls._apply_v5_migration(connection)
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif version == 4:
                cls._validate_schema(connection, schema_version=4)
                cls._apply_v5_migration(connection)
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise ValueError("unsupported quick-scan work database schema version")
            cls._validate_schema(connection, schema_version=SCHEMA_VERSION)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _apply_v4_migration(connection: sqlite3.Connection) -> None:
        for statement in _DDL_V4_ADDITIONS:
            connection.execute(statement)
        # Pre-v4 settlements did not retain the terminal outcome. Preserve their
        # ledger rows while explicitly marking that missing historical fact.
        connection.execute(
            "INSERT INTO quick_scan_budget_terminal "
            "(budget_attempt_id,terminal_outcome,settled_at) "
            "SELECT budget_attempt_id,'legacy_unverified',reconciled_at "
            "FROM quick_scan_budget_attempt WHERE status='settled'"
        )

    @staticmethod
    def _apply_v5_migration(connection: sqlite3.Connection) -> None:
        for statement in _DDL_V5_ADDITIONS:
            connection.execute(statement)

    @staticmethod
    def _schema_objects(connection: sqlite3.Connection) -> dict[tuple[str, str], str]:
        rows = connection.execute(
            "SELECT type,name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' AND type IN ('table','index','view','trigger')"
        ).fetchall()
        return {(row["type"], row["name"]): row["sql"] for row in rows}

    @classmethod
    def _validate_schema(
        cls, connection: sqlite3.Connection, *, schema_version: int = SCHEMA_VERSION
    ) -> None:
        actual = cls._schema_objects(connection)
        expected = {
            ("table", "work_item"): _DDL_V1[0],
            ("table", "work_run_ref"): _DDL_V1[1],
            ("table", "attempt"): _DDL_V1[2],
            ("table", "work_event"): _DDL_V1[3],
            ("index", "work_item_status_expiry_idx"): _DDL_V1[4],
            ("index", "attempt_work_phase_idx"): _DDL_V1[5],
            ("index", "attempt_request_cache_key_idx"): _DDL_V1[6],
        }
        if schema_version >= 2:
            expected.update(
                {
                    ("table", "answer_checkpoint"): _DDL_V2_ADDITIONS[0],
                    ("trigger", "answer_checkpoint_no_update"): _DDL_V2_ADDITIONS[1],
                    ("trigger", "answer_checkpoint_no_delete"): _DDL_V2_ADDITIONS[2],
                }
            )
        if schema_version >= 3:
            expected.update(
                {
                    ("table", "quick_scan_budget_policy"): _DDL_V3_ADDITIONS[0],
                    ("table", "quick_scan_budget_attempt"): _DDL_V3_ADDITIONS[1],
                    ("index", "quick_scan_budget_attempt_policy_idx"): _DDL_V3_ADDITIONS[2],
                    ("index", "quick_scan_budget_attempt_slots_idx"): _DDL_V3_ADDITIONS[3],
                }
            )
        if schema_version >= 4:
            expected.update(
                {
                    ("table", "quick_scan_budget_terminal"): _DDL_V4_ADDITIONS[0],
                    ("trigger", "quick_scan_budget_terminal_no_update"): _DDL_V4_ADDITIONS[1],
                    ("trigger", "quick_scan_budget_terminal_no_delete"): _DDL_V4_ADDITIONS[2],
                }
            )
        if schema_version >= 5:
            expected.update(
                {
                    ("table", "quick_scan_result_delivery"): _DDL_V5_ADDITIONS[0],
                    ("index", "quick_scan_delivery_state_idx"): _DDL_V5_ADDITIONS[1],
                    ("table", "quick_scan_result_delivery_event"): _DDL_V5_ADDITIONS[2],
                    ("trigger", "quick_scan_delivery_package_immutable"): _DDL_V5_ADDITIONS[3],
                    ("trigger", "quick_scan_delivery_state_transition"): _DDL_V5_ADDITIONS[4],
                    ("trigger", "quick_scan_delivery_no_delete"): _DDL_V5_ADDITIONS[5],
                    ("trigger", "quick_scan_delivery_event_no_update"): _DDL_V5_ADDITIONS[6],
                    ("trigger", "quick_scan_delivery_event_no_delete"): _DDL_V5_ADDITIONS[7],
                    ("trigger", "quick_scan_delivery_ack_immutable"): _DDL_V5_ADDITIONS[8],
                }
            )
        elif schema_version not in {1, 2, 3, 4}:
            raise ValueError("unsupported quick-scan work database schema version")
        if actual.keys() != expected.keys() or any(
            _normalized_sql(actual[key]) != _normalized_sql(statement)
            for key, statement in expected.items()
        ):
            raise ValueError("quick-scan work database schema mismatch")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("quick-scan work database integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("quick-scan work database foreign key check failed")
        if schema_version >= 4:
            inconsistent_terminals = connection.execute(
                "SELECT COUNT(*) FROM quick_scan_budget_attempt a "
                "LEFT JOIN quick_scan_budget_terminal t USING (budget_attempt_id) "
                "WHERE (a.status='settled' AND (t.budget_attempt_id IS NULL "
                "OR t.settled_at!=a.reconciled_at)) "
                "OR (a.status!='settled' AND t.budget_attempt_id IS NOT NULL) "
                "OR (t.terminal_outcome='confirmed_not_sent' "
                "AND (a.actual_cost_micros!=0 OR a.request_counted!=0)) "
                "OR (t.terminal_outcome IN ('completed','confirmed_failure','unknown') "
                "AND a.request_counted!=1)"
            ).fetchone()[0]
            if inconsistent_terminals:
                raise ValueError("quick-scan budget terminal outcome state mismatch")
        if schema_version >= 5:
            inconsistent_delivery = connection.execute(
                "SELECT COUNT(*) FROM quick_scan_result_delivery d "
                "JOIN work_item w USING (work_item_id) "
                "WHERE (d.state='delivered' AND w.status!='delivered') "
                "OR (d.state!='delivered' AND w.status!='result_ready')"
            ).fetchone()[0]
            if inconsistent_delivery:
                raise ValueError("quick-scan result delivery/work status mismatch")
            for row in connection.execute("SELECT * FROM quick_scan_result_delivery"):
                cls._result_delivery_record(row)
        if schema_version >= 2:
            inconsistent = connection.execute(
                "SELECT COUNT(*) FROM work_item w LEFT JOIN answer_checkpoint c "
                "ON c.work_item_id=w.work_item_id "
                "WHERE (w.status IN ('result_ready','delivered') AND c.work_item_id IS NULL) "
                "OR (c.work_item_id IS NOT NULL AND w.status NOT IN ('result_ready','delivered'))"
            ).fetchone()[0]
            if inconsistent:
                raise ValueError("quick-scan work/checkpoint status mismatch")
            for row in connection.execute(
                "SELECT c.*,w.*,a.attempt_id AS bound_attempt_id "
                "FROM answer_checkpoint c JOIN work_item w ON w.work_item_id=c.work_item_id "
                "JOIN attempt a ON a.attempt_id=c.attempt_id"
            ):
                cls._checkpoint_record(connection, row, validate_status=True)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _now(self) -> float:
        return _positive_number(self.clock(), "clock", maximum=10**12)

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        work_item_id: str,
        kind: str,
        epoch: int,
        now: float,
        *,
        attempt_id: Optional[str] = None,
        old: Optional[str] = None,
        new: Optional[str] = None,
    ) -> None:
        connection.execute(
            "INSERT INTO work_event "
            "(work_item_id,attempt_id,event_type,from_status,to_status,lease_epoch,occurred_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (work_item_id, attempt_id, kind, old, new, epoch, now),
        )

    @staticmethod
    def _item(connection: sqlite3.Connection, work_item_id: str) -> sqlite3.Row:
        row: Optional[sqlite3.Row] = connection.execute(
            "SELECT * FROM work_item WHERE work_item_id=?", (work_item_id,)
        ).fetchone()
        if row is None:
            raise KeyError("unknown work item")
        return row

    @staticmethod
    def _assert_lease(row: sqlite3.Row, lease: Lease, now: float) -> None:
        if (
            row["status"] != "leased"
            or row["lease_token"] != lease.lease_token
            or row["lease_epoch"] != lease.lease_epoch
            or row["lease_expires_at"] is None
            or now >= row["lease_expires_at"]
        ):
            raise LeaseFencedError("quick-scan work lease is stale")

    def configure_quick_scan_budget(self, policy: dict) -> dict:
        """Persist a secret-free budget/dispatch snapshot without resetting spend."""
        normalized = _budget_policy_projection(policy)
        now = self._now()
        with self._transaction() as connection:
            previous = connection.execute(
                "SELECT * FROM quick_scan_budget_policy WHERE policy_id=?",
                (normalized["policy_id"],),
            ).fetchone()
            attempts = connection.execute(
                "SELECT COUNT(*) FROM quick_scan_budget_attempt WHERE policy_id=?",
                (normalized["policy_id"],),
            ).fetchone()[0]
            if previous is not None:
                if previous["currency"] != normalized["currency"] and attempts:
                    raise BudgetPolicyConflict("currency cannot change after budget attempts")
                if (
                    previous["policy_version"] != normalized["policy_version"]
                    and connection.execute(
                        "SELECT COUNT(*) FROM quick_scan_budget_attempt "
                        "WHERE policy_id=? AND status!='settled'",
                        (normalized["policy_id"],),
                    ).fetchone()[0]
                ):
                    raise BudgetPolicyConflict(
                        "policy version cannot change while attempts are active or unreconciled"
                    )
                if (
                    previous["policy_version"] == normalized["policy_version"]
                    and previous["policy_snapshot_sha256"] != normalized["policy_snapshot_sha256"]
                ):
                    raise BudgetPolicyConflict("policy version has conflicting contents")
            connection.execute(
                "INSERT INTO quick_scan_budget_policy (policy_id,policy_version,currency,"
                "pricing_basis,pricing_ref,max_cost_micros,max_requests,"
                "max_cost_per_attempt_micros,max_in_flight_total,quota_group_limits_json,"
                "route_limits_json,policy_snapshot_sha256,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(policy_id) DO UPDATE SET policy_version=excluded.policy_version,"
                "currency=excluded.currency,pricing_basis=excluded.pricing_basis,"
                "pricing_ref=excluded.pricing_ref,max_cost_micros=excluded.max_cost_micros,"
                "max_requests=excluded.max_requests,"
                "max_cost_per_attempt_micros=excluded.max_cost_per_attempt_micros,"
                "max_in_flight_total=excluded.max_in_flight_total,"
                "quota_group_limits_json=excluded.quota_group_limits_json,"
                "route_limits_json=excluded.route_limits_json,"
                "policy_snapshot_sha256=excluded.policy_snapshot_sha256,updated_at=excluded.updated_at",
                (
                    normalized["policy_id"],
                    normalized["policy_version"],
                    normalized["currency"],
                    normalized["pricing_basis"],
                    normalized["pricing_ref"],
                    normalized["max_cost_micros"],
                    normalized["max_requests"],
                    normalized["max_cost_per_attempt_micros"],
                    normalized["max_in_flight_total"],
                    normalized["group_limits_json"],
                    normalized["route_limits_json"],
                    normalized["policy_snapshot_sha256"],
                    now,
                ),
            )
        return normalized

    @staticmethod
    def _reserve_budget_attempt_tx(
        connection: sqlite3.Connection,
        *,
        normalized_policy: dict,
        budget_attempt_id: str,
        work_attempt_id: Optional[str],
        route_id: str,
        provider: str,
        model_requested: str,
        quota_group: str,
        now: float,
    ) -> dict:
        _safe(budget_attempt_id, "budget_attempt_id")
        if work_attempt_id is not None:
            _safe(work_attempt_id, "work_attempt_id")
        route_id = _safe_text(route_id, "route_id", maximum=200)
        provider = _safe_text(provider, "provider", maximum=200)
        model_requested = _safe_text(model_requested, "model_requested", maximum=200)
        quota_group = _safe_text(quota_group, "quota_group", maximum=200)
        policy_id = normalized_policy["policy_id"]
        current = connection.execute(
            "SELECT * FROM quick_scan_budget_policy WHERE policy_id=?", (policy_id,)
        ).fetchone()
        if current is None:
            raise BudgetAdmissionError("budget_policy_not_configured")
        if (
            current["policy_version"] != normalized_policy["policy_version"]
            or current["policy_snapshot_sha256"] != normalized_policy["policy_snapshot_sha256"]
        ):
            raise BudgetAdmissionError("budget_policy_version_stale")
        route_identity = normalized_policy["route_identity"].get(route_id)
        if route_identity != {
            "quota_group": quota_group,
            "provider_config_ref": provider,
            "model": model_requested,
        }:
            raise BudgetAdmissionError("budget_route_not_in_policy")
        unreconciled = connection.execute(
            "SELECT COUNT(*) FROM quick_scan_budget_attempt WHERE policy_id=? "
            "AND status IN ('response_unpriced','failure_unpriced','outcome_uncertain')",
            (policy_id,),
        ).fetchone()[0]
        if unreconciled:
            raise BudgetAdmissionError("budget_reconciliation_required")
        totals = connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN status='settled' THEN actual_cost_micros ELSE 0 END),0) "
            "AS spent,COALESCE(SUM(CASE WHEN status!='settled' THEN reserved_cost_micros ELSE 0 END),0) "
            "AS reserved,COALESCE(SUM(request_counted),0) AS requests "
            "FROM quick_scan_budget_attempt WHERE policy_id=?",
            (policy_id,),
        ).fetchone()
        if (
            totals["spent"] + totals["reserved"] + current["max_cost_per_attempt_micros"]
            > current["max_cost_micros"]
        ):
            raise BudgetAdmissionError("budget_cost_limit")
        if totals["requests"] >= current["max_requests"]:
            raise BudgetAdmissionError("budget_request_limit")

        in_flight = connection.execute(
            "SELECT COUNT(*) FROM quick_scan_budget_attempt WHERE policy_id=? AND in_flight=1",
            (policy_id,),
        ).fetchone()[0]
        if in_flight >= current["max_in_flight_total"]:
            raise BudgetAdmissionError("dispatch_global_capacity_full")
        group_limits = json.loads(current["quota_group_limits_json"])
        route_limits = json.loads(current["route_limits_json"])
        if quota_group not in group_limits or route_id not in route_limits:
            raise BudgetAdmissionError("budget_route_limit_missing")
        group_active = connection.execute(
            "SELECT COUNT(*) FROM quick_scan_budget_attempt WHERE policy_id=? "
            "AND quota_group=? AND in_flight=1",
            (policy_id, quota_group),
        ).fetchone()[0]
        if group_active >= group_limits[quota_group]:
            raise BudgetAdmissionError("dispatch_quota_group_capacity_full")
        route_active = connection.execute(
            "SELECT COUNT(*) FROM quick_scan_budget_attempt WHERE policy_id=? "
            "AND route_id=? AND in_flight=1",
            (policy_id, route_id),
        ).fetchone()[0]
        if route_active >= route_limits[route_id]:
            raise BudgetAdmissionError("dispatch_route_capacity_full")
        connection.execute(
            "INSERT INTO quick_scan_budget_attempt (budget_attempt_id,work_attempt_id,policy_id,"
            "policy_version,route_id,provider,model_requested,quota_group,currency,pricing_basis,"
            "pricing_ref,reserved_cost_micros,actual_cost_micros,request_counted,in_flight,status,"
            "created_at,send_intent_at) VALUES (?,?,?,?,?,?,?,?,?,?,?, ?,NULL,1,1,'in_flight',?,?)",
            (
                budget_attempt_id,
                work_attempt_id,
                policy_id,
                current["policy_version"],
                route_id,
                provider,
                model_requested,
                quota_group,
                current["currency"],
                current["pricing_basis"],
                current["pricing_ref"],
                current["max_cost_per_attempt_micros"],
                now,
                now,
            ),
        )
        return dict(
            connection.execute(
                "SELECT * FROM quick_scan_budget_attempt WHERE budget_attempt_id=?",
                (budget_attempt_id,),
            ).fetchone()
        )

    def reserve_budget_attempt(
        self,
        policy: dict,
        *,
        budget_attempt_id: str,
        route_id: str,
        provider: str,
        model_requested: str,
        quota_group: str,
    ) -> dict:
        """Atomically reserve cost, request count, and global/group/route slots."""
        normalized = self.configure_quick_scan_budget(policy)
        with self._transaction() as connection:
            return self._reserve_budget_attempt_tx(
                connection,
                normalized_policy=normalized,
                budget_attempt_id=budget_attempt_id,
                work_attempt_id=None,
                route_id=route_id,
                provider=provider,
                model_requested=model_requested,
                quota_group=quota_group,
                now=self._now(),
            )

    @staticmethod
    def _record_budget_outcome_tx(
        connection: sqlite3.Connection,
        budget_attempt_id: str,
        *,
        outcome: str,
        http_status_code: Optional[int],
        now: float,
        actual_cost_micros: Optional[int] = None,
        cost_source_ref: Optional[str] = None,
    ) -> dict:
        row = connection.execute(
            "SELECT * FROM quick_scan_budget_attempt WHERE budget_attempt_id=?",
            (budget_attempt_id,),
        ).fetchone()
        if row is None:
            raise KeyError("unknown budget attempt")
        if outcome not in {"confirmed_failure", "response_available", "unknown"}:
            raise ValueError("invalid budget attempt outcome")
        if http_status_code is not None and (
            type(http_status_code) is not int or not 100 <= http_status_code <= 599
        ):
            raise ValueError("invalid http_status_code")
        if outcome == "response_available" and (
            http_status_code is None or not 200 <= http_status_code < 300
        ):
            raise ValueError("successful outcome requires a 2xx response")
        if outcome == "confirmed_failure" and (
            http_status_code is None or not 400 <= http_status_code <= 599
        ):
            raise ValueError("confirmed failure requires a received error response")
        if actual_cost_micros is not None:
            if type(actual_cost_micros) is not int or actual_cost_micros < 0:
                raise ValueError("invalid actual cost")
            if not cost_source_ref:
                raise ValueError("exact actual cost requires a source reference")
            cost_source_ref = _safe_text(cost_source_ref, "cost_source_ref", maximum=300)
        if actual_cost_micros is not None and (
            outcome != "unknown" or http_status_code is not None
        ):
            status = "settled"
            in_flight = 0
            reconciled_at = now
        else:
            reconciled_at = None
            if outcome == "unknown" and http_status_code is None:
                status = "outcome_uncertain"
                in_flight = 1
            elif outcome == "response_available":
                status = "response_unpriced"
                in_flight = 0
            else:
                status = "failure_unpriced"
                in_flight = 0
        expected_cost = actual_cost_micros if status == "settled" else None
        expected_source = cost_source_ref if status == "settled" else None
        if row["status"] != "in_flight":
            terminal_matches = True
            if row["status"] == "settled":
                prior_terminal = connection.execute(
                    "SELECT terminal_outcome FROM quick_scan_budget_terminal "
                    "WHERE budget_attempt_id=?",
                    (budget_attempt_id,),
                ).fetchone()
                expected_terminal = {
                    "response_available": "completed",
                    "confirmed_failure": "confirmed_failure",
                    "unknown": "unknown",
                }[outcome]
                terminal_matches = (
                    prior_terminal is not None
                    and prior_terminal["terminal_outcome"] == expected_terminal
                )
            if (
                row["status"] == status
                and row["in_flight"] == in_flight
                and row["http_status_code"] == http_status_code
                and row["actual_cost_micros"] == expected_cost
                and row["cost_source_ref"] == expected_source
                and terminal_matches
            ):
                return dict(row)
            raise WorkConflictError("budget attempt already has a different outcome")
        connection.execute(
            "UPDATE quick_scan_budget_attempt SET status=?,in_flight=?,http_status_code=?,"
            "actual_cost_micros=?,cost_source_ref=?,completed_at=?,reconciled_at=? "
            "WHERE budget_attempt_id=? AND status='in_flight'",
            (
                status,
                in_flight,
                http_status_code,
                expected_cost,
                expected_source,
                now,
                reconciled_at,
                budget_attempt_id,
            ),
        )
        if status == "settled":
            terminal_outcome = {
                "response_available": "completed",
                "confirmed_failure": "confirmed_failure",
                "unknown": "unknown",
            }[outcome]
            connection.execute(
                "INSERT INTO quick_scan_budget_terminal "
                "(budget_attempt_id,terminal_outcome,settled_at) VALUES (?,?,?)",
                (budget_attempt_id, terminal_outcome, now),
            )
        return dict(
            connection.execute(
                "SELECT * FROM quick_scan_budget_attempt WHERE budget_attempt_id=?",
                (budget_attempt_id,),
            ).fetchone()
        )

    def record_budget_outcome(
        self,
        budget_attempt_id: str,
        *,
        outcome: str,
        http_status_code: Optional[int],
        actual_cost: Optional[object] = None,
        cost_source_ref: Optional[str] = None,
    ) -> dict:
        """Record transport completion; unknown charges remain reserved and pause admission."""
        actual_micros = (
            None
            if actual_cost is None
            else _cost_to_micros(actual_cost, "actual_cost", allow_zero=True)
        )
        now = self._now()
        with self._transaction() as connection:
            return self._record_budget_outcome_tx(
                connection,
                budget_attempt_id,
                outcome=outcome,
                http_status_code=http_status_code,
                now=now,
                actual_cost_micros=actual_micros,
                cost_source_ref=cost_source_ref,
            )

    def reconcile_budget_attempt(
        self,
        budget_attempt_id: str,
        *,
        resolved_outcome: str,
        actual_cost: object,
        cost_source_ref: str,
    ) -> dict:
        """Settle a retained reservation only after a verifiable provider cost/outcome."""
        if resolved_outcome not in {"completed", "confirmed_failure", "confirmed_not_sent"}:
            raise ValueError("invalid reconciled outcome")
        actual_micros = _cost_to_micros(actual_cost, "actual_cost", allow_zero=True)
        if resolved_outcome == "confirmed_not_sent" and actual_micros != 0:
            raise ValueError("confirmed_not_sent must have zero cost")
        request_counted = 0 if resolved_outcome == "confirmed_not_sent" else 1
        source = _safe_text(cost_source_ref, "cost_source_ref", maximum=300)
        now = self._now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM quick_scan_budget_attempt WHERE budget_attempt_id=?",
                (budget_attempt_id,),
            ).fetchone()
            if row is None:
                raise KeyError("unknown budget attempt")
            if row["status"] == "settled":
                prior_terminal = connection.execute(
                    "SELECT terminal_outcome FROM quick_scan_budget_terminal "
                    "WHERE budget_attempt_id=?",
                    (budget_attempt_id,),
                ).fetchone()
                if (
                    prior_terminal is not None
                    and prior_terminal["terminal_outcome"] == resolved_outcome
                    and row["actual_cost_micros"] == actual_micros
                    and row["cost_source_ref"] == source
                    and row["request_counted"] == request_counted
                ):
                    return dict(row)
                raise WorkConflictError("budget attempt was already reconciled differently")
            prior_terminal = connection.execute(
                "SELECT terminal_outcome FROM quick_scan_budget_terminal "
                "WHERE budget_attempt_id=?",
                (budget_attempt_id,),
            ).fetchone()
            if prior_terminal is not None:
                raise WorkConflictError("budget attempt has an unexpected prior terminal outcome")
            connection.execute(
                "UPDATE quick_scan_budget_attempt SET status='settled',actual_cost_micros=?,"
                "request_counted=?,in_flight=0,cost_source_ref=?,completed_at=?,reconciled_at=? "
                "WHERE budget_attempt_id=?",
                (actual_micros, request_counted, source, now, now, budget_attempt_id),
            )
            connection.execute(
                "INSERT INTO quick_scan_budget_terminal "
                "(budget_attempt_id,terminal_outcome,settled_at) VALUES (?,?,?)",
                (budget_attempt_id, resolved_outcome, now),
            )
            return dict(
                connection.execute(
                    "SELECT * FROM quick_scan_budget_attempt WHERE budget_attempt_id=?",
                    (budget_attempt_id,),
                ).fetchone()
            )

    def get_quick_scan_budget_status(self, policy_id: str) -> dict:
        _safe(policy_id, "policy_id")
        with closing(self._connect()) as connection:
            policy = connection.execute(
                "SELECT * FROM quick_scan_budget_policy WHERE policy_id=?", (policy_id,)
            ).fetchone()
            if policy is None:
                raise KeyError("unknown budget policy")
            totals = connection.execute(
                "SELECT COALESCE(SUM(CASE WHEN status='settled' THEN actual_cost_micros ELSE 0 END),0) "
                "AS spent,COALESCE(SUM(CASE WHEN status!='settled' THEN reserved_cost_micros ELSE 0 END),0) "
                "AS reserved,COALESCE(SUM(request_counted),0) AS requests,"
                "COALESCE(SUM(in_flight),0) AS in_flight,"
                "SUM(CASE WHEN status IN ('response_unpriced','failure_unpriced','outcome_uncertain') "
                "THEN 1 ELSE 0 END) AS unreconciled "
                "FROM quick_scan_budget_attempt WHERE policy_id=?",
                (policy_id,),
            ).fetchone()
            return {
                "policy_id": policy["policy_id"],
                "policy_version": policy["policy_version"],
                "currency": policy["currency"],
                "max_cost_micros": policy["max_cost_micros"],
                "max_requests": policy["max_requests"],
                "spent_micros": totals["spent"],
                "reserved_micros": totals["reserved"],
                "requests": totals["requests"],
                "in_flight": totals["in_flight"],
                "unreconciled_attempts": totals["unreconciled"] or 0,
            }

    def create_or_attach(
        self,
        *,
        entity_id: str,
        question_id: str,
        generation: int,
        scope: str,
        scope_id: str,
        identity_revision: int,
        source_binding_version: int,
        identity_state: str,
        source_binding_ref: str,
        source_binding_refs: Sequence[str],
        identity_snapshot_sha256: str,
        question_fingerprint: str,
        routing_fingerprint: str,
        run_id: str,
        scan_id: str,
    ) -> dict:
        """Atomically attach a run to the one immutable logical work item."""
        if not _ENTITY_ID.fullmatch(_safe(entity_id, "entity_id")):
            raise ValueError("invalid entity_id")
        _safe(question_id, "question_id")
        _safe(scope_id, "scope_id")
        if type(generation) is not int or generation < 1:
            raise ValueError("invalid generation")
        if type(identity_revision) is not int or identity_revision < 1:
            raise ValueError("invalid identity_revision")
        if type(source_binding_version) is not int or source_binding_version < 1:
            raise ValueError("invalid source_binding_version")
        if not isinstance(identity_state, str) or identity_state not in {"provisional", "verified"}:
            raise ValueError("invalid identity_state")
        if scope == "entity":
            if scope_id != entity_id:
                raise ValueError("entity scope_id must equal entity_id")
        elif scope == "security":
            if not _SECURITY_ID.fullmatch(scope_id):
                raise ValueError("invalid security scope_id")
        elif scope == "segment":
            if not _SEGMENT_ID.fullmatch(scope_id):
                raise ValueError("invalid segment scope_id")
        else:
            raise ValueError("invalid scope")
        if not _BINDING_ID.fullmatch(_safe(source_binding_ref, "source_binding_ref")):
            raise ValueError("invalid source_binding_ref")
        source_binding_refs_json = _canonical_bindings(source_binding_ref, source_binding_refs)
        if identity_state == "provisional" and len(source_binding_refs) != 1:
            raise ValueError("provisional identity requires one source binding")
        _sha256(identity_snapshot_sha256, "identity_snapshot_sha256")
        _sha256(question_fingerprint, "question_fingerprint")
        _sha256(routing_fingerprint, "routing_fingerprint")
        _safe(run_id, "run_id")
        _safe(scan_id, "scan_id")
        now = self._now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM work_item WHERE entity_id=? AND question_id=? "
                "AND generation=? AND scope=? AND scope_id=? AND identity_revision=? "
                "AND source_binding_version=? AND identity_state=? AND source_binding_refs_json=?",
                (
                    entity_id,
                    question_id,
                    generation,
                    scope,
                    scope_id,
                    identity_revision,
                    source_binding_version,
                    identity_state,
                    source_binding_refs_json,
                ),
            ).fetchone()
            if row is None:
                work_item_id = "WORK_" + uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO work_item (work_item_id,entity_id,question_id,generation,scope,scope_id,"
                    "identity_revision,source_binding_version,identity_state,source_binding_ref,"
                    "source_binding_refs_json,identity_snapshot_sha256,"
                    "question_fingerprint,routing_fingerprint,status,lease_epoch,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',0,?,?)",
                    (
                        work_item_id,
                        entity_id,
                        question_id,
                        generation,
                        scope,
                        scope_id,
                        identity_revision,
                        source_binding_version,
                        identity_state,
                        source_binding_ref,
                        source_binding_refs_json,
                        identity_snapshot_sha256,
                        question_fingerprint,
                        routing_fingerprint,
                        now,
                        now,
                    ),
                )
                self._event(connection, work_item_id, "created", 0, now, new="pending")
            else:
                work_item_id = row["work_item_id"]
                frozen = (
                    "identity_snapshot_sha256",
                    "question_fingerprint",
                    "routing_fingerprint",
                )
                proposed = locals()
                if any(row[name] != proposed[name] for name in frozen):
                    raise WorkConflictError("logical work key conflicts with frozen inputs")
            added = connection.execute(
                "INSERT OR IGNORE INTO work_run_ref (work_item_id,run_id,scan_id,attached_at) "
                "VALUES (?,?,?,?)",
                (work_item_id, run_id, scan_id, now),
            ).rowcount
            if added:
                epoch = 0 if row is None else row["lease_epoch"]
                self._event(connection, work_item_id, "attached_run", epoch, now)
            return dict(self._item(connection, work_item_id))

    def claim(self, work_item_id: str, *, lease_seconds: float) -> Optional[Lease]:
        """Claim only pending work; expired leases require explicit recovery first."""
        _safe(work_item_id, "work_item_id")
        seconds = _positive_number(lease_seconds, "lease_seconds", maximum=86400)
        now = self._now()
        with self._transaction() as connection:
            row = self._item(connection, work_item_id)
            if row["status"] != "pending":
                return None
            token = uuid.uuid4().hex
            epoch = row["lease_epoch"] + 1
            changed = connection.execute(
                "UPDATE work_item SET status='leased', lease_token=?, lease_epoch=?, "
                "lease_expires_at=?, updated_at=? WHERE work_item_id=? AND status='pending' "
                "AND lease_epoch=?",
                (token, epoch, now + seconds, now, work_item_id, row["lease_epoch"]),
            ).rowcount
            if changed != 1:
                raise LeaseFencedError("quick-scan work claim lost race")
            self._event(
                connection, work_item_id, "claimed", epoch, now, old="pending", new="leased"
            )
            return Lease(token, epoch)

    def prepare_attempt(
        self,
        work_item_id: str,
        lease: Lease,
        *,
        route_id: str,
        provider: str,
        model_requested: str,
        request_cache_key: str,
        prompt_sha256: str,
        allow_format_repair: bool = False,
    ) -> dict:
        """Freeze one transport attempt under a current lease, before POST."""
        if type(allow_format_repair) is not bool:
            raise ValueError("allow_format_repair must be a boolean")
        for label, value in (
            ("route_id", route_id),
            ("provider", provider),
            ("model_requested", model_requested),
        ):
            _safe(value, label)
        if not isinstance(request_cache_key, str) or not _REQUEST_KEY.fullmatch(request_cache_key):
            raise ValueError("invalid request_cache_key")
        _sha256(prompt_sha256, "prompt_sha256")
        now = self._now()
        with self._transaction() as connection:
            item = self._item(connection, work_item_id)
            self._assert_lease(item, lease, now)
            previous = connection.execute(
                "SELECT * FROM attempt WHERE work_item_id=? ORDER BY ordinal DESC LIMIT 1",
                (work_item_id,),
            ).fetchone()
            if previous is not None and previous["phase"] not in {
                "confirmed_failure",
                "abandoned_unsent",
            }:
                is_explicit_format_repair = (
                    allow_format_repair
                    and previous["phase"] == "response_available"
                    and previous["lease_epoch"] == lease.lease_epoch
                    and previous["lease_token"] == lease.lease_token
                    and previous["route_id"] == route_id
                    and previous["provider"] == provider
                    and previous["model_requested"] == model_requested
                    and previous["prompt_sha256"] != prompt_sha256
                    and previous["request_cache_key"] != request_cache_key
                    and connection.execute(
                        "SELECT COUNT(*) FROM attempt WHERE work_item_id=? "
                        "AND lease_epoch=? AND lease_token=? AND phase='response_available'",
                        (work_item_id, lease.lease_epoch, lease.lease_token),
                    ).fetchone()[0]
                    == 1
                )
                if not is_explicit_format_repair:
                    raise WorkConflictError("previous attempt is unresolved")
            same_key = connection.execute(
                "SELECT work_item_id,provider,model_requested,route_id,prompt_sha256 "
                "FROM attempt WHERE request_cache_key=? LIMIT 1",
                (request_cache_key,),
            ).fetchone()
            exact_request = {
                "provider": provider,
                "model_requested": model_requested,
                "route_id": route_id,
                "prompt_sha256": prompt_sha256,
            }
            if same_key is not None and (
                same_key["work_item_id"] != work_item_id
                or any(same_key[name] != value for name, value in exact_request.items())
            ):
                raise WorkConflictError("request key conflicts with frozen attempt inputs")
            attempt_id = "ATTEMPT_" + uuid.uuid4().hex
            ordinal = 1 if previous is None else previous["ordinal"] + 1
            connection.execute(
                "INSERT INTO attempt (attempt_id,work_item_id,lease_epoch,lease_token,ordinal,"
                "route_id,provider,model_requested,request_cache_key,prompt_sha256,phase,prepared_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,'prepared',?)",
                (
                    attempt_id,
                    work_item_id,
                    lease.lease_epoch,
                    lease.lease_token,
                    ordinal,
                    route_id,
                    provider,
                    model_requested,
                    request_cache_key,
                    prompt_sha256,
                    now,
                ),
            )
            self._event(
                connection,
                work_item_id,
                "attempt_prepared",
                lease.lease_epoch,
                now,
                attempt_id=attempt_id,
            )
            return dict(
                connection.execute(
                    "SELECT * FROM attempt WHERE attempt_id=?", (attempt_id,)
                ).fetchone()
            )

    def mark_send_intent(
        self,
        work_item_id: str,
        lease: Lease,
        attempt_id: str,
        *,
        budget_policy: Optional[dict] = None,
        budget_route: Optional[dict] = None,
    ) -> SendPermit:
        """Commit intent with synchronous=FULL before returning permission to POST."""
        _safe(attempt_id, "attempt_id")
        if (budget_policy is None) != (budget_route is None):
            raise ValueError("budget policy and route must be supplied together")
        normalized_policy = (
            None if budget_policy is None else _budget_policy_projection(budget_policy)
        )
        now = self._now()
        with self._transaction() as connection:
            item = self._item(connection, work_item_id)
            self._assert_lease(item, lease, now)
            if normalized_policy is not None:
                if budget_route is None:
                    raise RuntimeError("mark_send_intent: budget_route is None")
                route_id = budget_route.get("route_id")
                provider = budget_route.get("provider")
                model_requested = budget_route.get("model_requested")
                quota_group = budget_route.get("quota_group")
                if (
                    route_id is None
                    or provider is None
                    or model_requested is None
                    or quota_group is None
                ):
                    raise RuntimeError("mark_send_intent: budget_route fields are missing")
                self._reserve_budget_attempt_tx(
                    connection,
                    normalized_policy=normalized_policy,
                    budget_attempt_id=attempt_id,
                    work_attempt_id=attempt_id,
                    route_id=route_id,
                    provider=provider,
                    model_requested=model_requested,
                    quota_group=quota_group,
                    now=now,
                )
            changed = connection.execute(
                "UPDATE attempt SET phase='send_intent',send_intent_at=? "
                "WHERE attempt_id=? AND work_item_id=? AND lease_epoch=? "
                "AND lease_token=? AND phase='prepared'",
                (now, attempt_id, work_item_id, lease.lease_epoch, lease.lease_token),
            ).rowcount
            if changed != 1:
                raise WorkConflictError("attempt is not prepared under this lease")
            self._event(
                connection,
                work_item_id,
                "send_intent",
                lease.lease_epoch,
                now,
                attempt_id=attempt_id,
            )
        return SendPermit(attempt_id, now, lease.lease_epoch)

    def record_attempt_outcome(
        self,
        work_item_id: str,
        lease: Lease,
        attempt_id: str,
        *,
        outcome: str,
        http_status_code: Optional[int] = None,
        receipt_sha256: Optional[str] = None,
        failure_category: Optional[str] = None,
        provider_error_code: Optional[str] = None,
        request_id: Optional[str] = None,
        actual_cost: Optional[object] = None,
        cost_source_ref: Optional[str] = None,
    ) -> None:
        """Record a sanitized transport outcome; answer checkpoint is Q07."""
        if outcome not in {"confirmed_failure", "response_available", "unknown"}:
            raise ValueError("invalid attempt outcome")
        if http_status_code is not None and (
            type(http_status_code) is not int or not 100 <= http_status_code <= 599
        ):
            raise ValueError("invalid http_status_code")
        if receipt_sha256 is not None:
            _sha256(receipt_sha256, "receipt_sha256")
        if failure_category is not None:
            _safe(failure_category, "failure_category")
        if provider_error_code is not None:
            _safe(provider_error_code, "provider_error_code")
        if request_id is not None:
            _safe(request_id, "request_id")
        actual_cost_micros = (
            None
            if actual_cost is None
            else _cost_to_micros(actual_cost, "actual_cost", allow_zero=True)
        )
        if actual_cost_micros is not None and cost_source_ref is None:
            raise ValueError("exact actual cost requires a source reference")
        if cost_source_ref is not None:
            cost_source_ref = _safe_text(cost_source_ref, "cost_source_ref", maximum=300)
        confirmed_auth_refusal = (
            http_status_code in {401, 403}
            and receipt_sha256 is not None
            and failure_category == "authentication_rejected"
        )
        confirmed_model_refusal = (
            http_status_code == 404
            and receipt_sha256 is not None
            and failure_category == "model_or_endpoint_unavailable"
        )
        confirmed_quota_refusal = (
            http_status_code == 429
            and receipt_sha256 is not None
            and (
                (
                    failure_category == "rate_limited"
                    and provider_error_code
                    in {"rate_limit_exceeded", "too_many_requests", "rate_limited"}
                )
                or (
                    failure_category == "quota_exhausted"
                    and provider_error_code
                    in {
                        "insufficient_quota",
                        "quota_exceeded",
                        "billing_hard_limit_reached",
                        "account_quota_exceeded",
                        "insufficient_funds",
                    }
                )
            )
        )
        confirmed_refusal = (
            confirmed_auth_refusal or confirmed_model_refusal or confirmed_quota_refusal
        )
        if outcome == "confirmed_failure" and not confirmed_refusal:
            raise ValueError("confirmed refusal requires a recognized provider rejection")
        if outcome == "response_available" and (
            http_status_code is None or not 200 <= http_status_code < 300 or receipt_sha256 is None
        ):
            raise ValueError("response requires successful HTTP status and receipt hash")
        now = self._now()
        with self._transaction() as connection:
            item = self._item(connection, work_item_id)
            self._assert_lease(item, lease, now)
            changed = connection.execute(
                "UPDATE attempt SET phase=?,completed_at=?,http_status_code=?,request_id=?,"
                "receipt_sha256=?,failure_category=?,provider_error_code=? "
                "WHERE attempt_id=? AND work_item_id=? "
                "AND lease_epoch=? AND lease_token=? AND phase='send_intent'",
                (
                    "uncertain" if outcome == "unknown" else outcome,
                    now,
                    http_status_code,
                    request_id,
                    receipt_sha256,
                    failure_category,
                    provider_error_code,
                    attempt_id,
                    work_item_id,
                    lease.lease_epoch,
                    lease.lease_token,
                ),
            ).rowcount
            if changed != 1:
                raise WorkConflictError("attempt is not in send_intent")
            if outcome == "unknown":
                connection.execute(
                    "UPDATE work_item SET status='uncertain',uncertain_attempt_id=?,"
                    "lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE work_item_id=?",
                    (attempt_id, now, work_item_id),
                )
            budget_attempt = connection.execute(
                "SELECT budget_attempt_id FROM quick_scan_budget_attempt "
                "WHERE work_attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if budget_attempt is not None:
                self._record_budget_outcome_tx(
                    connection,
                    budget_attempt["budget_attempt_id"],
                    outcome=outcome,
                    http_status_code=http_status_code,
                    now=now,
                    actual_cost_micros=actual_cost_micros,
                    cost_source_ref=cost_source_ref,
                )
            self._event(
                connection,
                work_item_id,
                outcome,
                lease.lease_epoch,
                now,
                attempt_id=attempt_id,
                old="leased",
                new="uncertain" if outcome == "unknown" else "leased",
            )

    def recover_expired(self, work_item_id: str) -> str:
        """Release provably unsent work; a send intent is always uncertain."""
        now = self._now()
        with self._transaction() as connection:
            item = self._item(connection, work_item_id)
            if item["status"] != "leased" or now < item["lease_expires_at"]:
                return str(item["status"])
            unresolved = connection.execute(
                "SELECT attempt_id FROM attempt WHERE work_item_id=? AND lease_epoch=? "
                "AND phase IN ('send_intent','response_available','uncertain') "
                "ORDER BY ordinal DESC LIMIT 1",
                (work_item_id, item["lease_epoch"]),
            ).fetchone()
            target = "uncertain" if unresolved is not None else "pending"
            attempt_id = unresolved["attempt_id"] if unresolved is not None else None
            if target == "pending":
                abandoned = connection.execute(
                    "SELECT attempt_id FROM attempt WHERE work_item_id=? AND lease_epoch=? "
                    "AND phase='prepared' ORDER BY ordinal",
                    (work_item_id, item["lease_epoch"]),
                ).fetchall()
                for prepared in abandoned:
                    connection.execute(
                        "UPDATE attempt SET phase='abandoned_unsent',completed_at=? "
                        "WHERE attempt_id=? AND phase='prepared'",
                        (now, prepared["attempt_id"]),
                    )
                    self._event(
                        connection,
                        work_item_id,
                        "attempt_abandoned_unsent",
                        item["lease_epoch"],
                        now,
                        attempt_id=prepared["attempt_id"],
                    )
            connection.execute(
                "UPDATE work_item SET status=?,lease_token=NULL,lease_expires_at=NULL,"
                "uncertain_attempt_id=?,updated_at=? WHERE work_item_id=? AND status='leased' "
                "AND lease_epoch=?",
                (target, attempt_id, now, work_item_id, item["lease_epoch"]),
            )
            self._event(
                connection,
                work_item_id,
                "lease_expired_uncertain" if unresolved is not None else "lease_expired_unsent",
                item["lease_epoch"],
                now,
                attempt_id=attempt_id,
                old="leased",
                new=target,
            )
            return target

    def note_late_receipt(
        self,
        work_item_id: str,
        lease: Lease,
        attempt_id: str,
        *,
        receipt_sha256: str,
        budget_outcome: Optional[str] = None,
        http_status_code: Optional[int] = None,
        actual_cost: Optional[object] = None,
        cost_source_ref: Optional[str] = None,
    ) -> None:
        """Keep a late receipt and close its budget transport slot atomically."""
        _sha256(receipt_sha256, "receipt_sha256")
        if budget_outcome is None and any(
            value is not None for value in (http_status_code, actual_cost, cost_source_ref)
        ):
            raise ValueError("late budget details require a budget outcome")
        actual_cost_micros = (
            None
            if actual_cost is None
            else _cost_to_micros(actual_cost, "actual_cost", allow_zero=True)
        )
        if actual_cost_micros is not None and not cost_source_ref:
            raise ValueError("exact actual cost requires a source reference")
        now = self._now()
        with self._transaction() as connection:
            item = self._item(connection, work_item_id)
            attempt = connection.execute(
                "SELECT * FROM attempt WHERE attempt_id=? AND work_item_id=? "
                "AND lease_epoch=? AND lease_token=?",
                (attempt_id, work_item_id, lease.lease_epoch, lease.lease_token),
            ).fetchone()
            if attempt is None or attempt["phase"] not in {
                "send_intent",
                "response_available",
                "uncertain",
            }:
                raise WorkConflictError("late receipt does not match a sent attempt")
            if (
                item["status"] == "leased"
                and item["lease_epoch"] == lease.lease_epoch
                and item["lease_token"] == lease.lease_token
                and now < item["lease_expires_at"]
            ):
                raise WorkConflictError("attempt still has a current lease")
            previous = attempt["late_receipt_sha256"]
            if (
                attempt["receipt_sha256"] is not None
                and attempt["receipt_sha256"] != receipt_sha256
            ):
                raise WorkConflictError("late receipt hash conflicts with attempt receipt")
            if previous is not None:
                if previous != receipt_sha256:
                    raise WorkConflictError("late receipt hash conflicts with stored receipt")
            else:
                connection.execute(
                    "UPDATE attempt SET late_receipt_sha256=? WHERE attempt_id=?",
                    (receipt_sha256, attempt_id),
                )
                self._event(
                    connection,
                    work_item_id,
                    "late_receipt",
                    lease.lease_epoch,
                    now,
                    attempt_id=attempt_id,
                )
            if budget_outcome is not None:
                budget_attempt = connection.execute(
                    "SELECT budget_attempt_id,status FROM quick_scan_budget_attempt "
                    "WHERE work_attempt_id=?",
                    (attempt_id,),
                ).fetchone()
                if budget_attempt is None:
                    raise WorkConflictError("late budget outcome has no linked reservation")
                if budget_attempt["status"] != "in_flight" and previous is None:
                    raise WorkConflictError("late budget reservation is no longer in flight")
                self._record_budget_outcome_tx(
                    connection,
                    budget_attempt["budget_attempt_id"],
                    outcome=budget_outcome,
                    http_status_code=http_status_code,
                    now=now,
                    actual_cost_micros=actual_cost_micros,
                    cost_source_ref=cost_source_ref,
                )

    @staticmethod
    def _checkpoint_record(
        connection: sqlite3.Connection, row: sqlite3.Row, *, validate_status: bool = True
    ) -> dict:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            raise ValueError("answer checkpoint payload is invalid") from None
        expected_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
                "utf-8"
            )
        ).hexdigest()
        if expected_hash != row["payload_sha256"]:
            raise ValueError("answer checkpoint hash mismatch")
        if (
            not isinstance(payload, dict)
            or payload.get("checkpoint_schema") != "quick-scan-answer"
            or payload.get("checkpoint_schema_version") != 1
            or row["checkpoint_schema_version"] != 1
            or payload.get("checkpointed_at") != row["checkpointed_at"]
            or payload.get("attempt_id") != row["attempt_id"]
            or payload.get("work_item_id") != row["work_item_id"]
        ):
            raise ValueError("answer checkpoint binding mismatch")
        item = connection.execute(
            "SELECT * FROM work_item WHERE work_item_id=?", (row["work_item_id"],)
        ).fetchone()
        attempt = connection.execute(
            "SELECT * FROM attempt WHERE attempt_id=? AND work_item_id=?",
            (row["attempt_id"], row["work_item_id"]),
        ).fetchone()
        if item is None or attempt is None:
            raise ValueError("answer checkpoint references missing work")
        expected_work = {
            key: item[key]
            for key in (
                "entity_id",
                "question_id",
                "generation",
                "scope",
                "scope_id",
                "identity_revision",
                "source_binding_version",
                "identity_state",
                "source_binding_ref",
                "source_binding_refs_json",
                "identity_snapshot_sha256",
                "question_fingerprint",
                "routing_fingerprint",
            )
        }
        if payload.get("work") != expected_work or item["identity_state"] != "verified":
            raise ValueError("answer checkpoint identity/question binding mismatch")
        answer = payload.get("answer")
        answer_status = answer.get("status") if isinstance(answer, dict) else None
        if (
            not isinstance(answer, dict)
            or set(answer) != {"entity_id", "question_id", "status", "score", "description"}
            or answer.get("entity_id") != item["entity_id"]
            or answer.get("question_id") != item["question_id"]
            or not isinstance(answer.get("description"), str)
            or not answer["description"].strip()
            or len(answer["description"]) > 5000
            or not isinstance(answer_status, str)
            or answer_status not in {"scored", "unknown", "insufficient_evidence", "not_applicable"}
            or (
                answer_status == "scored"
                and (type(answer.get("score")) is not int or not 1 <= answer["score"] <= 10)
            )
            or (answer_status != "scored" and answer.get("score") is not None)
        ):
            raise ValueError("answer checkpoint answer contract mismatch")
        provenance = payload.get("provenance")
        actual_provider = (
            provenance.get("actual_provider") if isinstance(provenance, dict) else None
        )
        if (
            not isinstance(provenance, dict)
            or provenance.get("route_id") != attempt["route_id"]
            or provenance.get("route_provider") != attempt["provider"]
            or provenance.get("model_requested") != attempt["model_requested"]
            or provenance.get("work_prompt_sha256") != attempt["prompt_sha256"]
            or provenance.get("receipt_sha256") != attempt["receipt_sha256"]
            or provenance.get("attempt_lease_epoch") != attempt["lease_epoch"]
            or provenance.get("actual_model") != attempt["model_requested"]
            or not isinstance(actual_provider, str)
            or actual_provider not in {"openai", "minimax", "mimo"}
            or provenance.get("search_status") != "executed"
            or provenance.get("response_status") != "completed"
            or provenance.get("http_status_code") != attempt["http_status_code"]
            or attempt["phase"] != "response_available"
            or attempt["http_status_code"] is None
            or not 200 <= attempt["http_status_code"] < 300
        ):
            raise ValueError("answer checkpoint attempt binding mismatch")
        try:
            _sha256(provenance.get("receipt_sha256"), "checkpoint receipt hash")
            _sha256(provenance.get("provider_prompt_sha256"), "provider prompt hash")
            _safe_text(provenance.get("provider_attempt_id"), "provider attempt id", maximum=300)
            _timestamp(provenance.get("response_completed_at"), "response completion timestamp")
            _canonical_source_urls(provenance.get("source_urls"))
        except ValueError as error:
            raise ValueError("answer checkpoint provenance is invalid") from error
        if validate_status and item["status"] not in {"result_ready", "delivered"}:
            raise ValueError("answer checkpoint work status mismatch")
        return {
            "checkpoint_id": row["checkpoint_id"],
            "work_item_id": row["work_item_id"],
            "attempt_id": row["attempt_id"],
            "checkpoint_schema_version": row["checkpoint_schema_version"],
            "payload_sha256": row["payload_sha256"],
            "checkpointed_at": row["checkpointed_at"],
            "payload": payload,
        }

    def save_answer_checkpoint(
        self,
        work_item_id: str,
        lease: Lease,
        attempt_id: str,
        *,
        answer: dict,
        execution_receipt: dict,
    ) -> dict:
        """Atomically persist one validated answer and mark its question complete."""
        _safe(work_item_id, "work_item_id")
        _safe(attempt_id, "attempt_id")
        if not isinstance(answer, dict) or set(answer) != {
            "entity_id",
            "question_id",
            "status",
            "score",
            "description",
        }:
            raise ValueError("answer must match the normalized checkpoint contract")
        entity_id = answer["entity_id"]
        question_id = _safe(answer["question_id"], "question_id")
        if not _ENTITY_ID.fullmatch(_safe(entity_id, "entity_id")):
            raise ValueError("invalid answer entity_id")
        status = answer["status"]
        score = answer["score"]
        description = _safe_text(answer["description"], "answer description", maximum=5000)
        if not isinstance(status, str) or status not in {
            "scored",
            "unknown",
            "insufficient_evidence",
            "not_applicable",
        }:
            raise ValueError("answer status is not reusable")
        if status == "scored":
            if type(score) is not int or not 1 <= score <= 10:
                raise ValueError("scored answer requires an integer score from 1 to 10")
        elif score is not None:
            raise ValueError("non-scored answer must have a null score")
        normalized_answer = {
            "entity_id": entity_id,
            "question_id": question_id,
            "status": status,
            "score": score,
            "description": description,
        }

        if not isinstance(execution_receipt, dict):
            raise ValueError("execution receipt is required")
        receipt = _sanitized_receipt(execution_receipt)
        if not receipt:
            raise ValueError("execution receipt is empty")
        if receipt.get("search_status") != "executed":
            raise ValueError("answer checkpoint requires a verified search")
        actual_provider = receipt.get("provider")
        if not isinstance(actual_provider, str) or actual_provider not in {
            "openai",
            "minimax",
            "mimo",
        }:
            raise ValueError("execution receipt has no verified provider")
        actual_model = _safe_text(receipt.get("actual_model"), "actual_model", maximum=160)
        response_id = _safe_text(receipt.get("response_id"), "response_id", maximum=300)
        provider_attempt_id = _safe_text(
            receipt.get("attempt_id"), "provider_attempt_id", maximum=300
        )
        search_receipt_id = _safe_text(
            receipt.get("search_receipt_id"), "search_receipt_id", maximum=300
        )
        if receipt.get("response_status") != "completed":
            raise ValueError("execution receipt response is not complete")
        if type(receipt.get("http_status_code")) is not int or not (
            200 <= receipt["http_status_code"] < 300
        ):
            raise ValueError("execution receipt has no successful HTTP status")
        completed_at = _timestamp(receipt.get("completed_at"), "response completion timestamp")
        source_urls = _canonical_source_urls(receipt.get("source_urls"))
        request_id = receipt.get("request_id")
        if request_id is not None:
            request_id = _safe_text(request_id, "request_id", maximum=300)
        receipt_sha256 = quick_scan_receipt_sha256(execution_receipt)
        now = self._now()
        with self._transaction() as connection:
            item = self._item(connection, work_item_id)
            existing = connection.execute(
                "SELECT * FROM answer_checkpoint WHERE work_item_id=?", (work_item_id,)
            ).fetchone()
            if existing is not None:
                saved = self._checkpoint_record(connection, existing)
                saved_provenance = saved["payload"]["provenance"]
                if (
                    saved["attempt_id"] == attempt_id
                    and saved["payload"]["answer"] == normalized_answer
                    and saved_provenance.get("receipt_sha256") == receipt_sha256
                ):
                    return saved
                raise WorkConflictError("answer checkpoint is immutable")
            self._assert_lease(item, lease, now)
            if item["identity_state"] != "verified":
                raise WorkConflictError("answer checkpoint requires verified identity")
            if entity_id != item["entity_id"] or question_id != item["question_id"]:
                raise WorkConflictError("answer entity/question does not match work item")
            attempt = connection.execute(
                "SELECT * FROM attempt WHERE attempt_id=? AND work_item_id=?",
                (attempt_id, work_item_id),
            ).fetchone()
            if (
                attempt is None
                or attempt["phase"] != "response_available"
                or attempt["lease_epoch"] != lease.lease_epoch
                or attempt["lease_token"] != lease.lease_token
                or attempt["http_status_code"] != receipt["http_status_code"]
                or attempt["receipt_sha256"] != receipt_sha256
                or attempt["request_id"] != request_id
                or attempt["model_requested"] != actual_model
            ):
                raise WorkConflictError("execution receipt does not match successful attempt")
            work = {
                key: item[key]
                for key in (
                    "entity_id",
                    "question_id",
                    "generation",
                    "scope",
                    "scope_id",
                    "identity_revision",
                    "source_binding_version",
                    "identity_state",
                    "source_binding_ref",
                    "source_binding_refs_json",
                    "identity_snapshot_sha256",
                    "question_fingerprint",
                    "routing_fingerprint",
                )
            }
            payload = {
                "checkpoint_schema": "quick-scan-answer",
                "checkpoint_schema_version": 1,
                "work_item_id": work_item_id,
                "attempt_id": attempt_id,
                "work": work,
                "answer": normalized_answer,
                "provenance": {
                    "attempt_lease_epoch": attempt["lease_epoch"],
                    "route_id": attempt["route_id"],
                    "route_provider": attempt["provider"],
                    "actual_provider": actual_provider,
                    "model_requested": attempt["model_requested"],
                    "actual_model": actual_model,
                    "work_prompt_sha256": attempt["prompt_sha256"],
                    "provider_prompt_sha256": receipt.get("prompt_sha256"),
                    "request_id": request_id,
                    "provider_attempt_id": provider_attempt_id,
                    "response_id": response_id,
                    "search_receipt_id": search_receipt_id,
                    "response_status": "completed",
                    "http_status_code": receipt["http_status_code"],
                    "search_status": "executed",
                    "source_urls": source_urls,
                    "response_completed_at": completed_at,
                    "receipt_sha256": receipt_sha256,
                    "answer_contract": "validated-normalized-v1",
                },
                "checkpointed_at": now,
            }
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            payload_sha256 = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            checkpoint_id = "CHECKPOINT_" + uuid.uuid4().hex
            connection.execute(
                "INSERT INTO answer_checkpoint (checkpoint_id,work_item_id,attempt_id,"
                "checkpoint_schema_version,payload_json,payload_sha256,checkpointed_at) "
                "VALUES (?,?,?,1,?,?,?)",
                (checkpoint_id, work_item_id, attempt_id, encoded, payload_sha256, now),
            )
            changed = connection.execute(
                "UPDATE work_item SET status='result_ready',lease_token=NULL,"
                "lease_expires_at=NULL,uncertain_attempt_id=NULL,updated_at=? "
                "WHERE work_item_id=? AND status='leased' AND lease_epoch=? AND lease_token=?",
                (now, work_item_id, lease.lease_epoch, lease.lease_token),
            ).rowcount
            if changed != 1:
                raise LeaseFencedError("quick-scan work lease changed before checkpoint commit")
            self._event(
                connection,
                work_item_id,
                "answer_checkpointed",
                lease.lease_epoch,
                now,
                attempt_id=attempt_id,
                old="leased",
                new="result_ready",
            )
            row = connection.execute(
                "SELECT c.*,w.*,a.attempt_id AS bound_attempt_id "
                "FROM answer_checkpoint c JOIN work_item w ON w.work_item_id=c.work_item_id "
                "JOIN attempt a ON a.attempt_id=c.attempt_id WHERE c.work_item_id=?",
                (work_item_id,),
            ).fetchone()
            return self._checkpoint_record(connection, row)

    def get_answer_checkpoint(self, work_item_id: str) -> Optional[dict]:
        _safe(work_item_id, "work_item_id")
        with closing(self._connect()) as connection:
            self._item(connection, work_item_id)
            row = connection.execute(
                "SELECT c.*,w.*,a.attempt_id AS bound_attempt_id "
                "FROM answer_checkpoint c JOIN work_item w ON w.work_item_id=c.work_item_id "
                "JOIN attempt a ON a.attempt_id=c.attempt_id WHERE c.work_item_id=?",
                (work_item_id,),
            ).fetchone()
            return None if row is None else self._checkpoint_record(connection, row)

    @staticmethod
    def _result_delivery_record(row: sqlite3.Row) -> dict:
        from .quick_scan_result_outbox import (
            canonical_bytes,
            canonical_sha256,
            delivery_key,
            validate_exchange_package,
            validate_import_ack,
        )

        result = dict(row)
        if result["package_json"] is not None:
            try:
                package = json.loads(result["package_json"])
                canonical = canonical_bytes(package)
                validated = validate_exchange_package(package)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError("quick-scan delivery package is invalid") from error
            item = validated["items"][0]
            if (
                canonical.decode("utf-8") != result["package_json"]
                or hashlib.sha256(canonical).hexdigest() != result["package_bytes_sha256"]
                or validated["package_sha256"] != result["package_sha256"]
                or validated["package_id"] != result["package_id"]
                or item["item_id"] != result["item_id"]
                or item["observation_id"] != result["observation_id"]
                or item["payload_sha256"] != result["payload_sha256"]
                or delivery_key(result["package_id"], result["item_id"], result["payload_sha256"])
                != result["delivery_key"]
            ):
                raise ValueError("quick-scan delivery package binding is corrupt")
            result["package"] = validated
            result["package_bytes"] = canonical
        else:
            result["package"] = None
            result["package_bytes"] = None
        if result["state"] == "blocked":
            from .quick_scan_result_outbox import _SAFE_TOKEN

            if not isinstance(result["block_code"], str) or not _SAFE_TOKEN.fullmatch(
                result["block_code"]
            ):
                raise ValueError("quick-scan delivery block reason is invalid")
        if result["ack_json"] is not None:
            try:
                ack = json.loads(result["ack_json"])
                if canonical_bytes(ack).decode("utf-8") != result["ack_json"]:
                    raise ValueError("ACK serialization is not canonical")
                if canonical_sha256(ack) != result["ack_sha256"]:
                    raise ValueError("ACK hash mismatch")
                validate_import_ack(ack, result)
                expected_state = (
                    "delivered"
                    if ack["status"] in {"accepted", "already_present"}
                    else ack["status"]
                )
                if (
                    expected_state != result["state"]
                    or ack["consumer"]["store_id"] != result["consumer_store_id"]
                ):
                    raise ValueError("ACK outcome does not match delivery terminal state")
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError("quick-scan delivery ACK is invalid") from error
            result["ack"] = ack
        else:
            result["ack"] = None
        return result

    @staticmethod
    def _delivery_event(
        connection: sqlite3.Connection,
        delivery_id: str,
        event_type: str,
        *,
        old: Optional[str],
        new: str,
        now: float,
        reason_code: Optional[str] = None,
        ack_sha256: Optional[str] = None,
    ) -> None:
        connection.execute(
            "INSERT INTO quick_scan_result_delivery_event "
            "(delivery_id,event_type,from_state,to_state,reason_code,ack_sha256,occurred_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (delivery_id, event_type, old, new, reason_code, ack_sha256, now),
        )

    @staticmethod
    def _delivery_row(connection: sqlite3.Connection, work_item_id: str) -> Optional[sqlite3.Row]:
        row: Optional[sqlite3.Row] = connection.execute(
            "SELECT * FROM quick_scan_result_delivery WHERE work_item_id=?",
            (work_item_id,),
        ).fetchone()
        return row

    def list_result_ready_without_delivery(self, *, limit: int = 100) -> list[str]:
        """Return checkpointed results that still need an adapter or package."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("delivery candidate limit must be from 1 to 1000")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT w.work_item_id FROM work_item w "
                "JOIN answer_checkpoint c USING (work_item_id) "
                "LEFT JOIN quick_scan_result_delivery d USING (work_item_id) "
                "WHERE w.status='result_ready' AND d.work_item_id IS NULL "
                "ORDER BY w.updated_at,w.work_item_id LIMIT ?",
                (limit,),
            ).fetchall()
            return [row["work_item_id"] for row in rows]

    def mark_result_delivery_blocked(self, work_item_id: str, reason_code: str) -> dict:
        """Durably record why a completed answer cannot yet form a C06 package."""
        from .quick_scan_result_outbox import _SAFE_TOKEN

        _safe(work_item_id, "work_item_id")
        reason_code = _safe_text(reason_code, "delivery block reason", maximum=160)
        if not _SAFE_TOKEN.fullmatch(reason_code):
            raise ValueError("invalid delivery block reason")
        now = self._now()
        with self._transaction() as connection:
            item = self._item(connection, work_item_id)
            if item["status"] != "result_ready":
                raise WorkConflictError("delivery can only be blocked for a result-ready item")
            checkpoint = connection.execute(
                "SELECT 1 FROM answer_checkpoint WHERE work_item_id=?", (work_item_id,)
            ).fetchone()
            if checkpoint is None:
                raise WorkConflictError("result-ready work has no answer checkpoint")
            current = self._delivery_row(connection, work_item_id)
            if current is None:
                delivery_id = "DELIVERY_" + uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO quick_scan_result_delivery "
                    "(delivery_id,work_item_id,state,block_code,created_at,updated_at) "
                    "VALUES (?,?,'blocked',?,?,?)",
                    (delivery_id, work_item_id, reason_code, now, now),
                )
                old_state = None
                event_type = "blocked"
            elif current["state"] == "blocked":
                if current["block_code"] == reason_code:
                    return self._result_delivery_record(current)
                connection.execute(
                    "UPDATE quick_scan_result_delivery SET block_code=?,updated_at=? "
                    "WHERE delivery_id=? AND state='blocked'",
                    (reason_code, now, current["delivery_id"]),
                )
                delivery_id = current["delivery_id"]
                old_state = "blocked"
                event_type = "blocked"
            else:
                raise WorkConflictError("a prepared delivery cannot be replaced by a block")
            self._delivery_event(
                connection,
                delivery_id,
                event_type,
                old=old_state,
                new="blocked",
                now=now,
                reason_code=reason_code,
            )
            row = connection.execute(
                "SELECT * FROM quick_scan_result_delivery WHERE delivery_id=?", (delivery_id,)
            ).fetchone()
            return self._result_delivery_record(row)

    def prepare_result_delivery(self, work_item_id: str, package: dict) -> dict:
        """Seal a complete one-item package supplied by a verified C06 adapter."""
        from .quick_scan_result_outbox import (
            canonical_bytes,
            delivery_key,
            validate_checkpoint_binding,
            validate_exchange_package,
        )

        _safe(work_item_id, "work_item_id")
        validated = validate_exchange_package(package)
        item_data = validated["items"][0]
        package_bytes = canonical_bytes(validated)
        package_json = package_bytes.decode("utf-8")
        package_bytes_sha256 = hashlib.sha256(package_bytes).hexdigest()
        item_id = item_data["item_id"]
        observation_id = item_data["observation_id"]
        payload_sha256 = item_data["payload_sha256"]
        idempotency_key = delivery_key(validated["package_id"], item_id, payload_sha256)
        now = self._now()
        with self._transaction() as connection:
            item = self._item(connection, work_item_id)
            if item["status"] != "result_ready":
                raise WorkConflictError("only result-ready work can be exported")
            checkpoint_row = connection.execute(
                "SELECT c.*,w.*,a.attempt_id AS bound_attempt_id "
                "FROM answer_checkpoint c JOIN work_item w USING (work_item_id) "
                "JOIN attempt a ON a.attempt_id=c.attempt_id WHERE c.work_item_id=?",
                (work_item_id,),
            ).fetchone()
            if checkpoint_row is None:
                raise WorkConflictError("result-ready work has no answer checkpoint")
            checkpoint = self._checkpoint_record(connection, checkpoint_row, validate_status=True)
            validate_checkpoint_binding(validated, checkpoint)
            current = self._delivery_row(connection, work_item_id)
            if current is not None and current["package_json"] is not None:
                existing = self._result_delivery_record(current)
                if existing["package_bytes"] != package_bytes:
                    raise WorkConflictError("quick-scan delivery package is immutable")
                return existing
            if current is None:
                delivery_id = "DELIVERY_" + uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO quick_scan_result_delivery "
                    "(delivery_id,work_item_id,state,package_json,package_sha256,"
                    "package_bytes_sha256,package_id,item_id,observation_id,payload_sha256,"
                    "delivery_key,created_at,updated_at) "
                    "VALUES (?,?,'ready',?,?,?,?,?,?,?,?,?,?)",
                    (
                        delivery_id,
                        work_item_id,
                        package_json,
                        validated["package_sha256"],
                        package_bytes_sha256,
                        validated["package_id"],
                        item_id,
                        observation_id,
                        payload_sha256,
                        idempotency_key,
                        now,
                        now,
                    ),
                )
                old_state = None
            elif current["state"] == "blocked":
                delivery_id = current["delivery_id"]
                old_state = "blocked"
                connection.execute(
                    "UPDATE quick_scan_result_delivery SET state='ready',block_code=NULL,"
                    "package_json=?,package_sha256=?,package_bytes_sha256=?,package_id=?,"
                    "item_id=?,observation_id=?,payload_sha256=?,delivery_key=?,updated_at=? "
                    "WHERE delivery_id=? AND state='blocked'",
                    (
                        package_json,
                        validated["package_sha256"],
                        package_bytes_sha256,
                        validated["package_id"],
                        item_id,
                        observation_id,
                        payload_sha256,
                        idempotency_key,
                        now,
                        delivery_id,
                    ),
                )
            else:
                raise WorkConflictError("quick-scan delivery state cannot accept a new package")
            self._delivery_event(
                connection,
                delivery_id,
                "package_prepared",
                old=old_state,
                new="ready",
                now=now,
            )
            row = connection.execute(
                "SELECT * FROM quick_scan_result_delivery WHERE delivery_id=?", (delivery_id,)
            ).fetchone()
            return self._result_delivery_record(row)

    def get_result_delivery(self, work_item_id: str) -> Optional[dict]:
        _safe(work_item_id, "work_item_id")
        with closing(self._connect()) as connection:
            row = self._delivery_row(connection, work_item_id)
            return None if row is None else self._result_delivery_record(row)

    def list_result_deliveries(
        self, *, states: Optional[Sequence[str]] = None, limit: int = 100
    ) -> list[dict]:
        """List durable deliveries without choosing retry behavior for the caller."""
        allowed = {"blocked", "ready", "send_uncertain", "delivered", "rejected", "conflict"}
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("delivery limit must be from 1 to 1000")
        if states is not None:
            if isinstance(states, (str, bytes)) or not isinstance(states, (list, tuple)):
                raise ValueError("delivery states must be a sequence")
            if not states or any(
                not isinstance(state, str) or state not in allowed for state in states
            ):
                raise ValueError("invalid delivery state filter")
            if len(set(states)) != len(states):
                raise ValueError("duplicate delivery state filter")
        with closing(self._connect()) as connection:
            if states is None:
                rows = connection.execute(
                    "SELECT * FROM quick_scan_result_delivery "
                    "ORDER BY updated_at,delivery_id LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                placeholders = ",".join("?" for _ in states)
                rows = connection.execute(
                    "SELECT * FROM quick_scan_result_delivery WHERE state IN ("  # nosec B608 -- only literal "?" placeholders are joined; all values bound as parameters
                    + placeholders
                    + ") ORDER BY updated_at,delivery_id LIMIT ?",
                    (*states, limit),
                ).fetchall()
            return [self._result_delivery_record(row) for row in rows]

    def begin_result_delivery(self, work_item_id: str) -> dict:
        """Commit send intent before returning the exact immutable bytes once."""
        _safe(work_item_id, "work_item_id")
        now = self._now()
        with self._transaction() as connection:
            row = self._delivery_row(connection, work_item_id)
            if row is None or row["state"] != "ready":
                raise WorkConflictError("delivery is not safely dispatchable")
            item = self._item(connection, work_item_id)
            if item["status"] != "result_ready":
                raise WorkConflictError("delivery work item is not result-ready")
            changed = connection.execute(
                "UPDATE quick_scan_result_delivery SET state='send_uncertain',updated_at=? "
                "WHERE delivery_id=? AND state='ready'",
                (now, row["delivery_id"]),
            ).rowcount
            if changed != 1:
                raise WorkConflictError("delivery state changed before send intent")
            self._delivery_event(
                connection,
                row["delivery_id"],
                "send_intent",
                old="ready",
                new="send_uncertain",
                now=now,
            )
            result = self._result_delivery_record(
                connection.execute(
                    "SELECT * FROM quick_scan_result_delivery WHERE delivery_id=?",
                    (row["delivery_id"],),
                ).fetchone()
            )
            result["request_bytes"] = result["package_bytes"]
            result["idempotency_key"] = result["delivery_key"]
            return result

    def confirm_result_delivery_not_sent(self, work_item_id: str, reason_code: str) -> dict:
        """Re-arm only when the transport proves no request bytes were dispatched."""
        _safe(work_item_id, "work_item_id")
        reason_code = _safe_text(reason_code, "not-sent reason", maximum=160)
        if reason_code not in _DELIVERY_NOT_SENT_REASONS:
            raise ValueError("invalid not-sent reason")
        now = self._now()
        with self._transaction() as connection:
            row = self._delivery_row(connection, work_item_id)
            if row is None or row["state"] != "send_uncertain":
                raise WorkConflictError("only an uncertain delivery can be confirmed not sent")
            connection.execute(
                "UPDATE quick_scan_result_delivery SET state='ready',updated_at=? "
                "WHERE delivery_id=? AND state='send_uncertain'",
                (now, row["delivery_id"]),
            )
            self._delivery_event(
                connection,
                row["delivery_id"],
                "confirmed_not_sent",
                old="send_uncertain",
                new="ready",
                now=now,
                reason_code=reason_code,
            )
            return self._result_delivery_record(
                connection.execute(
                    "SELECT * FROM quick_scan_result_delivery WHERE delivery_id=?",
                    (row["delivery_id"],),
                ).fetchone()
            )

    def apply_result_delivery_ack(self, work_item_id: str, ack: dict) -> dict:
        """Persist an exact C06 ACK and mark work delivered in the same transaction."""
        from .quick_scan_result_outbox import (
            canonical_bytes,
            canonical_sha256,
            validate_import_ack,
        )

        _safe(work_item_id, "work_item_id")
        ack_bytes = canonical_bytes(ack)
        ack_json = ack_bytes.decode("utf-8")
        ack_sha256 = canonical_sha256(ack)
        now = self._now()
        with self._transaction() as connection:
            row = self._delivery_row(connection, work_item_id)
            if row is None or row["package_json"] is None:
                raise WorkConflictError("result delivery package is not prepared")
            if row["state"] in {"delivered", "rejected", "conflict"}:
                current = self._result_delivery_record(row)
                if current["ack_json"] == ack_json:
                    return current
                raise WorkConflictError("terminal result delivery ACK is immutable")
            validate_import_ack(ack, dict(row))
            if row["state"] not in {"ready", "send_uncertain"}:
                raise WorkConflictError("result delivery cannot accept an ACK in this state")
            final_state = {
                "accepted": "delivered",
                "already_present": "delivered",
                "rejected": "rejected",
                "conflict": "conflict",
            }[ack["status"]]
            changed = connection.execute(
                "UPDATE quick_scan_result_delivery SET state=?,ack_json=?,ack_sha256=?,"
                "consumer_store_id=?,updated_at=? WHERE delivery_id=? AND state=?",
                (
                    final_state,
                    ack_json,
                    ack_sha256,
                    ack["consumer"]["store_id"],
                    now,
                    row["delivery_id"],
                    row["state"],
                ),
            ).rowcount
            if changed != 1:
                raise WorkConflictError("result delivery state changed before ACK commit")
            if final_state == "delivered":
                item = self._item(connection, work_item_id)
                if item["status"] != "result_ready":
                    raise WorkConflictError("ACK work item is not result-ready")
                changed = connection.execute(
                    "UPDATE work_item SET status='delivered',updated_at=? "
                    "WHERE work_item_id=? AND status='result_ready'",
                    (now, work_item_id),
                ).rowcount
                if changed != 1:
                    raise WorkConflictError("ACK work item changed before delivery commit")
                self._event(
                    connection,
                    work_item_id,
                    "result_delivered",
                    item["lease_epoch"],
                    now,
                    old="result_ready",
                    new="delivered",
                )
            self._delivery_event(
                connection,
                row["delivery_id"],
                ack["status"],
                old=row["state"],
                new=final_state,
                now=now,
                ack_sha256=ack_sha256,
            )
            return self._result_delivery_record(
                connection.execute(
                    "SELECT * FROM quick_scan_result_delivery WHERE delivery_id=?",
                    (row["delivery_id"],),
                ).fetchone()
            )

    def list_result_delivery_events(self, work_item_id: str) -> list[dict]:
        _safe(work_item_id, "work_item_id")
        with closing(self._connect()) as connection:
            row = self._delivery_row(connection, work_item_id)
            if row is None:
                return []
            return [
                dict(event)
                for event in connection.execute(
                    "SELECT * FROM quick_scan_result_delivery_event "
                    "WHERE delivery_id=? ORDER BY event_id",
                    (row["delivery_id"],),
                )
            ]

    def list_run_items(self, run_id: str, scan_id: str) -> list[dict]:
        """Rebuild a scan by separating checkpointed questions from pending work."""
        _safe(run_id, "run_id")
        _safe(scan_id, "scan_id")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT w.* FROM work_item w JOIN work_run_ref r "
                "ON r.work_item_id=w.work_item_id WHERE r.run_id=? AND r.scan_id=? "
                "ORDER BY w.question_id,w.work_item_id",
                (run_id, scan_id),
            ).fetchall()
            result = []
            for item in rows:
                entry = dict(item)
                checkpoint = connection.execute(
                    "SELECT c.*,w.*,a.attempt_id AS bound_attempt_id "
                    "FROM answer_checkpoint c JOIN work_item w ON w.work_item_id=c.work_item_id "
                    "JOIN attempt a ON a.attempt_id=c.attempt_id WHERE c.work_item_id=?",
                    (item["work_item_id"],),
                ).fetchone()
                entry["checkpoint"] = (
                    None if checkpoint is None else self._checkpoint_record(connection, checkpoint)
                )
                result.append(entry)
            return result

    def cancel_pending(self, work_item_id: str) -> str:
        """Cancel work that has not started; active requests remain leased and finish."""
        _safe(work_item_id, "work_item_id")
        now = self._now()
        with self._transaction() as connection:
            item = self._item(connection, work_item_id)
            if item["status"] == "cancelled":
                return "cancelled"
            if item["status"] != "pending":
                raise WorkConflictError("only pending work can be cancelled")
            changed = connection.execute(
                "UPDATE work_item SET status='cancelled',updated_at=? "
                "WHERE work_item_id=? AND status='pending'",
                (now, work_item_id),
            ).rowcount
            if changed != 1:
                raise WorkConflictError("pending cancellation lost race")
            self._event(
                connection,
                work_item_id,
                "cancelled",
                item["lease_epoch"],
                now,
                old="pending",
                new="cancelled",
            )
            return "cancelled"

    def get_item(self, work_item_id: str) -> dict:
        with closing(self._connect()) as connection:
            return dict(self._item(connection, work_item_id))

    def list_run_refs(self, work_item_id: str) -> list[dict]:
        with closing(self._connect()) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM work_run_ref WHERE work_item_id=? ORDER BY run_id,scan_id",
                    (work_item_id,),
                )
            ]

    def list_attempts(self, work_item_id: str) -> list[dict]:
        with closing(self._connect()) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM attempt WHERE work_item_id=? ORDER BY ordinal",
                    (work_item_id,),
                )
            ]

    def list_events(self, work_item_id: str) -> list[dict]:
        with closing(self._connect()) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM work_event WHERE work_item_id=? ORDER BY event_id",
                    (work_item_id,),
                )
            ]
