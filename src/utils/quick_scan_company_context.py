"""Public, detached scan inputs projected by their original StockQA owner.

Projection is pure: current authority is checked at send/commit boundaries.
It neither authorizes work nor returns live owner/store objects.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

from src.interfaces.quick_scan_company import (
    COMPANY_SCAN_CONTRACT_PROTOCOL,
    CompanyScanContract,
)
from src.interfaces.quick_scan_lifecycle import QuickScanWorkLifecyclePort

COMPANY_SCAN_TASK_PORT_PROTOCOL = "stockqa.company_scan_task_port/1.0.0"


class CompanyScanTaskPort:
    """Company task inputs and original validation without the native lifecycle handle."""

    __slots__ = ("__lifecycle",)

    def __init__(self, lifecycle: QuickScanWorkLifecyclePort) -> None:
        self.__lifecycle = lifecycle

    def inputs(self):
        return company_scan_inputs(self.__lifecycle)

    def contract(self):
        return company_scan_contract(self.__lifecycle)

    def check_admission(self):
        assert_company_scan_admission(self.__lifecycle)

    def pack_group_key(self, question_id, prompt):
        return company_scan_pack_group_key(self.__lifecycle, question_id, prompt)

    def validate_task(self, task):
        validate_company_scan_task(self.__lifecycle, task)


def _admitted_work(lifecycle):
    if not isinstance(lifecycle, QuickScanWorkLifecyclePort):
        raise ValueError("original_native_lifecycle_required")
    try:
        work = lifecycle.registered_research_work()
    except ValueError as error:
        raise ValueError("original_admitted_native_owner_required") from error
    if work.transport_managed is not True:
        raise ValueError("original_admitted_native_owner_required")
    owner = getattr(work, "owner", None)
    store = getattr(work, "store", None)
    try:
        from src.utils.quick_scan_owner_refresh_api import is_owner_refresh_port

        # is_owner_refresh_port is a structural bool check only; the port
        # operations stay unreachable for a non-port owner.
        if not is_owner_refresh_port(owner) or owner is None:
            raise ValueError("original_admitted_native_owner_required")
        if not owner.is_bound_to(store) or owner.snapshot().kind != "approved":
            raise ValueError("original_admitted_native_owner_required")
    except (AttributeError, TypeError) as error:
        raise ValueError("original_admitted_native_owner_required") from error
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
    snapshot = owner.snapshot()
    if (
        not isinstance(context, dict)
        or context.get("schema") != "stockqa.quick_scan_observation_context/2.0.0"
        or canonical_sha256(context) != canonical_sha256(snapshot.observation_context)
    ):
        raise ValueError("original_native_subject_context_required")
    return SimpleNamespace(
        **copy.deepcopy(
            dict(
                requested_question_ids=snapshot.requested_question_ids,
                entries=snapshot.entries,
                bundle=snapshot.bundle,
                plan=snapshot.plan,
                proof=snapshot.proof,
                observation_context=context,
                route_id=work.route_id,
                route_provider=work.provider_actual(),
                route_model=work.model_requested,
            )
        )
    )


def company_scan_contract(lifecycle):
    """Project admitted owner data into a detached task contract with no raw bytes."""
    if isinstance(lifecycle, CompanyScanTaskPort):
        return lifecycle.contract()
    work = _admitted_work(lifecycle)
    owner, context = work.owner, work.observation_context
    snapshot = owner.snapshot()
    from src.utils.quick_scan_result_outbox import canonical_sha256

    if (
        not isinstance(context, dict)
        or context.get("schema") != "stockqa.quick_scan_observation_context/2.0.0"
        or canonical_sha256(context) != canonical_sha256(snapshot.observation_context)
    ):
        raise ValueError("original_native_subject_context_required")
    manifest = (snapshot.bundle or {}).get("manifest", {})
    questions = manifest.get("questions")
    module_locks = manifest.get("module_locks")
    if not isinstance(questions, list) or not isinstance(module_locks, list):
        raise ValueError("original_native_manifest_required")
    requested = tuple(snapshot.requested_question_ids)
    question_contracts = {
        row["id"]: copy.deepcopy(row)
        for row in questions
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    if not set(requested) <= set(question_contracts):
        raise ValueError("original_native_manifest_question_required")
    eligible = tuple(
        question_id
        for question_id in requested
        if isinstance(snapshot.entries.get(question_id), dict)
        and snapshot.entries[question_id].get("kind") == "work"
    )
    identity = owner.identity_payload()
    if not isinstance(identity, dict):
        raise ValueError("original_native_identity_required")
    identity_projection = {
        key: copy.deepcopy(identity[key])
        for key in ("entity_id", "canonical_name", "listings")
        if key in identity
    }
    cutoff = snapshot.plan.get("information_cutoff")
    if not isinstance(cutoff, str) or not cutoff:
        raise ValueError("registered_information_cutoff_required")
    return CompanyScanContract(
        protocol=COMPANY_SCAN_CONTRACT_PROTOCOL,
        requested_question_ids=requested,
        eligible_question_ids=eligible,
        question_contracts=question_contracts,
        module_locks=tuple(copy.deepcopy(module_locks)),
        identity_payload=identity_projection,
        native_input_references=owner.native_input_references(),
        observation_context=copy.deepcopy(context),
        information_cutoff=cutoff,
        route_id=work.route_id,
        route_provider=work.provider_actual(),
        route_model=work.model_requested,
    )


def create_native_task(lifecycle, **kwargs):
    from src.utils._quick_scan_company_task_factory import create_native_task as factory

    return factory(lifecycle, **kwargs)


def create_company_scan_task(lifecycle, **kwargs):
    from src.utils._quick_scan_company_task_factory import (
        create_company_scan_task as factory,
    )

    return factory(lifecycle, **kwargs)


def company_scan_pack_group_key(lifecycle, question_id, prompt):
    """Use the original provider grouping without exposing the StockQA Question type."""
    from src.core.models import Question

    if isinstance(lifecycle, CompanyScanTaskPort):
        return lifecycle.pack_group_key(question_id, prompt)
    return lifecycle.question_pack_group_key(Question(prompt, question_id=question_id))


def company_scan_capabilities(client, *, native_search=True):
    """Report the implemented research channel, not generic SDK web search."""
    from src.providers import llm_research_client
    from src.utils.quick_scan_company_api import supports_public_api

    if not supports_public_api(
        getattr(llm_research_client, "MODULE_API_PROTOCOL", None),
        "stockqa.llm-research-client-module",
        (1, 0, 0),
    ) or "research_channel_capabilities" not in getattr(
        llm_research_client, "MODULE_API_EXPORTS", frozenset()
    ):
        raise ValueError("research_capabilities_provider_protocol_unsupported")

    capabilities = llm_research_client.research_channel_capabilities(client)
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
    work = _admitted_work(lifecycle)
    owner = work.owner
    proof = owner.snapshot().proof or {}
    if proof.get("request", {}).get("protocol") == "iqs.refresh_request/1.5.0":
        from src.utils.quick_scan_owner_budget_continuation import assert_research_task

        assert_research_task(owner, task, store=work.store, search_service=None)
