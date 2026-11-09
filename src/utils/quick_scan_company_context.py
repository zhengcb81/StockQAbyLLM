"""Public, detached scan inputs projected by their original StockQA owner.

Projection is pure: current authority is checked at send/commit boundaries.
It neither authorizes work nor returns live owner/store objects.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.runners.llm_runner import QuickScanWorkLifecycle

COMPANY_SCAN_TASK_PORT_PROTOCOL = "stockqa.company_scan_task_port/1.0.0"


class CompanyScanTaskPort:
    """Company task inputs and original validation without the native lifecycle handle."""

    __slots__ = ("__lifecycle",)

    def __init__(self, lifecycle: QuickScanWorkLifecycle) -> None:
        self.__lifecycle = lifecycle

    def inputs(self):
        return company_scan_inputs(self.__lifecycle)

    def check_admission(self):
        assert_company_scan_admission(self.__lifecycle)

    def pack_group_key(self, question_id, prompt):
        return company_scan_pack_group_key(self.__lifecycle, question_id, prompt)

    def validate_task(self, task):
        validate_company_scan_task(self.__lifecycle, task)


def _admitted_work(lifecycle):
    from src.runners.llm_runner import QuickScanWorkLifecycle

    if not isinstance(lifecycle, QuickScanWorkLifecycle):
        raise ValueError("original_native_lifecycle_required")
    try:
        work = lifecycle.registered_research_work()
    except ValueError as error:
        raise ValueError("original_admitted_native_owner_required") from error
    if not work.transport_managed:
        raise ValueError("original_admitted_native_owner_required")
    return work


def assert_company_scan_admission(lifecycle):
    """Recheck the original live owner without giving the task factory its handle."""
    if isinstance(lifecycle, CompanyScanTaskPort):
        return lifecycle.check_admission()
    _admitted_work(lifecycle).owner.check()


def company_scan_inputs(lifecycle):
    from src.utils.quick_scan_result_outbox import canonical_sha256

    if isinstance(lifecycle, CompanyScanTaskPort):
        return lifecycle.inputs()
    work = _admitted_work(lifecycle)
    owner, context = work.owner, work.observation_context
    if (
        not isinstance(context, dict)
        or context.get("schema") != "stockqa.quick_scan_observation_context/2.0.0"
        or canonical_sha256(context) != canonical_sha256(owner.native.observation_context)
    ):
        raise ValueError("original_native_subject_context_required")
    return SimpleNamespace(
        **copy.deepcopy(
            dict(
                requested_question_ids=owner.requested_question_ids,
                entries=owner.entries,
                bundle=owner.bundle,
                input_raw=owner.input_raw,
                plan=owner.plan,
                proof=owner.proof,
                observation_context=context,
                route_id=work.route_id,
                route_provider=work.provider_actual(),
                route_model=work.model_requested,
            )
        )
    )


def company_scan_pack_group_key(lifecycle, question_id, prompt):
    """Use the original provider grouping without exposing the StockQA Question type."""
    from src.core.models import Question

    if isinstance(lifecycle, CompanyScanTaskPort):
        return lifecycle.pack_group_key(question_id, prompt)
    return lifecycle.question_pack_group_key(Question(prompt, question_id=question_id))


def company_scan_capabilities(client, *, native_search=True):
    """Report the implemented research channel, not generic SDK web search."""
    from src.providers.llm_research_client import research_channel_capabilities

    capabilities = research_channel_capabilities(client)
    return dict(
        native_web_search=capabilities["native_final_json"],
        question_packs=capabilities[
            "native_final_json" if native_search else "external_final_json"
        ],
        max_output_tokens=capabilities["final_json_max_output_tokens"],
    )


def validate_company_scan_task(lifecycle, task):
    """Keep the existing v1.5 owner/task check behind the owner projection."""
    if isinstance(lifecycle, CompanyScanTaskPort):
        return lifecycle.validate_task(task)
    owner = _admitted_work(lifecycle).owner
    if owner.proof["request"].get("protocol") == "iqs.refresh_request/1.5.0":
        from src.utils.quick_scan_owner_budget_continuation import assert_research_task

        assert_research_task(owner, task, search_service=None)
