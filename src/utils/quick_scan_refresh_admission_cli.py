"""Locate frozen execution inputs or register a native Q13 confirmation."""

import argparse
import json
import sys
from contextlib import redirect_stdout


def main(argv=None):
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--config", required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--confirmation-id")
    action.add_argument(
        "--input-locations",
        action="store_true",
        help="Read subject refs from stdin and project their input paths",
    )
    args = parser.parse_args(argv)
    # Original JSONConfigManager initializes stdout log handlers at import.
    # Import AND execute under the stderr boundary; stdout is one JSON object.
    with redirect_stdout(sys.stderr):
        from .quick_scan_refresh_admission import (
            AdmissionRejected,
            RefreshAdmissionService,
            execution_input_paths,
            safe_read,
        )

        try:
            if args.input_locations:
                root = safe_read(args.config).get("execution_inputs_root")
                refs = json.load(sys.stdin)
                if (
                    not isinstance(root, str)
                    or not isinstance(refs, list)
                    or not all(isinstance(ref, dict) for ref in refs)
                ):
                    raise ValueError("invalid execution input location request")
                result = {
                    "locations": [
                        {role: str(path) for role, path in execution_input_paths(root, ref).items()}
                        for ref in refs
                    ],
                    "API_requests": 0,
                }
            else:
                result = RefreshAdmissionService(args.config).admit(args.confirmation_id)
            code = 0
        except (AdmissionRejected, ValueError) as error:
            result = dict(
                protocol="stockqa.refresh_admission/1.0.0",
                status="rejected",
                reason=getattr(error, "reason", "refresh_admission_rejected"),
                handoff_status="rejected",
                permission_authenticated=False,
                registered_with_executor=False,
                dispatch_started=False,
                API_requests=0,
            )
            code = 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
