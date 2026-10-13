"""Fixed local inspect CLI; trusted deployment paths, no keys/model calls."""

import argparse
import json
import sqlite3
from pathlib import Path

from src.utils.quick_scan_refresh_inspect import inspect_refresh
from src.utils.quick_scan_result_outbox import strict_json_loads


def read(path):
    with path.open("rb") as stream:
        raw = stream.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("refresh_input_too_large")
    return strict_json_loads(raw.decode("utf-8-sig"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--schema-version", choices=("1.0.0", "1.1.0"), default="1.0.0")
    args = parser.parse_args(argv)
    try:
        result = inspect_refresh(
            args.ledger,
            read(args.request),
            policy=read(args.policy),
            schema_version=args.schema_version,
        )
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (
        ValueError,
        TypeError,
        KeyError,
        OSError,
        UnicodeError,
        RecursionError,
        sqlite3.Error,
    ):
        print(
            json.dumps(
                dict(
                    protocol="stockqa.refresh_inspection/" + args.schema_version,
                    status="rejected",
                    error_code="refresh_executor_inspection_rejected",
                    API_requests=0,
                    reservation_created=False,
                )
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
