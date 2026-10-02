"""Durable quick-scan provider health: isolated SQLite fault cases."""

import sqlite3
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import pytest

from src.utils.quick_scan_provider_health import QuickScanProviderHealth


def _ledger(tmp_path, clock):
    return QuickScanProviderHealth(tmp_path / "health.sqlite", clock=lambda: clock[0])


def _admit(ledger, group="account-a", route="route-a", cooldown=60, lease=10):
    return ledger.admit(
        route_id=route,
        group_id=group,
        unknown_reset_cooldown_seconds=cooldown,
        probe_lease_seconds=lease,
    )


def _process_admit(path_and_time):
    path, now = path_and_time
    ledger = QuickScanProviderHealth(Path(path), clock=lambda: now)
    return _admit(ledger).allowed


def test_quota_group_survives_restart_and_backup_success_does_not_restore_primary(tmp_path):
    clock = [1_800_000_000.0]
    first = _ledger(tmp_path, clock)
    first.quota_exhausted("account-a", unknown_reset_cooldown_seconds=60)
    second = _ledger(tmp_path, clock)
    assert not _admit(second).allowed
    assert not _admit(second, route="same-account-other-model").allowed

    assert _admit(second, group="account-b", route="route-b").allowed
    second.probe_succeeded("account-b", None)
    assert not _admit(first).allowed
    clock[0] += 60
    probe = _admit(second)
    assert probe.allowed and probe.probe_token
    assert not _admit(first).allowed
    second.probe_succeeded("account-b", probe.probe_token)
    assert not _admit(first).allowed
    first.probe_succeeded("account-a", probe.probe_token)
    assert _admit(_ledger(tmp_path, clock)).allowed


def test_plain_429_is_route_only_and_does_not_invent_five_hour_reset(tmp_path):
    clock = [1_800_000_000.0]
    ledger = _ledger(tmp_path, clock)
    ledger.rate_limited("route-a", cooldown_seconds=7, source="retry_after")
    blocked = _admit(ledger)
    assert not blocked.allowed and blocked.reason == "route_rate_limited"
    assert blocked.wait_seconds == 7
    assert blocked.reset_at_source == "retry_after"
    assert _admit(ledger, route="other-model").allowed
    clock[0] += 7
    assert _admit(_ledger(tmp_path, clock)).allowed


def test_late_short_429_keeps_effective_route_deadline_and_source(tmp_path):
    clock = [1_800_000_000.0]
    ledger = _ledger(tmp_path, clock)
    known = ledger.rate_limited("route-a", cooldown_seconds=3600, source="retry_after")
    clock[0] += 1
    late = ledger.rate_limited("route-a", cooldown_seconds=60, source="unknown")
    blocked = _admit(_ledger(tmp_path, clock))
    assert late == known == blocked.next_probe_at
    assert blocked.reset_at_source == "retry_after"
    assert blocked.wait_seconds == 3599


def test_late_unknown_quota_refusal_preserves_known_group_reset(tmp_path):
    clock = [1_800_000_000.0]
    ledger = _ledger(tmp_path, clock)
    known = ledger.quota_exhausted(
        "account-a", unknown_reset_cooldown_seconds=60, retry_after_seconds=3600
    )
    clock[0] += 1
    late = ledger.quota_exhausted("account-a", unknown_reset_cooldown_seconds=60)
    blocked = _admit(_ledger(tmp_path, clock))
    assert late == known == blocked.next_probe_at
    assert blocked.reset_at_source == "retry_after"
    with sqlite3.connect(ledger.path) as connection:
        reset, source = connection.execute(
            "SELECT reset_at,reset_at_source FROM group_health"
        ).fetchone()
    assert reset == 1_800_003_600.0
    assert source == "retry_after"


def test_confirmed_quota_reset_source_and_unknown_probe_are_distinct(tmp_path):
    clock = [1_800_000_000.0]
    ledger = _ledger(tmp_path, clock)
    ledger.quota_exhausted("known", unknown_reset_cooldown_seconds=60, retry_after_seconds=10)
    ledger.quota_exhausted("unknown", unknown_reset_cooldown_seconds=60)
    assert _admit(ledger, group="known").reset_at_source == "retry_after"
    unknown = _admit(ledger, group="unknown")
    assert unknown.reset_at_source == "unknown"
    assert unknown.next_probe_at is not None
    with sqlite3.connect(ledger.path) as connection:
        rows = connection.execute(
            "SELECT group_id, reset_at, reset_at_source FROM group_health ORDER BY group_id"
        ).fetchall()
    by_source = {row[2]: row[1] for row in rows}
    assert by_source["retry_after"] is not None
    assert by_source["unknown"] is None


def test_two_workers_get_only_one_half_open_probe_and_failure_reopens(tmp_path):
    clock = [1_800_000_000.0]
    ledger = _ledger(tmp_path, clock)
    ledger.quota_exhausted("account-a", unknown_reset_cooldown_seconds=60)
    clock[0] += 60
    workers = [_ledger(tmp_path, clock), _ledger(tmp_path, clock)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(_admit, workers))
    admitted = [item for item in decisions if item.allowed]
    assert len(admitted) == 1
    assert sum(item.reason == "quota_group_probe_in_flight" for item in decisions) == 1
    ledger.probe_failed("account-a", admitted[0].probe_token, unknown_reset_cooldown_seconds=60)
    assert not _admit(_ledger(tmp_path, clock)).allowed
    clock[0] += 60
    assert _admit(_ledger(tmp_path, clock)).allowed


def test_two_processes_get_only_one_half_open_probe(tmp_path):
    clock = [1_800_000_000.0]
    ledger = _ledger(tmp_path, clock)
    ledger.quota_exhausted("account-a", unknown_reset_cooldown_seconds=60)
    clock[0] += 60
    args = [(str(ledger.path), clock[0])] * 2
    with ProcessPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(_process_admit, args))
    assert sorted(outcomes) == [False, True]
    assert not _admit(_ledger(tmp_path, clock)).allowed


def test_crashed_probe_waits_again_and_stale_token_cannot_clear_new_state(tmp_path):
    clock = [1_800_000_000.0]
    ledger = _ledger(tmp_path, clock)
    ledger.quota_exhausted("account-a", unknown_reset_cooldown_seconds=60)
    clock[0] += 60
    crashed = _admit(ledger)
    clock[0] += 10
    after_crash = _admit(_ledger(tmp_path, clock))
    assert not after_crash.allowed
    assert after_crash.reason == "quota_group_cooldown"
    ledger.probe_succeeded("account-a", crashed.probe_token)
    assert not _admit(_ledger(tmp_path, clock)).allowed


def test_fake_v1_schema_fails_closed_without_modifying_existing_bytes(tmp_path):
    path = tmp_path / "health.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE group_health(group_id TEXT PRIMARY KEY)")
        connection.execute("PRAGMA user_version=1")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="health database"):
        QuickScanProviderHealth(path)
    assert path.read_bytes() == before


def test_corrupted_next_probe_time_fails_closed_before_dispatch(tmp_path):
    clock = [1_800_000_000.0]
    ledger = _ledger(tmp_path, clock)
    ledger.quota_exhausted("account-a", unknown_reset_cooldown_seconds=60)
    with sqlite3.connect(ledger.path) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("UPDATE group_health SET next_probe_at=-1")
    with pytest.raises(ValueError, match="next_probe_at"):
        _admit(ledger)


def test_extra_health_table_fails_closed(tmp_path):
    clock = [1_800_000_000.0]
    ledger = _ledger(tmp_path, clock)
    with sqlite3.connect(ledger.path) as connection:
        connection.execute("CREATE TABLE unrelated(data TEXT)")
    with pytest.raises(ValueError, match="unknown tables"):
        _ledger(tmp_path, clock)


@pytest.mark.parametrize("bad", [0, -1, True, float("nan"), float("inf")])
def test_invalid_cooldown_fails_before_mutating_state(tmp_path, bad):
    clock = [1_800_000_000.0]
    ledger = _ledger(tmp_path, clock)
    with pytest.raises(ValueError):
        ledger.quota_exhausted("account-a", unknown_reset_cooldown_seconds=bad)
    assert _admit(ledger).allowed


def test_state_database_contains_no_credential_or_company_or_url(tmp_path):
    clock = [1_800_000_000.0]
    ledger = _ledger(tmp_path, clock)
    ledger.quota_exhausted("account-a", unknown_reset_cooldown_seconds=60)
    raw = ledger.path.read_bytes()
    for forbidden in (b"api_key", b"Bearer", b"https://", b"Fixture Corp", b"account-a"):
        assert forbidden not in raw


def test_misconfigured_group_id_is_fingerprinted_not_stored_verbatim(tmp_path):
    clock = [1_800_000_000.0]
    ledger = _ledger(tmp_path, clock)
    accidental_secret = "https://provider.example/account?api_key=private"
    ledger.quota_exhausted(accidental_secret, unknown_reset_cooldown_seconds=60)
    assert not _admit(ledger, group=accidental_secret).allowed
    assert accidental_secret.encode() not in ledger.path.read_bytes()
