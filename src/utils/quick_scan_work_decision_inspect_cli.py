"""Fixed read-only local work-decision CLI; never another dispatch entry."""

import argparse
import json
import sqlite3
from pathlib import Path

from .quick_scan_refresh_inspect_cli import read
from .quick_scan_work_decision_inspect import (
    derive_work_decision,
    inspect_work_decision,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--request", type=Path)
    operation.add_argument("--intent", type=Path)
    parser.add_argument("--comparison-target", type=Path)
    parser.add_argument("--saved-research-task", type=Path)
    parser.add_argument("--saved-research-deployment", type=Path)
    args = parser.parse_args(argv)
    try:
        function = inspect_work_decision if args.request else derive_work_decision
        options = {}
        if args.saved_research_task:
            if args.saved_research_deployment is None:
                raise ValueError("saved_research_fixed_deployment_required")
            options["saved_research_task"] = args.saved_research_task
            options["saved_research_deployment"] = read(args.saved_research_deployment)
        if args.comparison_target:
            if not args.request:
                raise ValueError("comparison_target_requires_exact_request")
            options["comparison_target"] = read(args.comparison_target)
        result = function(
            args.ledger, read(args.request or args.intent), policy=read(args.policy), **options
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
        RuntimeError,
    ):
        print(
            json.dumps(
                dict(
                    protocol="stockqa.work_decision_inspection/1.0.0",
                    status="rejected",
                    error_code="owner_work_read_inspection_rejected",
                    API_requests=0,
                    database_writes=0,
                )
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
