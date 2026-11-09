"""Bind the original delivery receiver or reconcile original ACKs in StockQA.

Run with a trusted absolute ``--ledger`` path. Stdin takes a versioned JSON
request with ``operation`` and ``payload``. Stdout returns the matching
operation, the original per-item outcome and zero model/network counts.
"""

import argparse
import json
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.utils._quick_scan_owner_io import deny_network, read_request

if TYPE_CHECKING:
    from src.utils.quick_scan_work_store import QuickScanWorkStore

REQUEST_PROTOCOL = "stockqa.delivery_control_request/1.0.0"
RESPONSE_PROTOCOL = "stockqa.delivery_control_response/1.0.0"
_FIELDS = {
    "acknowledge": {"work_item_ids", "receipt"},
    "bind": {"work_item_ids", "target", "source_ref"},
}


def _acknowledge(store: "QuickScanWorkStore", mapping, acks) -> dict[str, Any]:
    from src.utils.quick_scan_work_store import LeaseFencedError

    rows = []
    present = set()
    for ack in acks:
        observation = ack["observation_id"]
        work = mapping[observation]
        row = {"work_item_id": work, "observation_id": observation}
        try:
            row["state"] = store.apply_result_delivery_ack(work, ack)["state"]
        except (ValueError, LeaseFencedError) as error:
            row.update(state="ack_rejected", error_type=type(error).__name__)
        rows.append(row)
        present.add(observation)
    rows.extend(
        {"work_item_id": work, "observation_id": observation, "state": "ack_missing"}
        for observation, work in mapping.items()
        if observation not in present
    )
    return {
        "status": (
            "delivered" if all(row["state"] == "delivered" for row in rows) else "owner_rejected"
        ),
        "items": rows,
    }


def _bind(store: "QuickScanWorkStore", mapping, target, source_ref) -> dict[str, Any]:
    originals = []
    for observation, work in mapping.items():
        delivery = store.get_result_delivery(work)
        if delivery is None or delivery["observation_id"] != observation:
            raise ValueError("observation_scope_mismatch")
        binding = delivery.get("consumer_binding")
        if binding is not None and binding["consumer"] != target:
            raise ValueError("original_consumer_binding_mismatch")
        originals.append((work, binding))

    rows = []
    for work, binding in originals:
        if binding is None:
            binding = store.bind_result_delivery_consumer(work, target, source_ref=source_ref)
        rows.append({"work_item_id": work, "binding_sha256": binding["binding_sha256"]})
    return {"status": "bound", "items": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--ledger", type=Path, required=True)
    args = parser.parse_args(argv)
    error_code = "delivery_control_request_invalid"
    try:
        with redirect_stdout(sys.stderr):
            request = read_request()
            operation, payload = request.get("operation"), request.get("payload")
            if (
                set(request) != {"protocol", "operation", "payload"}
                or request["protocol"] != REQUEST_PROTOCOL
                or operation not in ("bind", "acknowledge")
                or not isinstance(payload, dict)
                or set(payload) != _FIELDS[operation]
            ):
                raise ValueError("delivery_control_request_invalid")
            error_code = "ack_owner_unconfigured"
            mapping = payload["work_item_ids"]
            if (
                not args.ledger.is_absolute()
                or not args.ledger.is_file()
                or not isinstance(mapping, dict)
                or not mapping
                or any(
                    not isinstance(key, str) or not key or not isinstance(work, str) or not work
                    for key, work in mapping.items()
                )
                or len(set(mapping.values())) != len(mapping)
            ):
                raise ValueError("original_work_ledger_or_mapping_invalid")
            if operation == "acknowledge":
                error_code = "ack_scope_mismatch"
                receipt = payload["receipt"]
                acks = receipt.get("acks") if isinstance(receipt, dict) else None
                if (
                    not isinstance(acks, list)
                    or not acks
                    or any(
                        not isinstance(ack, dict) or not isinstance(ack.get("observation_id"), str)
                        for ack in acks
                    )
                    or len({ack["observation_id"] for ack in acks}) != len(acks)
                    or not {ack["observation_id"] for ack in acks} <= mapping.keys()
                ):
                    raise ValueError("ack_scope_mismatch")
                error_code = "original_ack_rejected"
            else:
                error_code = "original_consumer_binding_rejected"
                if not isinstance(payload["target"], dict) or not isinstance(
                    payload["source_ref"], str
                ):
                    raise ValueError("original_consumer_binding_invalid")

            from src.utils.quick_scan_refresh_inspect import ReadOnlyWorkView
            from src.utils.quick_scan_work_store import QuickScanWorkStore

            # Apply the same owner path checks as the read command before opening a writer.
            existing = ReadOnlyWorkView(args.ledger)
            store = QuickScanWorkStore(existing.path, create=False)
            if operation == "acknowledge":
                result = _acknowledge(store, mapping, acks)
            else:
                result = _bind(store, mapping, payload["target"], payload["source_ref"])
        print(
            json.dumps(
                {
                    "protocol": RESPONSE_PROTOCOL,
                    "operation": operation,
                    **result,
                    "API_requests": 0,
                    "network_calls": 0,
                },
                allow_nan=False,
            )
        )
        return 0
    except (ValueError, TypeError, KeyError, OSError, RuntimeError, sqlite3.Error):
        print(
            json.dumps(
                {
                    "protocol": RESPONSE_PROTOCOL,
                    "status": "rejected",
                    "error_code": error_code,
                    "API_requests": 0,
                    "network_calls": 0,
                }
            )
        )
        return 2


if __name__ == "__main__":
    sys.addaudithook(deny_network)
    raise SystemExit(main())
