"""Public, detached scan inputs projected by their original StockQA owner.

Projection is pure: current authority is checked at send/commit boundaries.
It neither authorizes work nor returns live owner/store objects.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace


def company_scan_inputs(lifecycle):
    from src.runners.llm_runner import QuickScanWorkLifecycle
    from src.utils.quick_scan_owner_refresh import OwnerRefreshSession
    from src.utils.quick_scan_refresh_execution import ApprovedRefreshSession
    from src.utils.quick_scan_result_outbox import canonical_sha256

    if not isinstance(lifecycle, QuickScanWorkLifecycle):
        raise ValueError("original_native_lifecycle_required")
    owner = lifecycle._owner_refresh
    if (
        not isinstance(owner, ApprovedRefreshSession)
        or not isinstance(owner.native, OwnerRefreshSession)
        or owner.service.store is not lifecycle._store
        or owner.native.store is not lifecycle._store
        or not lifecycle._transport_managed
    ):
        raise ValueError("original_admitted_native_owner_required")
    context = lifecycle._observation_context
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
                route_id=lifecycle._route_id,
                route_provider=(
                    next(
                        t["provider"]
                        for t in owner.ordered_route_targets
                        if t["route_id"] == lifecycle._route_id
                    )
                    if owner.proof["request"].get("protocol") == "iqs.refresh_request/1.6.0"
                    else owner.plan["provider"]
                ),
                route_model=lifecycle._model_requested,
            )
        )
    )


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
    owner = lifecycle._owner_refresh
    if owner.proof["request"].get("protocol") == "iqs.refresh_request/1.5.0":
        from src.utils.quick_scan_owner_budget_continuation import assert_research_task

        assert_research_task(owner, task, search_service=None)
