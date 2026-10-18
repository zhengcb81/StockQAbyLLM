"""Explicitly installed ONLY in an owned delegated engineering test runtime.
Not part of production imports, no fake TTY/confirmation/provider response.
"""

import atexit
import json
import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

root = Path(os.environ["IQS_C06_OWN"]).resolve()
allowed = [root]
if os.environ.get("IQS_DELEG_EVIDENCE_ROOT"):
    allowed.append(Path(os.environ["IQS_DELEG_EVIDENCE_ROOT"]).resolve())


def _inside(value):
    if isinstance(value, int):
        return True
    value = os.fsdecode(value)
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    path = Path(value).absolute().resolve()
    # Windows resolves the null sink to a device path; it cannot store data.
    if os.name == "nt" and str(path).casefold() == "\\\\.\\nul":
        return True
    return any(path == base or base in path.parents for base in allowed)


counts = {"network": 0, "foreign_sqlite": 0, "foreign_write": 0}


def _audit(event, args):
    if event in {
        "socket.connect",
        "socket.connect_ex",
        "socket.getaddrinfo",
        "socket.sendto",
        "socket.bind",
    }:
        counts["network"] += 1
        raise RuntimeError("delegated_fixture_network_forbidden")
    if event == "sqlite3.connect":
        value = os.fsdecode(args[0])
        if value == ":memory:" or value.startswith("file::memory:"):
            return
        if value.startswith("file:"):
            value = unquote(urlparse(value).path)
            if os.name == "nt" and len(value) > 2 and value[0] == "/" and value[2] == ":":
                value = value[1:]
        if not _inside(value):
            counts["foreign_sqlite"] += 1
            raise PermissionError("delegated_fixture_foreign_sqlite_forbidden")
    if event == "open":
        value, mode, flags = args
        write = (isinstance(mode, str) and any(char in mode for char in "wax+")) or (
            isinstance(flags, int)
            and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        )
        if write and not _inside(value):
            counts["foreign_write"] += 1
            raise PermissionError("delegated_fixture_foreign_write_forbidden")
    if event in {"os.mkdir", "os.remove", "os.rmdir"} and not _inside(args[0]):
        counts["foreign_write"] += 1
        raise PermissionError("delegated_fixture_foreign_write_forbidden")
    if event in {"os.rename", "os.link", "os.symlink"} and any(
        not _inside(value) for value in args[:2]
    ):
        counts["foreign_write"] += 1
        raise PermissionError("delegated_fixture_foreign_write_forbidden")


sys.addaudithook(_audit)


def _evidence():
    # Written only after the audit hook was installed and this process exits.
    # It records actual refusals; it is not a permission/caller attestation.
    directory = root / "attestations"
    directory.mkdir(exist_ok=True)
    value = dict(pid=os.getpid(), guard_loaded=True, refusal_counts=counts, authority=False)
    (directory / (str(os.getpid()) + ".json")).write_text(
        json.dumps(value, sort_keys=True), encoding="utf-8"
    )


atexit.register(_evidence)
