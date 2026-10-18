"""Fixed native StockWiki consent inspection, NOT an admission endpoint.

Only trusted deployment configuration selects roots and original ledger/policy.
Consumer input is one immutable confirmation ID. A current_permissions read
does not authenticate a new registration; the Q13 journal decides replay.
"""

import copy
import os
import re

# The configured first-party OS CLI uses argv, never shell commands.
import subprocess  # nosec B404
import sys
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable

from .quick_scan_owner_refresh import OwnerRefreshClient, OwnerRefreshRejected, _read
from .quick_scan_result_outbox import canonical_sha256
from .quick_scan_work_store import _budget_policy_projection

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
        self._environment = process_env


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

    def _environment(self):
        source = self.owner._environment if self.owner._environment is not None else os.environ
        env = {
            k: v
            for k, v in source.items()
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
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        env["PYTHONPATH"] = self.owner.config["stockwiki_code_root"]
        if source.get("IQS_C06_RUN_INTEGRATION") == "1":
            owned = Path(source["IQS_C06_OWN"]).resolve()
            env.update(IQS_C06_RUN_INTEGRATION="1", IQS_C06_OWN=str(owned))
            env["PYTHONPATH"] = os.pathsep.join([str(owned / "guard"), env["PYTHONPATH"]])
            env.update(
                TEMP=str(owned / "tmp"),
                TMP=str(owned / "tmp"),
                TMPDIR=str(owned / "tmp"),
            )
        return env

    def inspect(self, confirmation_id, *, mode):
        if (
            not isinstance(confirmation_id, str)
            or not re.fullmatch("rc[12]_[a-f0-9]{64}", confirmation_id)
            or mode not in {"fresh_registration", "current_permissions"}
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
        try:
            # Fixed sys.executable/module; all paths and IDs are separate argv entries.
            completed = subprocess.run(  # nosec B603
                command,
                cwd=c["stockwiki_code_root"],
                env=self._environment(),
                capture_output=True,
                timeout=80,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise OwnerRefreshRejected("refresh_consent_native_unavailable") from error
        if completed.returncode != 0 or len(completed.stdout) > MAX_BYTES:
            raise OwnerRefreshRejected("refresh_consent_native_rejected")
        result = _read(completed.stdout)
        try:
            self._validate(result, confirmation_id, mode)
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            OSError,
            ValidationError,
            Unresolvable,
        ) as error:
            raise OwnerRefreshRejected("refresh_consent_binding_invalid") from error
        return copy.deepcopy(result)

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
        root = Path(self.owner.config["iqs_code_root"]) / "schemas/quick_scan"
        dialect = result["request"].get("protocol")
        refresh_name = {
            "iqs.refresh_request/1.0.0": "refresh-v1.schema.json",
            "iqs.refresh_request/1.1.0": "refresh-v1.1.schema.json",
            "iqs.refresh_request/1.2.0": "refresh-v1.2.schema.json",
            "iqs.refresh_request/1.3.0": "refresh-v1.3.schema.json",
            "iqs.refresh_request/1.4.0": "refresh-v1.4.schema.json",
        }.get(dialect)
        if refresh_name is None:
            raise ValueError("unsupported explicit refresh dialect")
        names = (refresh_name, "query-v2.schema.json", "query-v2.6.schema.json")
        schemas = [_read((root / name).read_bytes()) for name in names]
        registry = Registry().with_resources((s["$id"], Resource.from_contents(s)) for s in schemas)
        for field, kind in [
            ("request", "Request"),
            ("preview", "Preview"),
            ("owner_state", "OwnerState"),
            ("owner_policy", "OwnerPolicy"),
        ]:
            Draft202012Validator(
                {"$ref": schemas[0]["$id"] + "#/$defs/" + kind},
                registry=registry,
                format_checker=FormatChecker(),
            ).validate(result[field])
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
            or receipt["owner_principal"] != policy["owner_principal"]
            or receipt["consumer_id"] != self.consumer_id
            or result["request"]["consumer_id"] != self.consumer_id
            or self.consumer_id not in policy["allowed_consumer_ids"]
            or receipt["preview_id"] != preview["preview_id"]
            or preview["preview_id"]
            != "rv1_" + canonical_sha256({k: v for k, v in preview.items() if k != "preview_id"})
            or preview["request_sha256"] != canonical_sha256(result["request"])
            or preview["owner_state_sha256"] != canonical_sha256(state)
            or preview["policy_sha256"] != canonical_sha256(policy)
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
            or preview["provider"] != policy["provider"]
            or preview["model"] != policy["model"]
        ):
            raise ValueError("native consent scope mismatch")
        for key, expected in [
            ("code_root", Path(__file__).resolve().parents[2]),
            ("work_store", self.work_store),
            ("budget_policy_file", self.budget_policy_file),
        ]:
            if Path(policy["qa"][key]).resolve() != expected:
                raise ValueError("native consent deployment mismatch")
        budget = _budget_policy_projection(_read(self.budget_policy_file.read_bytes()))
        qa = policy["qa"]
        route = budget["route_identity"].get(qa["route_id"])
        if (
            qa["policy_id"] != budget["policy_id"]
            or preview["quote"]["currency"] != budget["currency"]
            or state["quote"] != preview["quote"]
            or route is None
            or route["provider_config_ref"] != qa["provider_config_ref"]
            or route["model"] != policy["model"]
        ):
            raise ValueError("native consent executor policy mismatch")
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
            )
