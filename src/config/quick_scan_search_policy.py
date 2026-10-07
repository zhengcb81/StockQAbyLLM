"""Import the versioned quick-scan search policy and admit external routes.

The IQS ``search-and-llm-policy.template.json`` is a fillable reference with
``template_only``/``execution_enabled`` markers — importing it as an executable
policy is refused. A route is admitted only when it is enabled, its credential
environment variable is named, its cost bound is verified with an explicit
pricing record, and its storage rights are confirmed; anything else is a
bounded, auditable refusal and the route sends ZERO requests.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "POLICY_SCHEMA_ID",
    "SearchPolicy",
    "SearchPolicyRejected",
    "load_search_policy",
    "policy_receipt",
]

POLICY_SCHEMA_ID = "stockqa.quick_scan_search_policy/1.0.0"
_SCHEMA_PATH = Path(__file__).resolve().parent / "quick_scan_search_policy.schema.json"


class SearchPolicyRejected(ValueError):
    """The document is not an executable StockQA search policy."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise SearchPolicyRejected(reason)


def _schema() -> dict[str, Any]:
    document = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
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

    @property
    def requires_external(self) -> bool:
        return self.mode in {"external_context", "explicit_hybrid"}


def _reject_route(route: dict[str, Any], reason: str) -> dict[str, str]:
    return {"route_id": route["route_id"], "reason": reason}


def _admit(route: dict[str, Any], *, require_storage_rights: bool) -> tuple[bool, str]:
    if route["enabled"] is not True:
        return False, "route_disabled"
    if not os.environ.get(route["credential_env"]):
        return False, "credential_env_unset"
    if route["cost_bound_verified"] is not True:
        return False, "cost_bound_unverified"
    pricing = route["pricing"]
    if pricing["unit_cost_micros"] is None and pricing["per_request_cap_micros"] is None:
        return False, "pricing_unknown"
    if pricing["basis"] == "quota_group" and not pricing["quota_group"]:
        return False, "quota_group_missing"
    if require_storage_rights and route["storage_rights"]["confirmed"] is not True:
        return False, "storage_rights_unconfirmed"
    return True, "admitted"


def _validate_shape(document: dict[str, Any]) -> None:
    """Hand-rolled check mirroring quick_scan_search_policy.schema.json.

    StockQA has no JSON Schema runtime dependency, so the loader enforces the
    published constraints directly — including the caps that bound spending.
    """
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


def load_search_policy(path: str | Path) -> SearchPolicy:
    """Validate one policy document and return its route admission result."""
    resolved = Path(path)
    try:
        raw = resolved.read_bytes()
    except OSError as error:
        raise SearchPolicyRejected("policy_unreadable", error.__class__.__name__) from error
    try:
        document = json.loads(raw.decode("utf-8"))
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
    if _schema().get("$id") != POLICY_SCHEMA_ID:
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
        ok, reason = _admit(route, require_storage_rights=require_storage_rights)
        if ok:
            admitted.append(route["route_id"])
        else:
            rejections.append(_reject_route(route, reason))
    admitted_in_priority = tuple(route_id for route_id in priority if route_id in admitted)
    if mode != "native_only" and not admitted_in_priority:
        rejections.append({"route_id": "*", "reason": "no_admitted_external_route"})

    return SearchPolicy(
        policy_id=document["policy_id"],
        schema_id=POLICY_SCHEMA_ID,
        policy_sha256=hashlib.sha256(raw).hexdigest(),
        mode=mode,
        external_search_priority=priority,
        routes=routes,
        retrieval=dict(document["retrieval"]),
        admitted=admitted_in_priority,
        rejections=tuple(rejections),
    )


def policy_receipt(policy: SearchPolicy) -> dict[str, Any]:
    """Bounded public receipt printed by the CLI before any dispatch."""
    return {
        "schema": "stockqa.search_policy_admission/1.0.0",
        "schema_id": policy.schema_id,
        "policy_id": policy.policy_id,
        "policy_sha256": policy.policy_sha256,
        "mode": policy.mode,
        "admitted_routes": list(policy.admitted),
        "route_rejections": list(policy.rejections),
        "external_dispatch_enabled": False,
        "external_dispatch_reason": (
            "external_context_not_implemented"
            if policy.requires_external and policy.admitted
            else "no_admitted_external_route" if policy.requires_external else "native_mode"
        ),
    }
