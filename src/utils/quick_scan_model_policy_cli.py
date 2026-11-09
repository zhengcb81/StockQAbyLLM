"""Project or update StockQA's model policy without exporting provider credentials."""

import argparse
import hashlib
import json
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path

from src.utils._quick_scan_owner_io import deny_network, read_request

REQUEST_PROTOCOL = "stockqa.model_policy_request/1.0.0"
RESPONSE_PROTOCOL = "stockqa.model_policy_response/1.0.0"
FIELDS = {
    "read": {"protocol", "operation"},
    "save": {"protocol", "operation", "policy", "revision_sha256"},
}


def _policy_projection(path: Path, request: dict) -> dict:
    from src.config.llm_config import LLMConfig

    raw = path.read_bytes()
    document = json.loads(raw.decode("utf-8-sig"))
    operation = request["operation"]
    policy = document.get("quick_scan_model_policy")
    revision = hashlib.sha256(raw).hexdigest()
    if operation == "save":
        requested_revision = request["revision_sha256"]
        if not isinstance(requested_revision, str) or not re.fullmatch(
            r"[a-f0-9]{64}", requested_revision
        ):
            raise ValueError("invalid_revision")
        if requested_revision != revision:
            raise RuntimeError("settings_changed")
        policy = request["policy"]
        LLMConfig(str(path)).save_quick_scan_model_policy(policy)
        revision = hashlib.sha256(path.read_bytes()).hexdigest()
    elif policy is not None:
        LLMConfig.validate_quick_scan_model_policy(policy)

    providers = document.get("providers")
    choices = []
    if isinstance(providers, dict):
        for name, value in sorted(providers.items()):
            if not name or len(name) > 200 or not isinstance(value, dict):
                continue
            model = value.get("model", "")
            choices.append(
                {
                    "provider_config_ref": name,
                    "model": model if isinstance(model, str) and len(model) <= 200 else "",
                    "enabled": value.get("enabled") is True,
                }
            )
    return {
        "policy": policy,
        "revision_sha256": revision,
        "provider_config_refs": [row["provider_config_ref"] for row in choices],
        "provider_choices": choices,
        "provider_metadata_available": isinstance(providers, dict),
        "effective": "next_run",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    sys.addaudithook(deny_network)
    try:
        with redirect_stdout(sys.stderr):
            request = read_request()
            operation = request.get("operation")
            if (
                request.get("protocol") != REQUEST_PROTOCOL
                or not isinstance(operation, str)
                or set(request) != FIELDS.get(operation)
                or not args.config.is_absolute()
            ):
                raise ValueError("invalid_policy_request")
            result = _policy_projection(args.config, request)
        response = {"protocol": RESPONSE_PROTOCOL, "operation": operation, **result}
        code = 0
    except Exception as error:
        reason = "settings_changed" if str(error) == "settings_changed" else "owner_policy_rejected"
        response = {"protocol": RESPONSE_PROTOCOL, "error_code": reason}
        code = 2
    print(json.dumps({**response, "API_requests": 0, "network_calls": 0}, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
