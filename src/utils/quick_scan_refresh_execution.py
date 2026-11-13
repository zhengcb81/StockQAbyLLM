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
)
from .quick_scan_refresh_admission import api_refresh_admission_snapshot as _snapshot
from .quick_scan_refresh_admission import (
    match_question,
)
from .quick_scan_refresh_batch import SPEC_FIELDS
from .quick_scan_result_outbox import canonical_bytes, canonical_sha256
from .quick_scan_work_store import (
    api_work_store_budget_policy_projection as _budget_policy_projection,
)
from .quick_scan_work_store import (
    api_work_store_canonical_bindings as _canonical_bindings,
)


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
                "stockqa.owner_refresh_binding/5.0.0",
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


def prepare_execution(
    config_path,
    confirmation_id,
    *,
    input_paths,
    process_env=None,
    recovery_only=False,
    allow_unknown_only=False,
):
    """Trusted original runner calls this after full manifest/context validation."""
    service = RefreshAdmissionService(
        config_path,
        process_env=process_env,
        recovery_only=recovery_only,
        allow_unknown_only=allow_unknown_only,
    )
    proof, bundles, record = (
        service.read_registered(confirmation_id, recovery_only=True)
        if recovery_only
        else service.read_registered(confirmation_id)
    )
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
    ordered = proof["request"].get("protocol") == "iqs.refresh_request/1.6.0"
    for entry in entries.values():
        if entry["kind"] != "work":
            continue
        binding = entry["specification"]["owner_refresh_binding"]
        if (
            (binding["protocol"] == "stockqa.owner_refresh_binding/5.0.0") != ordered
            or ordered
            and canonical_bytes(binding["ordered_route_targets"])
            != canonical_bytes(proof["preview"]["ordered_route_targets"])
        ):
            raise AdmissionRejected("refresh_execution_ordered_binding_mismatch")
    if recovery_only:
        # Reuse references are already retained in the original journal/store.
        # This invocation preserves paid work only; today's cache TTL is irrelevant.
        entries = {qid: entry for qid, entry in entries.items() if entry["kind"] == "work"}
    native_options = {"recovery_entries": entries} if recovery_only else {}
    if ordered:
        native_options.update(
            ordered_route_targets=copy.deepcopy(proof["preview"]["ordered_route_targets"]),
            cache_clock=proof["owner_policy"].get("cache_clock", "information_as_of"),
        )
    native = bundle["owner"].prepare(
        bundle["manifest"],
        service.store,
        identity=bundle["identity"],
        provider=proof["preview"]["provider"],
        model=proof["preview"]["model"],
        now=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        observation_context=bundle["authority"]["observation_context"],
        **native_options,
    )
    session = ApprovedRefreshSession(
        service,
        proof,
        bundles,
        bundle,
        record,
        entries,
        native,
        input_paths,
        raw,
        recovery_only=recovery_only,
        allow_unknown_only=allow_unknown_only,
    )
    session.original_registered_question_count = len(items)
    if recovery_only:
        session.check()
    return session


class ApprovedRefreshSession:
    """Compose current owner session with immutable original action work IDs."""

    def __init__(
        self,
        service,
        proof,
        bundles,
        bundle,
        record,
        entries,
        native,
        input_paths,
        raw,
        *,
        recovery_only=False,
        allow_unknown_only=False,
    ):
        self.service, self.proof, self.bundles, self.bundle, self.record = (
            service,
            proof,
            bundles,
            bundle,
            record,
        )
        self.entries, self.native = entries, native
        self.original_registered_question_count = len(entries)
        self.input_paths, self.input_raw = dict(input_paths), dict(raw)
        self.plan = copy.deepcopy(native.plan)  # Original owner plan/hash remains unchanged.
        self.scope_bindings = {qid: copy.deepcopy(native.scope_bindings[qid]) for qid in entries}
        self.fields, self.work, self.deferred = {}, {}, {}
        self.recovery_only = recovery_only
        self.allow_unknown_only = allow_unknown_only
        self.recovery_work_item_ids = list(getattr(service, "recovery_work_item_ids", []))
        self.remaining_work_item_ids = list(getattr(service, "remaining_work_item_ids", []))
        self._policy_check = None
        self._execution_controls = None
        self._refresh_states()
        if self.recovery_only:
            self._assert_paid_responses()

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
                if item["status"] == "leased" and not self.recovery_only:
                    self.service.store.recover_expired(item["work_item_id"])
                    item = self.service.store.get_item(item["work_item_id"])
                self.work[qid] = item
                if self.recovery_only:
                    # Preserve this exact historical generation, even if a newer
                    # route/work now owns the current scanning slot.
                    field.update(decision="resume_existing_work", generation=item["generation"])
                    self.fields[qid] = field
                    continue
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
    def ordered_route_targets(self):
        if (
            getattr(self, "proof", {}).get("request", {}).get("protocol")
            != "iqs.refresh_request/1.6.0"
        ):
            return None
        return copy.deepcopy(self.proof["preview"]["ordered_route_targets"])

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
        if getattr(self, "recovery_only", False):
            # This operation cannot dispatch. Original immutable journal/response
            # identity is sufficient; today's permission, budget and actor are not.
            record = self.service._record(self.proof["confirmation"]["confirmation_id"])
            if (
                record is None
                or canonical_bytes(record["inputs"]) != canonical_bytes(self.record["inputs"])
                or canonical_bytes(record["receipt"]) != canonical_bytes(self.record["receipt"])
            ):
                raise AdmissionRejected("refresh_execution_original_action_changed")
            self._assert_paid_responses()
            return
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
            self.proof["confirmation"]["confirmation_id"],
            mode=(
                "paid_response_recovery"
                if getattr(self, "recovery_only", False)
                else "current_permissions"
            ),
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
        if getattr(self, "recovery_only", False):
            self._assert_paid_responses()

    def _assert_paid_responses(self):
        if not getattr(self, "recovery_work_item_ids", None):
            received, remaining = [], []
            for entry in self.entries.values():
                if entry.get("kind") != "work":
                    continue
                wid = entry["work_item_id"]
                attempts = self.service.store.list_attempts(wid)
                if (
                    attempts
                    and attempts[-1].get("phase") == "response_available"
                    and self.service.store.get_attempt_response(attempts[-1]["attempt_id"])
                    is not None
                ):
                    received.append(wid)
                else:
                    remaining.append(wid)
            if not received and not getattr(self, "allow_unknown_only", False):
                raise AdmissionRejected("paid_recovery_original_response_required")
            self.recovery_work_item_ids, self.remaining_work_item_ids = received, remaining
        for entry in self.entries.values():
            if entry.get("kind") != "work":
                continue
            work_id = entry["work_item_id"]
            if work_id not in getattr(self, "recovery_work_item_ids", [work_id]):
                continue
            work = self.service.store.get_item(work_id)
            attempts = self.service.store.list_attempts(work_id)
            if (
                work["status"] in {"pending", "uncertain"}
                or not attempts
                or attempts[-1].get("phase") != "response_available"
                or self.service.store.get_attempt_response(attempts[-1]["attempt_id"]) is None
            ):
                raise AdmissionRejected("paid_recovery_original_response_required")

    def _assert_original_registered_binding(self, qid, original_work, original_binding, *, store):
        """Additional pure resume fence after the runner's fresh ``check()``.

        It does not authenticate a caller or replace check/POST rechecks. Only
        the actual current registered journal may bridge a monotonic activation
        epoch to its unchanged original work binding; nothing is rewritten.
        """

        def reject():
            raise AdmissionRejected("refresh_execution_original_anchor_context_required")

        if store is not self.service.store or qid not in self.entries:
            reject()
        confirmation = self.proof["confirmation"]["confirmation_id"]
        record = self.service._record(confirmation)
        if (
            record is None
            or record["inputs"]["action"]["confirmation_id"] != confirmation
            or canonical_bytes(record["inputs"]) != canonical_bytes(self.record["inputs"])
            or canonical_bytes(record["receipt"]) != canonical_bytes(self.record["receipt"])
        ):
            reject()
        items = [item for item in self.proof["preview"]["items"] if item["question_id"] == qid]
        if len(items) != 1:
            reject()
        cutoff = self.proof["request"]["source_query"]["payload"]["information_cutoff"]
        approved = registered_scope(items, self.bundle, record, information_cutoff=cutoff)[qid]
        if approved["kind"] != "work" or canonical_bytes(approved) != canonical_bytes(
            self.entries[qid]
        ):
            reject()
        spec, work_id = approved["specification"], approved["work_item_id"]
        actual = store.get_item(work_id)
        persisted = store.get_owner_refresh_binding(work_id)
        if (
            original_work["work_item_id"] != work_id
            or persisted is None
            or canonical_bytes(persisted["binding"]) != canonical_bytes(original_binding)
            or canonical_bytes(spec["owner_refresh_binding"]) != canonical_bytes(original_binding)
            or actual["source_binding_refs_json"]
            != _canonical_bindings(spec["source_binding_ref"], spec["source_binding_refs"])
            or any(
                canonical_bytes(actual.get(k)) != canonical_bytes(v)
                for k, v in spec.items()
                if k not in {"owner_refresh_binding", "source_binding_refs"}
            )
        ):
            reject()
        if getattr(self, "recovery_only", False):
            return  # The exact immutable original binding above owns this saved result.
        old, current = original_binding["anchor_version"], self.plan["anchor_version"]
        if (
            type(old) is not int
            or type(current) is not int
            or not 1 <= old <= current
            or type(self.native.current["anchor_version"]) is not int
            or current != self.native.current["anchor_version"]
            or type(self.native.plan["anchor_version"]) is not int
            or current != self.native.plan["anchor_version"]
            or original_binding["decision_id"] != self.native.current["expected_decision_id"]
        ):
            reject()
        for key in (
            "manifest_raw_sha256",
            "decision_id",
            "subject_key",
            "perimeter_sha256",
            "provider",
            "model",
            "information_cutoff",
        ):
            if canonical_bytes(original_binding[key]) != canonical_bytes(
                self.plan[key]
            ) or canonical_bytes(original_binding[key]) != canonical_bytes(self.native.plan[key]):
                reject()
        if (
            original_binding["information_cutoff"] != cutoff
            or original_binding["provider"] != self.proof["preview"]["provider"]
            or original_binding["model"] != self.proof["preview"]["model"]
        ):
            reject()
        if original_binding[
            "protocol"
        ] == "stockqa.owner_refresh_binding/5.0.0" and canonical_bytes(
            original_binding["ordered_route_targets"]
        ) != canonical_bytes(
            self.ordered_route_targets
        ):
            reject()

    def assert_reactivated_original_binding(self, qid, original_work, original_binding, *, store):
        self._assert_original_registered_binding(qid, original_work, original_binding, store=store)
        if original_binding["anchor_version"] >= self.plan["anchor_version"]:
            raise AdmissionRejected("refresh_execution_original_anchor_context_required")

    def assert_received_checkpoint_recovery(self, work_item_id, attempt_id, *, store):
        """Current original owner scope, never permission from a saved body."""
        if store is not self.service.store:
            raise AdmissionRejected("received_checkpoint_original_store_required")
        entry = next(
            (
                entry
                for entry in self.entries.values()
                if entry.get("kind") == "work" and entry.get("work_item_id") == work_item_id
            ),
            None,
        )
        if entry is None:
            raise AdmissionRejected("received_checkpoint_registered_work_required")
        qid = entry["item"]["question_id"]
        if qid in self.deferred:
            raise AdmissionRejected("received_checkpoint_registered_work_superseded")
        self.check()
        work = store.get_item(work_item_id)
        original = store.get_owner_refresh_binding(work_item_id)
        if original is None or not any(
            a["attempt_id"] == attempt_id for a in store.list_attempts(work_item_id)
        ):
            raise AdmissionRejected("received_checkpoint_original_binding_required")
        self._assert_original_registered_binding(qid, work, original["binding"], store=store)

    def recover_received_checkpoints(self, *, capture_policy=None):
        """Re-decode only configured owner-captured, settled original pack bodies.

        No caller answers, new attempts, leases, budget reservation or HTTP.
        The diagnostic is data; this session remains the permission authority.
        """
        import re

        from src.providers.llm_response_parser import LLMResponseParser

        from .quick_scan_delivery_seal import (
            get_matching_sealed_delivery,
            seal_result_delivery,
        )
        from .quick_scan_observation_context import (
            bind_question_context,
            parse_standard_answer,
        )
        from .quick_scan_result_outbox import strict_json_loads
        from .quick_scan_work_store import Lease, LeaseFencedError, WorkConflictError

        directory = None if capture_policy is None else capture_policy.get("directory")
        self.check()
        store = self.service.store
        wanted = {e["work_item_id"]: qid for qid, e in self.entries.items() if e["kind"] == "work"}
        packs = {}
        attached = []
        remaining = []
        recovery_errors = []
        for work_id, qid in wanted.items():
            try:
                item = store.get_item(work_id)
                if item["status"] in {"result_ready", "delivered"}:
                    saved = store.get_answer_checkpoint(work_id)
                    if saved is not None:
                        self.assert_received_checkpoint_recovery(
                            work_id, saved["attempt_id"], store=store
                        )
                        attached.append(work_id)
                    continue
                if item["status"] != "failed" and not (
                    getattr(self, "recovery_only", False) and item["status"] == "leased"
                ):
                    remaining.append(work_id)
                    continue  # Pending/security and unknown/in-flight work are never dispatched here.
                attempts = store.list_attempts(work_id)
                if not attempts or store.get_attempt_response(attempts[-1]["attempt_id"]) is None:
                    remaining.append(work_id)
                    continue  # Unsent/unknown work cannot supply a response, or block another work.
                attempt = attempts[-1]
                self.assert_received_checkpoint_recovery(
                    work_id, attempt["attempt_id"], store=store
                )
                with store.transaction() as con:
                    member = con.execute(
                        "SELECT pack_id FROM quick_scan_question_pack_member WHERE attempt_id=?",
                        (attempt["attempt_id"],),
                    ).fetchone()
                if member is None:
                    raise AdmissionRejected("received_checkpoint_original_pack_required")
                pack = store.get_question_pack(member["pack_id"])
                if (
                    pack["physical_outcome"] != "response_available"
                    or pack["members_uncertain"]
                    or any(m["work_item_id"] not in wanted for m in pack["members"])
                ):
                    raise AdmissionRejected("received_checkpoint_exact_registered_pack_required")
                packs[pack["pack_id"]] = pack
            except (AdmissionRejected, WorkConflictError, LeaseFencedError) as error:
                if isinstance(error, AdmissionRejected) and not str(error).startswith(
                    "received_checkpoint_"
                ):
                    raise
                self.check()
                recovery_errors.append(dict(work_item_ids=[work_id], reason=str(error)))
        recovered = []

        def recover_pack(pack):
            original = store.get_attempt_response(pack["members"][0]["attempt_id"])
            receipt = original["receipt"]
            transport = dict(
                question_pack_id=pack["pack_id"],
                execution_attempt_id=pack["execution_attempt_id"],
                physical_budget_attempt_id=pack["budget_attempt_id"],
                members=[
                    dict(
                        question_id=m["question_id"],
                        work_item_id=m["work_item_id"],
                        work_attempt_id=m["attempt_id"],
                    )
                    for m in pack["members"]
                ],
            )
            matches: dict[str, tuple[Path, bytes, str]] = {}
            visible = store.get_research_visible_output(pack["budget_attempt_id"])
            if (
                visible is None
                and directory is not None
                and (
                    not isinstance(directory, Path)
                    or directory.is_symlink()
                    or directory.resolve() != directory.absolute()
                    or not directory.is_dir()
                )
            ):
                raise AdmissionRejected("received_checkpoint_capture_path_invalid")
            for path in (
                () if visible is not None or directory is None else directory.glob("*.final.json")
            ):
                if not re.fullmatch(r"[a-f0-9]{64}\.final\.json", path.name):
                    continue
                try:
                    doc, raw = _snapshot(path)
                except (ValueError, OSError):
                    continue  # Unrelated damaged captures are not this pack's response.
                if (
                    doc.get("response_sha256") != original["response_sha256"]
                    or doc.get("original_transport", {}).get("question_pack_id") != pack["pack_id"]
                ):
                    continue
                text = doc.get("final_answer")
                if not isinstance(text, str):
                    raise AdmissionRejected("received_checkpoint_original_body_unavailable")
                final_raw = text.encode("utf8")
                final_sha = hashlib.sha256(final_raw).hexdigest()
                if (
                    doc.get("record_kind") != "final_answer_diagnostic"
                    or doc.get("final_answer_retained") is not True
                    or doc.get("final_answer_sha256") != final_sha
                    or doc.get("final_answer_bytes") != len(final_raw)
                    or doc.get("original_transport") != transport
                    or any(
                        doc.get(k) != receipt.get(v)
                        for k, v in (
                            ("provider", "provider"),
                            ("actual_model", "actual_model"),
                            ("response_id", "response_id"),
                            ("response_status", "response_status"),
                        )
                    )
                ):
                    raise AdmissionRejected("received_checkpoint_original_body_binding_mismatch")
                matches.setdefault(final_sha, (path, raw, text))
            replay_path: Path | None
            if visible is not None:
                text = visible["visible_output"]["content"]
                replay_path, raw = None, None
            else:
                if len(matches) != 1:
                    raise AdmissionRejected("received_checkpoint_unique_original_body_required")
                replay_path, raw, text = next(iter(matches.values()))
            identity_doc = strict_json_loads(self.input_raw["identity.json"].decode("utf8"))
            parsed = LLMResponseParser().parse_question_pack_response(
                text,
                tuple(m["question_id"] for m in pack["members"]),
                expected_entity_id=self.bundle["identity"]["entity_id"],
                expected_company_name=identity_doc["payload"]["canonical_name"],
                standard_answer_objects=True,
            )
            if parsed is None:
                raise AdmissionRejected("received_checkpoint_original_body_invalid")
            answers = {a.question_id: a for a in parsed.answers}
            self.check()
            for member in pack["members"]:
                try:
                    with store.transaction():
                        if replay_path is not None and _snapshot(replay_path)[1] != raw:
                            raise AdmissionRejected("received_checkpoint_original_body_changed")
                        wid = member["work_item_id"]
                        qid = member["question_id"]
                        if qid not in answers:
                            remaining.append(wid)
                            continue
                        existing = store.get_answer_checkpoint(wid)
                        if existing is not None:
                            self.assert_received_checkpoint_recovery(
                                wid, existing["attempt_id"], store=store
                            )
                            attached.append(wid)
                            continue
                        answer = answers[qid]
                        frozen = store.get_observation_context(wid)
                        if frozen is None:
                            raise AdmissionRejected("received_checkpoint_original_context_required")
                        item = store.get_item(wid)
                        bind_question_context(
                            frozen["context"],
                            manifest=self.bundle["manifest"],
                            identity_snapshot_sha256=item["identity_snapshot_sha256"],
                            entity_id=item["entity_id"],
                            question_id=qid,
                            scope=item["scope"],
                            scope_id=item["scope_id"],
                        )
                        from .quick_scan_work_transport import (
                            parse_standard_pack_member,
                        )

                        try:
                            body = parse_standard_pack_member(
                                dict(
                                    question_id=qid,
                                    entity_id=answer.entity_id,
                                    status=answer.status,
                                    score=answer.score,
                                    description=answer.description,
                                    information_as_of=answer.information_as_of,
                                ),
                                context=frozen["context"]["questions"][qid],
                                source_urls=list(receipt.get("source_urls") or []),
                            )
                        except (ValueError, KeyError, TypeError):
                            remaining.append(wid)
                            continue
                        store.save_answer_checkpoint(
                            wid,
                            Lease(member["lease_token"], member["lease_epoch"]),
                            member["attempt_id"],
                            answer=dict(
                                entity_id=answer.entity_id,
                                question_id=qid,
                                status=answer.status,
                                score=answer.score,
                                description=body["summary"],
                            ),
                            execution_receipt=receipt,
                            observation_context=frozen["context"],
                            standard_answer=body,
                            _recovery_session=self,
                        )
                        if replay_path is not None and _snapshot(replay_path)[1] != raw:
                            raise AdmissionRejected("received_checkpoint_original_body_changed")
                        self.check()
                    # Publish progress only after this member transaction commits.
                    if item["status"] in {"failed", "leased"}:
                        recovered.append(wid)
                except (AdmissionRejected, WorkConflictError, LeaseFencedError) as error:
                    if isinstance(error, AdmissionRejected) and not str(error).startswith(
                        "received_checkpoint_"
                    ):
                        raise
                    self.check()
                    recovery_errors.append(
                        dict(work_item_ids=[member["work_item_id"]], reason=str(error))
                    )

        for pack in packs.values():
            self.check()
            try:
                recover_pack(pack)
            except (AdmissionRejected, WorkConflictError, LeaseFencedError) as error:
                # A known original-body problem belongs to this physical pack.
                # Global authority/SQLite failures still propagate. Work-local
                # conflict/fencing errors remain explicit and are never retried.
                if isinstance(error, AdmissionRejected) and not str(error).startswith(
                    "received_checkpoint_"
                ):
                    raise
                self.check()
                recovery_errors.append(
                    dict(
                        pack_id=pack["pack_id"],
                        reason=str(error),
                        work_item_ids=[m["work_item_id"] for m in pack["members"]],
                    )
                )
        saved_ids = set(recovered + attached)
        remaining = [wid for wid in wanted if wid not in saved_ids]
        deliveries = []
        delivery_pending = []
        ambiguous_delivery = set()
        for wid in dict.fromkeys(recovered + attached):
            self.check()
            try:
                delivery = seal_result_delivery(store, wid, authority=self.bundle["authority"])
            except (WorkConflictError, LeaseFencedError) as error:
                self.check()
                recovery_errors.append(
                    dict(stage="delivery", work_item_ids=[wid], reason=str(error))
                )
                current = get_matching_sealed_delivery(
                    store, wid, authority=self.bundle["authority"]
                )
                if current is not None:
                    deliveries.append(
                        dict(
                            action="already_sealed",
                            work_item_id=wid,
                            state=current["state"],
                            package_id=current["package_id"],
                        )
                    )
                continue
            deliveries.append(delivery)
            if delivery.get("action") in {"blocked", "left_in_flight"}:
                ambiguous_delivery.add(wid)
        delivered_ids = {row["work_item_id"] for row in deliveries}
        for wid in dict.fromkeys(recovered + attached):
            current = store.get_result_delivery(wid)
            if current is None or current["state"] not in {"ready", "delivered"}:
                delivery_pending.append(wid)
            elif (
                wid in ambiguous_delivery or wid not in delivered_ids
            ) and get_matching_sealed_delivery(
                store, wid, authority=self.bundle["authority"]
            ) is None:
                delivery_pending.append(wid)
        return dict(
            recovery_errors=recovery_errors,
            remaining_work_item_ids=remaining,
            delivery_pending_work_item_ids=delivery_pending,
            status="partial" if remaining or delivery_pending else "completed",
            recovered_work_item_ids=recovered,
            attached_work_item_ids=attached,
            deliveries=deliveries,
            API_requests=0,
            model_calls=0,
            new_attempts=0,
            new_generations=0,
            dispatch_started=False,
        )

    def restore_registered_checkpoint(self, question, *, store) -> dict | None:
        """Read a completed original answer, never claim or re-seal its context.

        The normal engine restores its original timestamp/score/provenance.
        Full original inputs stay separately available here and in the native
        outbox; new authority is never attached to an old answer.
        """
        qid = question.question_id
        entry = self.entries.get(qid)
        if (
            store is not self.service.store
            or entry is None
            or entry["kind"] != "work"
            or qid in self.deferred
            or self.fields[qid]["decision"] != "resume_existing_work"
        ):
            return None
        work = self.existing_work(qid)
        if work["status"] not in {"result_ready", "delivered"}:
            return None
        self.check()
        original = store.get_owner_refresh_binding(work["work_item_id"])
        if original is None:
            raise AdmissionRejected("refresh_checkpoint_original_binding_required")
        binding = original["binding"]
        self._assert_original_registered_binding(qid, work, binding, store=store)
        checkpoint = store.get_answer_checkpoint(work["work_item_id"])
        context = store.get_observation_context(work["work_item_id"])
        standard = store.get_standard_answer(work["work_item_id"])
        if checkpoint is None or context is None or standard is None:
            raise AdmissionRejected("refresh_checkpoint_original_complete_inputs_required")
        if (
            context["context_sha256"] != canonical_sha256(context["context"])
            or standard["answer_sha256"] != canonical_sha256(standard["answer"])
            or context["context"]["schema"] != "stockqa.quick_scan_observation_context/2.0.0"
            or hashlib.sha256(question.text.strip().encode("utf-8")).hexdigest()
            != entry["specification"]["question_fingerprint"]
        ):
            raise AdmissionRejected("refresh_checkpoint_original_inputs_changed")
        from .quick_scan_observation_context import (
            bind_question_context,
            frozen_question_contract,
            validate_standard_answer,
        )

        bound = bind_question_context(
            context["context"],
            manifest=self.bundle["manifest"],
            identity_snapshot_sha256=work["identity_snapshot_sha256"],
            entity_id=work["entity_id"],
            question_id=qid,
            scope=work["scope"],
            scope_id=work["scope_id"],
        )
        if canonical_bytes(bound["subject_binding"]["subject_ref"]) != canonical_bytes(
            entry["item"]["subject_ref"]
        ):
            raise AdmissionRejected("refresh_checkpoint_original_subject_changed")
        provenance = checkpoint["payload"]["provenance"]
        selected = (
            binding.get("ordered_route_targets")
            if binding["protocol"] == "stockqa.owner_refresh_binding/5.0.0"
            else None
        )
        matches = (
            any(
                all(
                    provenance.get(actual) == target[expected]
                    for actual, expected in (
                        ("route_id", "route_id"),
                        ("route_provider", "provider_config_ref"),
                        ("actual_provider", "provider"),
                        ("model_requested", "model"),
                    )
                )
                for target in selected
            )
            if selected is not None
            else provenance["model_requested"] == binding["model"]
            and provenance["actual_provider"] == binding["provider"]
        )
        if not matches:
            raise AdmissionRejected("refresh_checkpoint_original_model_changed")
        validate_standard_answer(
            standard["answer"],
            metadata=bound["metadata"],
            question_contract=frozen_question_contract(bound),
            normalized_answer=checkpoint["payload"]["answer"],
            # get_answer_checkpoint has re-read the original sent intent and
            # response binding. Preserve its native search projection, while
            # validating citations against the independently verified context.
            source_urls=list(
                dict.fromkeys(
                    provenance["source_urls"]
                    + [row["source_url"] for row in provenance.get("provided_context_sources", [])]
                )
            ),
        )
        response = store.get_attempt_response(checkpoint["attempt_id"])
        if response is None:
            raise AdmissionRejected("refresh_checkpoint_original_response_required")
        delivery = store.get_result_delivery(work["work_item_id"])
        self.check()
        return {
            **checkpoint,
            "original_standard_answer": standard,
            "original_observation_context": context,
            "original_attempt_response": response,
            "original_delivery_reference": (
                {
                    k: delivery.get(k)
                    for k in (
                        "work_item_id",
                        "state",
                        "delivery_id",
                        "package_id",
                        "package_sha256",
                        "observation_id",
                    )
                }
                if delivery
                else None
            ),
            "restoration_only": True,
            "original_context_retained": True,
            "new_C06_package_created": False,
        }

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
        if getattr(self, "recovery_only", False):
            raise AdmissionRejected("paid_recovery_cannot_dispatch")
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

    def assert_executor_policy(
        self, config_file, policy, *, executor_store=None, policy_reader=None
    ):
        # The registered admission can use an explicit work-store location.
        # Authenticate the runner's actual store, not an unrelated conventional
        # filename beside its model configuration. Legacy callers still bind
        # to that conventional location when no actual store is supplied.
        executor_path = (
            Path(config_file).absolute().parent / "quick_scan_work.sqlite"
            if executor_store is None
            else executor_store.path
        )
        if executor_path.resolve() != self.service.store.path.resolve():
            raise AdmissionRejected("refresh_execution_original_ledger_required")
        if getattr(self, "recovery_only", False):
            return  # No current model configuration or budget is used by a saved response.
        expected = _budget_policy_projection(self.service.budget)
        if canonical_bytes(_budget_policy_projection(policy)) != canonical_bytes(expected):
            raise AdmissionRejected("refresh_execution_confirmed_model_policy_required")
        selected = self.ordered_route_targets
        original_routes = None
        if selected is not None and not getattr(self, "recovery_only", False):
            original_routes = self._ordered_route_config(config_file, selected)
            self._ordered_vendor_routes = copy.deepcopy(original_routes)
        if policy_reader is not None or original_routes is not None:

            def check_policy():
                if policy_reader is not None and canonical_bytes(
                    _budget_policy_projection(policy_reader())
                ) != canonical_bytes(expected):
                    raise AdmissionRejected("refresh_execution_model_policy_changed")
                if (
                    original_routes is not None
                    and self._ordered_route_config(config_file, selected) != original_routes
                ):
                    raise AdmissionRejected("refresh_execution_model_configuration_changed")

            self._policy_check = check_policy

    @staticmethod
    def _ordered_route_config(config_file, selected):
        from src.providers.llm_client import resolve_search_endpoint

        document = _snapshot(config_file)[0]
        projection = {}
        try:
            for target in selected:
                alias = target["provider_config_ref"]
                base_url = document["providers"][alias]["base_url"]
                endpoint, provider, protocol = resolve_search_endpoint(
                    base_url, target["model"], alias, external_context_only=True
                )
                if provider != target["provider"]:
                    raise ValueError("original provider type changed")
                projection[target["route_id"]] = dict(
                    provider=provider, base_url=base_url, endpoint=endpoint, protocol=protocol
                )
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            raise AdmissionRejected("refresh_execution_target_configuration_unavailable") from error
        return projection

    def assert_provider_model(self, provider, model):
        if self.ordered_route_targets is not None:
            if (provider, model) not in {
                (row["provider"], row["model"]) for row in self.ordered_route_targets
            }:
                raise AdmissionRejected("refresh_execution_target_route_not_approved")
            return
        if provider != self.proof["preview"]["provider"] or model != self.proof["preview"]["model"]:
            raise AdmissionRejected("refresh_execution_primary_route_changed")

    def binding(self, qid):
        if qid not in self.entries:
            raise AdmissionRejected("refresh_execution_question_not_approved")
        return None  # Every approved work already exists in the original Q13.

    def execution_routes(self, policy=None):
        """Select new routes or project historical received routes, never mint a budget."""
        if getattr(self, "recovery_only", False):
            received_routes = {}
            approved_targets = self.ordered_route_targets
            received_work_ids = set(getattr(self, "recovery_work_item_ids", []))
            if not received_work_ids:
                if getattr(self, "allow_unknown_only", False):
                    return []
                raise AdmissionRejected("paid_recovery_original_response_required")
            for qid, entry in self.entries.items():
                if entry["kind"] != "work" or entry["work_item_id"] not in received_work_ids:
                    continue
                attempts = self.service.store.list_attempts(entry["work_item_id"])
                received_attempt = attempts[-1] if attempts else None
                received_response = (
                    self.service.store.get_attempt_response(received_attempt["attempt_id"])
                    if received_attempt and received_attempt["phase"] == "response_available"
                    else None
                )
                if received_response is None or received_attempt is None:
                    continue
                received_receipt = received_response["receipt"]
                received_target = next(
                    (
                        t
                        for t in (approved_targets or [])
                        if (t["route_id"], t["provider_config_ref"], t["model"], t["provider"])
                        == (
                            received_attempt["route_id"],
                            received_attempt["provider"],
                            received_attempt["model_requested"],
                            received_receipt.get("provider"),
                        )
                    ),
                    None,
                )
                if approved_targets is not None and received_target is None:
                    raise AdmissionRejected("paid_recovery_original_target_response_required")
                if approved_targets is None and (
                    received_receipt.get("provider"),
                    received_attempt["model_requested"],
                ) != (
                    self.proof["preview"]["provider"],
                    self.proof["preview"]["model"],
                ):
                    raise AdmissionRejected("paid_recovery_original_target_response_required")
                received_routes[received_attempt["route_id"]] = dict(
                    id=received_attempt["route_id"],
                    provider_config_ref=received_attempt["provider"],
                    model=received_attempt["model_requested"],
                    provider=received_receipt["provider"],
                    quota_group=received_target["quota_group"] if received_target else None,
                    eligible=False,
                    unavailable_reason="received_response_only",
                )
            if not received_routes:
                raise AdmissionRejected("paid_recovery_original_response_required")
            order = (
                [t["route_id"] for t in approved_targets]
                if approved_targets is not None
                else list(received_routes)
            )
            return [copy.deepcopy(received_routes[rid]) for rid in order if rid in received_routes]
        if self.ordered_route_targets is not None:
            expected = self.proof["owner_policy"]
            vendors = getattr(self, "_ordered_vendor_routes", None)
            recovery = getattr(self, "recovery_only", False)
            if policy["policy_id"] != expected["qa"]["policy_id"] or (
                vendors is None and not recovery
            ):
                raise AdmissionRejected("refresh_execution_target_route_unavailable")
            paid_routes = set()
            if recovery:
                for entry in self.entries.values():
                    if entry["kind"] != "work":
                        raise AdmissionRejected("paid_recovery_registered_work_required")
                    attempts = self.service.store.list_attempts(entry["work_item_id"])
                    attempt = attempts[-1] if attempts else None
                    response = (
                        self.service.store.get_attempt_response(attempt["attempt_id"])
                        if attempt
                        else None
                    )
                    receipt = response.get("receipt") if isinstance(response, dict) else None
                    matches = [
                        target
                        for target in self.ordered_route_targets
                        if attempt is not None
                        and attempt["phase"] == "response_available"
                        and isinstance(receipt, dict)
                        and all(
                            attempt[key] == target[field]
                            for key, field in (
                                ("route_id", "route_id"),
                                ("provider", "provider_config_ref"),
                                ("model_requested", "model"),
                            )
                        )
                        and receipt.get("provider") == target["provider"]
                    ]
                    if len(matches) != 1:
                        raise AdmissionRejected("paid_recovery_original_target_response_required")
                    paid_routes.add(matches[0]["route_id"])
            result = []
            for target in self.ordered_route_targets:
                if recovery and target["route_id"] not in paid_routes:
                    continue
                matches = [
                    route
                    for route in policy["routes"]
                    if all(
                        route.get(key) == target[field]
                        for key, field in (
                            ("id", "route_id"),
                            ("provider_config_ref", "provider_config_ref"),
                            ("model", "model"),
                            ("quota_group", "quota_group"),
                        )
                    )
                ]
                if len(matches) != 1 or (
                    not recovery
                    and (vendors or {}).get(target["route_id"], {}).get("provider")
                    != target["provider"]
                ):
                    raise AdmissionRejected("refresh_execution_target_route_unavailable")
                result.append(copy.deepcopy(matches[0]))
            return result
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
            and (route.get("eligible") is True or not self.needs_new_dispatch)
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
        if getattr(self, "recovery_only", False):
            raise AdmissionRejected("paid_recovery_cannot_dispatch")
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

    def _recovery_scope_metadata(self):
        if not getattr(self, "recovery_only", False):
            return {}
        return dict(
            scope_label="registered_paid_work_subset",
            original_registered_question_count=getattr(
                self, "original_registered_question_count", len(self.entries)
            ),
            recovered_work_question_count=len(
                getattr(self, "recovery_work_item_ids", self.entries)
            ),
            remaining_work_item_ids=list(getattr(self, "remaining_work_item_ids", [])),
            original_cached_references_retained=True,
        )

    def dispatch_scope_report(self, batch):
        """Explicit subset denominator; never claim financial correctness."""
        from src.core.models import REUSABLE_ANSWER_STATUSES

        requested = self.requested_question_ids
        statuses = {}
        for result in batch.results:
            qid = result.question.question_id
            if qid not in self.entries or qid in statuses:
                raise AdmissionRejected("refresh_execution_output_scope_mismatch")
            statuses[qid] = result.answer.status
        answered = [qid for qid in requested if statuses.get(qid) in REUSABLE_ANSWER_STATUSES]
        complete = len(answered) == len(requested)
        return dict(
            protocol="stockqa.refresh_execution/1.0.0",
            mode="approved_scope_answers",
            **self._recovery_scope_metadata(),
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
            **self._recovery_scope_metadata(),
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
