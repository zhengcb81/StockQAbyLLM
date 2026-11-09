"""Read the original admission authority or verify recovery-only response scope."""

import argparse
import json
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path

from src.utils._quick_scan_owner_io import deny_network, read_request

REQUEST_PROTOCOL = "stockqa.registered_permissions_request/1.0.0"
RESPONSE_PROTOCOL = "stockqa.registered_permissions_response/1.0.0"
MODES = {"current_permissions", "paid_response_recovery"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    sys.addaudithook(deny_network)
    try:
        with redirect_stdout(sys.stderr):
            request = read_request()
            confirmation = request.get("confirmation_id")
            mode = request.get("mode")
            if (
                set(request) != {"protocol", "confirmation_id", "mode"}
                or request["protocol"] != REQUEST_PROTOCOL
                or not isinstance(mode, str)
                or mode not in MODES
                or not isinstance(confirmation, str)
                or not confirmation
                or not args.config.is_absolute()
            ):
                raise ValueError("owner_permissions_contract_invalid")

            from src.utils.quick_scan_refresh_admission import RefreshAdmissionService

            recovery_only = mode == "paid_response_recovery"
            service = RefreshAdmissionService(args.config, recovery_only=recovery_only)
            proof, _, _ = service.read_registered(confirmation, recovery_only=recovery_only)
            if proof["confirmation"]["confirmation_id"] != confirmation:
                raise ValueError("original_confirmation_mismatch")
        response = {
            "protocol": RESPONSE_PROTOCOL,
            "status": "passed",
            "confirmation_id": confirmation,
            "mode": mode,
            "permission_authenticated": True,
            "received_response_scope_verified": recovery_only,
        }
        code = 0
    except Exception as error:
        fallback = "owner_confirmation_permissions_rejected"
        reason = getattr(error, "reason", fallback)
        if not isinstance(reason, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,159}", reason):
            reason = fallback
        response = {"protocol": RESPONSE_PROTOCOL, "error_code": reason}
        code = 2
    print(json.dumps({**response, "API_requests": 0, "network_calls": 0}, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
