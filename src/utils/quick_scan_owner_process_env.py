"""Environment policy for fixed StockWiki CLI subprocesses."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

_OS_KEYS = {
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
_PYTHON_DEFAULTS = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
}


def system_subprocess_env(source: Mapping[str, str]) -> dict[str, str]:
    """Build the OS-only environment used for read-only native subprocesses."""
    return {
        key: value
        for key, value in source.items()
        if isinstance(key, str) and key.upper() in _OS_KEYS and isinstance(value, str)
    }


def route_subprocess_env(code_root: str, process_env: Mapping[str, str] | None) -> dict[str, str]:
    """Accept a trusted route override or inherit only the ambient OS keys."""
    env = system_subprocess_env(os.environ) if process_env is None else dict(process_env)
    env.update(_PYTHON_DEFAULTS)
    env["PYTHONPATH"] = os.pathsep.join((code_root, env.get("PYTHONPATH", "")))
    return env


def consent_subprocess_env(
    code_root: str,
    process_env: Mapping[str, str] | None,
    python_path: str | None = None,
) -> dict[str, str]:
    """Keep read-only consent subprocesses independent of caller credentials."""
    source = os.environ if process_env is None else process_env
    env = system_subprocess_env(source)
    env.update(_PYTHON_DEFAULTS)
    env["PYTHONPATH"] = python_path or code_root
    if source.get("IQS_C06_RUN_INTEGRATION") == "1":
        owned = Path(source["IQS_C06_OWN"]).resolve()
        env.update(IQS_C06_RUN_INTEGRATION="1", IQS_C06_OWN=str(owned))
        env["PYTHONPATH"] = os.pathsep.join((str(owned / "guard"), env["PYTHONPATH"]))
        scratch = str(owned / "tmp")
        env.update(TEMP=scratch, TMP=scratch, TMPDIR=scratch)
    return env
