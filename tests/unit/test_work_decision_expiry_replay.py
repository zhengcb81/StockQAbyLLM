"""Synthetic native ports; no real owner approval or financial labels."""

import copy

import pytest

from src.utils.quick_scan_owner_refresh import OwnerRefreshRejected
from tests.unit.test_quick_scan_known_outcome import authority, fixture, resolve
from tests.unit.test_quick_scan_work_decision_authority import client


def test_only_exact_actual_journal_row_can_select_permissions_replay(tmp_path):
    store, clock, members, pack, receipt, _, _ = fixture(tmp_path / "ledger")
    clock[0] += 121
    saved = resolve(store, members, pack, receipt)
    c = client(tmp_path / "port")
    c.work_store = store.path
    grant = saved["grant"]
    confirmation = dict(
        owner_store_id=grant["owner_store_id"],
        owner_principal=grant["principal_id"],
        confirmation_id=grant["confirmation_id"],
        action_id=grant["action_id"],
        request_sha256=grant["request_sha256"],
        confirmed_at=grant["confirmed_at"],
    )
    value = dict(
        request=dict(decision_request=copy.deepcopy(saved["request"])), confirmation=confirmation
    )
    before = store.path.read_bytes()
    assert c._existing_applied(value) is True
    for field in ("request_sha256", "owner_principal", "confirmation_id", "action_id"):
        bad = copy.deepcopy(value)
        bad["confirmation"][field] = "synthetic-forged"
        with pytest.raises(ValueError, match="existing_action"):
            c._existing_applied(bad)
    new = copy.deepcopy(value)
    new["request"]["decision_request"]["idempotency_key"] = "not-registered"
    assert c._existing_applied(new) is False
    assert store.path.read_bytes() == before


def test_first_apply_requires_native_fresh_read_even_after_permissions_read(tmp_path, monkeypatch):
    c = client(tmp_path)
    calls = []
    monkeypatch.setattr(c, "_existing_applied", lambda value: False)

    def read(mode):
        calls.append(mode)
        if mode == "fresh_registration":
            raise OwnerRefreshRejected("synthetic preview expired")
        return dict(admission_mode=mode)

    monkeypatch.setattr(c, "_read_native_inspection", read)
    with pytest.raises(OwnerRefreshRejected, match="expired"):
        c.inspect_confirmation()
    assert calls == ["current_permissions", "fresh_registration"]
    assert not c.work_store.exists()


def test_existing_exact_action_still_requires_current_native_permissions(tmp_path, monkeypatch):
    c = client(tmp_path)
    monkeypatch.setattr(c, "_existing_applied", lambda value: True)

    def revoked(mode):
        raise OwnerRefreshRejected("synthetic owner revoked")

    monkeypatch.setattr(c, "_read_native_inspection", revoked)
    with pytest.raises(OwnerRefreshRejected, match="revoked"):
        c.inspect_confirmation()
