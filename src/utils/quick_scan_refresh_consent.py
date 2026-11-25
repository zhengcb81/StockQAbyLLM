"""Fixed native StockWiki consent inspection, NOT an admission endpoint.

Only trusted deployment configuration selects roots and original ledger/policy.
Consumer input is one immutable confirmation ID. A current_permissions read
does not authenticate a new registration; the Q13 journal decides replay.
"""

import copy
import hashlib
import re

# The configured first-party OS CLI uses argv, never shell commands.
import subprocess  # nosec B404
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.interfaces.quick_scan_work_values import (
    budget_policy_projection as _budget_policy_projection,
)

from .quick_scan_owner_process_env import consent_subprocess_env
from .quick_scan_owner_refresh import OwnerRefreshClient, OwnerRefreshRejected
from .quick_scan_owner_refresh import api_owner_refresh_read as _read
from .quick_scan_result_outbox import canonical_bytes, canonical_sha256

MAX_BYTES = 8 * 1024 * 1024
WIRE_FIELDS = {
    "protocol",
    "authority",
    "confirmation",
    "request",
    "preview",
    "owner_state",
    "dispatch_started",
    "registered_with_executor",
    "API_requests",
    "inspection_mode",
    "owner_policy",
}


def _utc(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("UTC required")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    return parsed


class OwnerConsentEndpoint:
    """Trusted deployment roots for consent reads, without a fake subject."""

    def __init__(self, config, *, process_env=None):
        keys = {
            "stockwiki_code_root",
            "stockwiki_workspace_root",
            "iqs_code_root",
            "iqs_release_root",
        }
        if set(config) != keys or any(
            not isinstance(config[k], str) or not Path(config[k]).is_absolute() for k in keys
        ):
            raise OwnerRefreshRejected("refresh_consent_deployment_required")
        self.config = copy.deepcopy(config)
        self._process_env = process_env

    def consent_subprocess_env(self, python_path: str | None = None) -> dict[str, str]:
        return consent_subprocess_env(
            self.config["stockwiki_code_root"], self._process_env, python_path
        )


class RefreshConsentClient:
    def __init__(self, owner, *, work_store, budget_policy_file, consumer_id, clock=None):
        if not isinstance(owner, (OwnerRefreshClient, OwnerConsentEndpoint)):
            raise OwnerRefreshRejected("refresh_consent_deployment_required")
        self.owner = owner
        self.work_store = Path(work_store).resolve()
        self.budget_policy_file = Path(budget_policy_file).resolve()
        if not isinstance(consumer_id, str) or not 1 <= len(consumer_id) <= 160:
            raise OwnerRefreshRejected("refresh_consent_deployment_required")
        self.consumer_id = consumer_id
        self.clock = clock or (
            lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )

    def subprocess_env(self, python_path: str | None = None) -> dict[str, str]:
        return self.owner.consent_subprocess_env(python_path)

    def inspect(self, confirmation_id, *, mode):
        if (
            not isinstance(confirmation_id, str)
            or not re.fullmatch("rc[12]_[a-f0-9]{64}", confirmation_id)
            or mode not in {"fresh_registration", "current_permissions", "paid_response_recovery"}
        ):
            raise OwnerRefreshRejected("refresh_consent_inspection_request_invalid")
        c = self.owner.config
        if not (
            Path(c["stockwiki_code_root"]) / "stockwiki/quick_scan_refresh_owner_cli.py"
        ).is_file():
            raise OwnerRefreshRejected("refresh_consent_native_unavailable")
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
            "--inspect-confirmation",
            confirmation_id,
            "--admission-inspection",
            mode,
        ]
        result = self._run_read(
            command,
            cwd=c["stockwiki_code_root"],
            python_path=c["stockwiki_code_root"],
            operation="native",
        )
        try:
            self._validate(result, confirmation_id, mode)
        except OwnerRefreshRejected:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, OSError) as error:
            raise OwnerRefreshRejected("refresh_consent_binding_invalid") from error
        return copy.deepcopy(result)

    def _run_read(self, command, *, cwd, python_path, operation, input_bytes=None):
        try:
            completed = subprocess.run(  # nosec B603
                command,
                cwd=cwd,
                env=self.subprocess_env(python_path),
                input=input_bytes,
                capture_output=True,
                timeout=80,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise OwnerRefreshRejected(f"refresh_consent_{operation}_unavailable") from error
        if completed.returncode != 0 or len(completed.stdout) > MAX_BYTES:
            raise OwnerRefreshRejected(f"refresh_consent_{operation}_rejected")
        return _read(completed.stdout)

    def _validate_frozen_record(self, original):
        # StockWiki owns the authenticated record and current permissions. IQS
        # owns serialization of the original request, preview and owner state.
        record = {field: original[field] for field in ("request", "preview", "owner_state")}
        raw = canonical_bytes(record)
        # Only the immutable serializer result is reusable within this client.
        # inspect() still invokes the native current-permission CLI every time.
        digest = hashlib.sha256(raw).hexdigest()
        if getattr(self, "_validated_frozen_record_sha256", None) == digest:
            return
        root = Path(self.owner.config["iqs_code_root"])
        reply = self._run_read(
            [
                sys.executable,
                "-B",
                "-X",
                "utf8",
                str(root / "scripts/refresh_contract_cli.py"),
                "--operation",
                "frozen-record",
                "--input",
                "-",
            ],
            cwd=str(root),
            python_path=str(root / "scripts"),
            operation="frozen_contract",
            input_bytes=raw,
        )
        if (
            reply.get("protocol") != "iqs.refresh_validation/1.0.0"
            or reply.get("status") != "validated"
            or reply.get("operation") != "frozen-record"
            or reply.get("input_sha256") != digest
            or any(
                type(reply.get(field)) is not int or reply[field] != 0
                for field in ("API_requests", "database_writes")
            )
            or reply.get("permission_authenticated") is not False
        ):
            raise ValueError("original refresh contract read mismatch")
        self._validated_frozen_record_sha256 = digest

    def _validate(self, result, confirmation_id, mode):
        delegated = confirmation_id.startswith("rc2_")
        inspection_protocol = "stockwiki.refresh_approval_inspection/" + (
            "1.2.0" if delegated else "1.1.0"
        )
        if (
            set(result) != WIRE_FIELDS
            or result["protocol"] != inspection_protocol
            or result["authority"] != "native_owner_account_and_control_namespace"
            or result["inspection_mode"] != mode
            or type(result["API_requests"]) is not int
            or result["API_requests"] != 0
            or result["dispatch_started"] is not False
            or result["registered_with_executor"] is not False
        ):
            raise ValueError("native read receipt invalid")
        self._validate_frozen_record(result)
        dialect = result["request"]["protocol"]
        receipt = result["confirmation"]
        preview = result["preview"]
        policy = result["owner_policy"]
        state = result["owner_state"]
        if (
            receipt["protocol"]
            != "stockwiki.refresh_confirmation_record/" + ("2.0.0" if delegated else "1.0.0")
            or receipt["confirmation_id"] != confirmation_id
            or receipt["method"]
            != ("native_owner_delegated_agent" if delegated else "native_owner_terminal")
            or receipt["record_sha256"]
            != canonical_sha256({k: v for k, v in receipt.items() if k != "record_sha256"})
            or mode != "paid_response_recovery"
            and receipt["owner_principal"] != policy["owner_principal"]
            or receipt["consumer_id"] != self.consumer_id
            or result["request"]["consumer_id"] != self.consumer_id
            or mode != "paid_response_recovery"
            and self.consumer_id not in policy["allowed_consumer_ids"]
            or receipt["preview_id"] != preview["preview_id"]
            or (
                mode != "paid_response_recovery"
                and preview["policy_sha256"] != canonical_sha256(policy)
            )
            or any(
                receipt[k] != preview[k]
                for k in (
                    "request_sha256",
                    "scope_sha256",
                    "owner_state_sha256",
                    "policy_sha256",
                )
            )
            or preview["status"] != "ready"
            or preview["requires_user_confirmation"] is not True
            or preview["items"] != state["items"]
            or (
                mode != "paid_response_recovery"
                and (
                    preview["provider"] != policy["provider"] or preview["model"] != policy["model"]
                )
            )
        ):
            raise ValueError("native consent scope mismatch")
        for key, expected in [
            ("code_root", Path(__file__).resolve().parents[2]),
            ("work_store", self.work_store),
            ("budget_policy_file", self.budget_policy_file),
        ]:
            if mode != "paid_response_recovery" and Path(policy["qa"][key]).resolve() != expected:
                raise ValueError("native consent deployment mismatch")
        budget = (
            None
            if mode == "paid_response_recovery"
            else _budget_policy_projection(_read(self.budget_policy_file.read_bytes()))
        )
        qa = (
            receipt["delegation"]["binding"]["qa"]
            if delegated and mode == "paid_response_recovery"
            else policy["qa"]
        )
        route = None if budget is None else budget["route_identity"].get(qa["route_id"])
        if budget is not None and (
            qa["policy_id"] != budget["policy_id"]
            or preview["quote"]["currency"] != budget["currency"]
            or state["quote"] != preview["quote"]
            or route is None
            or route["provider_config_ref"] != qa["provider_config_ref"]
            or (mode != "paid_response_recovery" and route["model"] != policy["model"])
        ):
            raise ValueError("native consent executor policy mismatch")
        if dialect == "iqs.refresh_request/1.6.0":
            from .quick_scan_owner_refresh_journal import (
                api_owner_refresh_journal_ordered_targets as ordered_targets,
            )

            selected = ordered_targets(
                preview["ordered_route_targets"],
                provider=preview["provider"],
                model=preview["model"],
            )
            if (
                selected != state["ordered_route_targets"]
                or budget is not None
                and (
                    selected != policy["ordered_route_targets"]
                    or any(
                        selected[0][key] != qa[key] for key in ("route_id", "provider_config_ref")
                    )
                    or any(
                        budget["route_identity"].get(target["route_id"])
                        != {
                            key: target[key]
                            for key in ("provider_config_ref", "model", "quota_group")
                        }
                        for target in selected
                    )
                )
            ):
                raise ValueError("native consent ordered executor policy mismatch")
        if (
            not _utc(preview["created_at"])
            <= _utc(receipt["confirmed_at"])
            < _utc(preview["expires_at"])
        ):
            raise ValueError("native consent time mismatch")
        now = _utc(self.clock())
        if (
            _utc(receipt["confirmed_at"]) > now
            or mode == "fresh_registration"
            and now >= _utc(preview["expires_at"])
        ):
            raise ValueError("native consent expired or future")

        if delegated:
            from .quick_scan_delegated_consent import validate_delegated_native

            validate_delegated_native(
                result,
                workspace_root=self.owner.config["stockwiki_workspace_root"],
                budget_policy_file=self.budget_policy_file,
                now=self.clock(),
                recovery_only=mode == "paid_response_recovery",
            )
