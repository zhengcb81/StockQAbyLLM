"""Fixed local owner registration bridge, configured by the existing owner client."""

from __future__ import annotations

import os

# The configured first-party OS CLI uses argv, never shell commands.
import subprocess  # nosec B404
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from .quick_scan_result_outbox import canonical_bytes, strict_json_loads


def register(config, current, request, *, process_env=None) -> dict:
    env = (
        dict(process_env)
        if process_env is not None
        else {
            k: v
            for k, v in os.environ.items()
            if k.upper()
            in {
                "SYSTEMROOT",
                "WINDIR",
                "PATH",
                "PATHEXT",
                "COMSPEC",
                "USERPROFILE",
                "APPDATA",
                "LOCALAPPDATA",
                "SYSTEMDRIVE",
            }
        }
    )
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    env["PYTHONPATH"] = os.pathsep.join((config["stockwiki_code_root"], env.get("PYTHONPATH", "")))
    runs = Path(config["runs_dir"])
    runs.mkdir(parents=True, exist_ok=True)
    raw = canonical_bytes(request)
    if len(raw) > 128 * 1024:
        raise ValueError("subject_dispatch_request_too_large")
    try:
        with TemporaryDirectory(prefix="subject-dispatch-", dir=runs) as temp:
            path = Path(temp) / "request.json"
            path.write_bytes(raw)
            command = [
                sys.executable,
                "-B",
                "-X",
                "utf8",
                "-m",
                "stockwiki.quick_scan_subject_dispatch_cli",
                "--root",
                config["stockwiki_workspace_root"],
                "--iqs-code-root",
                config["iqs_code_root"],
                "--iqs-release-root",
                config["iqs_release_root"],
                "--subject-key",
                config["subject_key"],
                "--scope",
                config["scope"],
                "--scope-id",
                config["scope_id"],
                "--request",
                str(path),
                "--expected-decision-id",
                current["expected_decision_id"],
                "--expected-anchor-version",
                str(current["anchor_version"]),
            ]
            # Fixed sys.executable/module; all paths and IDs are separate argv entries.
            completed = subprocess.run(  # nosec B603
                command,
                cwd=config["stockwiki_code_root"],
                env=env,
                capture_output=True,
                timeout=120,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("subject_dispatch_owner_unavailable") from error
    if len(completed.stdout) > 128 * 1024:
        raise ValueError("subject_dispatch_owner_output_too_large")
    output = strict_json_loads(completed.stdout.decode("utf-8"))
    if (
        completed.returncode != 0
        or not isinstance(output, dict)
        or set(output) != {"protocol", "status", "paid_dispatch_started", "receipt"}
        or output["protocol"] != "stockwiki.subject_dispatch_cli/1.0.0"
        or output["status"] != "registered"
        or output["paid_dispatch_started"] is not False
    ):
        raise ValueError("subject_dispatch_owner_rejected")
    receipt = output["receipt"]
    if not isinstance(receipt, dict):
        raise ValueError("subject_dispatch_owner_receipt_invalid")
    return receipt
