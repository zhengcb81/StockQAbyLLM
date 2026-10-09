"""Import the versioned quick-scan search policy and admit external routes.

The IQS ``search-and-llm-policy.template.json`` is a fillable reference with
``template_only``/``execution_enabled`` markers — importing it as an executable
policy is refused. A route is admitted only when it is enabled, its credential
environment variable is named, its cost bound is verified with an explicit
pricing record, and its storage rights are confirmed; anything else is a
bounded, auditable refusal and the route sends ZERO requests.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any

from jsonschema import Draft7Validator

from src.providers.external_search_parsers import strict_search_json

__all__ = [
    "POLICY_SCHEMA_ID",
    "SearchPolicy",
    "SearchPolicyRejected",
    "load_search_policy",
    "policy_receipt",
    "execution_query_plans",
    "assert_policy_frozen",
]

POLICY_SCHEMA_ID = "stockqa.quick_scan_search_policy/1.0.0"
EXECUTION_POLICY_SCHEMA_ID = "stockqa.quick_scan_search_policy/1.1.0"
_SCHEMA_PATH = Path(__file__).resolve().parent / "quick_scan_search_policy.schema.json"
_EXECUTION_SCHEMA_PATH = _SCHEMA_PATH.with_name("quick_scan_search_policy_v1_1.schema.json")
EXTERNAL_ENDPOINTS = MappingProxyType(
    {
        "brave": "https://api.search.brave.com/res/v1/web/search",
        "tavily": "https://api.tavily.com/search",
        "zai_rest": "https://api.z.ai/api/paas/v4/web_search",
        "zai_mcp_streamable": "https://api.z.ai/api/mcp/web_search_prime/mcp",
    }
)
_SHARED_PUBLISHER_HOSTS = frozenset(
    {
        "sec.gov",
        "hkexnews.hk",
        "hkex.com.hk",
        "cninfo.com.cn",
        "sse.com.cn",
        "szse.cn",
        "finance.yahoo.com",
        "reuters.com",
        "bloomberg.com",
    }
)


class SearchPolicyRejected(ValueError):
    """The document is not an executable StockQA search policy."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise SearchPolicyRejected(reason)


def _schema(version: str = "1.0.0") -> dict[str, Any]:
    path = _EXECUTION_SCHEMA_PATH if version == "1.1.0" else _SCHEMA_PATH
    document = strict_search_json(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SearchPolicyRejected("policy schema artifact is invalid")
    return document


@dataclass(frozen=True)
class SearchPolicy:
    policy_id: str
    schema_id: str
    policy_sha256: str
    mode: str
    external_search_priority: tuple[str, ...]
    routes: tuple[dict[str, Any], ...]
    retrieval: dict[str, Any]
    admitted: tuple[str, ...] = ()
    rejections: tuple[dict[str, str], ...] = field(default_factory=tuple)
    execution_plan: dict[str, Any] | None = None
    _integrity_sha256: str = field(default="", repr=False)

    @property
    def requires_external(self) -> bool:
        return self.mode in {"external_context", "explicit_hybrid"}


def _policy_digest(policy: SearchPolicy) -> str:
    value = {
        key: getattr(policy, key)
        for key in (
            "policy_id",
            "schema_id",
            "policy_sha256",
            "mode",
            "external_search_priority",
            "routes",
            "retrieval",
            "admitted",
            "rejections",
            "execution_plan",
        )
    }
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def assert_policy_frozen(policy: SearchPolicy) -> None:
    """Reject mutable nested configuration changes after admission.

    Legacy manually constructed 1.0 records remain readable; executable 1.1
    policies always require the loader's integrity record.
    """
    if not isinstance(policy, SearchPolicy):
        raise SearchPolicyRejected("policy_not_frozen")
    if policy.schema_id == EXECUTION_POLICY_SCHEMA_ID and not policy._integrity_sha256:
        raise SearchPolicyRejected("policy_not_frozen")
    if policy._integrity_sha256:
        try:
            matches = _policy_digest(policy) == policy._integrity_sha256
        except (TypeError, ValueError):
            matches = False
        _require(matches, "policy_no_longer_matches_frozen_admission")


def validate_external_domain_bindings(bindings: Any, identity_sha256: str) -> None:
    """Validate explicit operator attestations; this is not an owner golden.

    A shared publisher requires an issuer-specific canonical path. Queries and
    page titles never certify the target company. Percent-encoded or ambiguous
    path attestations are refused rather than being broadened during matching.
    """
    _require(isinstance(bindings, list) and 1 <= len(bindings) <= 20, "entity_bindings_invalid")
    seen: set[tuple[str, str]] = set()
    for binding in bindings:
        _require(
            isinstance(binding, dict)
            and set(binding)
            == {
                "host",
                "path_prefix",
                "binding_kind",
                "source_ref",
                "identity_snapshot_sha256",
            },
            "entity_binding_fields_invalid",
        )
        host, prefix = binding["host"], binding["path_prefix"]
        _require(
            isinstance(host, str)
            and len(host) <= 253
            and re.fullmatch(
                r"[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?)+", host
            )
            is not None,
            "entity_binding_host_invalid",
        )
        _require(
            isinstance(prefix, str)
            and 1 <= len(prefix) <= 1000
            and prefix.startswith("/")
            and not any(ord(c) < 33 or c in "\\%?#" for c in prefix)
            and "//" not in prefix
            and not any(part in {".", ".."} for part in prefix.split("/")),
            "entity_binding_path_invalid",
        )
        kind = binding["binding_kind"]
        _require(
            kind in {"issuer_owned_domain", "issuer_specific_path"}, "entity_binding_kind_invalid"
        )
        if kind == "issuer_owned_domain":
            _require(
                not any(
                    host == shared or host.endswith("." + shared)
                    for shared in _SHARED_PUBLISHER_HOSTS
                ),
                "shared_publisher_cannot_certify_one_issuer",
            )
        else:
            _require(prefix != "/", "issuer_specific_path_required")
        _require((host, prefix) not in seen, "duplicate_entity_domain_binding")
        seen.add((host, prefix))
        source = binding["source_ref"]
        _require(
            isinstance(source, str)
            and 1 <= len(source) <= 300
            and bool(source.strip())
            and not any(ord(c) < 32 for c in source),
            "entity_binding_source_invalid",
        )
        _require(
            binding["identity_snapshot_sha256"] == identity_sha256,
            "entity_binding_identity_mismatch",
        )


def _validate_execution_shape(document: dict[str, Any]) -> None:
    if next(Draft7Validator(_schema("1.1.0")).iter_errors(document), None) is not None:
        raise SearchPolicyRejected("execution_policy_schema_invalid")
    # Retain all established 1.0 checks, without changing its published schema.
    base = copy.deepcopy(document)
    base["schema_version"] = "1.0.0"
    base.pop("execution_plan")
    for route in base["external_routes"]:
        route.pop("dispatch")
        route.pop("metering")
    _validate_shape(base)
    plan = document["execution_plan"]
    cutoff = plan["information_as_of"]
    try:
        valid_cutoff = date.fromisoformat(cutoff).isoformat() == cutoff
    except ValueError:
        valid_cutoff = False
    _require(valid_cutoff, "execution_cutoff_invalid")
    expected_mode = {
        "external_context": "external_context_only",
        "explicit_hybrid": "native_with_external_context",
    }
    _require(
        plan["answer_search_mode"] == expected_mode.get(document["selection"]["mode"]),
        "execution_answer_search_mode_mismatch",
    )
    ids = [query["query_id"] for query in plan["queries"]]
    _require(len(ids) == len(set(ids)), "duplicate_execution_query_id")
    queries = [query["query"].strip() for query in plan["queries"]]
    _require(len(queries) == len(set(queries)), "duplicate_execution_query")
    priority = [item["route_id"] for item in document["selection"]["external_search_priority"]]
    _require(len(priority) == len(set(priority)), "duplicate_external_priority")
    for query in plan["queries"]:
        _require(
            query["query"].strip() and not any(ord(c) < 32 for c in query["query"]),
            "execution_query_invalid",
        )
    validate_external_domain_bindings(
        plan["entity_domain_bindings"], plan["identity_snapshot_sha256"]
    )


def execution_query_plans(policy: SearchPolicy) -> tuple[dict[str, Any], ...]:
    """Return isolated journal-ready queries from one admitted frozen plan."""
    assert_policy_frozen(policy)
    plan = policy.execution_plan
    if policy.schema_id != EXECUTION_POLICY_SCHEMA_ID or plan is None:
        raise SearchPolicyRejected("frozen_execution_plan_required")
    common = {
        key: copy.deepcopy(plan[key])
        for key in (
            "entity_id",
            "identity_snapshot_sha256",
            "question_manifest_sha256",
            "information_as_of",
            "locale",
            "entity_domain_bindings",
        )
    }
    common.update(
        schema_version="quick_scan_external_plan/1.1.0",
        ttl_seconds=policy.retrieval["search_ttl_seconds"],
        company_limit=policy.retrieval["max_company_evidence_unicode_characters"],
        snippet_limit=policy.retrieval["max_snippet_unicode_characters"],
    )
    return tuple({**copy.deepcopy(common), **copy.deepcopy(query)} for query in plan["queries"])


def _reject_route(route: dict[str, Any], reason: str) -> dict[str, str]:
    return {"route_id": route["route_id"], "reason": reason}


def _admit(
    route: dict[str, Any], *, require_storage_rights: bool, require_credentials: bool = True
) -> tuple[bool, str]:
    if route["enabled"] is not True:
        return False, "route_disabled"
    if require_credentials and not os.environ.get(route["credential_env"]):
        return False, "credential_env_unset"
    if route["cost_bound_verified"] is not True:
        return False, "cost_bound_unverified"
    if (
        route["kind"] == "zai_mcp_streamable"
        and route.get("metering", {}).get("charge_policy") != "all_http_requests"
    ):
        return False, "mcp_control_pricing_required"
    pricing = route["pricing"]
    if pricing["unit_cost_micros"] is None and pricing["per_request_cap_micros"] is None:
        return False, "pricing_unknown"
    if pricing["basis"] == "quota_group" and not pricing["quota_group"]:
        return False, "quota_group_missing"
    if require_storage_rights:
        storage = route["storage_rights"]
        if (
            storage["confirmed"] is not True
            or not isinstance(storage["entitlement_ref"], str)
            or not storage["entitlement_ref"].strip()
        ):
            return False, "storage_rights_unconfirmed"
    if "metering" in route:
        if route["endpoint"] != EXTERNAL_ENDPOINTS.get(route["kind"]):
            return False, "external_endpoint_protocol_mismatch"
        metering = route["metering"]
        for key in ("pricing_ref", "source_ref"):
            value = metering[key]
            if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
                return False, "pricing_reference_unverified"
        try:
            if (
                date.fromisoformat(metering["source_checked_at"]).isoformat()
                != metering["source_checked_at"]
            ):
                return False, "pricing_reference_unverified"
        except ValueError:
            return False, "pricing_reference_unverified"
        unit, cap = pricing["unit_cost_micros"], pricing["per_request_cap_micros"]
        units = metering["max_usage_units_per_request"]
        rejected = metering["rejected_request_cost_micros"]
        if (
            type(unit) is not int
            or type(cap) is not int
            or type(units) is not int
            or min(unit, cap) < 0
            or units < 1
            or unit * units > cap
            or (rejected is not None and (type(rejected) is not int or not 0 <= rejected <= cap))
        ):
            return False, "pricing_bound_unverified"
        charge = metering["charge_policy"]
        if charge == "all_http_requests" and rejected is not None and rejected != unit:
            return False, "pricing_charge_policy_conflict"
        expected = {
            "all_http_requests": ("per_request", "http_requests"),
            "successful_search_only": ("per_search", "search_calls"),
            "provider_usage": ("per_search", "credits"),
        }
        if (pricing["basis"], metering["usage_unit"]) != expected.get(charge):
            return False, "pricing_usage_basis_mismatch"
        if charge != "provider_usage" and units != 1:
            return False, "pricing_usage_basis_mismatch"
        if charge == "provider_usage" and route["kind"] != "tavily":
            return False, "provider_usage_protocol_unverified"
        if route["dispatch"]["max_in_flight"] > route["dispatch"]["quota_group_max_in_flight"]:
            return False, "external_concurrency_bound_invalid"
    return True, "admitted"


def _validate_shape(document: dict[str, Any]) -> None:
    """Stable legacy checks followed by complete versioned schema validation."""
    if document.get("schema_version") == "1.1.0":
        _validate_execution_shape(document)
        return
    required = {
        "schema_version",
        "policy_id",
        "selection",
        "external_routes",
        "retrieval",
        "budget",
    }
    _require(required <= set(document), "policy_field_set")
    _require(set(document) <= required | {"source_reference"}, "policy_field_set")
    _require(document["schema_version"] == "1.0.0", "policy_schema_version")
    policy_id = document["policy_id"]
    _require(isinstance(policy_id, str) and 1 <= len(policy_id) <= 120, "policy_id_invalid")

    selection = document["selection"]
    _require(isinstance(selection, dict), "selection_invalid")
    _require(
        selection.get("mode") in {"native_only", "external_context", "explicit_hybrid"},
        "selection_mode_invalid",
    )
    priority = selection.get("external_search_priority")
    _require(isinstance(priority, list) and len(priority) <= 8, "selection_priority_invalid")
    for entry in priority:
        _require(
            isinstance(entry, dict)
            and set(entry) == {"route_id"}
            and isinstance(entry.get("route_id"), str)
            and bool(entry["route_id"]),
            "selection_priority_invalid",
        )
    if "answer_model_priority_source" in selection:
        _require(
            selection["answer_model_priority_source"] == "existing_stockqa_model_policy",
            "answer_model_priority_source_invalid",
        )
    if "hybrid_plan_reference" in selection:
        _require(
            selection["hybrid_plan_reference"] is None
            or isinstance(selection["hybrid_plan_reference"], str),
            "hybrid_plan_reference_invalid",
        )

    routes = document["external_routes"]
    _require(isinstance(routes, list) and len(routes) <= 8, "external_routes_invalid")
    route_fields = {
        "route_id",
        "kind",
        "endpoint",
        "credential_env",
        "enabled",
        "cost_bound_verified",
        "pricing",
        "storage_rights",
    }
    seen_route_ids: set[str] = set()
    for route in routes:
        _require(
            isinstance(route, dict) and set(route) == route_fields,
            "external_route_invalid",
        )
        route_id = route["route_id"]
        _require(
            isinstance(route_id, str) and bool(route_id) and route_id not in seen_route_ids,
            "external_route_invalid",
        )
        seen_route_ids.add(route_id)
        _require(
            route["kind"] in {"brave", "tavily", "zai_rest", "zai_mcp_streamable"},
            "external_route_kind_invalid",
        )
        _require(
            isinstance(route["endpoint"], str) and route["endpoint"].startswith("https://"),
            "external_endpoint_invalid",
        )
        _require(
            isinstance(route["credential_env"], str)
            and re.fullmatch(r"[A-Z][A-Z0-9_]{1,60}", route["credential_env"]) is not None,
            "credential_env_invalid",
        )
        _require(
            type(route["enabled"]) is bool and type(route["cost_bound_verified"]) is bool,
            "external_route_invalid",
        )
        pricing = route["pricing"]
        _require(
            isinstance(pricing, dict)
            and set(pricing)
            == {
                "basis",
                "currency",
                "unit_cost_micros",
                "per_request_cap_micros",
                "quota_group",
            },
            "pricing_invalid",
        )
        _require(
            pricing["basis"] in {"per_request", "per_search", "quota_group"},
            "pricing_invalid",
        )
        _require(
            isinstance(pricing["currency"], str)
            and re.fullmatch(r"[A-Z]{3}", pricing["currency"]) is not None,
            "pricing_invalid",
        )
        for key in ("unit_cost_micros", "per_request_cap_micros"):
            _require(
                pricing[key] is None or (type(pricing[key]) is int and pricing[key] >= 0),
                "pricing_invalid",
            )
        _require(
            pricing["quota_group"] is None or isinstance(pricing["quota_group"], str),
            "pricing_invalid",
        )
        storage = route["storage_rights"]
        _require(
            isinstance(storage, dict)
            and set(storage) == {"confirmed", "entitlement_ref"}
            and type(storage["confirmed"]) is bool
            and (storage["entitlement_ref"] is None or isinstance(storage["entitlement_ref"], str)),
            "storage_rights_invalid",
        )

    retrieval = document["retrieval"]
    _require(isinstance(retrieval, dict), "retrieval_invalid")
    _require(
        type(retrieval.get("max_snippet_unicode_characters")) is int
        and 1 <= retrieval["max_snippet_unicode_characters"] <= 500,
        "snippet_cap_invalid",
    )
    _require(
        type(retrieval.get("max_company_evidence_unicode_characters")) is int
        and 1 <= retrieval["max_company_evidence_unicode_characters"] <= 30000,
        "company_cap_invalid",
    )
    _require(
        type(retrieval.get("max_json_unwrap_layers")) is int
        and 1 <= retrieval["max_json_unwrap_layers"] <= 3,
        "unwrap_layers_invalid",
    )
    _require(
        type(retrieval.get("max_in_memory_response_bytes")) is int
        and retrieval["max_in_memory_response_bytes"] >= 1024,
        "retrieval_invalid",
    )
    _require(
        retrieval.get("raw_response_persistence") is False,
        "raw_response_persistence_forbidden",
    )

    budget = document["budget"]
    _require(isinstance(budget, dict), "budget_invalid")
    _require(
        budget.get("source") == "existing_stockqa_shared_budget",
        "budget_source_invalid",
    )
    _require(
        budget.get("include_search_model_repair_probe_and_discovery_requests") is True,
        "budget_scope_invalid",
    )
    _require(
        budget.get("unknown_cost_action") == "hold_reservation_and_reconcile",
        "budget_unknown_cost_invalid",
    )
    _require(budget.get("reset_on_restart") is False, "budget_reset_invalid")
    # Preserve established reason codes above, then enforce the complete
    # published nested constraints, including unknown fields and TTL types.
    if next(Draft7Validator(_schema()).iter_errors(document), None) is not None:
        raise SearchPolicyRejected("policy_schema_invalid")


def load_search_policy(path: str | Path, *, allow_cached_recovery: bool = False) -> SearchPolicy:
    """Validate policy; the explicit 1.1 recovery path may defer credentials.

    Only credential availability is deferred. Enabled route, schema, identity,
    price and retention admission stay mandatory. The coordinator reads the
    actual owner cache first, and ExternalSearchProvider always rechecks
    credentials before health admission, budget reservation or HTTP. Legacy
    1.0 and ordinary callers retain their original credential admission.
    """
    _require(type(allow_cached_recovery) is bool, "cached_recovery_flag_invalid")
    resolved = Path(path)
    try:
        raw = resolved.read_bytes()
    except OSError as error:
        raise SearchPolicyRejected("policy_unreadable", error.__class__.__name__) from error
    try:
        document = strict_search_json(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise SearchPolicyRejected("policy_invalid_json", str(error)) from error
    if not isinstance(document, dict):
        raise SearchPolicyRejected("policy_not_an_object")
    # the IQS fillable template must never become an execution authorization
    if document.get("template_only") is True or document.get("execution_enabled") is False:
        raise SearchPolicyRejected("template_not_executable")
    if "template_only" in document or "execution_enabled" in document:
        raise SearchPolicyRejected("template_not_executable")

    _validate_shape(document)
    version = document["schema_version"]
    schema_id = EXECUTION_POLICY_SCHEMA_ID if version == "1.1.0" else POLICY_SCHEMA_ID
    if _schema(version).get("$id") != schema_id:
        raise SearchPolicyRejected("policy_schema_artifact_id_mismatch")

    selection = document["selection"]
    mode = selection["mode"]
    priority = tuple(entry["route_id"] for entry in selection["external_search_priority"])
    routes = tuple(document["external_routes"])
    known = {route["route_id"] for route in routes}
    unknown = [route_id for route_id in priority if route_id not in known]
    if unknown:
        raise SearchPolicyRejected("priority_references_unknown_route", ",".join(unknown))
    if mode != "native_only" and not priority:
        raise SearchPolicyRejected("external_mode_without_priority")
    if mode == "explicit_hybrid" and not selection.get("hybrid_plan_reference"):
        raise SearchPolicyRejected("hybrid_mode_without_frozen_plan")

    # storage rights are required whenever external evidence may be retained
    require_storage_rights = mode != "native_only"
    admitted: list[str] = []
    rejections: list[dict[str, str]] = []
    for route in routes:
        ok, reason = _admit(
            route,
            require_storage_rights=require_storage_rights,
            require_credentials=not (allow_cached_recovery and version == "1.1.0"),
        )
        if ok:
            admitted.append(route["route_id"])
        else:
            rejections.append(_reject_route(route, reason))
    admitted_in_priority = tuple(route_id for route_id in priority if route_id in admitted)
    if mode != "native_only" and not admitted_in_priority:
        rejections.append({"route_id": "*", "reason": "no_admitted_external_route"})

    policy = SearchPolicy(
        policy_id=document["policy_id"],
        schema_id=schema_id,
        policy_sha256=hashlib.sha256(raw).hexdigest(),
        mode=mode,
        external_search_priority=priority,
        routes=routes,
        retrieval=dict(document["retrieval"]),
        admitted=admitted_in_priority,
        rejections=tuple(rejections),
        execution_plan=copy.deepcopy(document.get("execution_plan")),
    )
    object.__setattr__(policy, "_integrity_sha256", _policy_digest(policy))
    return policy


def policy_receipt(
    policy: SearchPolicy, *, external_dispatch_enabled: bool = False
) -> dict[str, Any]:
    """Bounded public receipt printed by the CLI before any dispatch."""
    assert_policy_frozen(policy)
    if type(external_dispatch_enabled) is not bool:
        raise ValueError("external dispatch admission must be explicit boolean")
    if external_dispatch_enabled:
        execution_query_plans(policy)
        if not policy.requires_external or not policy.admitted:
            raise ValueError("no executable external retrieval binding")
    # Static recovery eligibility is not permission to send without a key.
    enabled = external_dispatch_enabled and any(
        route["route_id"] in policy.admitted and _admit(route, require_storage_rights=True)[0]
        for route in policy.routes
    )
    return {
        "schema": "stockqa.search_policy_admission/1.0.0",
        "schema_id": policy.schema_id,
        "policy_id": policy.policy_id,
        "policy_sha256": policy.policy_sha256,
        "mode": policy.mode,
        "admitted_routes": list(policy.admitted),
        "route_rejections": list(policy.rejections),
        "execution_plan_ready": policy.schema_id == EXECUTION_POLICY_SCHEMA_ID
        and policy.execution_plan is not None,
        "external_dispatch_enabled": enabled,
        "external_dispatch_reason": (
            None
            if enabled
            else (
                "credentials_unavailable_cache_only"
                if external_dispatch_enabled
                else (
                    "external_context_not_implemented"
                    if policy.requires_external and policy.admitted
                    else "no_admitted_external_route" if policy.requires_external else "native_mode"
                )
            )
        ),
    }
