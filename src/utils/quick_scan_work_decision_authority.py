"""Trusted fixed owner CLI reads; a consumer approval dictionary is never a grant."""

import copy
import hashlib
import re
import subprocess  # nosec B404
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable

from .quick_scan_known_outcome_journal import (
    OwnerWorkDecisionAuthority,
    OwnerWorkDecisionGrant,
)
from .quick_scan_owner_refresh import OwnerRefreshRejected, _read
from .quick_scan_refresh_consent import OwnerConsentEndpoint, RefreshConsentClient
from .quick_scan_refresh_inspect import ReadOnlyWorkView
from .quick_scan_result_outbox import canonical_bytes, canonical_sha256
from .quick_scan_work_store import _budget_policy_projection

MAX_BYTES = 8 * 1024 * 1024
WIRE_FIELDS = {
    "protocol",
    "authority",
    "confirmation",
    "request",
    "owner_policy",
    "owner_state",
    "principal",
    "API_requests",
    "database_writes",
    "dispatch_started",
    "admission_mode",
}
CONFIRMATION_FIELDS = {
    "protocol",
    "confirmation_id",
    "owner_principal",
    "consumer_id",
    "idempotency_key_sha256",
    "preview_id",
    "preview_record_sha256",
    "request_sha256",
    "outer_request_sha256",
    "permission_request_sha256",
    "owner_state_sha256",
    "policy_sha256",
    "owner_store_id",
    "action_id",
    "confirmed_at",
    "method",
    "record_sha256",
}
STATE_FIELDS = {
    "store_id",
    "policy_id",
    "policy_version",
    "policy_sha256",
    "provider",
    "model",
    "ttl_hours",
    "member_versions",
    "anchors",
    "items",
}


def _utc(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("owner_work_UTC_required")
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _hash(value):
    return isinstance(value, str) and re.fullmatch("[a-f0-9]{64}", value) is not None


class StockWikiWorkDecisionAuthority(OwnerWorkDecisionAuthority):
    """Deployment chooses fixed roots and confirmation ID; payloads choose neither."""

    def __init__(
        self,
        owner,
        *,
        work_store,
        budget_policy_file,
        consumer_id,
        confirmation_id,
        refresh_schema_version=None,
        clock=None,
    ):
        if (
            not isinstance(owner, OwnerConsentEndpoint)
            or not isinstance(confirmation_id, str)
            or not re.fullmatch("rc1_[a-f0-9]{64}", confirmation_id)
            or not isinstance(consumer_id, str)
            or not re.fullmatch("[A-Za-z0-9_.:-]{1,160}", consumer_id)
            or refresh_schema_version not in {None, "1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.5.0"}
        ):
            raise OwnerRefreshRejected("owner_work_trusted_deployment_required")
        self.owner = owner
        self.work_store = Path(work_store).resolve()
        self.budget_policy_file = Path(budget_policy_file).resolve()
        self.consumer_id = consumer_id
        self.confirmation_id = confirmation_id
        self.refresh_schema_version = refresh_schema_version
        self.clock = clock or (
            lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        # Reuse the original sanitized native-read transport environment.
        self._transport = RefreshConsentClient(
            owner,
            work_store=self.work_store,
            budget_policy_file=self.budget_policy_file,
            consumer_id=consumer_id,
            clock=self.clock,
        )

    def _read_native_inspection(self, mode):
        c = self.owner.config
        if not (
            Path(c["stockwiki_code_root"]) / "stockwiki/quick_scan_refresh_owner_cli.py"
        ).is_file():
            raise OwnerRefreshRejected("owner_work_native_unavailable")
        command = [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            "-m",
            "stockwiki.quick_scan_refresh_owner_cli",
            "--root",
            c["stockwiki_workspace_root"],
            "--iqs-code-root",
            c["iqs_code_root"],
            "--iqs-release-root",
            c["iqs_release_root"],
            "--inspect-work-decision",
            self.confirmation_id,
            "--admission-inspection",
            mode,
        ]
        try:
            result = subprocess.run(
                command,
                cwd=c["stockwiki_code_root"],
                env=self._transport._environment(),
                capture_output=True,
                timeout=80,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )  # nosec B603
        except (OSError, subprocess.TimeoutExpired) as error:
            raise OwnerRefreshRejected("owner_work_native_unavailable") from error
        if result.returncode != 0 or len(result.stdout) > MAX_BYTES:
            raise OwnerRefreshRejected("owner_work_native_rejected")
        try:
            value = _read(result.stdout)
            self._validate(value)
            if value["admission_mode"] != mode:
                raise ValueError("owner_work_native_mode_mismatch")
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            OSError,
            ValidationError,
            Unresolvable,
            UnicodeError,
        ) as error:
            raise OwnerRefreshRejected("owner_work_confirmation_binding_invalid") from error
        return copy.deepcopy(value)

    def _existing_applied(self, value):
        """Only the actual validated local journal permits expiry replay."""
        request = value["request"]["decision_request"]
        view = ReadOnlyWorkView(self.work_store)
        with closing(view._connect()) as con:
            con.execute("BEGIN")
            if con.execute("PRAGMA user_version").fetchone()[0] < 19:
                return False
            if request["kind"] == "publish_saved_research":
                for member in request["publication"]["members"]:
                    checkpoint = view.get_answer_checkpoint(
                        member["work_item_id"], allow_research=True
                    )
                    if (
                        checkpoint is None
                        or checkpoint["checkpoint_schema_version"] != 3
                        or checkpoint["payload"]["publication"]["request"] != request
                        or checkpoint["payload"]["publication"]["grant"]["confirmation_id"]
                        != self.confirmation_id
                    ):
                        return False
                return True
            row = con.execute(
                "SELECT * FROM quick_scan_owner_work_decision WHERE work_item_id=? AND idempotency_key=?",
                (request["work_item_id"], request["idempotency_key"]),
            ).fetchone()
            if row is None:
                return False
            from .quick_scan_known_outcome_journal import _read

            document = _read(view, con, row)
            confirmation = value["confirmation"]
            expected_grant = dict(
                owner_store_id=confirmation["owner_store_id"],
                principal_id=confirmation["owner_principal"],
                confirmation_id=confirmation["confirmation_id"],
                action_id=confirmation["action_id"],
                request_sha256=confirmation["request_sha256"],
                confirmed_at=confirmation["confirmed_at"],
            )
            target = self._comparison_target(value)
            if target is not None:
                expected_grant["comparison_target"] = target
            if document["request"] != request or document["grant"] != expected_grant:
                raise ValueError("owner_work_existing_action_conflict")
            return True

    def inspect_confirmation(self):
        value = self._read_native_inspection("current_permissions")
        if self._existing_applied(value):
            return value
        fresh = self._read_native_inspection("fresh_registration")
        if {k: v for k, v in fresh.items() if k != "admission_mode"} != {
            k: v for k, v in value.items() if k != "admission_mode"
        }:
            raise ValueError("owner_work_current_permission_changed")
        return fresh

    def _schemas(self, value):
        root = Path(self.owner.config["iqs_code_root"]) / "schemas/quick_scan"
        protocol = value["owner_policy"]["protocol"]
        versions = {
            "stockwiki.refresh_owner_policy/1.0.0": "1.0.0",
            "stockwiki.refresh_owner_policy/1.1.0": "1.1.0",
            "stockwiki.refresh_owner_policy/1.2.0": "1.2.0",
            "stockwiki.refresh_owner_policy/1.3.0": "1.3.0",
            "stockwiki.refresh_owner_policy/1.5.0": "1.5.0",
        }
        if protocol not in versions:
            raise ValueError("owner_work_refresh_version_unsupported")
        version = versions[protocol]
        if self.refresh_schema_version is not None and version != self.refresh_schema_version:
            raise ValueError("owner_work_refresh_version_mismatch")
        name = {
            "1.0.0": "refresh-v1.schema.json",
            "1.1.0": "refresh-v1.1.schema.json",
            "1.2.0": "refresh-v1.2.schema.json",
            "1.3.0": "refresh-v1.3.schema.json",
            "1.5.0": "refresh-v1.5.schema.json",
        }[version]
        names = [
            name,
            "refresh-v1.schema.json",
            "refresh-v1.1.schema.json",
            "refresh-v1.2.schema.json",
            "refresh-v1.3.schema.json",
            "query-v2.schema.json",
            "query-v2.6.schema.json",
        ]
        schemas = [_read((root / p).read_bytes()) for p in names]
        registry = Registry().with_resources((s["$id"], Resource.from_contents(s)) for s in schemas)
        for kind, document in [
            ("OwnerPolicy", value["owner_policy"]),
            ("Request", value["request"]["permission_request"]),
        ]:
            chosen = schemas[0]
            if kind == "Request" and version in {"1.2.0", "1.3.0", "1.5.0"}:
                chosen = next(
                    s
                    for s in schemas
                    if s["$defs"]["Request"]["properties"]["protocol"]["const"]
                    == document["protocol"]
                )
            Draft202012Validator(
                {"$ref": chosen["$id"] + "#/$defs/" + kind},
                registry=registry,
                format_checker=FormatChecker(),
            ).validate(document)

    def _validate(self, value):
        publication = (
            isinstance(value, dict)
            and value.get("protocol") == "stockwiki.work_decision_inspection/1.1.0"
        )
        if (
            not isinstance(value, dict)
            or set(value)
            != (WIRE_FIELDS | {"publication_capability"} if publication else WIRE_FIELDS)
            or value["protocol"]
            != (
                "stockwiki.work_decision_inspection/1.1.0"
                if publication
                else "stockwiki.work_decision_inspection/1.0.0"
            )
            or value["authority"] != "native_owner_account_and_control_namespace"
            or value["admission_mode"] not in {"fresh_registration", "current_permissions"}
            or any(
                type(value[k]) is not int or value[k] != 0
                for k in ("API_requests", "database_writes")
            )
            or value["dispatch_started"] is not False
        ):
            raise ValueError("owner_work_native_wire_invalid")
        outer = value["request"]
        receipt = value["confirmation"]
        policy = value["owner_policy"]
        state = value["owner_state"]
        if (
            not isinstance(outer, dict)
            or set(outer) != {"protocol", "consumer_id", "decision_request", "permission_request"}
            or outer["protocol"]
            != (
                "stockwiki.work_decision_request/1.1.0"
                if publication
                else "stockwiki.work_decision_request/1.0.0"
            )
            or outer["consumer_id"] != self.consumer_id
            or not isinstance(outer["decision_request"], dict)
            or not isinstance(receipt, dict)
            or set(receipt) != CONFIRMATION_FIELDS
            or receipt["protocol"] != "stockwiki.work_decision_confirmation_record/1.0.0"
            or receipt["confirmation_id"] != self.confirmation_id
            or receipt["method"]
            not in (
                {"native_owner_terminal", "native_owner_delegated_agent"}
                if publication
                else {"native_owner_terminal"}
            )
            or not isinstance(state, dict)
            or set(state) != STATE_FIELDS
        ):
            raise ValueError("owner_work_confirmation_kind_invalid")
        self._schemas(value)
        request = outer["decision_request"]
        permission = outer["permission_request"]
        physical = request["physical"]
        if request["kind"] not in (
            {"publish_saved_research"}
            if publication
            else {"known_response_resolution", "retry_decision"}
        ):
            raise ValueError("owner_work_action_kind_invalid")
        if policy["protocol"] == "stockwiki.refresh_owner_policy/1.5.0" and not publication:
            raise ValueError("owner_work_publication_dialect_required")
        key = request["idempotency_key"]
        if not isinstance(key, str) or not re.fullmatch("[A-Za-z0-9_.:-]{1,160}", key):
            raise ValueError("owner_work_action_id_invalid")
        key_sha = hashlib.sha256(key.encode("ascii")).hexdigest()
        id_material = dict(
            domain="stockwiki.work_decision/1.0.0",
            owner_principal=receipt["owner_principal"],
            consumer_id=self.consumer_id,
            idempotency_key_sha256=key_sha,
        )
        if (
            receipt["confirmation_id"] != "rc1_" + canonical_sha256(id_material)
            or not isinstance(receipt["preview_id"], str)
            or not re.fullmatch("wdp1_[a-f0-9]{64}", receipt["preview_id"])
            or any(
                not _hash(receipt[k])
                for k in (
                    "idempotency_key_sha256",
                    "preview_record_sha256",
                    "request_sha256",
                    "outer_request_sha256",
                    "permission_request_sha256",
                    "owner_state_sha256",
                    "policy_sha256",
                    "record_sha256",
                )
            )
            or receipt["record_sha256"]
            != canonical_sha256({k: v for k, v in receipt.items() if k != "record_sha256"})
            or receipt["idempotency_key_sha256"] != key_sha
            or receipt["action_id"] != key
            or receipt["request_sha256"] != canonical_sha256(request)
            or receipt["outer_request_sha256"] != canonical_sha256(outer)
            or receipt["permission_request_sha256"] != canonical_sha256(permission)
            or receipt["owner_state_sha256"] != canonical_sha256(state)
            or receipt["policy_sha256"] != canonical_sha256(policy)
            or receipt["consumer_id"] != self.consumer_id
            or permission["consumer_id"] != self.consumer_id
            or receipt["owner_principal"] != value["principal"]
            or value["principal"] != policy["owner_principal"]
            or self.consumer_id not in policy["allowed_consumer_ids"]
            or receipt["owner_store_id"] != state["store_id"]
            or state["policy_sha256"] != receipt["policy_sha256"]
            or any(
                state[k] != policy[k]
                for k in (
                    "policy_id",
                    "policy_version",
                    "provider",
                    "model",
                    "ttl_hours",
                )
            )
            or _utc(receipt["confirmed_at"]) > _utc(self.clock())
        ):
            raise ValueError("owner_work_current_confirmation_mismatch")
        qa = policy["qa"]
        target = self._comparison_target(value)
        if (
            Path(qa["work_store"]).resolve() != self.work_store
            or Path(qa["budget_policy_file"]).resolve() != self.budget_policy_file
            or Path(qa["code_root"]).resolve() != Path(__file__).resolve().parents[2]
            or qa["policy_id"] != physical["budget_policy_id"]
            or target is None
            and (
                qa["route_id"] != physical["route_id"]
                or qa["provider_config_ref"] != physical["route_provider"]
                or policy["model"] != physical["model_requested"]
            )
        ):
            raise ValueError("owner_work_qa_deployment_mismatch")
        budget = _budget_policy_projection(_read(self.budget_policy_file.read_bytes()))
        route = budget["route_identity"].get(qa["route_id"])
        if (
            route is None
            or budget["policy_id"] != qa["policy_id"]
            or budget["currency"] != physical["budget_currency"]
            or route["model"] != policy["model"]
            or route["provider_config_ref"] != qa["provider_config_ref"]
        ):
            raise ValueError("owner_work_current_budget_route_mismatch")
        source_route = budget["route_identity"].get(physical["route_id"])
        if (
            source_route is None
            or source_route["model"] != physical["model_requested"]
            or source_route["provider_config_ref"] != physical["route_provider"]
        ):
            raise ValueError("owner_work_original_budget_route_mismatch")
        ref = physical["subject_binding"]["subject_ref"]
        if (
            permission["subject_refs"] != [ref]
            or not any(
                a["subject_ref"] == ref
                and a["identity_revision"] == physical["work_fingerprint"]["identity_revision"]
                for a in state["anchors"]
            )
            or physical["work_fingerprint"]["question_id"]
            not in {i["question_id"] for i in state["items"]}
            or set(permission["field_ids"]) != {i["field_id"] for i in state["items"]}
        ):
            raise ValueError("owner_work_current_subject_fields_mismatch")
        if publication:
            self._validate_publication(value)

    def _validate_publication(self, value):
        request = value["request"]["decision_request"]
        cap = value["publication_capability"]
        keys = {
            "protocol",
            "purpose",
            "enabled",
            "owner_principal",
            "consumer_id",
            "action_id",
            "task_bundle",
            "native_sdk_id",
            "expected_request_sha256",
            "expires_at",
            "confirmation_method",
            "c06_authority",
            "deployment",
        }
        if (
            not isinstance(cap, dict)
            or set(cap) != keys
            or cap["protocol"] != "stockwiki.saved_research_publication_capability/1.0.0"
            or cap["purpose"] != "research_no_verified_search"
            or cap["enabled"] is not True
            or cap["owner_principal"] != value["principal"]
            or cap["consumer_id"] != self.consumer_id
            or cap["action_id"] != request["idempotency_key"]
            or cap["expected_request_sha256"] != canonical_sha256(request)
            or cap["native_sdk_id"] != request["publication"]["native_sdk_id"]
            or cap["confirmation_method"] != value["confirmation"]["method"]
            or _utc(cap["expires_at"]) <= _utc(self.clock())
        ):
            raise ValueError("owner_work_publication_capability_not_current")
        for key, expected in [
            ("task_bundle", "task_bundle_sha256"),
            ("c06_authority", "c06_authority_sha256"),
        ]:
            ref = cap[key]
            if (
                not isinstance(ref, dict)
                or set(ref) != {"path", "sha256"}
                or not Path(ref["path"]).is_absolute()
                or hashlib.sha256(Path(ref["path"]).read_bytes()).hexdigest() != ref["sha256"]
                or ref["sha256"] != request["publication"][expected]
            ):
                raise ValueError("owner_work_publication_original_source_changed")
        from .quick_scan_saved_research_publication import inspect_publication

        native = inspect_publication(
            self.work_store,
            request,
            policy=_read(self.budget_policy_file.read_bytes()),
            task_bundle=Path(cap["task_bundle"]["path"]).read_bytes(),
            deployment=cap["deployment"],
        )
        if native["request"] != request or set(
            i["question_id"] for i in value["owner_state"]["items"]
        ) != set(request["publication"]["question_ids"]):
            raise ValueError("owner_work_publication_members_changed")

    @staticmethod
    def _comparison_target(value):
        policy = value.get("owner_policy", {})
        request = value["request"]["decision_request"]
        if policy.get("protocol") != "stockwiki.refresh_owner_policy/1.3.0":
            return None
        if request["kind"] != "retry_decision" or request.get("purpose") != "comparison":
            raise ValueError("owner_work_target_dialect_requires_comparison")
        from .quick_scan_known_outcome_journal import validate_comparison_target

        target = dict(
            provider=policy["provider"],
            model=policy["model"],
            route_id=policy["qa"]["route_id"],
            provider_config_ref=policy["qa"]["provider_config_ref"],
            policy_id=policy["qa"]["policy_id"],
            owner_policy_sha256=canonical_sha256(policy),
        )
        return validate_comparison_target(target, request)

    def authorize(self, decision_request):
        if (
            not isinstance(decision_request, dict)
            or len(canonical_bytes(decision_request)) > 1024 * 1024
        ):
            raise OwnerRefreshRejected("owner_work_decision_request_invalid")
        result = self.inspect_confirmation()
        if result["request"]["decision_request"] != decision_request:
            raise OwnerRefreshRejected("owner_work_authorized_request_mismatch")
        receipt = result["confirmation"]
        return OwnerWorkDecisionGrant(
            owner_store_id=receipt["owner_store_id"],
            principal_id=receipt["owner_principal"],
            confirmation_id=receipt["confirmation_id"],
            action_id=receipt["action_id"],
            request_sha256=receipt["request_sha256"],
            confirmed_at=receipt["confirmed_at"],
            comparison_target=self._comparison_target(result),
        )
