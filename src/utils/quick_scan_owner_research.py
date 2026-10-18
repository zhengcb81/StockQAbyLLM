"""Original approved owner -> shared Company Runner service seams.

This module sends no HTTP and creates no ledger. It composes the original
QuickScanWorkLifecycle, approved action, LLMClient and original transport.
Native search/evidence and research continuation must be supported by those
original services; absence is a refusal, never manufactured permission.
"""

from __future__ import annotations

import copy
from contextlib import ExitStack, contextmanager

from src.core.models import Answer, QAResult, Question
from src.providers.llm_client import LLMClient
from src.runners.llm_runner import QuickScanWorkLifecycle
from src.utils.quick_scan_owner_refresh import OwnerRefreshSession
from src.utils.quick_scan_refresh_execution import ApprovedRefreshSession
from src.utils.quick_scan_result_outbox import canonical_sha256
from src.utils.quick_scan_work_store import (
    _budget_policy_projection,
    _sanitized_receipt,
    quick_scan_receipt_sha256,
)


class OwnerResearchRejected(ValueError):
    """Bounded original-owner refusal; never a retry permission."""


class OwnerResearchServices:
    """Production service adapter requiring a real registered native action.

    Tests may use original public owner software over synthetic temporary
    identities and a scripted wire. Such execution is explicitly recorded as
    scripted_wire by the outer composition and confers no production status.
    The adapter itself never patches transport or reads environment secrets.
    """

    def __init__(
        self,
        lifecycle,
        client,
        *,
        task,
        cost_resolver=None,
        search_service=None,
        native_session=False,
        native_max_turns=24,
    ):
        if not isinstance(lifecycle, QuickScanWorkLifecycle) or not isinstance(client, LLMClient):
            raise OwnerResearchRejected("original_lifecycle_and_client_required")
        owner = lifecycle._owner_refresh
        if (
            not isinstance(owner, ApprovedRefreshSession)
            or not isinstance(owner.native, OwnerRefreshSession)
            or owner.service.store is not lifecycle._store
            or owner.native.store is not lifecycle._store
        ):
            raise OwnerResearchRejected("original_registered_owner_required")
        if not lifecycle._transport_managed or not lifecycle._budget_policy:
            raise OwnerResearchRejected("original_metered_transport_required")
        if _budget_policy_projection(lifecycle._budget_policy) != _budget_policy_projection(
            owner.service.budget
        ):
            raise OwnerResearchRejected("approved_budget_policy_required")
        if not callable(getattr(lifecycle._store, "get_research_visible_output", None)):
            raise OwnerResearchRejected("native_visible_output_durability_required")
        if (
            client.model != lifecycle._model_requested
            or client.provider_name != owner.plan["provider"]
        ):
            raise OwnerResearchRejected("approved_client_route_mismatch")
        owner.assert_provider_model(client.provider_name, client.model)
        routes = [
            r
            for r in owner.execution_routes(lifecycle._budget_policy)
            if r["id"] == lifecycle._route_id and r["model"] == client.model
        ]
        if (
            len(routes) != 1
            or routes[0] != lifecycle._budget_route
            or lifecycle._provider_name != routes[0]["provider_config_ref"]
            or not isinstance(routes[0].get("quota_group"), str)
        ):
            raise OwnerResearchRejected("approved_native_budget_route_required")
        self.lifecycle, self.client, self.owner = lifecycle, client, owner
        self.store, self.task = lifecycle._store, task
        self.cost_resolver, self.search_service = cost_resolver, search_service
        if (
            type(native_session) is not bool
            or type(native_max_turns) is not int
            or not 1 <= native_max_turns <= 32
        ):
            raise OwnerResearchRejected("native_session_controls_invalid")
        self.native_session, self.native_max_turns = native_session, native_max_turns
        task_cap = getattr(task, "limits", {}).get("max_model_requests_per_session")
        if task_cap is not None:
            if type(task_cap) is not int or task_cap < 1:
                raise OwnerResearchRejected("registered_native_turn_cap_invalid")
            self.native_max_turns = min(self.native_max_turns, task_cap)
        self._native_session_ids = {}
        self._handles, self._observations, self._replayed_refs = {}, {}, {}
        self._task_binding = None
        self._active = False
        self._responded_work_ids = set()
        self._validate_task()
        self._task_sha256 = canonical_sha256(task.raw)
        self._questions_snapshot = copy.deepcopy(task.questions)
        self._company_snapshot = copy.deepcopy(task.company_doc)
        self._company_ref_snapshot = copy.deepcopy(task.company_ref)
        self._limits_snapshot = copy.deepcopy(getattr(task, "limits", {}))
        self._client_endpoint_snapshot = client.base_url
        self._route_snapshot = (
            lifecycle._route_id,
            lifecycle._provider_name,
            copy.deepcopy(lifecycle._budget_route),
            copy.deepcopy(lifecycle._model_resolution),
        )
        self._context_sha256 = canonical_sha256(lifecycle._observation_context)
        self.owner.check()

    def _validate_task(self):
        if self.task.company_doc.get("origin") != "owner_public":
            raise OwnerResearchRejected("owner_public_task_required")
        rows = self.task.questions
        if not rows or len({q["question_id"] for q in rows}) != len(rows):
            raise OwnerResearchRejected("unique_original_questions_required")
        self.questions = {}
        context = self.lifecycle._observation_context
        if (
            not isinstance(context, dict)
            or context.get("schema") != "stockqa.quick_scan_observation_context/2.0.0"
        ):
            raise OwnerResearchRejected("original_subject_context_required")
        if canonical_sha256(context) != canonical_sha256(self.owner.native.observation_context):
            raise OwnerResearchRejected("original_subject_context_changed")
        subject = self.task.company_doc.get("subject", {}).get("subject_ref")
        module_locks = {
            row["module_id"]: row["version"]
            for row in self.owner.bundle["manifest"]["module_locks"]
        }
        for row in rows:
            qid = row["question_id"]
            binding = (context.get("questions", {}).get(qid) or {}).get("subject_binding") or {}
            contract = binding.get("question_contract")
            if (
                qid not in self.owner.entries
                or not isinstance(contract, dict)
                or contract.get("id") != qid
                or row.get("question") != contract
                or row.get("question_object_sha256") != canonical_sha256(contract)
                or row.get("module_id") != contract.get("module_id")
                or row.get("module_version") != module_locks.get(contract.get("module_id"))
                or binding.get("subject_ref") != subject
            ):
                raise OwnerResearchRejected("original_question_or_subject_mismatch")
            self.questions[qid] = Question(contract["prompt"], question_id=qid)
        if self.task.raw.get("information_cutoff") != self.owner.plan.get("information_cutoff"):
            raise OwnerResearchRejected("original_information_cutoff_required")
        for question in self.questions.values():
            self.lifecycle.question_pack_group_key(question)

    def bind_task_owner_binding(self, binding):
        self._assert_registered_inputs()
        subject = self.task.company_doc["subject"]["subject_ref"]
        if (
            not isinstance(binding, dict)
            or binding.get("synthetic") is not False
            or binding.get("subject_ref") != subject
            or not binding.get("authority_ref")
        ):
            raise OwnerResearchRejected("native_task_owner_binding_mismatch")
        self._task_binding = copy.deepcopy(binding)

    def _assert_registered_inputs(self):
        if (
            canonical_sha256(self.task.raw) != self._task_sha256
            or self.task.questions != self._questions_snapshot
            or self.task.company_doc != self._company_snapshot
            or self.task.company_ref != self._company_ref_snapshot
            or getattr(self.task, "limits", {}) != self._limits_snapshot
            or self.client.base_url != self._client_endpoint_snapshot
            or self.client.model != self.lifecycle._model_requested
            or self.client.provider_name != self.owner.plan["provider"]
            or self._route_snapshot
            != (
                self.lifecycle._route_id,
                self.lifecycle._provider_name,
                self.lifecycle._budget_route,
                self.lifecycle._model_resolution,
            )
            or canonical_sha256(self.lifecycle._observation_context) != self._context_sha256
            or _budget_policy_projection(self.lifecycle._budget_policy)
            != _budget_policy_projection(self.owner.service.budget)
        ):
            raise OwnerResearchRejected("registered_runtime_inputs_changed")
        self.owner.check()

    def _validate_envelope(self, envelope):
        self._assert_registered_inputs()
        if self._task_binding is None:
            raise OwnerResearchRejected("runtime_task_binding_required")
        dynamic = envelope.get("dynamic_context")
        if dynamic is None:  # Existing search request carrier.
            dynamic = envelope
        if (
            dynamic.get("company_ref") != self.task.company_ref
            or dynamic.get("company") != self.task.company_doc
            or dynamic.get("information_cutoff") != self.owner.plan["information_cutoff"]
        ):
            raise OwnerResearchRejected("runtime_subject_or_cutoff_mismatch")
        if (
            "stable_prefix" in envelope
            and envelope["stable_prefix"].get("questions") != self.task.questions
        ):
            raise OwnerResearchRejected("runtime_original_question_contract_changed")
        if "dynamic_context" in envelope:
            for key in ("stable_prefix", "dynamic_context"):
                if canonical_sha256(envelope[key]) != envelope.get(key + "_sha256"):
                    raise OwnerResearchRejected("runtime_envelope_hash_mismatch")
            selected = dynamic.get("requested_question_ids")
            if (
                not isinstance(selected, list)
                or not selected
                or len(set(selected)) != len(selected)
                or not set(selected) <= set(self.questions)
            ):
                raise OwnerResearchRejected("runtime_question_scope_mismatch")
            if "intent" in envelope and envelope["intent"].get("question_ids") != selected:
                raise OwnerResearchRejected("runtime_question_intent_mismatch")

    @contextmanager
    def generation_scope(self, envelope):
        from src.utils.quick_scan_work_transport import (
            bind_quick_scan_budget,
            bind_quick_scan_route,
        )

        self._validate_envelope(envelope)
        if self._active:
            raise OwnerResearchRejected("owner_generation_already_active")
        selected = envelope["dynamic_context"]["requested_question_ids"]
        if len(selected) > 1 and self.native_session:
            raise OwnerResearchRejected("native_multiturn_question_pack_not_supported")
        if (
            any(
                self._handles.get(qid, {}).get("work_item_id") in self._responded_work_ids
                for qid in selected
            )
            and not self.native_session
        ):
            raise OwnerResearchRejected("native_multiturn_registration_required")
        for qid in selected:
            if qid not in self._handles:
                question = self.questions[qid]
                handle = self.lifecycle.before_question(question)
                if handle.get("claimed") is not True:
                    # Earlier claims stay native leased work. Never release or
                    # re-create them just to make a later request go through.
                    raise OwnerResearchRejected(
                        "original_work_not_dispatchable:" + str(handle.get("reason"))
                    )
                self._handles[qid] = handle
        self._active = True
        try:
            with ExitStack() as stack:
                policy, budget_route = self.lifecycle._budget_policy, self.lifecycle._budget_route
                if not isinstance(policy, dict) or not isinstance(budget_route, dict):
                    raise OwnerResearchRejected("approved_native_budget_route_required")
                handles = [self._handles[qid] for qid in selected]
                binding = (
                    self.lifecycle.dispatch_context(handles[0])
                    if len(handles) == 1
                    else self.lifecycle.dispatch_question_pack_context(handles)
                )
                stack.enter_context(binding)
                # Single-question lifecycle supplies the work/owner/subject
                # context; preserve the approved policy's original quota group
                # when binding its native metered research transport.
                stack.enter_context(
                    bind_quick_scan_route(
                        route_id=self.lifecycle._route_id,
                        provider=self.lifecycle._provider_name,
                        model_requested=self.client.model,
                        model_resolution=self.lifecycle._model_resolution,
                        quota_group=budget_route["quota_group"],
                    )
                )
                if self.native_session:
                    if len(handles) != 1:
                        raise OwnerResearchRejected("native_multiturn_question_pack_not_supported")
                    from src.utils import quick_scan_research_session as native_sessions

                    handle = handles[0]
                    session_id = native_sessions.register(
                        self.store,
                        self.owner,
                        handle["work_item_id"],
                        handle["lease"],
                        task_sha256=canonical_sha256(self.task.raw),
                        route_id=self.lifecycle._route_id,
                        provider=self.lifecycle._provider_name,
                        provider_actual=self.client.provider_name,
                        model_requested=self.client.model,
                        max_turns=self.native_max_turns,
                        model_resolution=self.lifecycle._model_resolution,
                    )
                    if self._native_session_ids.get(handle["work_item_id"]) not in (
                        None,
                        session_id,
                    ):
                        raise OwnerResearchRejected("native_session_registration_changed")
                    self._native_session_ids[handle["work_item_id"]] = session_id
                    stack.enter_context(
                        native_sessions.bind(
                            self.store,
                            handle["work_item_id"],
                            handle["lease"],
                            session_id,
                            intent_sha256=canonical_sha256(envelope["intent"]),
                        )
                    )
                from src.utils.quick_scan_research_intent import bind_intent

                stack.enter_context(
                    bind_intent(
                        self.store,
                        self._task_sha256,
                        canonical_sha256(envelope["intent"]),
                        [h["work_item_id"] for h in handles],
                    )
                )
                stack.enter_context(
                    bind_quick_scan_budget(self.store, policy, cost_resolver=self.cost_resolver)
                )
                yield
        finally:
            self._active = False

    def client_for(self, envelope):
        self._validate_envelope(envelope)
        return self.client

    def search(self, envelope):
        self._validate_envelope(envelope)
        if self.search_service is None:
            raise OwnerResearchRejected("original_search_service_not_configured")
        # A composition supplies its existing search coordinator, never a
        # provider HTTP implementation in this adapter.
        return self.search_service.search(copy.deepcopy(envelope))

    def _accept_native(self, receipt):
        self._assert_registered_inputs()
        if not isinstance(receipt, dict) or not isinstance(receipt.get("attempt_id"), str):
            raise OwnerResearchRejected("original_native_attempt_receipt_required")
        sdk_id = receipt["attempt_id"]
        original = None
        native_work_ids = set()
        for handle in self._handles.values():
            for attempt in self.store.list_attempts(handle["work_item_id"]):
                response = self.store.get_attempt_response(attempt["attempt_id"])
                if response is not None and response["receipt"].get("attempt_id") == sdk_id:
                    if _sanitized_receipt(receipt) != response["receipt"]:
                        raise OwnerResearchRejected("native_receipt_differs_from_original_owner")
                    original = response["receipt"]
                    native_work_ids.add(handle["work_item_id"])
        if original is None:
            # Failed transport may carry a known wire receipt but cannot be
            # declared replayable without original durable response proof.
            original = _sanitized_receipt(receipt)
            physical = receipt.get("work_attempt_id")
            known = {
                a["attempt_id"]: a
                for h in self._handles.values()
                for a in self.store.list_attempts(h["work_item_id"])
            }
            matches = [
                a
                for key, a in known.items()
                if (physical is None or key == physical)
                and a.get("receipt_sha256") == quick_scan_receipt_sha256(receipt)
            ]
            if not matches:
                raise OwnerResearchRejected("native_attempt_not_owned_by_registered_work")
            native_work_ids.update(a["work_item_id"] for a in matches)
        ref = "stockqa:" + sdk_id
        entry = {"runtime_attempt_ref": ref, "native_receipt": copy.deepcopy(original)}
        if ref in self._observations and self._observations[ref] != entry:
            raise OwnerResearchRejected("native_attempt_reference_conflict")
        self._observations[ref] = entry
        self._replayed_refs.setdefault(ref, False)
        self._responded_work_ids.update(native_work_ids)
        return ref

    def observe_native_step(self, step):
        return self._accept_native(step.get("receipt"))

    def observe_native_failure(self, error):
        receipt = getattr(error, "attempt_receipt", None)
        return None if not receipt else self._accept_native(receipt)

    def attempt_observations(self, *, transport_origin, replayed=None):
        self._assert_registered_inputs()
        if transport_origin not in {"stockqa_live", "scripted_wire"} or (
            replayed is not None and type(replayed) is not bool
        ):
            raise OwnerResearchRejected("explicit_transport_origin_and_replay_required")
        if replayed is not None and any(v != replayed for v in self._replayed_refs.values()):
            raise OwnerResearchRejected("replay_classification_conflicts_with_native_observation")
        return [
            dict(
                copy.deepcopy(entry),
                transport_origin=transport_origin,
                replayed=self._replayed_refs[ref],
            )
            for ref, entry in self._observations.items()
        ]

    def _known_native(self, ref):
        if not isinstance(ref, str) or not ref.startswith("stockqa:"):
            return None
        sdk_id = ref[len("stockqa:") :]
        # Scope before lookup. A valid foreign SDK UUID is not sufficient.
        for qid, entry in self.owner.entries.items():
            if qid not in self.questions:
                continue
            if entry.get("kind") != "work":
                continue
            for attempt in self.store.list_attempts(entry["work_item_id"]):
                response = self.store.get_attempt_response(attempt["attempt_id"])
                if response and response["receipt"].get("attempt_id") == sdk_id:
                    return {
                        **response,
                        "native_attempt": {
                            key: attempt[key]
                            for key in ("attempt_id", "phase", "send_intent_at", "completed_at")
                        },
                    }
        return None

    def inspect_attempt(self, ref):
        self._assert_registered_inputs()
        response = self._known_native(ref)
        if response is None:
            return dict(attempt_ref=ref, known=False, outcome_recorded=None, response_saved=None)
        visible = self.store.get_research_visible_output(response["receipt"]["attempt_id"])
        if ref not in self._observations:
            self._observations[ref] = dict(
                runtime_attempt_ref=ref, native_receipt=copy.deepcopy(response["receipt"])
            )
            self._replayed_refs[ref] = True
        saved = (
            None
            if visible is None
            else self._restore_model_receipt(
                ref, response["receipt"], visible, response["native_attempt"]
            )
        )
        return dict(
            attempt_ref=ref,
            known=True,
            outcome_recorded=response["native_attempt"]["phase"],
            response_saved=saved,
            native_receipt=copy.deepcopy(response["receipt"]),
            native_visible_output=visible,
        )

    def _restore_model_receipt(self, ref, native, visible, native_attempt):
        from datetime import datetime, timezone
        from importlib import import_module

        try:
            mapping = import_module("iqs_agent_runtime_lab.receiptmap")
            validation = import_module("iqs_company_agent_lab.receipts")
        except ModuleNotFoundError as error:
            raise OwnerResearchRejected("shared_runtime_receipt_adapter_not_installed") from error

        observation = None
        usage = native.get("usage")
        names = (
            "input_tokens",
            "cached_input_tokens",
            "cache_creation_input_tokens",
            "output_tokens",
        )
        if isinstance(usage, dict) and all(
            type(usage.get(k)) is int and usage[k] >= 0 for k in names
        ):
            observation = dict(
                prompt_tokens=sum(usage[k] for k in names[:3]),
                cached_input_tokens=usage["cached_input_tokens"],
                output_tokens_including_reasoning=usage["output_tokens"],
                reasoning_output_tokens=usage.get("reasoning_output_tokens"),
            )
        # The immutable native receipt intentionally omits its private SDK
        # start field. The original attempt's persisted send-intent clock is
        # the auditable timing source. This interval includes native guards;
        # it is historical native execution time, never new cache-read HTTP.
        sent_at = native_attempt.get("send_intent_at")
        if type(sent_at) not in (int, float):
            raise OwnerResearchRejected("original_native_dispatch_clock_unavailable")
        started = datetime.fromtimestamp(sent_at, timezone.utc).isoformat().replace("+00:00", "Z")
        ended = native["completed_at"]
        if datetime.fromisoformat(ended.replace("Z", "+00:00")) < datetime.fromisoformat(
            started.replace("Z", "+00:00")
        ):
            raise OwnerResearchRejected("original_native_dispatch_clock_inconsistent")
        duration = max(
            1,
            round(
                (
                    datetime.fromisoformat(ended.replace("Z", "+00:00"))
                    - datetime.fromisoformat(started.replace("Z", "+00:00"))
                ).total_seconds()
                * 1000
            ),
        )
        output = visible["visible_output"]
        saved = mapping.build_model_receipt(
            attempt_ref=ref,
            planned_model=native["requested_model"],
            actual_model=native.get("actual_model"),
            endpoint_profile_id=self.client.model + "@" + self.client.base_url,
            started_at_utc=started,
            ended_at_utc=ended,
            duration_ms=duration,
            final_content=output["content"] or None,
            tool_calls=mapping.tool_calls_to_receipt(output["tool_calls"]),
            usage=mapping.normalize_native_usage(observation),
            finish_reason=output["finish_reason"],
            session_handle=None,
        )
        validation.validate_model_receipt(saved)
        return saved

    def request_resume(self, ref):
        inspected = self.inspect_attempt(ref)
        return dict(
            attempt_ref=ref,
            decision=(
                "recover_saved_receipt" if inspected["response_saved"] else "pause_for_inspection"
            ),
            reason=(
                "original_safe_response_available"
                if inspected["response_saved"]
                else "no_native_replay_proof_never_resend"
            ),
            saved_receipt=inspected["response_saved"],
        )

    def recover_intent(self, intent):
        """Read exact native task/work/intent after process loss; never POST."""
        self._assert_registered_inputs()
        if not isinstance(intent, dict):
            raise OwnerResearchRejected("registered_runtime_intent_required")
        selected = intent.get("question_ids")
        if (
            not isinstance(selected, list)
            or not selected
            or len(set(selected)) != len(selected)
            or not set(selected) <= set(self.questions)
        ):
            raise OwnerResearchRejected("native_recovery_registered_question_scope_required")
        entries = [self.owner.entries[qid] for qid in selected]
        if any(entry.get("kind") != "work" for entry in entries):
            raise OwnerResearchRejected("native_recovery_original_work_required")
        from src.utils.quick_scan_research_intent import get_intent_response

        generic = get_intent_response(self.store, self._task_sha256, canonical_sha256(intent))
        if generic is not None:
            if len(generic["work_item_ids"]) != len(entries) or set(generic["work_item_ids"]) != {
                entry["work_item_id"] for entry in entries
            }:
                raise OwnerResearchRejected("native_intent_original_work_scope_mismatch")
            if generic.get("native_receipt") is None:
                return dict(
                    decision="pause_for_inspection",
                    saved_receipt=None,
                    reason="original_send_exists_without_replay_proof",
                    native_attempt_id=generic["native_attempt_id"],
                )
            return self.request_resume("stockqa:" + generic["native_receipt"]["attempt_id"])
        if len(entries) != 1:
            return None
        entry = entries[0]
        from src.utils.quick_scan_research_session import find_attempt_for_task_intent

        original = find_attempt_for_task_intent(
            self.store, entry["work_item_id"], self._task_sha256, canonical_sha256(intent)
        )
        if original is None:
            return None
        response = self.store.get_attempt_response(original["attempt"]["attempt_id"])
        if response is None:
            return dict(
                decision="pause_for_inspection",
                saved_receipt=None,
                reason="original_send_exists_without_replay_proof",
                native_attempt_id=original["attempt"]["attempt_id"],
            )
        return self.request_resume("stockqa:" + response["receipt"]["attempt_id"])

    def commit_standard_answers(self, answers, *, native_reference):
        """Use original native checkpoints; does not manufacture search proof.

        Answer contract/schema/evidence failures are enforced by the original
        lifecycle and stay recoverable. An expired lease remains refused.
        """
        import json

        self._assert_registered_inputs()
        response = self._known_native(native_reference)
        if response is None:
            raise OwnerResearchRejected("original_response_required_for_checkpoint")
        if not isinstance(answers, list) or not answers:
            raise OwnerResearchRejected("nonempty_standard_answers_required")
        qids = [a.get("question_id") for a in answers if isinstance(a, dict)]
        if (
            len(qids) != len(answers)
            or len(set(qids)) != len(qids)
            or not set(qids) <= set(self.questions)
        ):
            raise OwnerResearchRejected("standard_answer_scope_mismatch")
        native = response["receipt"]
        saved = []
        for body in answers:
            qid = body["question_id"]
            handle = self._handles.get(qid)
            if handle is None:
                raise OwnerResearchRejected("original_active_work_lease_required")
            attempts = self.store.list_attempts(handle["work_item_id"])
            matches = [
                a
                for a in attempts
                if (self.store.get_attempt_response(a["attempt_id"]) or {})
                .get("receipt", {})
                .get("attempt_id")
                == native["attempt_id"]
            ]
            if len(matches) != 1:
                raise OwnerResearchRejected("checkpoint_native_member_mapping_required")
            attempt_id = matches[0]["attempt_id"]
            execution = copy.deepcopy(native)
            execution["work_transport"] = dict(
                work_attempt_id=attempt_id, final_receipt=copy.deepcopy(native)
            )
            use = self.store.get_external_context_use(attempt_id)
            if use is not None:
                execution["external_context_use"] = copy.deepcopy(use["proof"])
            result = QAResult(
                self.questions[qid],
                Answer(
                    json.dumps(body, ensure_ascii=False),
                    score=body.get("score"),
                    status=body.get("status"),
                    source="llm",
                    metadata={"execution": execution},
                ),
            )
            self.lifecycle.after_question(handle, result)
            saved.append(qid)
        return dict(
            saved_question_ids=saved,
            native_reference=native_reference,
            financial_accuracy_assessed=False,
        )
