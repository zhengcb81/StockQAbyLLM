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
from .quick_scan_work_store import WorkConflictError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--request", type=Path)
    operation.add_argument("--intent", type=Path)
    operation.add_argument("--owner-controls", type=Path)
    parser.add_argument("--comparison-target", type=Path)
    parser.add_argument("--saved-research-task", type=Path)
    parser.add_argument("--saved-research-deployment", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.owner_controls:
            if (
                args.request
                or args.intent
                or args.comparison_target
                or args.saved_research_task
                or args.saved_research_deployment
            ):
                raise ValueError("owner_control_projection_arguments_invalid")
            query = read(args.owner_controls)
            if not isinstance(query, dict) or set(query) != {
                "entity_id",
                "question_ids",
                "scope_bindings",
            }:
                raise ValueError("owner_control_projection_request_invalid")
            from .quick_scan_refresh_inspect import ReadOnlyWorkView

            view = ReadOnlyWorkView(args.ledger)
            rows = view.owner_refresh_projection(**query, include_owner_decisions=True)
            print(
                json.dumps(
                    dict(
                        protocol="stockqa.owner_control_projection/1.0.0",
                        entity_id=query["entity_id"],
                        rows=rows,
                        authority="original_journal_read_not_permission",
                        API_requests=0,
                        database_writes=0,
                    ),
                    ensure_ascii=False,
                    allow_nan=False,
                )
            )
            return 0
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
        WorkConflictError,
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
