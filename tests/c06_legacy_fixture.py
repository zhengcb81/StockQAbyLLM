"""Remove later C06 additions from explicitly disposable historical test DBs."""

import re

from src.utils import quick_scan_work_store as module


def remove_c06_additions(connection, *, after_version=13):
    """Keep all earlier schema and rows; never run against an owner workspace."""
    assert after_version in (13, 14)
    for version in (16, 15, 14):
        if version <= after_version:
            continue
        for statement in reversed(getattr(module, f"_DDL_V{version}_ADDITIONS")):
            match = re.match(r"CREATE (TABLE|TRIGGER) ([A-Za-z_]+)", statement)
            assert match is not None
            connection.execute(f"DROP {match[1]} {match[2]}")
