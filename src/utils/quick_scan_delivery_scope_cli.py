"""Read the original StockQA delivery scope from an existing ledger.

The local caller supplies a trusted absolute ``--ledger`` path and a
``stockqa.delivery_scope_request/1.0.0`` JSON object on stdin. ``inspect``
may read current work without a confirmation. ``receivers`` and ``export``
require the original registered confirmation. Stdout always uses the
``stockqa.delivery_scope_response/1.0.0`` JSON envelope after argv parsing.
"""

import argparse
import json
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from src.utils._quick_scan_owner_io import deny_network, read_request

REQUEST_PROTOCOL = "stockqa.delivery_scope_request/1.0.0"
RESPONSE_PROTOCOL = "stockqa.delivery_scope_response/1.0.0"


def _read_scope() -> tuple[str, dict[str, Any]]:
    request = read_request()
    if (
        set(request) != {"protocol", "operation", "scope"}
        or request["protocol"] != REQUEST_PROTOCOL
        or request["operation"] not in ("inspect", "receivers", "export")
    ):
        raise ValueError("delivery_scope_request_invalid")
    operation, scope = request["operation"], request["scope"]
    required = {"entity_id", "question_ids"}
    if (
        not isinstance(scope, dict)
        or not required <= scope.keys() <= required | {"scope_bindings", "confirmation_id"}
        or not isinstance(scope["entity_id"], str)
        or not scope["entity_id"]
        or not isinstance(scope["question_ids"], list)
        or any(not isinstance(qid, str) or not qid for qid in scope["question_ids"])
        or (
            scope.get("scope_bindings") is not None
            and not isinstance(scope["scope_bindings"], dict)
        )
    ):
        raise ValueError("delivery_scope_request_invalid")
    if ("confirmation_id" in scope or operation != "inspect") and (
        not isinstance(scope.get("confirmation_id"), str) or not scope["confirmation_id"]
    ):
        raise ValueError("original_refresh_action_required")
    if operation == "inspect":
        scope["read_only_scope"] = True
    elif operation == "receivers":
        scope["receiver_only"] = True
    return operation, scope


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--ledger", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        with redirect_stdout(sys.stderr):
            operation, scope = _read_scope()
            if not args.ledger.is_absolute():
                raise ValueError("delivery_scope_ledger_not_absolute")
            from src.utils.quick_scan_refresh_inspect import delivery_scope_projection

            result = delivery_scope_projection(args.ledger, scope)
        response = {
            "protocol": RESPONSE_PROTOCOL,
            "operation": operation,
            "status": "ok",
            "API_requests": result.pop("API_requests"),
            "network_calls": result.pop("network_calls"),
            "result": result,
        }
        print(json.dumps(response, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, TypeError, KeyError, OSError, RuntimeError, sqlite3.Error):
        print(
            json.dumps(
                {
                    "protocol": RESPONSE_PROTOCOL,
                    "status": "rejected",
                    "error_code": "original_delivery_export_rejected",
                    "API_requests": 0,
                    "network_calls": 0,
                }
            )
        )
        return 2


if __name__ == "__main__":
    sys.addaudithook(deny_network)
    raise SystemExit(main())
