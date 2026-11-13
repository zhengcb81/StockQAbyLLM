"""Versioned public API for owner refresh capabilities and detached snapshots."""

from __future__ import annotations

from importlib import import_module
from types import MappingProxyType

from src.interfaces.quick_scan_owner_refresh import (
    QUICK_SCAN_OWNER_REFRESH_PORT_PROTOCOL,
    QUICK_SCAN_OWNER_REFRESH_RUNTIME_PORT_PROTOCOL,
    OwnerRefreshRuntimeProjection,
    OwnerRefreshSnapshot,
    QuickScanOwnerRefreshPort,
    QuickScanOwnerRefreshRuntimePort,
)

PUBLIC_API_PROTOCOL = "stockqa.quick_scan_owner_refresh_public_api/1.2.0"


def _implementation():
    return import_module("src.utils._quick_scan_owner_refresh_port")


def adapt_owner_refresh(owner):
    """Adapt a concrete owner session to its opaque, versioned public port."""
    return _implementation().adapt_owner_refresh(owner)


def is_owner_refresh_port(value) -> bool:
    return bool(_implementation().is_owner_refresh_port(value))


def supports_runtime_projection(value) -> bool:
    """Check the optional projection capability through its public method."""
    return callable(getattr(value, "runtime_projection", None))


def _runtime_projection_from_snapshot(
    snapshot: OwnerRefreshSnapshot,
) -> OwnerRefreshRuntimeProjection:
    """Adapt an older public facade's detached snapshot to the additive DTO."""
    from src.utils.quick_scan_result_outbox import canonical_sha256

    proof = snapshot.proof or {}
    request = proof.get("request", {}) if isinstance(proof, dict) else {}
    protocol = request.get("protocol") if isinstance(request, dict) else None
    plan = snapshot.plan
    entries = {
        question_id: MappingProxyType(
            {key: entry[key] for key in ("kind", "work_item_id") if key in entry}
        )
        for question_id, entry in snapshot.entries.items()
        if isinstance(question_id, str) and isinstance(entry, dict)
    }
    decisions = {
        question_id: entry.get("decision")
        for question_id, entry in snapshot.fields.items()
        if isinstance(question_id, str) and isinstance(entry, dict)
    }
    bundle = snapshot.bundle or {}
    module_locks = {
        row["module_id"]: row["version"]
        for row in bundle.get("manifest", {}).get("module_locks", [])
        if isinstance(row, dict)
        and isinstance(row.get("module_id"), str)
        and isinstance(row.get("version"), str)
    }
    policy = proof.get("owner_policy", {}) if isinstance(proof, dict) else {}
    qa_policy = policy.get("qa", {}) if isinstance(policy, dict) else {}
    legacy_route = None
    if protocol != "iqs.refresh_request/1.6.0":
        legacy_route = MappingProxyType(
            {
                "route_id": qa_policy.get("route_id"),
                "provider_config_ref": qa_policy.get("provider_config_ref"),
                "model": policy.get("model"),
                "provider": policy.get("provider"),
            }
        )
    context_hash = (
        canonical_sha256(snapshot.observation_context)
        if snapshot.observation_context is not None
        else None
    )
    plan_keys = (
        "information_cutoff",
        "perimeter_sha256",
        "manifest_raw_sha256",
        "decision_id",
        "subject_key",
        "provider",
        "model",
        "anchor_version",
    )
    targets = snapshot.ordered_route_targets
    return OwnerRefreshRuntimeProjection(
        kind=snapshot.kind,
        request_protocol=protocol if isinstance(protocol, str) else None,
        recovery_only=snapshot.recovery_only,
        information_cutoff=plan.get("information_cutoff"),
        observation_context_sha256=context_hash,
        plan_bindings=MappingProxyType({key: plan[key] for key in plan_keys if key in plan}),
        entries=MappingProxyType(entries),
        decisions=MappingProxyType(decisions),
        module_locks=MappingProxyType(module_locks),
        ordered_route_targets=(
            None if targets is None else tuple(MappingProxyType(dict(target)) for target in targets)
        ),
        legacy_route_target=legacy_route,
        requested_question_ids=tuple(snapshot.requested_question_ids),
        recovery_work_item_ids=tuple(snapshot.recovery_work_item_ids),
        remaining_work_item_ids=tuple(snapshot.remaining_work_item_ids),
    )


def runtime_projection(owner_port) -> OwnerRefreshRuntimeProjection:
    """Return a narrow fresh projection, falling back for an older facade."""
    if not is_owner_refresh_port(owner_port):
        raise ValueError("owner_refresh_port_required")
    if supports_runtime_projection(owner_port):
        projection: object = owner_port.runtime_projection()
        if not isinstance(projection, OwnerRefreshRuntimeProjection):
            raise ValueError("owner_refresh_runtime_projection_invalid")
        return projection
    return _runtime_projection_from_snapshot(owner_port.snapshot())


def build_refresh_result(batch, owner_port, quick_scan_context):
    """Render an owner result without exposing its concrete session to QAEngine."""
    return _implementation().build_refresh_result(batch, owner_port, quick_scan_context)


def original_semantic_resources(owner_port, context):
    """Return frozen answer semantics bound to this admitted owner."""
    return _implementation().original_semantic_resources(owner_port, context)


__all__ = [
    "PUBLIC_API_PROTOCOL",
    "QUICK_SCAN_OWNER_REFRESH_PORT_PROTOCOL",
    "QUICK_SCAN_OWNER_REFRESH_RUNTIME_PORT_PROTOCOL",
    "OwnerRefreshSnapshot",
    "OwnerRefreshRuntimeProjection",
    "QuickScanOwnerRefreshRuntimePort",
    "QuickScanOwnerRefreshPort",
    "adapt_owner_refresh",
    "is_owner_refresh_port",
    "supports_runtime_projection",
    "runtime_projection",
    "build_refresh_result",
    "original_semantic_resources",
]
