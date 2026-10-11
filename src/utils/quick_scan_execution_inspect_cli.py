"""Fixed native read-only execution inspection CLI; no credentials or API calls."""

import argparse
import json
import sqlite3
from pathlib import Path

from src.utils.quick_scan_execution_inspect import inspect_executions
from src.utils.quick_scan_result_outbox import strict_json_loads


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        with args.request.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("execution_inspection_input_too_large")
        request = strict_json_loads(raw.decode("utf-8-sig"))
        result = inspect_executions(args.ledger, request)
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
    ) as error:
        message = str(error)
        code = (
            message
            if message.startswith("execution_inspection_") and "\n" not in message
            else "execution_inspection_rejected"
        )
        print(
            json.dumps(
                dict(
                    protocol="stockqa.execution_inspection/1.0.0",
                    status="rejected",
                    error_code=code,
                    API_requests=0,
                    database_writes=0,
                    financial_accuracy_proven=False,
                    screening_audited=False,
                )
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
