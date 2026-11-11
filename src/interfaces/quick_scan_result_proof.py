"""Public result-proof protocol used by the domain result serializer."""

from __future__ import annotations

from typing import Any, Dict, Protocol, runtime_checkable

QUICK_SCAN_RESULT_PROOF_PROTOCOL = "stockqa.quick_scan_result_proof/1.1.0"


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


__all__ = ["QUICK_SCAN_RESULT_PROOF_PROTOCOL", "QuickScanResultProofPort"]
