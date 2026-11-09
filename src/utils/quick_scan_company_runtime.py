"""Public StockQA port for IQS company orchestration over an admitted service.

The port owns access to the runner, native receipt, ledger and outbox. Its
caller schedules work and renders results using detached projections.
"""

from __future__ import annotations

import copy
import json

from src.utils import quick_scan_company_context


def is_admitted_research_service(services):
    from src.utils.quick_scan_owner_research import OwnerResearchServices

    return isinstance(services, OwnerResearchServices)


class CompanyResearchRuntime:
    def __init__(self, services):
        self._services = services

    @property
    def model(self):
        return self._services.client.model

    @property
    def provider(self):
        return self._services.client.provider_name

    def original_inputs(self):
        return quick_scan_company_context.company_scan_inputs(self._services.lifecycle)

    def pack_group_key(self, question_id):
        services = self._services
        return services.lifecycle.question_pack_group_key(services.questions[question_id])

    def check_admission(self):
        self._services.owner.check()

    def assert_execution_controls(self, **controls):
        self._services.owner.assert_execution_controls(**controls)

    def work_item_id(self, question_id):
        entry = self._services.owner.entries.get(question_id)
        return (
            entry["work_item_id"]
            if isinstance(entry, dict) and entry.get("kind") == "work"
            else None
        )

    def _matching_attempt(self, work_id, receipt, *, successful=False):
        store = self._services.store
        matches = [
            row["attempt_id"]
            for row in store.list_attempts(work_id)
            if (not successful or row["phase"] == "response_available")
            and (store.get_attempt_response(row["attempt_id"]) or {}).get("receipt") == receipt
        ]
        return matches[0] if len(matches) == 1 else None

    def inspect_answer_reference(self, work_id, reference):
        inspected = self._services.inspect_attempt(reference)
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

        services = self._services
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
        verified_search = projection.get("search_status") == "executed" and bool(
            projection.get("search_source_urls")
        )
        return dict(
            response=copy.deepcopy(inspected["response_saved"]),
            verified_search=verified_search,
            source_urls=tuple(projection["source_urls"]) if verified_search else (),
            entity_id=services.store.get_item(work_id)["entity_id"],
            metadata=copy.deepcopy(context["questions"][question_id]["metadata"]),
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
            normalized_answer=normalized,
            source_urls=sources["source_urls"],
        )

    def checkpoint(self, work_id, answer, attempt_id, provider_attempt_id):
        store = self._services.store
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

        services = self._services
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

        services = self._services
        saved = services.store.get_result_delivery(work_id)
        matching = get_matching_sealed_delivery(
            services.store, work_id, authority=services.owner.bundle["authority"]
        )
        return {
            "state": None if saved is None else saved["state"],
            "matches_authority": matching is not None,
        }
