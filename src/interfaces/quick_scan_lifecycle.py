"""Public protocols for the Quick Scan execution lifecycle.

The core engine and lower-level company-scan utilities depend on these
structural contracts, not on the LLM runner implementation class.
"""

from __future__ import annotations

from typing import Any, Dict, List, Protocol, runtime_checkable

QUICK_SCAN_WORK_LIFECYCLE_PROTOCOL = "stockqa.quick_scan_work_lifecycle/1.1.0"
QUICK_SCAN_WORK_LIFECYCLE_METHODS = frozenset(
    {
        "registered_research_work",
        "company_scan_progress",
        "company_scan_uses_native_search",
        "for_approved_company_route",
        "reference_question",
        "hydrate_question",
        "before_question",
        "before_registered_questions",
        "dispatch_context",
        "seal_saved_delivery",
        "after_question",
        "after_question_failed",
        "assert_question_pack_ready",
        "question_pack_group_key",
        "dispatch_question_pack_context",
        "project_question_pack_failure",
    }
)


@runtime_checkable
class QuickScanWorkLifecyclePort(Protocol):
    """Structural public surface implemented by Quick Scan lifecycle adapters."""

    def registered_research_work(self) -> Any: ...

    def company_scan_progress(self, question_ids: List[str]) -> dict: ...

    def company_scan_uses_native_search(self, *, recovery_only: bool = False) -> bool: ...

    def for_approved_company_route(self, route: dict, client: Any) -> Any: ...

    def reference_question(self, question: Any) -> Any: ...

    def hydrate_question(self, question: Any) -> Any: ...

    def before_question(self, question: Any) -> Dict[str, Any]: ...

    def before_registered_questions(self, questions: List[Any]) -> List[Dict[str, Any]]: ...

    def dispatch_context(
        self, handle: Dict[str, Any], *, research_budget_managed: bool = False
    ) -> Any: ...

    def seal_saved_delivery(self, work_item_id: str) -> None: ...

    def after_question(self, handle: Dict[str, Any], result: Any) -> None: ...

    def after_question_failed(self, handle: Dict[str, Any], error: Any) -> None: ...

    def assert_question_pack_ready(self, questions: List[Any]) -> None: ...

    def question_pack_group_key(self, question: Any) -> tuple: ...

    def dispatch_question_pack_context(self, handles: List[Dict[str, Any]]) -> Any: ...

    def project_question_pack_failure(self, execution: Dict[str, Any]) -> Dict[str, Any]: ...


__all__ = [
    "QUICK_SCAN_WORK_LIFECYCLE_PROTOCOL",
    "QUICK_SCAN_WORK_LIFECYCLE_METHODS",
    "QuickScanWorkLifecyclePort",
]
