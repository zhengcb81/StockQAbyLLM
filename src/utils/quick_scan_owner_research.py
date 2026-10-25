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


def _native_batch_scope(store, task, entries, sdk_id):
    """Read original task/intent/receipt/member bindings on every cache intake."""
    if task.raw.get("protocol") != "iqs.company-scan-task/1.0.0":
        return None
    from contextlib import closing
    from importlib import import_module

    batch_response_scope = import_module("iqs_agent_runtime_lab.receiptmap").batch_response_scope

    from src.utils.quick_scan_research_intent import get_intent_response

    if batch_response_scope(task) is None:
        return None
    with closing(store._connect()) as connection:
        rows = connection.execute(
            "SELECT i.intent_sha256 FROM quick_scan_research_intent i "
            "JOIN quick_scan_attempt_response r ON r.attempt_id=i.native_attempt_id "
            "WHERE i.task_sha256=? AND json_extract(r.receipt_json,'$.attempt_id')=?",
            (canonical_sha256(task.raw), sdk_id),
        ).fetchall()
    if len(rows) != 1:
        raise OwnerResearchRejected("batch_original_native_intent_required")
    original = get_intent_response(store, canonical_sha256(task.raw), rows[0][0])
    if (
        original is None
        or original.get("native_receipt") is None
        or original["native_receipt"].get("attempt_id") != sdk_id
    ):
        raise OwnerResearchRejected("batch_original_native_receipt_required")
    members = original["work_item_ids"]
    selected = [
        qid
        for qid in task.question_ids
        if entries.get(qid, {}).get("kind") == "work" and entries[qid]["work_item_id"] in members
    ]
    if len(selected) != len(members) or set(members) != {
        entries[q]["work_item_id"] for q in selected
    }:
        raise OwnerResearchRejected("batch_original_native_members_required")
    batch_response_scope(task, selected)
    return selected


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
        owner_batch_claim=False,
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
        if type(owner_batch_claim) is not bool or owner_batch_claim and native_session:
            raise OwnerResearchRejected("owner_batch_claim_controls_invalid")
        self.owner_batch_claim = self._owner_batch_claim_snapshot = owner_batch_claim
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
        self.subject_dispatch_readiness = self.preflight_subject_dispatch()

    def preflight_subject_dispatch(self):
        """Read the original owner's capability CLI before any paid retrieval.

        Readable schema 2 is deliberately insufficient for pre-send subject
        registration. This is initialization readiness only: every original
        owner check, registration, lease and budget guard still runs at send.
        """
        import os
        import subprocess  # nosec B404
        import sys
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from jsonschema import (
            Draft202012Validator,
            FormatChecker,
            SchemaError,
            ValidationError,
        )
        from referencing import Registry
        from referencing.exceptions import Unresolvable

        from src.utils.quick_scan_result_outbox import strict_json_loads

        self._assert_registered_inputs()
        original_client = self.owner.native.client
        config = original_client.config
        maximum = 8 * 1024 * 1024
        try:
            schema_path = (
                Path(config["iqs_code_root"]) / "schemas/quick_scan/query-capabilities.schema.json"
            )
            if schema_path.stat().st_size > maximum:
                raise OwnerResearchRejected("subject_dispatch_capability_schema_size")
            schema = strict_json_loads(schema_path.read_text(encoding="utf-8"))
            validator = Draft202012Validator(
                schema, registry=Registry(), format_checker=FormatChecker()
            )
            runs = Path(config["runs_dir"])
            runs.mkdir(parents=True, exist_ok=True)
            supplied = original_client._environment
            source_env = os.environ if supplied is None else supplied
            allowed = {
                "SYSTEMROOT",
                "WINDIR",
                "PATH",
                "PATHEXT",
                "COMSPEC",
                "USERPROFILE",
                "APPDATA",
                "LOCALAPPDATA",
                "SYSTEMDRIVE",
            }
            env = {key: value for key, value in source_env.items() if key.upper() in allowed}
            command = [
                sys.executable,
                "-B",
                "-X",
                "utf8",
                "-m",
                "stockwiki.quick_scan_query_v2_cli",
                "--root",
                config["stockwiki_workspace_root"],
                "--iqs-code-root",
                config["iqs_code_root"],
                "--capabilities",
                "--capabilities-version",
                "1.1.0",
            ]
            with TemporaryDirectory(prefix="subject-ready-", dir=runs) as temporary:
                env.update(
                    PYTHONDONTWRITEBYTECODE="1",
                    PYTHONUTF8="1",
                    PYTHONIOENCODING="utf-8",
                    PYTHONPATH=config["stockwiki_code_root"],
                    TEMP=temporary,
                    TMP=temporary,
                )
                # File-backed output avoids unbounded capture into memory. Only
                # the bounded public JSON is read; no stderr/body is propagated.
                with (Path(temporary) / "capabilities.json").open("w+b") as output:
                    completed = subprocess.run(  # nosec B603
                        command,
                        cwd=config["stockwiki_code_root"],
                        env=env,
                        stdout=output,
                        stderr=subprocess.DEVNULL,
                        timeout=240,
                        check=False,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    if completed.returncode != 0:
                        raise OwnerResearchRejected("subject_dispatch_capability_cli_failed")
                    output.flush()
                    if output.seek(0, 2) > maximum:
                        raise OwnerResearchRejected("subject_dispatch_capability_response_size")
                    output.seek(0)
                    response = strict_json_loads(output.read(maximum + 1).decode("utf-8"))
            validator.validate(response)
            if (
                response.get("protocol") != "stockwiki.c06_query_capabilities/1.1.0"
                or response.get("schema_version") != "1.1.0"
                or response.get("message_type") != "response"
                or response.get("operation") != "capabilities"
            ):
                raise OwnerResearchRejected("subject_dispatch_capability_envelope_invalid")
            result = response["result"]
            watermark = result["watermark"]
            observations = [
                mark
                for mark in watermark["source_watermarks"]
                if mark["namespace"] == "observations"
            ]
            native = [
                item
                for item in result["native_interfaces"]
                if item["purpose"] == "pre_send_subject_registration"
            ]
            if (
                len(observations) != 1
                or observations[0]["status"] != "available"
                or observations[0]["schema_version"] not in {3, 4}
            ):
                raise OwnerResearchRejected("subject_dispatch_initialization_required")
            if (
                result["store_id"] != watermark["store_id"]
                or result["dispatch_started"] is not False
                or type(result["API_requests"]) is not int
                or result["API_requests"] != 0
                or type(result["database_writes"]) is not int
                or result["database_writes"] != 0
                or len(native) != 1
                or native[0]["component"] != "StockWiki"
                or native[0]["entry_type"] != "module"
                or native[0]["entry_point"] != "stockwiki.quick_scan_subject_dispatch_cli"
                or native[0]["permission_boundary"]
                != "independent_current_route_manifest_owner_anchor_before_send"
                or not {
                    "--root",
                    "--iqs-code-root",
                    "--iqs-release-root",
                    "--operation",
                    "--request",
                    "--subject-key",
                    "--scope",
                    "--scope-id",
                    "--expected-decision-id",
                    "--expected-anchor-version",
                }.issubset(native[0]["selectors"])
            ):
                raise OwnerResearchRejected("subject_dispatch_capability_contract_mismatch")
            self._assert_registered_inputs()
            return dict(
                protocol="stockqa.subject_dispatch_readiness/1.0.0",
                capabilities_schema_version="1.1.0",
                store_id=result["store_id"],
                observed_at=response["response_at"],
                observation_schema_version=observations[0]["schema_version"],
                capabilities_sha256=canonical_sha256(response),
                dispatch_started=False,
                API_requests=0,
                database_writes=0,
                financial_accuracy_proven=False,
            )
        except OwnerResearchRejected:
            raise
        except (
            OSError,
            subprocess.TimeoutExpired,
            ValueError,
            TypeError,
            KeyError,
            UnicodeError,
            RecursionError,
            SchemaError,
            ValidationError,
            Unresolvable,
        ) as error:
            raise OwnerResearchRejected("subject_dispatch_readiness_unavailable") from error

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
        from .quick_scan_owner_budget_continuation import assert_research_task

        assert_research_task(self.owner, self.task, search_service=self.search_service)

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
            type(self.owner_batch_claim) is not bool
            or self.owner_batch_claim != self._owner_batch_claim_snapshot
            or canonical_sha256(self.task.raw) != self._task_sha256
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
            if self.task.raw.get("protocol") == "iqs.company-scan-task/1.0.0":
                from importlib import import_module

                batch_response_scope = import_module(
                    "iqs_agent_runtime_lab.receiptmap"
                ).batch_response_scope

                profile = batch_response_scope(self.task, selected)
                if profile is not None and (
                    dynamic.get("execution_profile") != profile
                    or dynamic.get("execution_profile_sha256")
                    != self.task.raw["execution_profile_sha256"]
                    or envelope.get("intent", {})
                    .get("payload", {})
                    .get("allowed_tools", {})
                    .get("tool_names")
                    != []
                ):
                    raise OwnerResearchRejected("runtime_original_batch_response_contract_required")

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
        unclaimed = [qid for qid in selected if qid not in self._handles]
        if self.owner_batch_claim and unclaimed:
            if len(unclaimed) != len(selected):
                raise OwnerResearchRejected("owner_batch_partial_handle_set")
            # One indivisible original admitted-work claim. No permission proof
            # is retained for the unchanged original transport/POST guards.
            handles = self.lifecycle.before_registered_questions(
                [self.questions[qid] for qid in selected]
            )
            self._handles.update(zip(selected, handles))
        else:
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
                policy, budget_route = (
                    self.lifecycle._budget_policy,
                    self.lifecycle._budget_route,
                )
                if not isinstance(policy, dict) or not isinstance(budget_route, dict):
                    raise OwnerResearchRejected("approved_native_budget_route_required")
                handles = [self._handles[qid] for qid in selected]
                binding = (
                    self.lifecycle.dispatch_context(handles[0], research_budget_managed=True)
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
                            for key in (
                                "attempt_id",
                                "phase",
                                "send_intent_at",
                                "completed_at",
                            )
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
                runtime_attempt_ref=ref,
                native_receipt=copy.deepcopy(response["receipt"]),
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
        selected = _native_batch_scope(
            self.store, self.task, self.owner.entries, native["attempt_id"]
        )
        projected = (
            output
            if selected is None
            else mapping.project_batch_response(
                self.task,
                selected,
                content=output["content"],
                tool_calls=output["tool_calls"],
                finish_reason=output["finish_reason"],
            )
        )
        saved = mapping.build_model_receipt(
            attempt_ref=ref,
            planned_model=native["requested_model"],
            actual_model=native.get("actual_model"),
            endpoint_profile_id=self.client.model + "@" + self.client.base_url,
            started_at_utc=started,
            ended_at_utc=ended,
            duration_ms=duration,
            final_content=projected["content"] or None,
            tool_calls=mapping.tool_calls_to_receipt(projected["tool_calls"]),
            usage=mapping.normalize_native_usage(observation),
            finish_reason=projected["finish_reason"],
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
            self.store,
            entry["work_item_id"],
            self._task_sha256,
            canonical_sha256(intent),
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

    def restore_finalization_handle(self, question_id, *, native_reference):
        """Restore an original live response handle without reclaiming or renewal.

        Only the registered question's own successful attempt may supply its
        original token/epoch. The store rechecks that same lease again when
        committing the answer, so a race cannot upgrade a stale handle.
        This changes in-memory handles only; it creates no dispatch or grant.
        """
        from src.utils.quick_scan_work_store import Lease

        self._assert_registered_inputs()
        entry = self.owner.entries.get(question_id)
        if (
            question_id not in self.questions
            or not isinstance(entry, dict)
            or entry.get("kind") != "work"
        ):
            raise OwnerResearchRejected("finalization_registered_question_required")
        response = self._known_native(native_reference)
        if response is None:
            raise OwnerResearchRejected("finalization_original_native_response_required")
        work_id = entry["work_item_id"]
        matches = [
            attempt
            for attempt in self.store.list_attempts(work_id)
            if (self.store.get_attempt_response(attempt["attempt_id"]) or {}).get("receipt")
            == response["receipt"]
        ]
        if len(matches) != 1 or matches[0]["phase"] != "response_available":
            raise OwnerResearchRejected("finalization_original_successful_member_required")
        attempt = matches[0]
        item = self.store.get_item(work_id)
        if (
            item["lease_token"] != attempt["lease_token"]
            or item["lease_epoch"] != attempt["lease_epoch"]
        ):
            raise OwnerResearchRejected("finalization_original_lease_changed")
        lease = Lease(attempt["lease_token"], attempt["lease_epoch"])
        self.store._assert_lease(item, lease, self.store._now())
        handle = dict(
            claimed=True,
            reason="restored_original_finalization_handle",
            work_item_id=work_id,
            lease=lease,
        )
        existing = self._handles.get(question_id)
        if existing is not None and (
            existing.get("work_item_id") != work_id or existing.get("lease") != lease
        ):
            raise OwnerResearchRejected("finalization_existing_handle_conflict")
        self._assert_registered_inputs()
        self._handles[question_id] = handle
        return dict(handle)

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
            entry = self.owner.entries.get(qid)
            if not isinstance(entry, dict) or entry.get("kind") != "work":
                raise OwnerResearchRejected("checkpoint_original_registered_work_required")
            work_id = entry["work_item_id"]
            checkpoint = self.store.get_answer_checkpoint(work_id)
            if checkpoint is not None:
                standard = self.store.get_standard_answer(work_id)
                original = self.store.get_attempt_response(checkpoint["attempt_id"])
                if (
                    not isinstance(standard, dict)
                    or standard.get("answer") != body
                    or original is None
                    or original["receipt"] != native
                    or checkpoint["payload"]["provenance"].get("provider_attempt_id")
                    != native["attempt_id"]
                ):
                    raise OwnerResearchRejected("checkpoint_existing_original_answer_conflict")
                # A process may die after the atomic answer save but before
                # delivery sealing. Reuse the exact saved answer, never a new
                # lease or model send, and let the original lifecycle reseal.
                self.lifecycle._seal_delivery(work_id)
                saved.append(qid)
                continue
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
