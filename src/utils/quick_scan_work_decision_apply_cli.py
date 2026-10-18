"""Apply an actual native owner decision; never send a question or reset budget."""

import argparse
import json
import sqlite3
from pathlib import Path

from .quick_scan_owner_refresh import OwnerRefreshRejected
from .quick_scan_refresh_consent import OwnerConsentEndpoint
from .quick_scan_work_decision_authority import StockWikiWorkDecisionAuthority
from .quick_scan_work_store import Lease, QuickScanWorkStore


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--owner-code-root", type=Path, required=True)
    parser.add_argument("--owner-workspace", type=Path, required=True)
    parser.add_argument("--iqs-code-root", type=Path, required=True)
    parser.add_argument("--iqs-release-root", type=Path, required=True)
    parser.add_argument("--consumer-id", required=True)
    parser.add_argument("--confirmation-id", required=True)
    parser.add_argument(
        "--refresh-schema-version",
        choices=["1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.5.0"],
    )
    args = parser.parse_args(argv)
    try:
        config = dict(
            stockwiki_code_root=str(args.owner_code_root.resolve()),
            stockwiki_workspace_root=str(args.owner_workspace.resolve()),
            iqs_code_root=str(args.iqs_code_root.resolve()),
            iqs_release_root=str(args.iqs_release_root.resolve()),
        )
        authority = StockWikiWorkDecisionAuthority(
            OwnerConsentEndpoint(config),
            work_store=args.ledger,
            budget_policy_file=args.policy,
            consumer_id=args.consumer_id,
            confirmation_id=args.confirmation_id,
            refresh_schema_version=args.refresh_schema_version,
        )
        # Authenticate scope/principal/current proof before opening a mutating
        # handle. Rejection cannot even initialize an empty DB or migrate one.
        inspected = authority.inspect_confirmation()
        request = inspected["request"]["decision_request"]
        store = QuickScanWorkStore(args.ledger)
        if request["kind"] == "publish_saved_research":
            from .quick_scan_c06_authority import load_c06_authority
            from .quick_scan_saved_research_publication import publish_saved_research

            cap = inspected["publication_capability"]
            result = publish_saved_research(
                store,
                Path(cap["task_bundle"]["path"]).read_bytes(),
                sdk_id=cap["native_sdk_id"],
                idempotency_key=request["idempotency_key"],
                owner_authority=authority,
                c06_authority=load_c06_authority(cap["c06_authority"]["path"]),
            )
        elif request["kind"] == "known_response_resolution":
            proof = request["physical"]
            result = store.resolve_known_response(
                request["work_item_id"],
                attempt_id=request["attempt_id"],
                expected_lease=Lease(proof["lease_token"], proof["lease_epoch"]),
                receipt_sha256=proof["receipt_sha256"],
                request_payload_sha256=proof["request_payload_sha256"],
                reason=request["reason"],
                diagnostic=request["diagnostic"],
                owner_authority=authority,
                idempotency_key=request["idempotency_key"],
            )
        else:
            result = store.record_owner_retry_decision(
                request["work_item_id"],
                purpose=request["purpose"],
                retry_after_seconds=request["retry_after_seconds"],
                owner_authority=authority,
                idempotency_key=request["idempotency_key"],
                comparison_controls=request["comparison_controls"],
            )
        print(
            json.dumps(
                dict(
                    protocol="stockqa.work_decision_apply/1.0.0",
                    status="recorded",
                    decision=result,
                    API_requests=0,
                    dispatch_started=False,
                ),
                ensure_ascii=False,
                allow_nan=False,
            )
        )
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
        OwnerRefreshRejected,
    ):
        print(
            json.dumps(
                dict(
                    protocol="stockqa.work_decision_apply/1.0.0",
                    status="rejected",
                    error_code="owner_work_apply_rejected",
                    API_requests=0,
                    dispatch_started=False,
                )
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
