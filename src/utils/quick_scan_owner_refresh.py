"""Optional W15/Q13 owner bridge through a configured first-party OS CLI.

Never imports another repository, accepts a supplied refresh-plan file, or
rewrites old observations. Owner transport is trusted configuration; hashes
alone are not authentication. All financial data still needs its accuracy gate.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os

# Fixed first-party CLI bridge; no shell or supplied executable.
import subprocess  # nosec B404
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from src.utils.quick_scan_result_outbox import canonical_sha256, strict_json_loads
from src.utils.quick_scan_work_store import QuickScanWorkStore

_CONFIG_KEYS = {
    "protocol",
    "stockwiki_code_root",
    "stockwiki_workspace_root",
    "iqs_code_root",
    "iqs_release_root",
    "subject_key",
    "scope",
    "scope_id",
    "ttl_hours",
    "runs_dir",
}
_CURRENT_KEYS = ("expected_decision_id", "anchor_version", "owner_source_binding_refs")
_SNAPSHOT_KEYS = (
    "decision_id",
    "subject_key",
    "entity_id",
    "identity_revision",
    "perimeter_sha256",
    "scope",
    "scope_id",
    "route_raw_sha256",
    "manifest_raw_sha256",
    "module_locks",
    "module_package_id",
    "module_release_id",
    "subject_binding",
)


class OwnerRefreshRejected(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _validate_schema(value: dict, name: str) -> None:
    from jsonschema import Draft202012Validator, ValidationError

    schema = _read((Path(__file__).resolve().parents[1] / "config" / name).read_bytes())
    try:
        Draft202012Validator(schema).validate(value)
    except ValidationError as error:
        raise OwnerRefreshRejected("owner_refresh_schema_mismatch") from error


def _read(raw: bytes) -> dict:
    if not isinstance(raw, bytes) or len(raw) > 16 * 1024 * 1024:
        raise OwnerRefreshRejected("owner_refresh_response_size")
    try:
        value = strict_json_loads(raw.decode("utf-8"))
        canonical_sha256(value)
    except (ValueError, UnicodeDecodeError, TypeError, RecursionError) as error:
        raise OwnerRefreshRejected("owner_refresh_invalid_json") from error
    if not isinstance(value, dict):
        raise OwnerRefreshRejected("owner_refresh_invalid_json")
    return value


def _validate_owner_wire(value: dict, code_root: Path) -> None:
    """Read the configured IQS wire contracts without cross-repo imports.

    Only these three local resources resolve. No network/schema retrieval is
    enabled, and passing this structure gate never authenticates its origin.
    """
    from jsonschema import Draft202012Validator, FormatChecker, ValidationError
    from referencing import Registry, Resource
    from referencing.exceptions import Unresolvable

    resources = []
    for name in (
        "route-store-cli.schema.json",
        "module-refresh.schema.json",
        "route-store-validation.schema.json",
    ):
        schema = _read((Path(code_root) / "schemas/quick_scan" / name).read_bytes())
        resources.append((schema["$id"], Resource.from_contents(schema)))
    try:
        Draft202012Validator(
            resources[0][1].contents,
            registry=Registry().with_resources(resources),
            format_checker=FormatChecker(),
        ).validate(value)
    except (ValidationError, Unresolvable) as error:
        raise OwnerRefreshRejected("owner_refresh_wire_schema_mismatch") from error


class OwnerRefreshClient:
    """Explicit trusted configuration selects code/workspace, never JSON commands."""

    def __init__(self, config_path: Path, *, process_env: dict[str, str] | None = None):
        self.config = _read(Path(config_path).read_bytes())
        _validate_schema(self.config, "quick_scan_owner_refresh_config.schema.json")
        config = self.config
        if (
            set(config) != _CONFIG_KEYS
            or config["protocol"] != "stockqa.owner_refresh_config/1.0.0"
            or config["scope"] not in {"entity", "segment"}
            or type(config["ttl_hours"]) is not int
            or config["ttl_hours"] < 1
            or any(
                not isinstance(config[k], str) or not config[k]
                for k in _CONFIG_KEYS - {"ttl_hours"}
            )
        ):
            raise OwnerRefreshRejected("owner_refresh_config_invalid")
        for key in (
            "stockwiki_code_root",
            "stockwiki_workspace_root",
            "iqs_code_root",
            "iqs_release_root",
            "runs_dir",
        ):
            if not Path(config[key]).is_absolute():
                raise OwnerRefreshRejected("owner_refresh_config_absolute_paths_required")
        if not (
            Path(config["stockwiki_code_root"]) / "stockwiki/quick_scan_routes_cli.py"
        ).is_file():
            raise OwnerRefreshRejected("owner_refresh_cli_unavailable")
        self._environment = process_env

    def _call(self, operation: str, extra: list[str]) -> dict:
        c = self.config
        env = (
            self._environment
            if self._environment is not None
            else {
                k: v
                for k, v in os.environ.items()
                if k.upper()
                in {
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
            }
        )
        env = {
            **env,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        env["PYTHONPATH"] = os.pathsep.join((c["stockwiki_code_root"], env.get("PYTHONPATH", "")))
        command = [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            "-m",
            "stockwiki.quick_scan_routes_cli",
            "--root",
            c["stockwiki_workspace_root"],
            "quick-scan-route-" + operation,
            "--iqs-code-root",
            c["iqs_code_root"],
            "--iqs-release-root",
            c["iqs_release_root"],
            "--subject-key",
            c["subject_key"],
            "--scope",
            c["scope"],
            "--scope-id",
            c["scope_id"],
            *extra,
        ]
        try:
            # Validated roots, fixed argv, shell=False.
            completed = subprocess.run(  # nosec B603
                command,
                cwd=c["stockwiki_code_root"],
                env=env,
                capture_output=True,
                timeout=120,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise OwnerRefreshRejected("owner_refresh_transport_unavailable") from error
        receipt = _read(completed.stdout)
        _validate_owner_wire(receipt, Path(c["iqs_code_root"]))
        if (
            completed.returncode != 0
            or receipt.get("protocol") != "stockwiki.route_store_cli/1.0.0"
            or receipt.get("schema_version") != "1.0.0"
            or receipt.get("status") != "ok"
            or receipt.get("action") != operation
            or receipt.get("paid_dispatch_started") is not False
            or not isinstance(receipt.get("payload"), dict)
        ):
            raise OwnerRefreshRejected("owner_refresh_transport_rejected")
        return dict(receipt["payload"])

    def current(self, now: str) -> dict:
        return self._call("current", ["--now-utc", now, "--include-raw-bundle"])

    @staticmethod
    def current_key(current: dict) -> dict:
        try:
            return {
                **{k: current[k] for k in _CURRENT_KEYS},
                "snapshot": {k: current["snapshot"][k] for k in _SNAPSHOT_KEYS},
            }
        except (KeyError, TypeError) as error:
            raise OwnerRefreshRejected("owner_refresh_current_invalid") from error

    def assert_current(self, current: dict, now: str) -> None:
        anchor = self._call("anchor", ["--now-utc", now])
        if anchor.get("new_execution_authorized") is not False or self.current_key(
            anchor
        ) != self.current_key(current):
            raise OwnerRefreshRejected("owner_refresh_current_changed")

    def prepare(
        self,
        manifest: dict,
        store: QuickScanWorkStore,
        *,
        identity: dict,
        provider: str,
        model: str,
        now: str,
        observation_context: dict | None = None,
    ) -> "OwnerRefreshSession":
        current = self.current(now)
        snap = current.get("snapshot", {})
        c = self.config
        try:
            raw = base64.b64decode(snap["manifest_raw_base64"], validate=True)
            route_raw = base64.b64decode(snap["route_raw_base64"], validate=True)
            frozen = _read(raw)
            route = _read(route_raw)
            binding = snap["subject_binding"]
            if (
                hashlib.sha256(raw).hexdigest() != manifest["manifest_sha256"]
                or hashlib.sha256(raw).hexdigest() != snap["manifest_raw_sha256"]
                or hashlib.sha256(route_raw).hexdigest() != snap["route_raw_sha256"]
                or snap["subject_key"] != c["subject_key"]
                or snap["scope"] != c["scope"]
                or snap["scope_id"] != c["scope_id"]
                or snap["entity_id"] != identity["entity_id"]
                or snap["identity_revision"] != identity["identity_revision"]
                or identity.get("identity_state") != "verified"
                or sorted(identity.get("source_binding_refs", []))
                != current.get("owner_source_binding_refs")
                or identity.get("source_binding_ref")
                not in current.get("owner_source_binding_refs", [])
                or current["expected_decision_id"] != route["decision_id"]
                or snap["decision_id"] != route["decision_id"]
                or binding["route_identity_ref"] != route["identity_ref"]
                or binding["subject_key"] != snap["subject_key"]
                or binding["perimeter_sha256"] != snap["perimeter_sha256"]
                or frozen["module_locks"] != snap["module_locks"]
                or current["execution_validation"].get("new_execution_authorized") is not True
            ):
                raise OwnerRefreshRejected("owner_refresh_manifest_identity_mismatch")
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, OwnerRefreshRejected):
                raise
            raise OwnerRefreshRejected("owner_refresh_current_invalid") from error
        if observation_context is not None:
            for row in observation_context["questions"].values():
                if row["metadata"].get("analysis_subject") != binding["subject"]:
                    raise OwnerRefreshRejected("owner_refresh_subject_context_mismatch")
        scopes = {}
        for q in frozen["questions"]:
            effective_scope = (
                "segment"
                if route.get("scope") == "segment" and q["scope"] == "entity"
                else q["scope"]
            )
            sid = (
                snap["entity_id"]
                if effective_scope == "entity"
                else (
                    route.get("profile_context", {}).get("security_id")
                    if effective_scope == "security"
                    else route.get("segment_id")
                )
            )
            scopes[q["id"]] = {
                "scope": effective_scope if sid else "unbound",
                "scope_id": sid or "",
            }
        projection = store.owner_refresh_projection(
            entity_id=snap["entity_id"],
            question_ids=[q["id"] for q in frozen["questions"]],
            scope_bindings=scopes,
        )
        runs = Path(c["runs_dir"])
        runs.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="owner-refresh-", dir=runs) as temporary:
            path = Path(temporary) / "work-projection.json"
            path.write_text(
                json.dumps(projection, ensure_ascii=False, allow_nan=False), encoding="utf-8"
            )
            plan = self._call(
                "refresh",
                [
                    "--now-utc",
                    now,
                    "--provider",
                    provider,
                    "--model",
                    model,
                    "--ttl-hours",
                    str(c["ttl_hours"]),
                    "--work-projection",
                    str(path),
                ],
            )
        self.assert_current(current, now)
        for k, value in (
            ("subject_key", c["subject_key"]),
            ("scope", c["scope"]),
            ("scope_id", c["scope_id"]),
            ("provider", provider),
            ("model", model),
            ("now", now),
            ("ttl_hours", c["ttl_hours"]),
            ("decision_id", current["expected_decision_id"]),
            ("anchor_version", current["anchor_version"]),
            ("manifest_raw_sha256", snap["manifest_raw_sha256"]),
            ("identity_revision", snap["identity_revision"]),
            ("perimeter_sha256", snap["perimeter_sha256"]),
            ("module_locks", frozen["module_locks"]),
            ("module_package_id", frozen["module_package_id"]),
            ("module_release_id", frozen["module_release_id"]),
            ("information_cutoff", route["as_of"]),
            ("cutoff_policy", "exact_period"),
            ("protocol", "stockwiki.module_refresh_plan/1.0.0"),
            ("model_API_requests", 0),
            ("dispatch_started", False),
        ):
            if plan.get(k) != value or type(plan.get(k)) is not type(value):
                raise OwnerRefreshRejected("owner_refresh_plan_binding_mismatch")
        fields = plan.get("fields")
        if (
            not isinstance(fields, list)
            or len(fields) != len(frozen["questions"])
            or {f.get("field_id") for f in fields} != {q["id"] for q in frozen["questions"]}
        ):
            raise OwnerRefreshRejected("owner_refresh_plan_question_mismatch")
        if "refresh_" + canonical_sha256(
            {k: v for k, v in plan.items() if k not in {"refresh_id", "reused_observations"}}
        ) != plan.get("refresh_id"):
            raise OwnerRefreshRejected("owner_refresh_plan_hash_mismatch")
        return OwnerRefreshSession(self, current, plan, scopes, projection, identity, store)


class OwnerRefreshSession:
    def __init__(
        self,
        client: OwnerRefreshClient,
        current: dict,
        plan: dict,
        scopes: dict,
        projection: dict,
        identity: dict,
        store: QuickScanWorkStore,
    ):
        self.client, self.current, self.plan = client, copy.deepcopy(current), copy.deepcopy(plan)
        self.scope_bindings, self.projection, self.identity, self.store = (
            scopes,
            projection,
            identity,
            store,
        )
        self.fields = {f["field_id"]: f for f in self.plan["fields"]}

    def check(self) -> None:
        from datetime import datetime, timezone

        self.client.assert_current(
            self.current, datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )

    def binding(self, qid: str) -> dict | None:
        row = self.fields[qid]
        if row["decision"] not in {"dispatch_new_work", "dispatch_new_generation"}:
            return None
        projected = self.projection.get(qid)
        if (
            projected is not None
            and row.get("freshness_status") == "absent"
            and projected["status"] == "delivered"
        ):
            raise OwnerRefreshRejected("owner_refresh_ack_observation_missing")
        scope = self.scope_bindings[qid]
        return dict(
            protocol="stockqa.owner_refresh_binding/1.0.0",
            subject_key=self.plan["subject_key"],
            perimeter_sha256=self.plan["perimeter_sha256"],
            decision_id=self.plan["decision_id"],
            anchor_version=self.plan["anchor_version"],
            manifest_raw_sha256=self.plan["manifest_raw_sha256"],
            provider=self.plan["provider"],
            model=self.plan["model"],
            information_cutoff=self.plan["information_cutoff"],
            request_identity_key=row["request_identity_key"],
            target_generation=row["generation"],
            question_id=qid,
            scope=scope["scope"],
            scope_id=scope["scope_id"],
            entity_id=self.identity["entity_id"],
            identity_revision=self.identity["identity_revision"],
        )

    def existing_work(self, qid: str) -> dict | None:
        if self.fields[qid]["decision"] != "resume_existing_work":
            return None
        projected = self.projection.get(qid)
        if not projected:
            raise OwnerRefreshRejected("owner_refresh_existing_work_missing")
        return self.store.get_item(projected["work_item_id"])

    def reference(self, question):
        from datetime import datetime

        from src.core.models import Answer, QAResult

        qid = question.question_id
        row = self.fields[qid]
        if row["decision"] != "reuse":
            return None
        self.check()
        original = self.plan.get("reused_observations", {}).get(row["reuse_observation_id"])
        if (
            not isinstance(original, dict)
            or original.get("observation_id") != row["reuse_observation_id"]
            or canonical_sha256(original["payload"]) != original.get("payload_sha256")
        ):
            raise OwnerRefreshRejected("owner_refresh_reuse_corrupted")
        payload = original["payload"]
        scope = self.scope_bindings[qid]
        frozen = _read(
            base64.b64decode(self.current["snapshot"]["manifest_raw_base64"], validate=True)
        )
        question_meta = next(q for q in frozen["questions"] if q["id"] == qid)
        if (
            payload.get("question_id") != qid
            or payload.get("entity_id") != self.identity["entity_id"]
            or payload.get("scope") != scope["scope"]
            or payload.get("information_cutoff") != self.plan["information_cutoff"]
            or payload.get("identity_revision") != self.identity["identity_revision"]
            or payload.get("analysis_subject")
            != self.current["snapshot"]["subject_binding"]["subject"]
            or payload.get("question_semantic_sha256") != question_meta["semantic_sha256"]
            or payload.get("question_definition_sha256") != question_meta["definition_sha256"]
            or payload.get("question_version") != question_meta["rubric_version"]
            or payload.get("security_id")
            != (scope["scope_id"] if scope["scope"] == "security" else None)
            or payload.get("segment_id")
            != (scope["scope_id"] if scope["scope"] == "segment" else None)
            or payload.get("execution", {}).get("provider") != self.plan["provider"]
            or payload.get("execution", {}).get("model_resolved") != self.plan["model"]
        ):
            raise OwnerRefreshRejected("owner_refresh_reuse_binding_mismatch")
        answer, execution = payload["answer"], payload["execution"]
        metadata = {
            "actual_model": execution["model_resolved"],
            "actual_provider": execution["provider"],
            "execution": copy.deepcopy(execution),
            "owner_observation_reference": copy.deepcopy(original),
        }
        if answer["status"] not in {"scored", "not_applicable"}:
            raise OwnerRefreshRejected("owner_refresh_reference_status_unsupported")
        from datetime import timedelta, timezone

        info = answer["information_as_of"]
        stamp = info if "T" in info else info + "T00:00:00Z"
        valid_until = min(
            datetime.fromisoformat(payload["observed_at"].replace("Z", "+00:00")),
            datetime.fromisoformat(stamp.replace("Z", "+00:00")),
        ) + timedelta(hours=self.plan["ttl_hours"])
        if datetime.now(timezone.utc) >= valid_until:
            raise OwnerRefreshRejected("owner_refresh_reuse_expired")
        return QAResult(
            question=question,
            answer=Answer(
                text=answer["summary"],
                score=answer["score"],
                status=answer["status"],
                source="stockwiki_observation_reference",
                metadata=metadata,
                created_at=datetime.fromisoformat(payload["observed_at"].replace("Z", "+00:00")),
            ),
            metadata={
                "owner_observation_reference": {
                    "observation_id": original["observation_id"],
                    "payload_sha256": original["payload_sha256"],
                    "original_payload_retained": True,
                    "new_work_created": False,
                    "new_C06_package_created": False,
                }
            },
        )


def build_refresh_result(batch, session: OwnerRefreshSession, quick_scan_context: dict) -> dict:
    """Do not repackage an old answer as a current-manifest provider response."""
    from src.core.models import QABatchResult

    references = {}
    dispatch = QABatchResult(total_questions=0)
    for result in batch.results:
        reference = result.metadata.get("owner_observation_reference")
        if reference is None:
            dispatch.add_result(result)
            continue
        qid = result.question.question_id
        original = session.plan.get("reused_observations", {}).get(reference["observation_id"])
        if (
            session.fields[qid]["decision"] != "reuse"
            or original is None
            or reference["observation_id"] != session.fields[qid]["reuse_observation_id"]
            or reference["payload_sha256"] != original["payload_sha256"]
            or canonical_sha256(original["payload"]) != reference["payload_sha256"]
        ):
            raise OwnerRefreshRejected("owner_refresh_output_reference_mismatch")
        references[qid] = {
            "observation_id": original["observation_id"],
            "payload_sha256": original["payload_sha256"],
            "original_observation": copy.deepcopy(original["payload"]),
            "original_qualification_status": original.get("qualification_status"),
            "new_observation_created": False,
        }
    dispatch.total_questions = len(dispatch.results)
    result = {
        "schema_version": "stockqa.owner_refresh_result/1.0.0",
        "subject_key": session.plan["subject_key"],
        "decision_id": session.plan["decision_id"],
        "anchor_version": session.plan["anchor_version"],
        "manifest_raw_sha256": session.plan["manifest_raw_sha256"],
        "refresh_id": session.plan["refresh_id"],
        "observation_references": references,
        "question_actions": copy.deepcopy(session.plan["fields"]),
        "dispatched_result": (
            dispatch.to_quick_scan_dict(**quick_scan_context) if dispatch.results else None
        ),
        "original_observations_resealed": False,
    }
    _validate_schema(result, "quick_scan_owner_refresh_result.schema.json")
    return result
