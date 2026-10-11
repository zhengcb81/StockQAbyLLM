"""Reconstruct immutable synthetic unit input; no archived runtime required.

This is test-only software provenance from a mocked vendor response.  It
does not issue a receipt, grant permission, or establish financial accuracy.
The complete live software chain is tested separately with fresh native
stores and the actual public main.  Cross-owner tests require an explicitly
owned layout, not production owner databases or API credentials.
"""

import hashlib
import os
import sqlite3
from contextlib import closing
from pathlib import Path

SQL_SHA256 = "1772a5544c2322f881fb8075730a22b19b5db423f85a9aff780105216fc5632e"
_cache = {}


def execution_fixture(root=None):
    if root is None:
        root = Path(os.environ["IQS_C06_OWN"]) / f"reproducible-test-input/{os.getpid()}"
    root = Path(root).resolve()
    source = Path(__file__).parent / "fixtures/c06_execution_ledger.sql"
    raw = source.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SQL_SHA256
    path = root / "c06_execution.sqlite"
    if path not in _cache:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with closing(sqlite3.connect(path)) as con:
                con.executescript(raw.decode("utf-8"))
        # A retained file must match the exact source SQL, not a caller's
        # newly sealed checksum. Never repair/replace an unexpected ledger.
        expected = raw.decode("utf-8").split("PRAGMA user_version=16;\n", 1)[1]
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as con:
            assert con.execute("PRAGMA user_version").fetchone()[0] == 16
            assert "\n".join(con.iterdump()) + "\n" == expected
        _cache[path] = hashlib.sha256(path.read_bytes()).hexdigest()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == _cache[path]
    return path, _cache[path]
