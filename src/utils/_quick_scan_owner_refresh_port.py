"""Private adapter from native refresh sessions to the public owner port."""

from __future__ import annotations

import copy
import importlib
import inspect
import json
import os
import weakref
from pathlib import Path
from types import MappingProxyType
from typing import Any

from src.interfaces.quick_scan_owner_refresh import (
    OwnerRefreshRuntimeProjection,
    OwnerRefreshSnapshot,
)

_PORTS: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()


class _OwnerRefreshPort:
    """Opaque capability handle; it carries no reference to the owner object."""

    __slots__ = ("__weakref__",)

    def snapshot(self) -> OwnerRefreshSnapshot:
        owner = _owner_for(self)
        approved = _is_approved(owner)
        native = getattr(owner, "native", owner) if approved else owner
        entries = copy.deepcopy(getattr(owner, "entries", {}))
        bundle = copy.deepcopy(getattr(owner, "bundle", None))
        proof = copy.deepcopy(getattr(owner, "proof", None))
        record = copy.deepcopy(getattr(owner, "record", None))
        if approved:
            questions = (bundle or {}).get("manifest", {}).get("questions", [])
            requested = tuple(
                row["id"] for row in questions if isinstance(row, dict) and row.get("id") in entries
            )
            request = (proof or {}).get("request", {})
            ordered = (
                copy.deepcopy((proof or {}).get("preview", {}).get("ordered_route_targets"))
                if request.get("protocol") == "iqs.refresh_request/1.6.0"
                else None
            )
        else:
            requested = tuple(getattr(owner, "requested_question_ids", ()) or ())
            ordered = getattr(owner, "ordered_route_targets", None)
        work = copy.deepcopy(getattr(owner, "work", {}))
        deferred = copy.deepcopy(getattr(owner, "deferred", {}))
        return OwnerRefreshSnapshot(
            kind="approved" if approved else "native",
            plan=copy.deepcopy(getattr(owner, "plan", {})),
            proof=proof,
            record=record,
            bundle=bundle,
            entries=entries,
            fields=copy.deepcopy(getattr(owner, "fields", {})),
            work=work,
            deferred=deferred,
            scope_bindings=copy.deepcopy(getattr(owner, "scope_bindings", {})),
            observation_context=copy.deepcopy(getattr(native, "observation_context", None)),
            ordered_route_targets=(None if ordered is None else tuple(copy.deepcopy(ordered))),
            requested_question_ids=requested,
            needs_new_dispatch=any(
                isinstance(item, dict)
                and item.get("status") == "pending"
                and question_id not in deferred
                for question_id, item in work.items()
            ),
            recovery_only=bool(getattr(owner, "recovery_only", False)),
            recovery_work_item_ids=tuple(
                copy.deepcopy(getattr(owner, "recovery_work_item_ids", ())) or ()
            ),
            remaining_work_item_ids=tuple(
                copy.deepcopy(getattr(owner, "remaining_work_item_ids", ())) or ()
            ),
        )

    def runtime_projection(self) -> OwnerRefreshRuntimeProjection:
        """Return fresh, bounded owner bindings without copying large inputs."""
        owner = _owner_for(self)
        approved = _is_approved(owner)
        native = getattr(owner, "native", owner) if approved else owner
        proof = getattr(owner, "proof", None) if approved else None
        proof = proof if isinstance(proof, dict) else {}
        request = proof.get("request", {})
        request = request if isinstance(request, dict) else {}
        request_protocol = request.get("protocol")
        plan = getattr(owner, "plan", {})
        plan = plan if isinstance(plan, dict) else {}
        entries_raw = getattr(owner, "entries", {})
        entries_raw = entries_raw if isinstance(entries_raw, dict) else {}
        fields_raw = getattr(owner, "fields", {})
        fields_raw = fields_raw if isinstance(fields_raw, dict) else {}
        projected_entries = {
            question_id: MappingProxyType(
                {
                    key: value
                    for key in ("kind", "work_item_id")
                    if isinstance((value := row.get(key)), str)
                }
            )
            for question_id, row in entries_raw.items()
            if isinstance(question_id, str) and isinstance(row, dict)
        }
        projected_decisions = {
            question_id: row.get("decision")
            for question_id, row in fields_raw.items()
            if isinstance(question_id, str)
            and isinstance(row, dict)
            and (row.get("decision") is None or isinstance(row.get("decision"), str))
        }
        bundle = getattr(owner, "bundle", None) if approved else None
        manifest = bundle.get("manifest", {}) if isinstance(bundle, dict) else {}
        module_locks = {
            row["module_id"]: row["version"]
            for row in manifest.get("module_locks", [])
            if isinstance(row, dict)
            and isinstance(row.get("module_id"), str)
            and isinstance(row.get("version"), str)
        }
        if approved:
            questions = manifest.get("questions", [])
            requested = tuple(
                row["id"]
                for row in questions
                if isinstance(row, dict)
                and isinstance(row.get("id"), str)
                and row["id"] in projected_entries
            )
            targets = (
                proof.get("preview", {}).get("ordered_route_targets")
                if request_protocol == "iqs.refresh_request/1.6.0"
                else None
            )
        else:
            requested = tuple(
                question_id
                for question_id in getattr(owner, "requested_question_ids", ()) or ()
                if isinstance(question_id, str)
            )
            targets = getattr(owner, "ordered_route_targets", None)
        projected_targets = (
            None
            if targets is None
            else tuple(
                MappingProxyType(copy.deepcopy(dict(target)))
                for target in targets
                if isinstance(target, dict)
            )
        )
        policy = proof.get("owner_policy", {})
        policy = policy if isinstance(policy, dict) else {}
        qa_policy = policy.get("qa", {})
        qa_policy = qa_policy if isinstance(qa_policy, dict) else {}
        legacy_route = None
        if request_protocol != "iqs.refresh_request/1.6.0":
            legacy_route = MappingProxyType(
                {
                    "route_id": qa_policy.get("route_id"),
                    "provider_config_ref": qa_policy.get("provider_config_ref"),
                    "model": policy.get("model"),
                    "provider": policy.get("provider"),
                }
            )
        context = getattr(native, "observation_context", None)
        context_sha256 = None
        if context is not None:
            from .quick_scan_result_outbox import canonical_sha256

            context_sha256 = canonical_sha256(context)
        from .quick_scan_result_outbox import canonical_sha256

        plan_keys = (
            "information_cutoff",
            "perimeter_sha256",
            "manifest_raw_sha256",
            "decision_id",
            "subject_key",
            "provider",
            "model",
            "anchor_version",
        )
        return OwnerRefreshRuntimeProjection(
            kind="approved" if approved else "native",
            request_protocol=request_protocol if isinstance(request_protocol, str) else None,
            recovery_only=bool(getattr(owner, "recovery_only", False)),
            information_cutoff=plan.get("information_cutoff"),
            observation_context_sha256=context_sha256,
            plan_bindings=MappingProxyType(
                {key: copy.deepcopy(plan.get(key)) for key in plan_keys if key in plan}
            ),
            entries=MappingProxyType(projected_entries),
            decisions=MappingProxyType(projected_decisions),
            module_locks=MappingProxyType(module_locks),
            ordered_route_targets=projected_targets,
            legacy_route_target=legacy_route,
            requested_question_ids=requested,
            recovery_work_item_ids=tuple(
                value
                for value in (getattr(owner, "recovery_work_item_ids", ()) or ())
                if isinstance(value, str)
            ),
            remaining_work_item_ids=tuple(
                value
                for value in (getattr(owner, "remaining_work_item_ids", ()) or ())
                if isinstance(value, str)
            ),
        )

    def check(self) -> None:
        _owner_for(self).check()

    def is_bound_to(self, store: Any) -> bool:
        owner = _owner_for(self)
        native = owner.native if _is_approved(owner) else owner
        if getattr(native, "store", None) is not store:
            return False
        return (
            not _is_approved(owner)
            or getattr(getattr(owner, "service", None), "store", None) is store
        )

    def is_bound_to_path(self, path: Any) -> bool:
        owner = _owner_for(self)
        native = owner.native if _is_approved(owner) else owner
        store = getattr(native, "store", None)
        owner_path = getattr(store, "path", None)
        return owner_path is not None and Path(path).resolve() == Path(owner_path).resolve()

    def assert_work_item_scope(self, store: Any, work_item_ids: tuple[str, ...]) -> None:
        """Prove every active work item is owned by this session through public store APIs."""
        if not self.is_bound_to(store):
            raise ValueError("owner_refresh_store_mismatch")
        if not work_item_ids:
            raise ValueError("owner_refresh_work_scope_required")
        approved = _is_approved(_owner_for(self))
        for work_item_id in work_item_ids:
            try:
                item = store.get_item(work_item_id)
                question_id = item.get("question_id") if isinstance(item, dict) else None
                if not isinstance(question_id, str) or not question_id:
                    raise ValueError("owner_refresh_work_scope_mismatch")
                if approved:
                    original_work = self.existing_work(question_id)
                    if (
                        not isinstance(original_work, dict)
                        or original_work.get("work_item_id") != work_item_id
                    ):
                        raise ValueError("owner_refresh_work_scope_mismatch")
                    continue
                registered = store.get_owner_refresh_binding(work_item_id)
                persisted_binding = (
                    registered.get("binding") if isinstance(registered, dict) else None
                )
                if not isinstance(persisted_binding, dict):
                    raise ValueError("owner_refresh_work_scope_mismatch")
                owner_control = persisted_binding.get("owner_control")
                expected_binding = self.binding(
                    question_id,
                    **({"owner_control": owner_control} if owner_control is not None else {}),
                )
                if expected_binding is not None:
                    if expected_binding != persisted_binding:
                        raise ValueError("owner_refresh_work_scope_mismatch")
                    continue
                original_work = self.existing_work(question_id)
                if (
                    not isinstance(original_work, dict)
                    or original_work.get("work_item_id") != work_item_id
                ):
                    raise ValueError("owner_refresh_work_scope_mismatch")
            except (AttributeError, KeyError, TypeError, ValueError) as error:
                if str(error) in {
                    "owner_refresh_store_mismatch",
                    "owner_refresh_work_scope_required",
                    "owner_refresh_work_scope_mismatch",
                }:
                    raise
                raise ValueError("owner_refresh_work_scope_mismatch") from error

    def assert_budget_policy(self, policy) -> None:
        owner = _owner_for(self)
        if not _is_approved(owner):
            raise ValueError("owner_refresh_approved_budget_policy_required")
        from .quick_scan_result_outbox import canonical_bytes
        from .quick_scan_work_store import api_work_store_budget_policy_projection

        if canonical_bytes(api_work_store_budget_policy_projection(policy)) != canonical_bytes(
            api_work_store_budget_policy_projection(owner.service.budget)
        ):
            raise ValueError("approved_budget_policy_required")

    def routing_fingerprints(self):
        snapshot = self.snapshot()
        from .quick_scan_result_outbox import canonical_sha256

        return {
            question_id: canonical_sha256(
                {
                    "subject_key": snapshot.plan["subject_key"],
                    "request_identity_key": row["request_identity_key"],
                }
            )
            for question_id, row in snapshot.fields.items()
            if row.get("request_identity_key")
        }

    def assert_provider_model(self, provider, model) -> None:
        _owner_for(self).assert_provider_model(provider, model)

    def execution_routes(self, policy=None):
        owner = _owner_for(self)
        operation = getattr(owner, "execution_routes", None)
        if not callable(operation):
            raise ValueError("approved_owner_execution_routes_required")
        return copy.deepcopy(operation(policy))

    def binding(self, question_id, **kwargs):
        return copy.deepcopy(_owner_for(self).binding(question_id, **kwargs))

    def existing_work(self, question_id):
        return copy.deepcopy(_owner_for(self).existing_work(question_id))

    def reference(self, question):
        return copy.deepcopy(_owner_for(self).reference(question))

    def register_subject_dispatch(self, request):
        return copy.deepcopy(_owner_for(self).register_subject_dispatch(copy.deepcopy(request)))

    def claim_registered_question_batch(self, question_ids, **kwargs):
        return copy.deepcopy(
            _owner_for(self).claim_registered_question_batch(list(question_ids), **kwargs)
        )

    def assert_reactivated_original_binding(self, question_id, work, binding, *, store):
        owner = _owner_for(self)
        if not _is_approved(owner):
            raise ValueError("approved_refresh_original_binding_required")
        return owner.assert_reactivated_original_binding(
            question_id, copy.deepcopy(work), copy.deepcopy(binding), store=store
        )

    def assert_received_checkpoint_recovery(self, work_item_id, attempt_id, *, store):
        owner = _owner_for(self)
        if not _is_approved(owner):
            raise ValueError("approved_refresh_checkpoint_recovery_required")
        return owner.assert_received_checkpoint_recovery(work_item_id, attempt_id, store=store)

    def assert_execution_controls(self, **kwargs):
        return _owner_for(self).assert_execution_controls(**kwargs)

    def assert_executor_policy(self, config_file, policy, **kwargs):
        operation = getattr(_owner_for(self), "assert_executor_policy", None)
        if not callable(operation):
            raise ValueError("approved_owner_executor_policy_required")
        return operation(config_file, policy, **kwargs)

    def restore_registered_checkpoint(self, question, *, store):
        return _owner_for(self).restore_registered_checkpoint(question, store=store)

    def recover_received_checkpoints(self, **kwargs):
        return copy.deepcopy(_owner_for(self).recover_received_checkpoints(**kwargs))

    def dispatch_scope_report(self, batch):
        return copy.deepcopy(_owner_for(self).dispatch_scope_report(batch))

    def recovery_report(self):
        owner = _owner_for(self)
        operation = getattr(owner, "recovery_report", None)
        return {} if not callable(operation) else copy.deepcopy(operation())

    def subject_dispatch_setup(self) -> dict:
        owner = _owner_for(self)
        native = owner.native if _is_approved(owner) else owner
        client = getattr(native, "client", None)
        config = getattr(client, "config", None)
        if not isinstance(config, dict):
            raise ValueError("owner_refresh_subject_dispatch_config_unavailable")
        path_keys = (
            "iqs_code_root",
            "runs_dir",
            "stockwiki_workspace_root",
            "stockwiki_code_root",
        )
        paths = {key: config[key] for key in path_keys if isinstance(config.get(key), str)}
        if len(paths) != len(path_keys):
            raise ValueError("owner_refresh_subject_dispatch_config_incomplete")
        source_env = getattr(client, "_environment", None)
        if source_env is None:
            source_env = os.environ
        if not isinstance(source_env, dict):
            source_env = dict(source_env)
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
        safe_env = {
            key: value
            for key, value in source_env.items()
            if isinstance(key, str) and key.upper() in allowed and isinstance(value, str)
        }
        paths["environment"] = safe_env
        return paths

    def analysis_subject(self):
        owner = _owner_for(self)
        native = owner.native if _is_approved(owner) else owner
        current = getattr(native, "current", None)
        snapshot = current.get("snapshot", {}) if isinstance(current, dict) else {}
        binding = snapshot.get("subject_binding", {})
        subject = binding.get("subject") if isinstance(binding, dict) else None
        return copy.deepcopy(subject) if isinstance(subject, dict) else None

    def identity_payload(self) -> dict:
        owner = _owner_for(self)
        inputs = getattr(owner, "input_raw", None)
        raw = inputs.get("identity.json") if isinstance(inputs, dict) else None
        if not isinstance(raw, bytes):
            raise ValueError("owner_refresh_identity_snapshot_unavailable")
        from .quick_scan_result_outbox import strict_json_loads

        document = strict_json_loads(raw.decode("utf-8"))
        payload = document.get("payload") if isinstance(document, dict) else None
        if not isinstance(payload, dict):
            raise ValueError("owner_refresh_identity_payload_invalid")
        return copy.deepcopy(payload)

    def native_input_references(self) -> dict:
        """Expose stable hashes of original input bytes, never the bytes themselves."""
        import hashlib

        owner = _owner_for(self)
        inputs = getattr(owner, "input_raw", None)
        expected = {
            "manifest.json",
            "identity.json",
            "authority.json",
            "owner-config.json",
            "questions.json",
        }
        if (
            not isinstance(inputs, dict)
            or set(inputs) != expected
            or any(not isinstance(raw, bytes) for raw in inputs.values())
        ):
            raise ValueError("owner_refresh_native_inputs_unavailable")
        return {
            name: {
                "path": "native-input/" + name,
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
            for name, raw in inputs.items()
        }

    def original_semantic_resources(self, context) -> dict:
        """Resolve frozen answer semantics through the original owner release.

        Filesystem roots remain inside this owner operation. The only returned
        value is the IQS answer-validation DTO, which contains detached JSON
        resources and their digests rather than paths or source bytes.
        The historical context and release authenticate this read; current
        dispatch permission is checked at the claim and transport boundaries.
        """
        owner = _owner_for(self)
        if not _is_approved(owner):
            raise ValueError("approved_owner_semantic_resources_required")
        native = owner.native
        original_context = getattr(native, "observation_context", None)
        from src.utils.quick_scan_result_outbox import canonical_sha256

        if (
            not isinstance(context, dict)
            or not isinstance(original_context, dict)
            or canonical_sha256(context) != canonical_sha256(original_context)
        ):
            raise ValueError("original_semantic_context_mismatch")
        client = getattr(native, "client", None)
        config = getattr(client, "config", None)
        code_root_value = config.get("iqs_code_root") if isinstance(config, dict) else None
        release_root_value = config.get("iqs_release_root") if isinstance(config, dict) else None
        if not isinstance(code_root_value, str) or not isinstance(release_root_value, str):
            raise ValueError("original_semantic_owner_config_unavailable")
        code_root = Path(code_root_value).resolve()
        release_root = Path(release_root_value).resolve()
        api_path = (code_root / "scripts" / "answer_validation.py").resolve()
        try:
            api = importlib.import_module("answer_validation")
            api_origin = Path(inspect.getfile(api)).resolve()
            if api_origin != api_path:
                raise ValueError("original_semantic_api_origin_mismatch")
            if getattr(
                api, "API_PROTOCOL", None
            ) != "iqs.answer-validation-api/1.1.0" or "project_original_semantic_resources" not in getattr(
                api, "PUBLIC_API_EXPORTS", frozenset()
            ):
                raise ValueError("original_semantic_api_version_unsupported")
            project = getattr(api, "project_original_semantic_resources", None)
            if not callable(project):
                raise ValueError("original_semantic_api_operation_missing")
            projection = project(context, release_root=release_root)
            encoded = json.dumps(projection, ensure_ascii=False, sort_keys=True, allow_nan=False)
            if str(code_root) in encoded or str(release_root) in encoded:
                raise ValueError("original_semantic_resource_path_leak")
            return copy.deepcopy(projection)
        except (ImportError, OSError, TypeError, KeyError, ValueError) as error:
            reason = str(error)
            if reason in {
                "original_semantic_context_mismatch",
                "original_semantic_owner_config_unavailable",
                "original_semantic_api_origin_mismatch",
                "original_semantic_api_version_unsupported",
                "original_semantic_api_operation_missing",
                "original_semantic_resource_path_leak",
            }:
                raise
            raise ValueError("original_semantic_resources_unavailable") from error


def _owner_for(handle):
    """Resolve the opaque capability only inside its owning implementation."""
    owner = _PORTS.get(handle)
    if owner is None:
        raise ValueError("owner_refresh_port_expired")
    return owner


def _is_approved(owner) -> bool:
    from .quick_scan_refresh_execution import ApprovedRefreshSession

    return isinstance(owner, ApprovedRefreshSession)


def adapt_owner_refresh(owner):
    """Return an opaque public handle for a concrete owner-created session."""
    if isinstance(owner, _OwnerRefreshPort):
        _owner_for(owner)
        return owner
    from .quick_scan_owner_refresh import OwnerRefreshSession
    from .quick_scan_refresh_execution import ApprovedRefreshSession

    if not isinstance(owner, (OwnerRefreshSession, ApprovedRefreshSession)):
        raise ValueError("original_owner_refresh_session_required")
    handle = _OwnerRefreshPort()
    _PORTS[handle] = owner
    return handle


def is_owner_refresh_port(value) -> bool:
    if not isinstance(value, _OwnerRefreshPort):
        return False
    try:
        _owner_for(value)
    except ValueError:
        return False
    return True


def build_refresh_result(batch, owner_port, quick_scan_context):
    try:
        owner_port = adapt_owner_refresh(owner_port)
    except ValueError as error:
        raise ValueError("owner_refresh_port_required") from error
    if not is_owner_refresh_port(owner_port):
        raise ValueError("owner_refresh_port_required")
    owner = _owner_for(owner_port)
    from .quick_scan_owner_refresh import build_refresh_result as build

    return build(batch, owner, quick_scan_context)


def original_semantic_resources(owner_port, context):
    if not is_owner_refresh_port(owner_port):
        raise ValueError("owner_refresh_port_required")
    return owner_port.original_semantic_resources(context)


__all__ = ["adapt_owner_refresh", "is_owner_refresh_port", "build_refresh_result"]
