"""Bounded paid continuation checks, real temporary Store and synthetic responses."""

import copy
import sqlite3
from types import SimpleNamespace

import pytest

from src.utils import quick_scan_delivery_seal as sealing
from src.utils.quick_scan_work_store import LeaseFencedError, WorkConflictError
from tests.unit.test_q10_delivery import (
    _c06_authority,
    _lifecycle,
    _seed_checkpoint,
    _store,
)


@pytest.mark.parametrize("error_type", [WorkConflictError, LeaseFencedError])
@pytest.mark.parametrize("after_success", [False, True])
def test_batch_sealer_keeps_later_checkpoint_and_actual_ready_outcome(
    tmp_path, monkeypatch, error_type, after_success
):
    store = _store(tmp_path)
    lifecycle = _lifecycle(store)
    rows = [_seed_checkpoint(store, lifecycle, q) for q in ("Q_A", "Q_B", "Q_C")]
    ids = [r["handle"]["work_item_id"] for r in rows]
    original = copy.deepcopy([store.get_answer_checkpoint(w) for w in ids])
    real = sealing.seal_result_delivery
    calls = []

    def seal_member(s, wid, **kwargs):
        calls.append(wid)
        if wid == ids[1]:
            if after_success:
                real(s, wid, **kwargs)
            raise error_type("synthetic local delivery conflict")
        return real(s, wid, **kwargs)

    monkeypatch.setattr(sealing, "seal_result_delivery", seal_member)
    report = sealing.seal_pending_deliveries(store, authority=_c06_authority())
    assert calls == ids
    assert store.get_result_delivery(ids[2])["state"] == "ready"
    assert report["model_calls"] == 0
    pending = [row["work_item_id"] for row in report["untouched"]]
    assert pending == ([] if after_success else [ids[1]])
    if after_success:
        assert ids[1] in [row["work_item_id"] for row in report["already_sealed"]]
    assert [store.get_answer_checkpoint(w) for w in ids] == original
    monkeypatch.setattr(sealing, "seal_result_delivery", real)
    sealing.seal_pending_deliveries(store, authority=_c06_authority())
    assert all(store.get_result_delivery(w)["state"] == "ready" for w in ids)
    assert [store.get_answer_checkpoint(w) for w in ids] == original


@pytest.mark.parametrize("error_type", [PermissionError, sqlite3.DatabaseError])
def test_batch_sealer_keeps_global_failures_visible(tmp_path, monkeypatch, error_type):
    store = _store(tmp_path)
    row = _seed_checkpoint(store, _lifecycle(store), "Q_GLOBAL")

    def fail(*a, **kw):
        raise error_type("synthetic global failure")

    monkeypatch.setattr(sealing, "seal_result_delivery", fail)
    with pytest.raises(error_type):
        sealing.seal_pending_deliveries(store, authority=_c06_authority())
    assert store.get_answer_checkpoint(row["handle"]["work_item_id"]) is not None


def test_public_paid_runner_does_not_require_usable_optional_capture(tmp_path, monkeypatch):
    import src.runners.llm_runner as runner
    import src.utils.quick_scan_refresh_execution as execution

    monkeypatch.chdir(tmp_path)
    bad_capture = tmp_path / "final-answer-diagnostics"
    bad_capture.write_text("synthetic obsolete optional capture", encoding="utf8")
    seen = []

    def prepare(*args, **kwargs):
        assert kwargs["recovery_only"] is True

        def recover(**options):
            seen.append(options)
            return dict(
                status="completed",
                API_requests=0,
                model_calls=0,
                recovered_work_item_ids=["WORK_SYNTHETIC"],
                attached_work_item_ids=[],
                deliveries=[],
            )

        return SimpleNamespace(recover_received_checkpoints=recover)

    monkeypatch.setattr(execution, "prepare_execution", prepare)
    monkeypatch.setattr(
        runner, "LLMConfig", lambda *a, **kw: pytest.fail("paid result must not load model config")
    )
    code = runner.LLMRunner().run(
        company="Synthetic",
        seal_deliveries=True,
        recover_checkpoints=True,
        config="questions.json",
        identity_snapshot="identity.json",
        question_manifest="manifest.json",
        c06_authority="authority.json",
        owner_refresh_config="owner-config.json",
        refresh_admission_config="admission.json",
        refresh_confirmation_id="original",
        output=str(tmp_path / "result.json"),
    )
    assert code == 0 and seen
    assert seen[0]["capture_policy"]["directory"] == bad_capture


def test_batch_after_conflict_does_not_claim_old_compact_head_matches_complete_authority(
    tmp_path, monkeypatch
):
    from tests.unit.test_quick_scan_c06_complete_seal import (
        V1_AUTHORITY,
        _checkpointed,
        _v2_authority,
    )

    store, item, _, _ = _checkpointed(tmp_path)
    wid = item["work_item_id"]
    real = sealing.seal_result_delivery
    real(store, wid, authority=V1_AUTHORITY)
    old = store.get_result_delivery(wid)
    authority = _v2_authority(tmp_path)

    def conflict(*a, **kw):
        raise WorkConflictError("synthetic supersede race")

    monkeypatch.setattr(sealing, "seal_result_delivery", conflict)
    report = sealing.seal_pending_deliveries(store, authority=authority)
    assert [r["work_item_id"] for r in report["untouched"]] == [wid]
    assert report["already_sealed"] == []
    assert store.get_result_delivery(wid)["package_id"] == old["package_id"]
    monkeypatch.setattr(sealing, "seal_result_delivery", real)
    again = sealing.seal_pending_deliveries(store, authority=authority)
    assert [r["work_item_id"] for r in again["sealed"]] == [wid]
    assert store.get_result_delivery(wid)["package_id"] != old["package_id"]
    assert again["model_calls"] == 0
