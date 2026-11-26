"""Public result-proof protocol used by the domain result serializer."""

from __future__ import annotations

from typing import Any, Dict, Protocol, runtime_checkable

QUICK_SCAN_RESULT_PROOF_PROTOCOL = "stockqa.quick_scan_result_proof/1.1.0"


class QuickScanProofLedger(Protocol):
    """Read original evidence through the owner without acquiring its database."""

    def get_item(self, work_item_id: str) -> dict: ...

    def list_attempts(self, work_item_id: str) -> list[dict]: ...

    def get_attempt_response(self, attempt_id: str) -> dict | None: ...

    def read_external_checkpoint_search(
        self, attempt_id: str, original_receipt: dict, proof: dict
    ) -> dict | None: ...


class QuickScanRecoveryLedger(QuickScanProofLedger, Protocol):
    """The additional reads needed to classify original pending paid work."""

    def get_attempt_transmission(self, attempt_id: str) -> dict: ...

    def get_subject_dispatch_receipt(self, attempt_id: str) -> dict | None: ...


@runtime_checkable
class QuickScanResultProofPort(Protocol):
    """Verify and project persisted transport evidence for one public answer."""

    def sanitize_transport_receipt(self, receipt: Any) -> Dict[str, Any]: ...

    def project_checkpoint_search(
        self, execution_metadata: Dict[str, Any], *, work_item_id: Any = None
    ) -> Dict[str, Any]: ...

    def project_external_result(
        self,
        execution_metadata: Dict[str, Any],
        *,
        entity_id: str,
        question_id: str,
    ) -> Dict[str, Any]:
        """Return search_projection, search_binding, public_original and persisted attempts."""
        ...


__all__ = [
    "QUICK_SCAN_RESULT_PROOF_PROTOCOL",
    "QuickScanProofLedger",
    "QuickScanRecoveryLedger",
    "QuickScanResultProofPort",
]
