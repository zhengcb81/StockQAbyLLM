"""Remove later C06 additions from explicitly disposable historical test DBs."""

import re

from src.utils import quick_scan_work_store as module


def remove_c06_additions(connection, *, after_version=13):
    """Keep all earlier schema and rows; never run against an owner workspace."""
    assert after_version in (13, 14, 15, 18, 19)
    # A historical fixture must remove every later additive release; lowering
    # only the pragma while retaining newer tables is not an old database.
    for version in range(module.SCHEMA_VERSION, after_version, -1):
        if version <= after_version:
            continue
        for statement in reversed(getattr(module, f"_DDL_V{version}_ADDITIONS")):
            match = re.match(r"CREATE (TABLE|TRIGGER) ([A-Za-z_]+)", statement)
            assert match is not None
            connection.execute(f"DROP {match[1]} {match[2]}")
    if module.SCHEMA_VERSION >= 21:
        # v21 replaced a v13 trigger rather than adding a distinct name.
        connection.execute(module._MCP_GUARD_OLD)
    if module.SCHEMA_VERSION >= 25 and after_version < 25:
        # v25 widened a CHECK constraint by table copy, which the additive loop
        # cannot undo. Recreate the pre-v25 checkpoint table with its old rows.
        connection.execute(
            "CREATE TEMP TABLE checkpoint_before_v25 AS SELECT * FROM answer_checkpoint"
        )
        connection.execute("DROP TABLE answer_checkpoint")
        connection.execute(module._DDL_CHECKPOINT_V11)
        for statement in module._DDL_V2_ADDITIONS[1:]:
            connection.execute(statement)
        connection.execute("INSERT INTO answer_checkpoint SELECT * FROM checkpoint_before_v25")
        connection.execute("DROP TABLE checkpoint_before_v25")
