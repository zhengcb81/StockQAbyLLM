"""Read the original frozen question bank bindings without claiming new work.

Stdin takes ``stockqa.continuation_bank_request/1.0.0`` and a subject binding.
Choose an existing ledger with the trusted absolute ``--ledger`` or the
original ``--admission-config``. Stdout uses the existing versioned bank
binding projection, with zero database writes and model calls.
"""

import argparse
import json
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from src.utils._quick_scan_owner_io import deny_network, read_request

REQUEST_PROTOCOL = "stockqa.continuation_bank_request/1.0.0"
RESPONSE_PROTOCOL = "iqs.original_work_bank_bindings/1.0.0"


def _binding_request() -> dict[str, Any]:
    request = read_request()
    binding = request.get("binding")
    text_fields = {"entity_id", "subject_key", "perimeter_sha256", "information_cutoff"}
    required = text_fields | {"identity_revision", "scope_bindings"}
    if (
        set(request) != {"protocol", "binding"}
        or request["protocol"] != REQUEST_PROTOCOL
        or not isinstance(binding, dict)
        or not required <= binding.keys() <= required | {"controlled_work_refs"}
        or any(not isinstance(binding[key], str) or not binding[key] for key in text_fields)
        or type(binding["identity_revision"]) is not int
        or not isinstance(binding["scope_bindings"], dict)
    ):
        raise ValueError("continuation_bank_request_invalid")
    refs = binding.get("controlled_work_refs", [])
    if not isinstance(refs, list) or any(
        not isinstance(ref, dict)
        or set(ref) != {"work_item_id", "decision_id"}
        or any(not isinstance(value, str) or not value for value in ref.values())
        for ref in refs
    ):
        raise ValueError("continuation_bank_controlled_refs_invalid")
    return binding


def _ledger(args: argparse.Namespace) -> Path:
    if args.ledger is not None:
        return Path(args.ledger)
    if not args.admission_config.is_absolute():
        raise ValueError("existing_work_deployment_invalid")
    from src.utils.quick_scan_refresh_admission import safe_read

    config = safe_read(args.admission_config)
    if config.get("protocol") != "stockqa.refresh_admission_config/1.0.0":
        raise ValueError("existing_work_deployment_invalid")
    return Path(config["work_store"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--ledger", type=Path)
    selector.add_argument("--admission-config", type=Path)
    args = parser.parse_args(argv)
    try:
        with redirect_stdout(sys.stderr):
            binding = _binding_request()
            ledger = _ledger(args)
            if not ledger.is_absolute():
                raise ValueError("existing_work_deployment_invalid")
            from src.utils.quick_scan_refresh_inspect import continuation_bank_bindings

            result = continuation_bank_bindings(ledger, binding)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, TypeError, KeyError, OSError, RuntimeError, sqlite3.Error):
        print(
            json.dumps(
                {
                    "protocol": RESPONSE_PROTOCOL,
                    "status": "rejected",
                    "error_code": "existing_work_binding_unavailable",
                    "API_requests": 0,
                    "database_writes": 0,
                }
            )
        )
        return 2


if __name__ == "__main__":
    sys.addaudithook(deny_network)
    raise SystemExit(main())
