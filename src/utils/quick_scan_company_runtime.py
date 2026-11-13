"""Public StockQA port for IQS company orchestration over an admitted service.

The port owns access to the runner, native receipt, ledger and outbox. Its
caller schedules work and renders results using detached projections.
"""

from __future__ import annotations

import copy
import inspect
import json
import weakref
from typing import Any

from src.utils import quick_scan_company_context

COMPANY_SCAN_RUNTIME_PORT_PROTOCOL = "stockqa.company_scan_runtime_port/2.3.0"
_RUNTIME_SERVICES: weakref.WeakKeyDictionary[object, Any] = weakref.WeakKeyDictionary()
_RUNTIME_TASK_CONTRACTS: weakref.WeakKeyDictionary[object, dict[str, Any]] = (
    weakref.WeakKeyDictionary()
)
_RUNTIME_SESSIONS: weakref.WeakKeyDictionary[object, Any] = weakref.WeakKeyDictionary()
RUNTIME_SERVICES_PROTOCOL = "iqs.agent-runtime-services/1.0.0"


class _RuntimeSessionHandle:
    """Opaque handle for a StockQA-owned research SDK session."""

    __slots__ = ("__weakref__",)


def _session_for(handle):
    try:
        return _RUNTIME_SESSIONS[handle]
    except KeyError as error:
        raise RuntimeError("company runtime session is unavailable") from error


def _services_for(runtime):
    try:
        return _RUNTIME_SERVICES[runtime]
    except KeyError as error:
        raise RuntimeError("company runtime is not initialized") from error


def is_admitted_research_service(services):
    from src.utils.quick_scan_owner_research import OwnerResearchServices

    return isinstance(services, OwnerResearchServices)


def _service_task_contract(services):
    """Read only the detached contract exported by the owner service."""
    contract = getattr(services, "task_contract", None)
    from src.utils.quick_scan_company_api import company_agent_public_api

    task_snapshot = company_agent_public_api("task_snapshot").task_snapshot

    task = getattr(services, "task_handle", None)
    if task is not None:
        current = task_snapshot(task)
        if contract is not None and current != contract:
            raise ValueError("original_service_bound_task_required")
        return current
    if isinstance(contract, dict):
        return copy.deepcopy(contract)
    return task_snapshot(services.task)


def create_company_research_runtime(services, *, task_contract=None):
    """Create the owner-validated opaque Runtime service port for one task DTO."""
    if isinstance(services, CompanyResearchRuntime):
        if task_contract is not None and not services.is_bound_to_task_contract(task_contract):
            raise ValueError("original_service_bound_task_required")
        return services
    return CompanyResearchRuntime(services, task_contract=task_contract)


class CompanyResearchRuntime:
    __slots__ = ("__weakref__",)

    def __init__(self, services, *, task_contract=None):
        if not is_admitted_research_service(services):
            raise ValueError("original_owner_research_services_required")
        if task_contract is None:
            task_contract = _service_task_contract(services)
        if not isinstance(task_contract, dict):
            raise ValueError("company_task_contract_required")
        if _service_task_contract(services) != task_contract:
            raise ValueError("original_service_bound_task_required")
        services.bind_task_owner_binding(copy.deepcopy(task_contract.get("owner_binding")))
        _RUNTIME_SERVICES[self] = services
        _RUNTIME_TASK_CONTRACTS[self] = copy.deepcopy(task_contract)

    @property
    def protocol(self):
        return RUNTIME_SERVICES_PROTOCOL

    def describe(self):
        services = _services_for(self)
        from src.providers.llm_research_client import ResearchChatSession

        step_parameters = inspect.signature(ResearchChatSession.step).parameters
        contract = _RUNTIME_TASK_CONTRACTS.get(self)
        return {
            "protocol": RUNTIME_SERVICES_PROTOCOL,
            "task_contract_sha256": None if contract is None else contract.get("contract_sha256"),
            "capabilities": {
                "native_search": bool(services.native_search),
                "continuation_context": "continuation_context" in step_parameters,
                "final_json_channel": True,
                "original_semantic_resources": True,
            },
            "synthetic": False,
        }

    def route(self, envelope):
        client = _services_for(self).client_for(copy.deepcopy(envelope))
        return {"provider": client.provider_name, "model": client.model}

    def endpoint_profile_id(self):
        """Return the owner's opaque endpoint descriptor, never a caller URL."""
        operation = getattr(_services_for(self), "endpoint_profile_id", None)
        if not callable(operation):
            return None
        value = operation()
        if value is None:
            return None
        from src.utils.quick_scan_research_intent import (
            api_research_intent_is_endpoint_profile_id as is_profile_id,
        )

        if not is_profile_id(value):
            raise ValueError("runtime_endpoint_profile_invalid")
        return value

    def create_session(self, envelope, session_spec):
        if not isinstance(session_spec, dict):
            raise ValueError("runtime_session_spec_object_required")
        allowed = {
            "system_prefix",
            "tools",
            "max_output_tokens",
            "session_input_tokens",
            "session_output_tokens",
            "wall_seconds",
            "response_channel",
        }
        if set(session_spec) - allowed or not {
            "system_prefix",
            "tools",
            "max_output_tokens",
            "session_input_tokens",
            "session_output_tokens",
            "wall_seconds",
        }.issubset(session_spec):
            raise ValueError("runtime_session_spec_invalid")
        services = _services_for(self)
        from src.providers.llm_research_client import ResearchChatSession

        client = services.client_for(copy.deepcopy(envelope))
        options = copy.deepcopy(session_spec)
        session = ResearchChatSession(
            client, **options, **({"native_search": True} if services.native_search else {})
        )
        handle = _RuntimeSessionHandle()
        _RUNTIME_SESSIONS[handle] = (self, session)
        return handle

    def step_session(self, handle, step_request):
        owner, session = _session_for(handle)
        if owner is not self:
            raise ValueError("runtime_session_handle_owner_mismatch")
        if not isinstance(step_request, dict) or set(step_request) - {
            "user_context",
            "tool_results",
            "continuation_context",
        }:
            raise ValueError("runtime_session_step_request_invalid")
        result = session.step(**copy.deepcopy(step_request))
        if not isinstance(result, dict):
            raise ValueError("runtime_session_step_projection_invalid")
        json.dumps(result, ensure_ascii=False, allow_nan=False)
        return copy.deepcopy(result)

    def close_session(self, handle):
        if isinstance(handle, _RuntimeSessionHandle):
            try:
                owner, _ = _RUNTIME_SESSIONS[handle]
            except KeyError:
                return
            if owner is self:
                del _RUNTIME_SESSIONS[handle]

    def generation_scope(self, envelope):
        return _services_for(self).generation_scope(copy.deepcopy(envelope))

    def search(self, envelope):
        return copy.deepcopy(_services_for(self).search(copy.deepcopy(envelope)))

    def observe_native_step(self, step):
        return _services_for(self).observe_native_step(copy.deepcopy(step))

    def observe_native_failure(self, error):
        return _services_for(self).observe_native_failure(error)

    @property
    def model(self):
        return _services_for(self).client.model

    @property
    def provider(self):
        return _services_for(self).client.provider_name

    def is_bound_to_task_contract(self, task_contract):
        """Compare the attached task only through its owner-published DTO."""
        if not isinstance(task_contract, dict):
            return False
        try:
            return _service_task_contract(_services_for(self)) == task_contract
        except (AttributeError, TypeError, ValueError):
            return False

    def has_native_session(self):
        """Whether the owner admitted this work through its native session."""
        return bool(_services_for(self).native_session)

    def attempt_observations(self, *, transport_origin):
        """Return the owner's detached, run-scoped attempt observations."""
        return _services_for(self).attempt_observations(transport_origin=transport_origin)

    def supports_intent_recovery(self):
        return callable(getattr(_services_for(self), "recover_intent", None))

    def recover_intent(self, intent):
        if not self.supports_intent_recovery():
            raise ValueError("native_intent_lookup_not_available")
        return _services_for(self).recover_intent(copy.deepcopy(intent))

    def request_resume(self, attempt_ref):
        return _services_for(self).request_resume(attempt_ref)

    def inspect_attempt(self, attempt_ref):
        return _services_for(self).inspect_attempt(attempt_ref)

    def validate_registered_inputs(self):
        return _services_for(self).validate_registered_inputs()

    def original_inputs(self):
        return quick_scan_company_context.company_scan_inputs(_services_for(self).lifecycle)

    def original_scan_context(self):
        """Return the owner-issued safe contract for the registered company scan."""
        return quick_scan_company_context.company_scan_contract(_services_for(self).lifecycle)

    def original_semantic_resources(self):
        """Resolve the semantic package using this task's registered owner."""
        services = _services_for(self)
        context = self.original_scan_context().observation_context
        from src.utils.quick_scan_owner_refresh_api import original_semantic_resources

        return copy.deepcopy(original_semantic_resources(services.owner, context))

    def pack_group_key(self, question_id):
        services = _services_for(self)
        return services.lifecycle.question_pack_group_key(services.questions[question_id])

    def check_admission(self):
        _services_for(self).owner.check()

    def assert_execution_controls(self, **controls):
        _services_for(self).owner.assert_execution_controls(**controls)

    def work_item_id(self, question_id):
        entry = _services_for(self).owner.snapshot().entries.get(question_id)
        return (
            entry["work_item_id"]
            if isinstance(entry, dict) and entry.get("kind") == "work"
            else None
        )

    def _matching_attempt(self, work_id, receipt, *, successful=False):
        store = _services_for(self).store
        matches = [
            row["attempt_id"]
            for row in store.list_attempts(work_id)
            if (not successful or row["phase"] == "response_available")
            and (store.get_attempt_response(row["attempt_id"]) or {}).get("receipt") == receipt
        ]
        return matches[0] if len(matches) == 1 else None

    def inspect_answer_reference(self, work_id, reference):
        inspected = _services_for(self).inspect_attempt(reference)
        saved = inspected.get("response_saved")
        if not inspected.get("known") or not isinstance(saved, dict):
            return None
        native = inspected["native_receipt"]
        attempt = self._matching_attempt(work_id, native)
        if attempt is None:
            return None
        return dict(
            response=copy.deepcopy(saved),
            attempt_id=attempt,
            provider_attempt_id=native["attempt_id"],
        )

    def answer_source_context(self, question_id, reference, context):
        from src.utils.quick_scan_result_outbox import canonical_sha256
        from src.utils.quick_scan_work_transport import (
            project_quick_scan_answer_sources,
        )

        services = _services_for(self)
        inspected = services.inspect_attempt(reference)
        if (
            not inspected.get("known")
            or not inspected.get("response_saved")
            or inspected.get("outcome_recorded") != "response_available"
        ):
            raise ValueError("original successful current native reference required")
        work_id = self.work_item_id(question_id)
        if work_id is None:
            raise ValueError("original question work member required")
        native = inspected["native_receipt"]
        attempt_id = self._matching_attempt(work_id, native, successful=True)
        if attempt_id is None:
            raise ValueError("exact original successful native work member required")
        use = services.store.get_external_context_use(attempt_id)
        if use is not None and (
            not isinstance(use, dict) or not isinstance(use.get("proof"), dict) or not use["proof"]
        ):
            raise ValueError("original external context use proof required")
        saved = services.store.get_observation_context(work_id)
        if (
            not saved
            or saved["context"] != context
            or saved["context_sha256"] != canonical_sha256(context)
        ):
            raise ValueError("original frozen observation context required")
        execution = copy.deepcopy(native)
        execution["work_transport"] = dict(
            work_attempt_id=attempt_id, final_receipt=copy.deepcopy(native)
        )
        if use is not None:
            execution["external_context_use"] = copy.deepcopy(use["proof"])
        projection = project_quick_scan_answer_sources(
            execution, work_store=services.store, work_item_id=work_id
        )
        from src.utils.quick_scan_observation_context import frozen_question_contract

        question_context = context["questions"][question_id]
        verified_search = projection.get("search_status") == "executed" and bool(
            projection.get("search_source_urls")
        )
        return dict(
            response=copy.deepcopy(inspected["response_saved"]),
            verified_search=verified_search,
            source_urls=tuple(projection["source_urls"]) if verified_search else (),
            entity_id=services.store.get_item(work_id)["entity_id"],
            metadata=copy.deepcopy(question_context["metadata"]),
            question_contract=copy.deepcopy(frozen_question_contract(question_context)),
        )

    @staticmethod
    def validate_answer_transport(answer, question_id, sources):
        from src.core.models import Answer
        from src.utils.quick_scan_observation_context import (
            split_standard_answer_transport,
            validate_standard_answer,
        )

        raw = json.dumps(answer, ensure_ascii=False)
        if sources["verified_search"]:
            # Match the native checkpoint's exact status and score types.
            Answer(raw, score=answer.get("score"), status=answer.get("status"), source="llm")
        summary, standard = split_standard_answer_transport(raw)
        if standard is None:
            raise ValueError("original standard answer transport required")
        normalized = dict(
            entity_id=sources["entity_id"],
            question_id=question_id,
            status=answer.get("status"),
            score=answer.get("score"),
            description=summary,
        )
        validate_standard_answer(
            standard,
            metadata=sources["metadata"],
            question_contract=sources.get("question_contract"),
            normalized_answer=normalized,
            source_urls=sources["source_urls"],
        )

    def checkpoint(self, work_id, answer, attempt_id, provider_attempt_id):
        store = _services_for(self).store
        saved = store.get_answer_checkpoint(work_id)
        if saved is None:
            return {"status": "missing"}
        standard = store.get_standard_answer(work_id)
        if (
            saved["attempt_id"] != attempt_id
            or not isinstance(standard, dict)
            or standard.get("answer") != answer
            or saved["payload"]["provenance"].get("provider_attempt_id") != provider_attempt_id
        ):
            return {"status": "conflict"}
        return {
            "status": "matching",
            "checkpoint_id": saved["checkpoint_id"],
            "sha256": saved["payload_sha256"],
        }

    def publish_answer(self, question_id, answer, reference, *, checkpoint_exists):
        from src.utils.quick_scan_work_store import LeaseFencedError, WorkConflictError

        services = _services_for(self)
        stage = "restore"
        try:
            if not checkpoint_exists:
                services.restore_finalization_handle(question_id, native_reference=reference)
            stage = "commit"
            services.commit_standard_answers([copy.deepcopy(answer)], native_reference=reference)
        except (WorkConflictError, LeaseFencedError) as error:
            return dict(stage=stage, error_type=type(error).__name__)
        return None

    def delivery(self, work_id):
        from src.utils.quick_scan_delivery_seal import get_matching_sealed_delivery

        services = _services_for(self)
        saved = services.store.get_result_delivery(work_id)
        matching = get_matching_sealed_delivery(
            services.store, work_id, authority=services.owner.snapshot().bundle["authority"]
        )
        return {
            "state": None if saved is None else saved["state"],
            "matches_authority": matching is not None,
        }
