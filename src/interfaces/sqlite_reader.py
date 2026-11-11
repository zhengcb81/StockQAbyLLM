"""Minimal structural connection contract for read-only SQLite views."""

from __future__ import annotations

from typing import Any, Iterable, Protocol, Sequence, runtime_checkable


@runtime_checkable
class SQLiteCursorReader(Protocol, Iterable[Any]):
    """Cursor operations used by schema and row readers."""

    def fetchone(self) -> Any: ...

    def fetchall(self) -> list[Any]: ...


@runtime_checkable
class SQLiteReadConnection(Protocol):
    """Connection operations shared by SQLite and guarded read-only adapters."""

    @property
    def in_transaction(self) -> bool: ...

    def execute(self, sql: str, parameters: Sequence[Any] = ...) -> SQLiteCursorReader: ...


__all__ = ["SQLiteCursorReader", "SQLiteReadConnection"]
