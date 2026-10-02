"""Small, durable provider cooldown ledger for ordered quick-scan dispatch.

The ledger stores only route/group IDs and timing/classification metadata. It
does not store provider credentials, request bodies, company data, or URLs.
Every admission decision is made under SQLite's single-writer transaction so
separate workers cannot both acquire the same half-open probe.
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

SCHEMA_VERSION = 1


def _utc_text(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class HealthDecision:
    allowed: bool
    reason: Optional[str] = None
    wait_seconds: Optional[float] = None
    next_probe_at: Optional[str] = None
    reset_at_source: Optional[str] = None
    probe_token: Optional[str] = None


class QuickScanProviderHealth:
    """Persist group quota and route rate-limit state across processes.

    A quota refusal without a trusted Retry-After has an *unknown* reset time.
    ``unknown_reset_cooldown_seconds`` defines only the next probe opportunity,
    never a claimed provider quota reset. An expired in-flight probe is treated
    as ambiguous and deferred again; restarting does not grant a new probe.
    """

    _GROUP_SCHEMA = """CREATE TABLE group_health (
        group_id TEXT PRIMARY KEY,
        state TEXT NOT NULL CHECK (state IN ('open', 'half_open')),
        observed_at REAL NOT NULL CHECK (observed_at > 0),
        next_probe_at REAL NOT NULL CHECK (next_probe_at > observed_at),
        reset_at REAL,
        reset_at_source TEXT NOT NULL CHECK (reset_at_source IN ('retry_after', 'unknown')),
        probe_token TEXT,
        probe_until REAL,
        CHECK ((reset_at_source = 'unknown' AND reset_at IS NULL)
            OR (reset_at_source = 'retry_after' AND reset_at IS NOT NULL)),
        CHECK ((state = 'open' AND probe_token IS NULL AND probe_until IS NULL)
            OR (state = 'half_open' AND probe_token IS NOT NULL AND probe_until > observed_at))
    )"""
    _ROUTE_SCHEMA = """CREATE TABLE route_health (
        route_id TEXT PRIMARY KEY,
        observed_at REAL NOT NULL CHECK (observed_at > 0),
        cooldown_until REAL NOT NULL CHECK (cooldown_until > observed_at),
        reset_at_source TEXT NOT NULL CHECK (reset_at_source IN ('retry_after', 'unknown'))
    )"""

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            self._initialize(connection)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @classmethod
    def _initialize(cls, connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                existing = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
                if existing:
                    raise ValueError("quick-scan health database has an unknown schema")
                connection.execute(cls._GROUP_SCHEMA)
                connection.execute(cls._ROUTE_SCHEMA)
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise ValueError("unsupported quick-scan health database version")
            actual_tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            if actual_tables != {"group_health", "route_health"}:
                raise ValueError("quick-scan health database contains unknown tables")
            for name, expected in (
                ("group_health", cls._GROUP_SCHEMA),
                ("route_health", cls._ROUTE_SCHEMA),
            ):
                actual = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (name,),
                ).fetchone()
                if actual is None or " ".join(actual[0].split()) != " ".join(expected.split()):
                    raise ValueError("quick-scan health database schema mismatch")
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("quick-scan health database integrity check failed")
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _positive_seconds(value: float, label: str, *, maximum: float = 86400) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{label} must be a positive number")
        value = float(value)
        if not math.isfinite(value) or value <= 0 or value > maximum:
            raise ValueError(f"{label} must be between 0 and {maximum:g} seconds")
        return value

    @staticmethod
    def _key(value: str, label: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{label} is required")
        # Policy IDs are user-controlled. Only opaque fingerprints enter SQLite,
        # even if a misconfigured ID contains a URL or credential-like text.
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _valid_time(value: object, label: str) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"invalid quick-scan health {label}")
        return float(value)

    @classmethod
    def _check_group_row(cls, row: sqlite3.Row) -> None:
        observed = cls._valid_time(row["observed_at"], "observed_at")
        next_probe = cls._valid_time(row["next_probe_at"], "next_probe_at")
        if next_probe <= observed:
            raise ValueError("invalid quick-scan health next_probe_at")
        if row["reset_at_source"] not in {"retry_after", "unknown"}:
            raise ValueError("invalid quick-scan health reset source")
        reset = row["reset_at"]
        if (row["reset_at_source"] == "retry_after") != (reset is not None):
            raise ValueError("invalid quick-scan health reset source")
        if reset is not None and cls._valid_time(reset, "reset_at") <= observed:
            raise ValueError("invalid quick-scan health reset_at")
        if row["state"] == "half_open":
            if (
                not row["probe_token"]
                or cls._valid_time(row["probe_until"], "probe_until") <= observed
            ):
                raise ValueError("invalid quick-scan health probe lease")
        elif (
            row["state"] != "open"
            or row["probe_token"] is not None
            or row["probe_until"] is not None
        ):
            raise ValueError("invalid quick-scan health probe state")

    def admit(
        self,
        *,
        route_id: str,
        group_id: str,
        unknown_reset_cooldown_seconds: int,
        probe_lease_seconds: float = 180,
    ) -> HealthDecision:
        """Atomically admit a normal request or the group's one half-open probe."""
        cooldown = self._positive_seconds(
            unknown_reset_cooldown_seconds, "unknown_reset_cooldown_seconds"
        )
        lease = self._positive_seconds(probe_lease_seconds, "probe_lease_seconds")
        route_id = self._key(route_id, "route_id")
        group_id = self._key(group_id, "group_id")
        now = self._valid_time(self.clock(), "clock")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM group_health WHERE group_id=?", (group_id,)
                ).fetchone()
                if row is not None:
                    self._check_group_row(row)
                if row is not None and row["state"] == "half_open":
                    if now >= row["probe_until"]:
                        # An interrupted probe may have been sent and charged.
                        # Never issue another probe immediately on restart.
                        next_probe = now + cooldown
                        connection.execute(
                            "UPDATE group_health SET state='open', observed_at=?, "
                            "next_probe_at=?, reset_at=NULL, reset_at_source='unknown', "
                            "probe_token=NULL, probe_until=NULL WHERE group_id=?",
                            (now, next_probe, group_id),
                        )
                        decision = HealthDecision(
                            False,
                            "quota_group_cooldown",
                            next_probe - now,
                            _utc_text(next_probe),
                            "unknown",
                        )
                    else:
                        decision = HealthDecision(
                            False,
                            "quota_group_probe_in_flight",
                            row["probe_until"] - now,
                            _utc_text(row["probe_until"]),
                            row["reset_at_source"],
                        )
                    connection.execute("COMMIT")
                    return decision
                if row is not None and now < row["next_probe_at"]:
                    decision = HealthDecision(
                        False,
                        "quota_group_cooldown",
                        row["next_probe_at"] - now,
                        _utc_text(row["next_probe_at"]),
                        row["reset_at_source"],
                    )
                    connection.execute("COMMIT")
                    return decision
                route = connection.execute(
                    "SELECT observed_at,cooldown_until,reset_at_source FROM route_health WHERE route_id=?",
                    (route_id,),
                ).fetchone()
                if route is not None:
                    observed = self._valid_time(route["observed_at"], "route observed_at")
                    until = self._valid_time(route["cooldown_until"], "route cooldown_until")
                    if until <= observed or route["reset_at_source"] not in {
                        "retry_after",
                        "unknown",
                    }:
                        raise ValueError("invalid quick-scan health route cooldown")
                if route is not None and now < route["cooldown_until"]:
                    decision = HealthDecision(
                        False,
                        "route_rate_limited",
                        route["cooldown_until"] - now,
                        _utc_text(route["cooldown_until"]),
                        route["reset_at_source"],
                    )
                    connection.execute("COMMIT")
                    return decision
                if row is not None:
                    token = uuid.uuid4().hex
                    connection.execute(
                        "UPDATE group_health SET state='half_open', probe_token=?, "
                        "probe_until=? WHERE group_id=?",
                        (token, now + lease, group_id),
                    )
                    decision = HealthDecision(True, probe_token=token)
                else:
                    decision = HealthDecision(True)
                connection.execute("COMMIT")
                return decision
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def quota_exhausted(
        self,
        group_id: str,
        *,
        unknown_reset_cooldown_seconds: int,
        retry_after_seconds: Optional[float] = None,
    ) -> str:
        """Trip a shared group; Retry-After is the only trusted known reset."""
        group_id = self._key(group_id, "group_id")
        cooldown = self._positive_seconds(
            unknown_reset_cooldown_seconds, "unknown_reset_cooldown_seconds"
        )
        wait = (
            self._positive_seconds(
                retry_after_seconds, "retry_after_seconds", maximum=100 * 365 * 86400
            )
            if retry_after_seconds is not None
            else cooldown
        )
        now = self._valid_time(self.clock(), "clock")
        source = "retry_after" if retry_after_seconds is not None else "unknown"
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM group_health WHERE group_id=?", (group_id,)
                ).fetchone()
                if existing is not None:
                    self._check_group_row(existing)
                proposed_probe = now + wait
                keep_existing = existing is not None and (
                    existing["next_probe_at"] > proposed_probe
                    or (
                        existing["next_probe_at"] == proposed_probe
                        and existing["reset_at_source"] == "retry_after"
                    )
                )
                next_probe = existing["next_probe_at"] if keep_existing else proposed_probe
                effective_source = existing["reset_at_source"] if keep_existing else source
                effective_reset = next_probe if effective_source == "retry_after" else None
                connection.execute(
                    "INSERT INTO group_health (group_id,state,observed_at,next_probe_at,"
                    "reset_at,reset_at_source,probe_token,probe_until) "
                    "VALUES (?, 'open', ?, ?, ?, ?, NULL, NULL) "
                    "ON CONFLICT(group_id) DO UPDATE SET state='open', observed_at=excluded.observed_at, "
                    "next_probe_at=excluded.next_probe_at, reset_at=excluded.reset_at, "
                    "reset_at_source=excluded.reset_at_source, probe_token=NULL, probe_until=NULL",
                    (
                        group_id,
                        now,
                        next_probe,
                        effective_reset,
                        effective_source,
                    ),
                )
                connection.execute("COMMIT")
                return _utc_text(next_probe)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def rate_limited(self, route_id: str, *, cooldown_seconds: float, source: str) -> str:
        """Cool one route only; a plain 429 never trips the quota group."""
        route_id = self._key(route_id, "route_id")
        if source not in {"retry_after", "unknown"}:
            raise ValueError("invalid quick-scan route cooldown source")
        wait = self._positive_seconds(
            cooldown_seconds, "cooldown_seconds", maximum=100 * 365 * 86400
        )
        now = self._valid_time(self.clock(), "clock")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT observed_at,cooldown_until,reset_at_source FROM route_health "
                    "WHERE route_id=?",
                    (route_id,),
                ).fetchone()
                if existing is not None:
                    observed = self._valid_time(existing["observed_at"], "route observed_at")
                    until = self._valid_time(existing["cooldown_until"], "route cooldown_until")
                    if until <= observed or existing["reset_at_source"] not in {
                        "retry_after",
                        "unknown",
                    }:
                        raise ValueError("invalid quick-scan health route cooldown")
                proposed_until = now + wait
                keep_existing = existing is not None and (
                    existing["cooldown_until"] > proposed_until
                    or (
                        existing["cooldown_until"] == proposed_until
                        and existing["reset_at_source"] == "retry_after"
                    )
                )
                effective_until = existing["cooldown_until"] if keep_existing else proposed_until
                effective_source = existing["reset_at_source"] if keep_existing else source
                connection.execute(
                    "INSERT INTO route_health (route_id,observed_at,cooldown_until,reset_at_source) "
                    "VALUES (?,?,?,?) "
                    "ON CONFLICT(route_id) DO UPDATE SET observed_at=excluded.observed_at, "
                    "cooldown_until=excluded.cooldown_until, "
                    "reset_at_source=excluded.reset_at_source",
                    (route_id, now, effective_until, effective_source),
                )
                connection.execute("COMMIT")
                return _utc_text(effective_until)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def probe_succeeded(self, group_id: str, probe_token: Optional[str]) -> None:
        """Only the matching successful probe restores its own group."""
        if probe_token is None:
            return
        group_id = self._key(group_id, "group_id")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "DELETE FROM group_health WHERE group_id=? AND state='half_open' "
                    "AND probe_token=?",
                    (group_id, probe_token),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def probe_failed(
        self,
        group_id: str,
        probe_token: Optional[str],
        *,
        unknown_reset_cooldown_seconds: int,
    ) -> None:
        """Defer a failed/ambiguous probe; a stale worker cannot clear newer state."""
        if probe_token is None:
            return
        group_id = self._key(group_id, "group_id")
        cooldown = self._positive_seconds(
            unknown_reset_cooldown_seconds, "unknown_reset_cooldown_seconds"
        )
        now = self._valid_time(self.clock(), "clock")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "UPDATE group_health SET state='open', observed_at=?, next_probe_at=?, "
                    "reset_at=NULL, reset_at_source='unknown', probe_token=NULL, probe_until=NULL "
                    "WHERE group_id=? AND state='half_open' AND probe_token=?",
                    (now, now + cooldown, group_id, probe_token),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
