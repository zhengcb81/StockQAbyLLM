"""Limited public sealing must progress across no-op and unrecoverable rows."""

import copy

import pytest

from src.utils import quick_scan_delivery_seal as sealing
from src.utils.quick_scan_work_store import QuickScanWorkStore, WorkConflictError
from tests.unit.test_q10_delivery import (
    _c06_authority,
    _lifecycle,
    _seed_checkpoint,
    _store,
)


def test_old_ready_does_not_starve_a_later_blocked_checkpoint(tmp_path):
    store = _store(tmp_path)
    lifecycle = _lifecycle(store)
    a, b = [
        _seed_checkpoint(store, lifecycle, q)["handle"]["work_item_id"]
        for q in ("Q_OLD", "Q_REPAIR")
    ]
    sealing.seal_result_delivery(store, a, authority=_c06_authority())
    sealing.seal_result_delivery(store, b, authority=None)
    original = copy.deepcopy([store.get_answer_checkpoint(w) for w in (a, b)])
    old_package = store.get_result_delivery(a)["package_bytes"]
    reopened = QuickScanWorkStore(store.path)
    report = sealing.seal_pending_deliveries(reopened, authority=_c06_authority(), limit=1)
    assert reopened.get_result_delivery(b)["state"] == "ready"
    assert report["model_calls"] == 0
    assert reopened.get_result_delivery(a)["package_bytes"] == old_package
    assert [reopened.get_answer_checkpoint(w) for w in (a, b)] == original


def test_matching_ready_does_not_starve_later_ready_revision_upgrade(tmp_path):
    from tests.unit.test_quick_scan_c06_complete_seal import (
        CLOCK,
        V1_AUTHORITY,
        _checkpointed,
        _v2_authority,
    )

    original_clock = CLOCK[0]
    try:
        store, first, _, _ = _checkpointed(tmp_path)
        target = _v2_authority(tmp_path)
        a = first["work_item_id"]
        sealing.seal_result_delivery(store, a, authority=target)
        CLOCK[0] += 1
        _, second, _, _ = _checkpointed(tmp_path, store=store, generation=2)
        b = second["work_item_id"]
        sealing.seal_result_delivery(store, b, authority=V1_AUTHORITY)
        old = copy.deepcopy([store.get_answer_checkpoint(w) for w in (a, b)])
        bytes_a = store.get_result_delivery(a)["package_bytes"]
        bytes_b = store.get_result_delivery(b)["package_bytes"]
        report = sealing.seal_pending_deliveries(store, authority=target, limit=1)
        assert store.get_result_delivery(b)["package_bytes"] != bytes_b
        assert store.get_result_delivery(a)["package_bytes"] == bytes_a
        assert sealing.get_matching_sealed_delivery(store, b, authority=target) is not None
        assert report["model_calls"] == 0
        assert [store.get_answer_checkpoint(w) for w in (a, b)] == old
    finally:
        CLOCK[0] = original_clock


def test_unsealable_checkpoint_does_not_use_limit_or_change_on_repeated_resume(tmp_path):
    from tests.unit.test_quick_scan_c06_complete_seal import (
        CLOCK,
        _checkpointed,
        _v2_authority,
    )

    original_clock = CLOCK[0]
    try:
        store, bad, _, _ = _checkpointed(tmp_path, with_context=False)
        CLOCK[0] += 1
        _, good, _, _ = _checkpointed(tmp_path, store=store, generation=2)
        ids = [row["work_item_id"] for row in (bad, good)]
        checkpoints = copy.deepcopy([store.get_answer_checkpoint(w) for w in ids])
        authority = _v2_authority(tmp_path)
        report = sealing.seal_pending_deliveries(store, authority=authority, limit=1)
        assert report["newly_sealed_count"] == 1
        assert report["processed_count"] == report["candidate_count"] == 2
        assert store.get_result_delivery(ids[0])["state"] == "blocked"
        assert store.get_result_delivery(ids[1])["state"] == "ready"
        before = [store.get_result_delivery(w) for w in ids]
        for _ in range(2):
            reopened = QuickScanWorkStore(store.path, clock=lambda: CLOCK[0])
            again = sealing.seal_pending_deliveries(reopened, authority=authority, limit=1)
            assert again["newly_sealed_count"] == 0
            assert [reopened.get_result_delivery(w) for w in ids] == before
            assert [reopened.get_answer_checkpoint(w) for w in ids] == checkpoints
    finally:
        CLOCK[0] = original_clock


@pytest.mark.parametrize("after_commit", [False, True])
def test_local_failure_limit_counts_only_durable_new_target(tmp_path, monkeypatch, after_commit):
    store = _store(tmp_path)
    lifecycle = _lifecycle(store)
    ids = [
        _seed_checkpoint(store, lifecycle, q)["handle"]["work_item_id"]
        for q in ("Q_FIRST", "Q_NEXT")
    ]
    original = copy.deepcopy([store.get_answer_checkpoint(w) for w in ids])
    real = sealing.seal_result_delivery
    calls = []

    def seal(s, wid, **options):
        calls.append(wid)
        if wid == ids[0]:
            if after_commit:
                real(s, wid, **options)
            raise WorkConflictError("synthetic local conflict")
        return real(s, wid, **options)

    monkeypatch.setattr(sealing, "seal_result_delivery", seal)
    report = sealing.seal_pending_deliveries(store, authority=_c06_authority(), limit=1)
    assert calls == (ids[:1] if after_commit else ids)
    assert report["newly_sealed_count"] == 1
    assert report["model_calls"] == 0
    if after_commit:
        assert store.get_result_delivery(ids[1]) is None
    else:
        assert store.get_result_delivery(ids[1])["state"] == "ready"
    assert [store.get_answer_checkpoint(w) for w in ids] == original


def test_fixed_snapshot_does_not_chase_new_candidates_and_reports_diagnostic_totals(
    tmp_path, monkeypatch
):
    store = _store(tmp_path)
    lifecycle = _lifecycle(store)
    ids = [_seed_checkpoint(store, lifecycle, f"Q_{i}")["handle"]["work_item_id"] for i in range(3)]
    for wid in ids:
        sealing.seal_result_delivery(store, wid, authority=_c06_authority())
    real = sealing.seal_result_delivery
    new_ids = []

    def seal(s, wid, **options):
        if not new_ids:
            new_ids.append(_seed_checkpoint(s, lifecycle, "Q_NEW")["handle"]["work_item_id"])
        return real(s, wid, **options)

    monkeypatch.setattr(sealing, "seal_result_delivery", seal)
    report = sealing.seal_pending_deliveries(store, authority=_c06_authority(), limit=1)
    assert report["candidate_count"] == report["processed_count"] == 3
    assert report["newly_sealed_count"] == 0
    assert report["diagnostic_counts"]["already_sealed"] == 3
    assert report["diagnostics_truncated"] is True
    assert len(report["already_sealed"]) == 1
    assert store.get_result_delivery(new_ids[0]) is None
    again = sealing.seal_pending_deliveries(store, authority=_c06_authority(), limit=1)
    assert again["newly_sealed_count"] == 1
    assert store.get_result_delivery(new_ids[0])["state"] == "ready"


@pytest.mark.parametrize("limit", [0, -1, 1001, True, 1.0])
def test_invalid_progress_limit_does_not_touch_candidates(tmp_path, limit):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="limit"):
        sealing.seal_pending_deliveries(store, authority=_c06_authority(), limit=limit)


def test_public_cli_reopens_same_store_and_continues_batch_without_resending(
    tmp_path, monkeypatch, capsys
):
    import functools
    import json

    import main_with_llm
    from tests.unit.test_qa_net01_c06_seal import _write_authority

    monkeypatch.chdir(tmp_path)
    store = QuickScanWorkStore(tmp_path / "quick_scan_work.sqlite")
    lifecycle = _lifecycle(store)
    ids = [
        _seed_checkpoint(store, lifecycle, q)["handle"]["work_item_id"]
        for q in ("Q_READY", "Q_WAITING")
    ]
    sealing.seal_result_delivery(store, ids[0], authority=_c06_authority())
    sealing.seal_result_delivery(store, ids[1], authority=None)
    checkpoints = copy.deepcopy([store.get_answer_checkpoint(w) for w in ids])
    # Narrow the native batch size to expose the former starvation with two
    # works; public parsing/config/store reopening and sealing remain real.
    monkeypatch.setattr(
        sealing,
        "seal_pending_deliveries",
        functools.partial(sealing.seal_pending_deliveries, limit=1),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "main_with_llm.py",
            "--seal-deliveries",
            "--c06-authority",
            str(_write_authority(tmp_path)),
        ],
    )
    for expected_new in (1, 0):
        capsys.readouterr()
        assert main_with_llm.main() == 0
        lines = capsys.readouterr().out.splitlines()
        payload = next(json.loads(line) for line in lines if line.startswith('{"already_sealed"'))
        assert payload["newly_sealed_count"] == expected_new
        assert payload["model_calls"] == 0
        reopened = QuickScanWorkStore(store.path)
        assert all(reopened.get_result_delivery(w)["state"] == "ready" for w in ids)
        assert [reopened.get_answer_checkpoint(w) for w in ids] == checkpoints
