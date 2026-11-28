"""Small producer and machine-CLI checks for the local developer command."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_feedback_owner_help_is_read_only(tmp_path):
    state = tmp_path / "feedback"
    system = {"PATH", "TEMP", "TMP", "TMPDIR", "SYSTEMROOT", "WINDIR", "COMSPEC"}
    env = {key: value for key, value in os.environ.items() if key.upper() in system}
    env.update(
        PYTHONPATH=str(ROOT),
        PYTHONUTF8="1",
        RESEARCH_FEEDBACK_DIR=str(state),
        STOCKQA_RUN_LIVE_E2E="0",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "src.utils.quick_scan_feedback_owner",
            "--help",
        ],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert b"reconcile" in result.stdout
    assert not state.exists()
