"""Permission/scope adapter for the ORIGINAL runner, not another executor.

Only a real original Q13 action plus current native consent can construct a
session. The full producer manifest remains immutable. References, delivery
and uncertain work never become a fresh generation under an old action.
"""

import copy
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from .quick_scan_owner_refresh import public_observation_reference
from .quick_scan_refresh_admission import (
    AdmissionRejected,
    RefreshAdmissionService,
    _snapshot,
    match_question,
)
from .quick_scan_refresh_batch import SPEC_FIELDS
from .quick_scan_result_outbox import canonical_bytes, canonical_sha256
from .quick_scan_work_store import _budget_policy_projection


def registered_scope(items, bundle, record, *, information_cutoff):
    """Pure mapping only. Its caller independently authenticates native records."""
    result = {}
    specs = record["inputs"]["specifications"]
    receipts = record["receipt"]["items"]
    if len(specs) != len(receipts):
        raise AdmissionRejected("refresh_execution_work_mismatch")
    for item in items:
        match_question(item, bundle, information_cutoff=information_cutoff)
        qid, ref = item["question_id"], item["subject_ref"]
        if qid in result:
            raise AdmissionRejected("refresh_execution_duplicate_question_scope")
        works = [
            (spec, receipt)
            for spec, receipt in zip(specs, receipts)
            if spec.get("entity_id") == ref["entity_id"]
            and spec.get("question_id") == qid
            and spec.get("scope") == ref["scope"]
            and spec.get("scope_id") == ref["scope_id"]
        ]
        refs = [
            row
            for row in record["inputs"].get("references", [])
            if row["question_id"] == qid
            and canonical_bytes(row["subject_ref"]) == canonical_bytes(ref)
        ]
        if len(works) + len(refs) != 1:
            raise AdmissionRejected("refresh_execution_registered_scope_missing_or_duplicate")
        if refs:
            descriptor = refs[0]
            if (
                item["decision"] != "reuse"
                or descriptor["field_id"] != item["field_id"]
                or descriptor["observation_id"] != item["reuse_observation_id"]
            ):
                raise AdmissionRejected("refresh_execution_reference_mismatch")
            result[qid] = dict(
                kind="reference",
                item=copy.deepcopy(item),
                reference=copy.deepcopy(descriptor),
            )
            continue
        spec, receipt = works[0]
        binding = spec.get("owner_refresh_binding", {})
        expected_routing = canonical_sha256(
            dict(
                subject_key=binding.get("subject_key"),
                request_identity_key=binding.get("request_identity_key"),
            )
        )
        if (
            set(spec) != SPEC_FIELDS
            or any(
                canonical_bytes(spec.get(key)) != canonical_bytes(value)
                for key, value in bundle["identity"].items()
            )
            or any(
                canonical_bytes(receipt.get(key)) != canonical_bytes(spec[key])
                for key in (
                    "entity_id",
                    "question_id",
                    "generation",
                    "scope",
                    "scope_id",
                )
            )
            or canonical_bytes(spec["generation"]) != canonical_bytes(item["generation"])
            or spec["question_fingerprint"]
            != hashlib.sha256(bundle["questions"][qid].text.strip().encode("utf-8")).hexdigest()
            or spec["routing_fingerprint"] != expected_routing
            or binding.get("protocol")
            not in {
                "stockqa.owner_refresh_binding/2.0.0",
                "stockqa.owner_refresh_binding/3.0.0",
                "stockqa.owner_refresh_binding/4.0.0",
            }
            or (
                binding.get("protocol")
                in {
                    "stockqa.owner_refresh_binding/3.0.0",
                    "stockqa.owner_refresh_binding/4.0.0",
                }
            )
            != (item.get("owner_control") is not None)
            or canonical_bytes(binding.get("owner_control"))
            != canonical_bytes(item.get("owner_control"))
            or binding.get("subject_context_schema")
            != "stockqa.quick_scan_observation_context/2.0.0"
            or binding.get("subject_key")
            != ref["analysis_subject_id"] + "@" + str(ref["analysis_subject_revision"])
            or binding.get("perimeter_sha256") != ref["perimeter_sha256"]
            or binding.get("manifest_raw_sha256") != bundle["manifest"]["manifest_sha256"]
            or binding.get("information_cutoff") != information_cutoff
            or any(
                canonical_bytes(binding.get(key)) != canonical_bytes(spec[key])
                for key in (
                    "entity_id",
                    "question_id",
                    "scope",
                    "scope_id",
                    "identity_revision",
                )
            )
            or canonical_bytes(binding.get("target_generation"))
            != canonical_bytes(spec["generation"])
        ):
            raise AdmissionRejected("refresh_execution_registered_work_binding_mismatch")
        result[qid] = dict(
            kind="work",
            item=copy.deepcopy(item),
            work_item_id=receipt["work_item_id"],
            specification=copy.deepcopy(spec),
        )
    if not result:
        raise AdmissionRejected("refresh_execution_empty_scope")
    return result


def prepare_execution(config_path, confirmation_id, *, input_paths, process_env=None):
    """Trusted original runner calls this after full manifest/context validation."""
    service = RefreshAdmissionService(config_path, process_env=process_env)
    proof, bundles, record = service.read_registered(confirmation_id)
    expected = {
        "manifest.json",
        "identity.json",
        "authority.json",
        "owner-config.json",
        "questions.json",
    }
    if set(input_paths) != expected:
        raise AdmissionRejected("refresh_execution_complete_inputs_required")
    raw = {name: _snapshot(path)[1] for name, path in input_paths.items()}
    selected = {
        key: bundle
        for key, bundle in bundles.items()
        if all(bundle["snapshots"][name][1] == raw[name] for name in expected)
    }
    if not selected:
        raise AdmissionRejected("refresh_execution_invocation_not_registered")
    # One invocation can cover entity/security fields of the same full bundle.
    # Different bundles must use their own original invocation; no name join.
    bundle = next(iter(selected.values()))
    items = [
        item
        for item in proof["preview"]["items"]
        if canonical_sha256(item["subject_ref"]) in selected
    ]
    entries = registered_scope(
        items,
        bundle,
        record,
        information_cutoff=proof["request"]["source_query"]["payload"]["information_cutoff"],
    )
    native = bundle["owner"].prepare(
        bundle["manifest"],
        service.store,
        identity=bundle["identity"],
        provider=proof["preview"]["provider"],
        model=proof["preview"]["model"],
        now=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        observation_context=bundle["authority"]["observation_context"],
    )
    return ApprovedRefreshSession(
        service, proof, bundles, bundle, record, entries, native, input_paths, raw
    )


class ApprovedRefreshSession:
    """Compose current owner session with immutable original action work IDs."""

    def __init__(self, service, proof, bundles, bundle, record, entries, native, input_paths, raw):
        self.service, self.proof, self.bundles, self.bundle, self.record = (
            service,
            proof,
            bundles,
            bundle,
            record,
        )
        self.entries, self.native = entries, native
        self.input_paths, self.input_raw = dict(input_paths), dict(raw)
        self.plan = copy.deepcopy(native.plan)  # Original owner plan/hash remains unchanged.
        self.scope_bindings = {qid: copy.deepcopy(native.scope_bindings[qid]) for qid in entries}
        self.fields, self.work, self.deferred = {}, {}, {}
        self._policy_check = None
        self._execution_controls = None
        self._refresh_states()

    def _refresh_states(self):
        self.work, self.deferred = {}, {}
        for qid, entry in self.entries.items():
            field = copy.deepcopy(self.native.fields[qid])
            if entry["kind"] == "reference":
                expected = entry["reference"]
                original = self.plan.get("reused_observations", {}).get(expected["observation_id"])
                if (
                    field["decision"] != "reuse"
                    or field["reuse_observation_id"] != expected["observation_id"]
                    or original is None
                    or original["payload_sha256"] != expected["payload_sha256"]
                ):
                    raise AdmissionRejected("refresh_execution_original_reference_unavailable")
            else:
                item = self.service.store.get_item(entry["work_item_id"])
                if item["status"] == "leased":
                    self.service.store.recover_expired(item["work_item_id"])
                    item = self.service.store.get_item(item["work_item_id"])
                self.work[qid] = item
                projected = self.native.projection.get(qid)
                if projected is None or projected["work_item_id"] != item["work_item_id"]:
                    self.deferred[qid] = "registered_work_superseded"
                    field["decision"] = "defer"
                else:
                    # Resuming the exact approved work, never the new generation
                    # a later owner plan might propose under this old action.
                    field.update(decision="resume_existing_work", generation=item["generation"])
                    binding = entry["specification"]["owner_refresh_binding"]
                    if binding["protocol"] in {
                        "stockqa.owner_refresh_binding/3.0.0",
                        "stockqa.owner_refresh_binding/4.0.0",
                    }:
                        field.update(
                            owner_control=copy.deepcopy(binding["owner_control"]),
                            request_identity_key=binding["request_identity_key"],
                        )
                if item["status"] == "delivered" and field["decision"] != "defer":
                    delivery = self.service.store.get_result_delivery(item["work_item_id"])
                    original = self.plan.get("reused_observations", {}).get(
                        field.get("reuse_observation_id")
                    )
                    if (
                        self.native.fields[qid]["decision"] == "reuse"
                        and delivery
                        and original
                        and delivery.get("observation_id") == original["observation_id"]
                        and delivery.get("payload_sha256") == original["payload_sha256"]
                    ):
                        field = copy.deepcopy(self.native.fields[qid])
                    else:
                        self.deferred[qid] = "original_delivery_historical_or_refresh_required"
            self.fields[qid] = field

    @property
    def requested_question_ids(self):
        return [
            row["id"] for row in self.bundle["manifest"]["questions"] if row["id"] in self.entries
        ]

    @property
    def needs_new_dispatch(self):
        return any(
            item["status"] == "pending" and qid not in self.deferred
            for qid, item in self.work.items()
        )

    def check(self):
        if (
            self.proof["request"]["protocol"]
            in {"iqs.refresh_request/1.2.0", "iqs.refresh_request/1.3.0"}
            and self.needs_new_dispatch
        ):
            controls = getattr(self, "_execution_controls", None)
            if not isinstance(controls, dict):
                raise AdmissionRejected("refresh_execution_approved_controls_required")
            self.assert_execution_controls(**controls)
        self.service._inputs_unchanged(self.bundles)
        if any(
            _snapshot(path)[1] != self.input_raw[name] for name, path in self.input_paths.items()
        ):
            raise AdmissionRejected("refresh_execution_invocation_changed")
        current = self.service.bridge.inspect(
            self.proof["confirmation"]["confirmation_id"], mode="current_permissions"
        )
        if any(
            canonical_bytes(current[key]) != canonical_bytes(self.proof[key])
            for key in ("confirmation", "preview", "owner_policy")
        ):
            raise AdmissionRejected("refresh_execution_current_permission_changed")
        record = self.service._record(self.proof["confirmation"]["confirmation_id"])
        if record is None or canonical_bytes(record["inputs"]) != canonical_bytes(
            self.record["inputs"]
        ):
            raise AdmissionRejected("refresh_execution_original_action_changed")
        if self._policy_check is not None:
            self._policy_check()
        self.native.check()

    def claim_registered_question_batch(
        self,
        question_ids,
        *,
        run_id,
        scan_id,
        lease_seconds,
        dispatch_deadline=None,
    ):
        """One fresh owner check for one exact native atomic claim action.

        This does not bind or memoize permission for transport. Every subsequent
        SDK POST retains its separate original current-owner/budget checks.
        """
        if (
            not isinstance(question_ids, (list, tuple))
            or not 1 <= len(question_ids) <= 30
            or any(not isinstance(qid, str) for qid in question_ids)
            or len(set(question_ids)) != len(question_ids)
        ):
            raise AdmissionRejected("refresh_batch_exact_questions_required")
        if any(
            qid not in self.entries
            or self.entries[qid]["kind"] != "work"
            or qid in self.deferred
            or self.fields[qid]["decision"]
            not in {
                "dispatch_new_work",
                "dispatch_new_generation",
                "resume_existing_work",
            }
            for qid in question_ids
        ):
            raise AdmissionRejected("refresh_batch_registered_work_scope_required")
        registered = {
            item["work_item_id"]: spec
            for spec, item in zip(
                self.record["inputs"]["specifications"], self.record["receipt"]["items"]
            )
        }
        if any(
            self.entries[qid]["specification"]["question_id"] != qid
            or self.entries[qid]["work_item_id"] not in registered
            or canonical_bytes(self.entries[qid]["specification"])
            != canonical_bytes(registered[self.entries[qid]["work_item_id"]])
            for qid in question_ids
        ):
            raise AdmissionRejected("refresh_batch_original_entry_changed")
        self.check()
        result = self.service.store.claim_refresh_action_batch(
            confirmation_id=self.proof["confirmation"]["confirmation_id"],
            expected_input_sha256=canonical_sha256(self.record["inputs"]),
            work_item_ids=[self.entries[qid]["work_item_id"] for qid in question_ids],
            run_id=run_id,
            scan_id=scan_id,
            lease_seconds=lease_seconds,
            dispatch_deadline=dispatch_deadline,
        )
        return [dict(claimed=True, reason="claimed", **handle) for handle in result]

    def assert_execution_controls(self, *, pack_size, thinking_enabled, max_output_tokens):
        if self.proof["request"]["protocol"] not in {
            "iqs.refresh_request/1.2.0",
            "iqs.refresh_request/1.3.0",
        }:
            return
        actual = dict(
            pack_size=pack_size,
            thinking_enabled=thinking_enabled,
            max_output_tokens=max_output_tokens,
        )
        for entry in self.entries.values():
            if entry["kind"] != "work":
                raise AdmissionRejected("refresh_execution_approved_controls_scope_invalid")
            binding = entry["specification"]["owner_refresh_binding"]
            control = binding.get("owner_control")
            expected_version = (
                "4.0.0"
                if self.proof["request"]["protocol"] == "iqs.refresh_request/1.3.0"
                else "3.0.0"
            )
            if (
                binding["protocol"] != "stockqa.owner_refresh_binding/" + expected_version
                or not isinstance(control, dict)
                or control["cohort_id"] != self.proof["request"]["cohort_id"]
            ):
                raise AdmissionRejected("refresh_execution_approved_controls_scope_invalid")
            expected = control["comparison_controls"]
            if expected is not None and (
                any(type(actual[k]) is not type(v) for k, v in expected.items())
                or actual != expected
            ):
                raise AdmissionRejected("refresh_execution_approved_controls_mismatch")
        self._execution_controls = actual

    def assert_executor_policy(self, config_file, policy, *, policy_reader=None):
        if (
            Path(config_file).absolute().parent / "quick_scan_work.sqlite"
        ).resolve() != self.service.store.path.resolve():
            raise AdmissionRejected("refresh_execution_original_ledger_required")
        expected = _budget_policy_projection(self.service.budget)
        if canonical_bytes(_budget_policy_projection(policy)) != canonical_bytes(expected):
            raise AdmissionRejected("refresh_execution_confirmed_model_policy_required")
        if policy_reader is not None:

            def check_policy():
                if canonical_bytes(_budget_policy_projection(policy_reader())) != canonical_bytes(
                    expected
                ):
                    raise AdmissionRejected("refresh_execution_model_policy_changed")

            self._policy_check = check_policy

    def assert_provider_model(self, provider, model):
        if provider != self.proof["preview"]["provider"] or model != self.proof["preview"]["model"]:
            raise AdmissionRejected("refresh_execution_primary_route_changed")

    def binding(self, qid):
        if qid not in self.entries:
            raise AdmissionRejected("refresh_execution_question_not_approved")
        return None  # Every approved work already exists in the original Q13.

    def execution_routes(self, policy):
        """Select approved targets without editing the original budget snapshot."""
        if self.proof["request"]["protocol"] != "iqs.refresh_request/1.3.0":
            return copy.deepcopy(policy["routes"])
        expected = self.proof["owner_policy"]
        qa = expected["qa"]
        selected = [
            route
            for route in policy["routes"]
            if route["id"] == qa["route_id"]
            and route["provider_config_ref"] == qa["provider_config_ref"]
            and route["model"] == expected["model"]
            and route.get("eligible") is True
        ]
        if len(selected) != 1 or policy["policy_id"] != qa["policy_id"]:
            raise AdmissionRejected("refresh_execution_target_route_unavailable")
        for entry in self.entries.values():
            if entry["kind"] != "work":
                raise AdmissionRejected("refresh_execution_target_scope_invalid")
            target = entry["specification"]["owner_refresh_binding"]["owner_control"].get(
                "comparison_target"
            )
            if target != dict(
                provider=expected["provider"],
                model=expected["model"],
                route_id=qa["route_id"],
                provider_config_ref=qa["provider_config_ref"],
                policy_id=qa["policy_id"],
                owner_policy_sha256=canonical_sha256(expected),
            ):
                raise AdmissionRejected("refresh_execution_target_not_approved")
        return copy.deepcopy(selected)

    def existing_work(self, qid):
        entry = self.entries.get(qid)
        if entry is None or entry["kind"] != "work":
            raise AdmissionRejected("refresh_execution_question_not_approved")
        return self.service.store.get_item(entry["work_item_id"])

    def reference(self, question):
        qid = question.question_id
        if qid not in self.entries:
            raise AdmissionRejected("refresh_execution_question_not_approved")
        if self.fields[qid]["decision"] != "reuse":
            return None
        self.check()
        return self.native.reference(question)

    def register_subject_dispatch(self, request):
        qid = request.get("question_id")
        if (
            qid not in self.entries
            or self.entries[qid]["kind"] != "work"
            or canonical_bytes(request.get("subject_ref"))
            != canonical_bytes(self.entries[qid]["item"]["subject_ref"])
        ):
            raise AdmissionRejected("refresh_execution_dispatch_scope_not_approved")
        self.check()
        return self.native.register_subject_dispatch(request)

    def dispatch_scope_report(self, batch):
        """Explicit subset denominator; never claim financial correctness."""
        requested = self.requested_question_ids
        statuses = {}
        for result in batch.results:
            qid = result.question.question_id
            if qid not in self.entries or qid in statuses:
                raise AdmissionRejected("refresh_execution_output_scope_mismatch")
            statuses[qid] = result.answer.status
        answered = [
            qid for qid in requested if statuses.get(qid) in {"scored", "not_applicable", "unknown"}
        ]
        complete = len(answered) == len(requested)
        return dict(
            protocol="stockqa.refresh_execution/1.0.0",
            mode="approved_scope_answers",
            confirmation_id=self.proof["confirmation"]["confirmation_id"],
            scope_sha256=self.proof["preview"]["scope_sha256"],
            reported_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            requested_question_ids=requested,
            requested_question_count=len(requested),
            manifest_question_count=len(self.bundle["manifest"]["questions"]),
            answered_question_ids=answered,
            answer_statuses=statuses,
            requested_scope_answered=complete,
            whole_manifest_answered=complete
            and len(requested) == len(self.bundle["manifest"]["questions"]),
            answer_completion_is_not_accuracy_or_qualification=True,
            original_observations_resealed=False,
        )

    def recovery_report(self):
        """Only original references or selected original delivery/reconciliation."""
        self.check()
        observations, deliveries, deferred = {}, [], []
        from .quick_scan_delivery_seal import seal_result_delivery

        for qid in self.requested_question_ids:
            if self.fields[qid]["decision"] == "reuse":
                result = self.reference(self.bundle["questions"][qid])
                observations[qid] = public_observation_reference(
                    result.answer.metadata["owner_observation_reference"]
                )
                continue
            item = self.existing_work(qid)
            delivery = self.service.store.get_result_delivery(item["work_item_id"])
            if qid in self.deferred:
                deferred.append(
                    dict(
                        question_id=qid,
                        work_item_id=item["work_item_id"],
                        status=item["status"],
                        reason=self.deferred[qid],
                    )
                )
                continue  # Never seal superseded work under the old action.
            if item["status"] == "result_ready" and (
                delivery is None or delivery.get("package") is None
            ):
                self.check()
                seal_result_delivery(
                    self.service.store,
                    item["work_item_id"],
                    authority=self.bundle["authority"],
                )
                delivery = self.service.store.get_result_delivery(item["work_item_id"])
            if delivery is not None:
                deliveries.append(
                    {
                        key: copy.deepcopy(delivery.get(key))
                        for key in (
                            "work_item_id",
                            "state",
                            "package_id",
                            "item_id",
                            "observation_id",
                            "payload_sha256",
                            "package_sha256",
                            "delivery_key",
                        )
                    }
                )
            if (
                qid in self.deferred
                or item["status"] not in {"result_ready", "delivered"}
                or delivery is None
                or delivery.get("package") is None
            ):
                deferred.append(
                    dict(
                        question_id=qid,
                        work_item_id=item["work_item_id"],
                        status=item["status"],
                        reason=self.deferred.get(
                            qid, "original_work_delivery_or_reconciliation_required"
                        ),
                    )
                )
        self.check()
        return dict(
            protocol="stockqa.refresh_execution/1.0.0",
            mode="original_references_and_recovery",
            confirmation_id=self.proof["confirmation"]["confirmation_id"],
            scope_sha256=self.proof["preview"]["scope_sha256"],
            requested_question_ids=self.requested_question_ids,
            manifest_question_count=len(self.bundle["manifest"]["questions"]),
            observation_references=observations,
            delivery_references=deliveries,
            deferred_work=deferred,
            model_dispatch_started=False,
            API_requests=0,
            original_observations_resealed=False,
            requested_scope_completed=not deferred
            and all(row["state"] == "delivered" for row in deliveries),
            whole_manifest_scanned=not deferred
            and not deliveries
            and len(self.entries) == len(self.bundle["manifest"]["questions"]),
        )
