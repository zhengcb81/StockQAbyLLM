"""Register one native confirmation in original Q13; zero model calls."""

import argparse
import json
import sys
from contextlib import redirect_stdout


def main(argv=None):
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--config", required=True)
    parser.add_argument("--confirmation-id", required=True)
    args = parser.parse_args(argv)
    # Original JSONConfigManager initializes stdout log handlers at import.
    # Import AND execute under the stderr boundary; stdout is one JSON object.
    with redirect_stdout(sys.stderr):
        from .quick_scan_refresh_admission import (
            AdmissionRejected,
            RefreshAdmissionService,
        )

        try:
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
