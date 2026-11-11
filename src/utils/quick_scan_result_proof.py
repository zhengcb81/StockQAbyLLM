"""Owner adapter for binding persisted Quick Scan search evidence to outputs.

The domain result model consumes only the versioned result-proof protocol. This
adapter owns the storage and transport-specific verification details.
"""

from __future__ import annotations

import copy
from typing import Any, Dict

from src.interfaces.quick_scan_result_proof import (
    QUICK_SCAN_RESULT_PROOF_PROTOCOL,
    QuickScanResultProofPort,
)
from src.utils.quick_scan_work_store import (
    QuickScanWorkStore,
    canonical_native_events,
    sanitize_transport_receipt,
)
from src.utils.quick_scan_work_transport import project_quick_scan_search


class QuickScanResultProofAdapter:
    """Read-only projection from the original work ledger to public results."""

    def __init__(self, work_store: QuickScanWorkStore | None = None) -> None:
        if work_store is not None and not isinstance(work_store, QuickScanWorkStore):
            raise TypeError("quick_scan_result_proof_requires_owner_work_store")
        self._work_store = work_store

    def sanitize_transport_receipt(self, receipt: Any) -> Dict[str, Any]:
        return sanitize_transport_receipt(receipt)

    def project_checkpoint_search(
        self, execution_metadata: Dict[str, Any], *, work_item_id: Any = None
    ) -> Dict[str, Any]:
        return project_quick_scan_search(
            execution_metadata,
            work_store=self._work_store,
            work_item_id=work_item_id,
        )

    def project_external_result(
        self,
        execution_metadata: Dict[str, Any],
        *,
        entity_id: str,
        question_id: str,
    ) -> Dict[str, Any]:
        if not isinstance(execution_metadata, dict):
            raise ValueError("external_public_result_execution_metadata_required")
        if self._work_store is None:
            raise ValueError("external_public_result_owner_store_required")
        proof = execution_metadata.get("external_context_use")
        if not isinstance(proof, dict):
            raise ValueError("external_public_result_owner_proof_required")
        try:
            item = self._work_store.get_item(proof["work_item_id"])
            if item["entity_id"] != entity_id or item["question_id"] != question_id:
                raise ValueError("external public output identity differs from its actual owner")
            search_projection = project_quick_scan_search(
                execution_metadata,
                work_store=self._work_store,
                work_item_id=item["work_item_id"],
            )
            original = self._work_store.get_attempt_response(proof["work_attempt_id"])
            if original is None:
                raise ValueError("external public output has no actual final response")
            if sanitize_transport_receipt(execution_metadata) != original["receipt"]:
                raise ValueError("external public output original HTTP receipt drifted")
            native_events = original.get("native_search_events")
            if (
                native_events is None
                or canonical_native_events(execution_metadata.get("web_search_calls", []))
                != native_events
            ):
                raise ValueError(
                    "external public output native events differ from their actual owner"
                )
            attempts = []
            for attempt in self._work_store.list_attempts(item["work_item_id"]):
                saved = self._work_store.get_attempt_response(attempt["attempt_id"])
                if saved is not None:
                    attempts.append(copy.deepcopy(saved["receipt"]))
            search_binding = {
                "schema": "stockqa.search_binding/1.1.0",
                "entity_id": entity_id,
                "question_id": question_id,
                "identity_snapshot_sha256": item["identity_snapshot_sha256"],
                "external_context_use": copy.deepcopy(search_projection["external_context_use"]),
                "native_receipt": copy.deepcopy(original["receipt"]),
            }
            return {
                "search_projection": search_projection,
                "search_binding": search_binding,
                "public_original": copy.deepcopy(original["receipt"]),
                "public_recorded_at": original["recorded_at"],
                "attempts": attempts,
                "web_search_calls": copy.deepcopy(native_events),
            }
        except Exception as error:
            raise ValueError("external public output could not verify its actual owner") from error


def result_proof_port_for_store(work_store: QuickScanWorkStore) -> QuickScanResultProofPort:
    """Create the public proof adapter for a caller's already-owned store."""
    return QuickScanResultProofAdapter(work_store)


def result_proof_port(work_store: QuickScanWorkStore | None = None) -> QuickScanResultProofPort:
    """Create a proof adapter, optionally bound to a durable Quick Scan store."""
    return QuickScanResultProofAdapter(work_store)


MODULE_API_PROTOCOL = "stockqa.quick-scan-result-proof-module/1.1.0"
MODULE_API_EXPORTS = frozenset(
    {
        "QUICK_SCAN_RESULT_PROOF_PROTOCOL",
        "QuickScanResultProofAdapter",
        "result_proof_port",
        "result_proof_port_for_store",
    }
)

__all__ = ["MODULE_API_PROTOCOL", "MODULE_API_EXPORTS", *sorted(MODULE_API_EXPORTS)]
