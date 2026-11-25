"""Synthetic native ports; no real owner approval or financial labels."""

import copy

import pytest

from src.utils.quick_scan_owner_refresh import OwnerRefreshRejected
from src.utils.quick_scan_result_outbox import canonical_sha256
from src.utils.quick_scan_work_decision_inspect import derive_work_decision
from src.utils.quick_scan_work_store import QuickScanWorkStore
from tests.unit.test_quick_scan_known_outcome import (
    arguments,
    fixture,
    immutable,
    resolve,
)
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


@pytest.mark.parametrize("kind", ["known_response_resolution", "retry_decision"])
def test_current_owner_authorizes_one_action_and_exact_replay_keeps_original(
    tmp_path, monkeypatch, kind
):
    store, clock, members, pack, receipt, policy, _ = fixture(tmp_path / "ledger")
    clock[0] += 121
    work_id = members[0]["work_item_id"]
    owner = client(tmp_path / "port")
    owner.work_store = store.path
    if kind == "known_response_resolution":
        inputs = arguments(store, members, pack, receipt, owner_authority=owner)
        intent = {
            "kind": kind,
            "work_item_id": work_id,
            **{
                key: inputs[key]
                for key in ("attempt_id", "idempotency_key", "reason", "diagnostic")
            },
        }

        def execute(ledger):
            return ledger.resolve_known_response(work_id, **inputs)

    else:
        resolve(store, members, pack, receipt)
        inputs = dict(
            purpose="retry_unusable_answer",
            retry_after_seconds=60,
            idempotency_key="retry-one-owner-read",
            comparison_controls=None,
        )
        intent = {"kind": kind, "work_item_id": work_id, **inputs}

        def execute(ledger):
            return ledger.record_owner_retry_decision(work_id, **inputs, owner_authority=owner)

    request = derive_work_decision(store.path, intent, policy=policy, clock=lambda: clock[0])[
        "request"
    ]
    confirmation = dict(
        owner_store_id="synthetic-owner",
        owner_principal="synthetic-operator",
        confirmation_id=owner.confirmation_id,
        action_id="synthetic-action",
        request_sha256=canonical_sha256(request),
        confirmed_at="2027-01-15T08:00:00Z",
    )
    modes = []
    fault = None

    def read(mode):
        modes.append(mode)
        if fault == "expired" and mode == "fresh_registration":
            raise OwnerRefreshRejected("synthetic preview expired")
        if fault == "revoked":
            raise OwnerRefreshRejected("synthetic owner revoked")
        value = dict(
            request={"decision_request": copy.deepcopy(request)},
            confirmation=copy.deepcopy(confirmation),
            admission_mode=mode,
        )
        if fault == "changed" and mode == "fresh_registration":
            value["confirmation"]["action_id"] = "changed-action"
        return value

    monkeypatch.setattr(owner, "_read_native_inspection", read)
    before = store.path.read_bytes()
    physical = immutable(store)
    fault = "expired"
    with pytest.raises(OwnerRefreshRejected, match="expired"):
        execute(store)
    assert modes == ["current_permissions", "fresh_registration"]
    assert store.path.read_bytes() == before
    modes.clear()

    fault = "changed"
    with pytest.raises(ValueError, match="current_permission_changed"):
        execute(store)
    assert modes == ["current_permissions", "fresh_registration"]
    assert store.path.read_bytes() == before
    modes.clear()

    fault = None
    saved = execute(store)
    assert modes == ["current_permissions", "fresh_registration"]
    assert saved["request"] == request
    assert saved["grant"]["confirmation_id"] == owner.confirmation_id
    assert immutable(store) == physical

    before = store.path.read_bytes()
    reopened = QuickScanWorkStore(store.path, clock=lambda: clock[0])
    clock[0] += 90
    modes.clear()
    assert execute(reopened) == saved
    assert modes == ["current_permissions"]
    assert store.path.read_bytes() == before

    modes.clear()
    fault = "revoked"
    with pytest.raises(OwnerRefreshRejected, match="revoked"):
        execute(reopened)
    assert modes == ["current_permissions"]
    assert store.path.read_bytes() == before

    modes.clear()
    fault = None
    confirmation["action_id"] = "mismatched-replay-record"
    with pytest.raises(ValueError, match="owner_work_existing_action_conflict"):
        execute(reopened)
    assert modes == ["current_permissions"]
    assert store.path.read_bytes() == before
