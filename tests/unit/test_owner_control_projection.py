"""Actual journal input, synthetic issuer/permission; no HTTP or financial gold."""

import copy

import pytest

from tests.unit.test_quick_scan_known_outcome import authority, fixture, resolve


def prepared(tmp_path):
    store, clock, members, pack, receipt, _, _ = fixture(tmp_path)
    clock[0] += 121
    resolve(store, members, pack, receipt)
    work = store.get_item(members[0]["work_item_id"])
    decision = store.record_owner_retry_decision(
        work["work_item_id"],
        purpose="comparison",
        retry_after_seconds=60,
        owner_authority=authority(),
        idempotency_key="control-projection",
        comparison_controls=dict(pack_size=3, thinking_enabled=True, max_output_tokens=32768),
    )
    return store, clock, work, decision


def project(work, decision, now, **kwargs):
    from src.utils.quick_scan_work_control import project_owner_control

    return project_owner_control(
        decision,
        source_work=work,
        subject_ref=decision["request"]["physical"]["subject_binding"]["subject_ref"],
        provider="mimo",
        model="mimo-v2.6-flash",
        now=now,
        cohort_id=kwargs.get("cohort_id", "B01_SYNTHETIC_PACK3"),
    )


def test_cooldown_blocks_even_when_no_observation_exists_then_new_generation_uses_fixed_controls(
    tmp_path,
):
    store, clock, work, decision = prepared(tmp_path)
    before = store.path.read_bytes()
    early = project(work, decision, clock[0])
    assert early["decision"] == "deferred_unknown" and early["generation"] is None
    ready = project(work, decision, clock[0] + 60)
    assert (
        ready["decision"] == "dispatch_new_generation"
        and ready["generation"] == work["generation"] + 1
    )
    assert ready["owner_control"]["decision_id"] == decision["decision_id"]
    assert ready["owner_control"]["cohort_id"] == "B01_SYNTHETIC_PACK3"
    assert ready["owner_control"]["comparison_controls"] == dict(
        pack_size=3, thinking_enabled=True, max_output_tokens=32768
    )
    assert (
        store.path.read_bytes() == before
        and store.get_answer_checkpoint(work["work_item_id"]) is None
    )


@pytest.mark.parametrize(
    "fault", ["hash", "work", "generation", "scope", "model", "purpose", "time", "cohort"]
)
def test_changed_control_context_is_refused_without_modifying_originals(tmp_path, fault):
    store, clock, work, decision = prepared(tmp_path)
    bad = copy.deepcopy(decision)
    kw = {}
    changed = copy.deepcopy(work)
    if fault == "hash":
        bad["decision_id"] = "owd1_" + "0" * 64
    elif fault == "work":
        changed["work_item_id"] = "WORK_OTHER"
    elif fault == "generation":
        changed["generation"] += 1
    elif fault == "scope":
        changed["scope_id"] = "ENT_OTHER"
    elif fault == "model":
        bad["request"]["physical"]["model_requested"] = "mimo-v2.6-pro"
    elif fault == "purpose":
        bad["request"]["purpose"] = "automatic_retry"
    elif fault == "time":
        clock[0] = float("nan")
    else:
        kw["cohort_id"] = "bad cohort"
    if fault != "hash":
        from src.utils.quick_scan_result_outbox import canonical_sha256

        bad["decision_id"] = "owd1_" + canonical_sha256(
            {k: v for k, v in bad.items() if k != "decision_id"}
        )
    before = store.path.read_bytes()
    with pytest.raises((ValueError, TypeError)):
        project(changed, bad, clock[0] + 60, **kw)
    assert store.path.read_bytes() == before


def test_projection_does_not_override_current_inflight_or_different_generation(tmp_path):
    _, clock, work, decision = prepared(tmp_path)
    work["status"] = "uncertain"
    with pytest.raises(ValueError):
        project(work, decision, clock[0] + 60)
