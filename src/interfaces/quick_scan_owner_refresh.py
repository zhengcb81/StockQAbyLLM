"""Public data and capability contract for Quick Scan owner refresh work.

The contract deliberately exposes detached snapshots and narrow operations.
Implementations keep the concrete approval/session classes private to the
owner-refresh subsystem.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Protocol, Tuple, runtime_checkable

QUICK_SCAN_OWNER_REFRESH_PORT_PROTOCOL = "stockqa.quick_scan_owner_refresh_port/1.3.0"
QUICK_SCAN_OWNER_REFRESH_RUNTIME_PORT_PROTOCOL = (
    "stockqa.quick_scan_owner_refresh_runtime_port/1.0.0"
)


@dataclass(frozen=True)
class OwnerRefreshSnapshot:
    """Detached read-only-by-contract state needed by collaborating modules."""

    kind: str
    plan: Mapping[str, Any]
    proof: Optional[Mapping[str, Any]]
    record: Optional[Mapping[str, Any]]
    bundle: Optional[Mapping[str, Any]]
    entries: Mapping[str, Any]
    fields: Mapping[str, Any]
    work: Mapping[str, Any]
    deferred: Mapping[str, Any]
    scope_bindings: Mapping[str, Any]
    observation_context: Optional[Mapping[str, Any]]
    ordered_route_targets: Optional[Tuple[Mapping[str, Any], ...]]
    requested_question_ids: Tuple[str, ...]
    needs_new_dispatch: bool
    recovery_only: bool
    recovery_work_item_ids: Tuple[str, ...]
    remaining_work_item_ids: Tuple[str, ...]


@dataclass(frozen=True)
class OwnerRefreshRuntimeProjection:
    """Small, fresh projection for repeated runtime admission checks.

    This intentionally omits the owner bundle, proof, native context, record,
    service, and store.  Callers receive only frozen scalar bindings and the
    bounded route targets needed to validate the current runtime operation.
    """

    kind: str
    request_protocol: Optional[str]
    recovery_only: bool
    information_cutoff: Any
    observation_context_sha256: Optional[str]
    plan_bindings: Mapping[str, Any]
    entries: Mapping[str, Mapping[str, Any]]
    decisions: Mapping[str, Optional[str]]
    module_locks: Mapping[str, str]
    ordered_route_targets: Optional[Tuple[Mapping[str, Any], ...]]
    legacy_route_target: Optional[Mapping[str, Any]]
    requested_question_ids: Tuple[str, ...]
    recovery_work_item_ids: Tuple[str, ...]
    remaining_work_item_ids: Tuple[str, ...]


@runtime_checkable
class QuickScanOwnerRefreshRuntimePort(Protocol):
    """Optional additive capability; older refresh ports remain valid."""

    def runtime_projection(self) -> OwnerRefreshRuntimeProjection: ...


@runtime_checkable
class QuickScanOwnerRefreshPort(Protocol):
    """Versioned integration seam for refresh approval and recovery workflows."""

    def snapshot(self) -> OwnerRefreshSnapshot: ...

    def check(self) -> None: ...

    def is_bound_to(self, store: Any) -> bool: ...

    def is_bound_to_path(self, path: Any) -> bool: ...

    def assert_work_item_scope(self, store: Any, work_item_ids: Tuple[str, ...]) -> None: ...

    def assert_budget_policy(self, policy: Mapping[str, Any]) -> None: ...

    def routing_fingerprints(self) -> Dict[str, str]: ...

    def assert_provider_model(self, provider: str, model: str) -> None: ...

    def execution_routes(self, policy: Optional[Mapping[str, Any]] = None) -> list[dict]: ...

    def binding(self, question_id: str, **kwargs: Any) -> Optional[dict]: ...

    def existing_work(self, question_id: str) -> Optional[dict]: ...

    def reference(self, question: Any) -> Any: ...

    def register_subject_dispatch(self, request: dict) -> dict: ...

    def claim_registered_question_batch(
        self, question_ids: list[str], **kwargs: Any
    ) -> list[dict]: ...

    def assert_reactivated_original_binding(
        self, question_id: str, work: dict, binding: dict, *, store: Any
    ) -> None: ...

    def assert_received_checkpoint_recovery(
        self, work_item_id: str, attempt_id: str, *, store: Any
    ) -> None: ...

    def assert_execution_controls(self, **kwargs: Any) -> None: ...

    def assert_executor_policy(
        self, config_file: Any, policy: Mapping[str, Any], **kwargs: Any
    ) -> None: ...

    def restore_registered_checkpoint(self, question: Any, *, store: Any) -> Optional[dict]: ...

    def recover_received_checkpoints(self, **kwargs: Any) -> dict: ...

    def dispatch_scope_report(self, batch: Any) -> dict: ...

    def recovery_report(self) -> dict: ...

    def subject_dispatch_setup(self) -> dict: ...

    def analysis_subject(self) -> Optional[dict]: ...

    def identity_payload(self) -> dict: ...

    def native_input_references(self) -> Dict[str, Dict[str, Any]]: ...

    def original_semantic_resources(self, context: Mapping[str, Any]) -> Dict[str, Any]: ...


__all__ = [
    "QUICK_SCAN_OWNER_REFRESH_PORT_PROTOCOL",
    "QUICK_SCAN_OWNER_REFRESH_RUNTIME_PORT_PROTOCOL",
    "OwnerRefreshSnapshot",
    "OwnerRefreshRuntimeProjection",
    "QuickScanOwnerRefreshRuntimePort",
    "QuickScanOwnerRefreshPort",
]
