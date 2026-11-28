"""Run StockQA's complete applicable offline inventory and prove what executed."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

POLICY_PATH = Path("tests/contract/test_policy.json")
REF = "tools.anti_trick_gate_spec"
SYSTEM_VARIABLES = {
    "SYSTEMROOT",
    "WINDIR",
    "SYSTEMDRIVE",
    "COMSPEC",
    "PATH",
    "PATHEXT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERNAME",
    "USER",
    "LOGNAME",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    args = parser.parse_args()
    platform = "windows" if sys.platform == "win32" else sys.platform
    policy = json.loads(POLICY_PATH.read_bytes())["platforms"][platform]
    evidence_dir = args.evidence_dir.resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    inventory = evidence_dir / "inventory.json"
    execution = evidence_dir / "execution.json"

    env = {name: value for name, value in os.environ.items() if name.upper() in SYSTEM_VARIABLES}
    env.update(
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
        PYTHONDONTWRITEBYTECODE="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        STOCKQA_RUN_LIVE_E2E="0",
        NO_PROXY="*",
    )
    fixture_home = evidence_dir / "fixture-home"
    fixture_home.mkdir(exist_ok=True)
    env.update(HOME=str(fixture_home), USERPROFILE=str(fixture_home))
    child = {
        "env": env,
        "stdin": subprocess.DEVNULL,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }
    real_revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], encoding="ascii", **child
    ).strip()
    if real_revision != args.revision:
        parser.error("evidence revision must match the checked-out commit")

    pytest = [
        sys.executable,
        "-B",
        "-m",
        "pytest",
        "-p",
        "pytest_asyncio.plugin",
        "-p",
        "pytest_benchmark.plugin",
        "-p",
        f"{REF}.pytest_probe",
        "-p",
        "no:cacheprovider",
        "--benchmark-disable",
        "--capture=sys",
        "--tb=short",
        "-q",
        "--s6-revision",
        args.revision,
        "-m",
        policy["markexpr"],
    ]
    for role, path in (("collect", inventory), ("execute", execution)):
        command = [
            *pytest,
            "--basetemp",
            str(evidence_dir / f"{role}-tmp"),
            "--s6-evidence",
            str(path),
        ]
        if role == "collect":
            command.append("--collect-only")
        result = subprocess.run([*command, *policy["roots"]], **child)
        if result.returncode:
            return result.returncode
    return subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            REF,
            "coverage",
            "--revision",
            args.revision,
            "--policy",
            str(POLICY_PATH),
            "--platform",
            platform,
            str(inventory),
            str(execution),
        ],
        **child,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
