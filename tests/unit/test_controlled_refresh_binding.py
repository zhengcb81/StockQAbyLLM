"""Native storage tests with synthetic owner authority and settled receipts.

No production identity/financial golden, network or paid request is involved.
"""

import copy
from contextlib import closing

import pytest

from src.utils.quick_scan_work_control import project_owner_control
from src.utils.quick_scan_work_store import QuickScanWorkStore, WorkConflictError
from tests.unit.test_quick_scan_known_outcome import (
    authority,
    fixture,
    immutable,
    resolve,
    save_synthetic_standard,
)
from tests.unit.test_w15_owner_refresh import _binding


def prepared(tmp_path, *, ready=False, purpose="comparison", delay=0):
    store, clock, members, pack, receipt, _, context = fixture(
        tmp_path, response_status="completed" if ready else "length"
    )
    if ready:
        save_synthetic_standard(store, members, pack, receipt, context)
        clock[0] += 121
    else:
        clock[0] += 121
        resolve(store, members, pack, receipt)
    source = store.get_item(members[0]["work_item_id"])
    controls = dict(pack_size=3, thinking_enabled=True, max_output_tokens=32768)
    document = store.record_owner_retry_decision(
        source["work_item_id"],
        purpose=purpose,
        retry_after_seconds=delay,
        owner_authority=authority(),
        idempotency_key="control-bind",
        comparison_controls=controls if purpose == "comparison" else None,
    )
    subject = document["request"]["physical"]["subject_binding"]["subject_ref"]
    control = project_owner_control(
        document,
        source_work=source,
        subject_ref=subject,
        provider="mimo",
        model="mimo-v2.6-flash",
        now=clock[0],
        cohort_id="B01_SYNTHETIC_PACK3",
    )["owner_control"]
    binding = _binding(
        protocol="stockqa.owner_refresh_binding/3.0.0",
        subject_context_schema="stockqa.quick_scan_observation_context/2.0.0",
        owner_control=control,
        subject_key=subject["analysis_subject_id"]
        + "@"
        + str(subject["analysis_subject_revision"]),
        perimeter_sha256=subject["perimeter_sha256"],
        entity_id=source["entity_id"],
        question_id=source["question_id"],
        scope=source["scope"],
        scope_id=source["scope_id"],
        identity_revision=source["identity_revision"],
        provider="mimo",
        model="mimo-v2.6-flash",
    )
    keys = (
        "entity_id",
        "question_id",
        "scope",
        "scope_id",
        "identity_revision",
        "source_binding_version",
        "identity_state",
        "source_binding_ref",
        "identity_snapshot_sha256",
        "question_fingerprint",
    )
    spec = {key: source[key] for key in keys}
    spec.update(source_binding_refs=["BND_FIXTURE"], generation=2, routing_fingerprint="a" * 64)
    return store, clock, source, document, binding, spec


def create(store, binding, spec):
    return store.create_or_attach(
        **spec,
        owner_refresh_binding=binding,
        run_id="SYNTHETIC_CONTROL_RUN",
        scan_id="SYNTHETIC_CONTROL_SCAN",
    )


@pytest.mark.parametrize("ready", [False, True])
def test_exact_native_comparison_creates_one_independent_generation_without_rewriting_old_result(
    tmp_path, ready
):
    store, clock, source, document, binding, spec = prepared(tmp_path, ready=ready)
    physical = immutable(store)
    row = create(store, binding, spec)
    assert row["generation"] == 2 and row["status"] == "pending"
    assert store.get_item(source["work_item_id"]) == source
    assert immutable(store) == physical
    assert store.get_owner_refresh_binding(row["work_item_id"])["binding"] == binding
    assert create(store, binding, spec)["work_item_id"] == row["work_item_id"]
    again = QuickScanWorkStore(store.path, clock=lambda: clock[0])
    assert again.get_owner_refresh_binding(row["work_item_id"])["binding"] == binding
    if ready:
        assert (
            again.get_answer_checkpoint(source["work_item_id"])["payload"]["answer"]["score"]
            is None
        )


@pytest.mark.parametrize(
    "fault",
    [
        "unknown_decision",
        "hash",
        "old_work",
        "generation",
        "scope",
        "model",
        "purpose",
        "pack_size",
        "thinking",
        "max_tokens",
        "cohort_shape",
    ],
)
def test_recomputed_consumer_binding_cannot_change_the_native_decision(tmp_path, fault):
    store, _, _, _, binding, spec = prepared(tmp_path)
    bad = copy.deepcopy(binding)
    control = bad["owner_control"]
    if fault == "unknown_decision":
        control["decision_id"] = "owd1_" + "0" * 64
    elif fault == "hash":
        control["document_sha256"] = "0" * 64
    elif fault == "old_work":
        control["source_work_item_id"] = "WORK_OTHER"
    elif fault == "generation":
        control["source_generation"] += 1
    elif fault == "scope":
        bad["perimeter_sha256"] = "0" * 64
    elif fault == "model":
        bad["model"] = "mimo-v2.6-pro"
    elif fault == "purpose":
        control["purpose"] = "retry_unusable_answer"
    elif fault == "pack_size":
        control["comparison_controls"]["pack_size"] = 1
    elif fault == "thinking":
        control["comparison_controls"]["thinking_enabled"] = False
    elif fault == "max_tokens":
        control["comparison_controls"]["max_output_tokens"] = 8192
    else:
        control["cohort_id"] = "bad cohort"
    before = store.path.read_bytes()
    with pytest.raises((ValueError, WorkConflictError)):
        create(store, bad, spec)
    assert store.path.read_bytes() == before


def test_cooldown_is_checked_in_original_write_transaction_not_only_in_preview(tmp_path):
    store, clock, _, _, binding, spec = prepared(tmp_path, delay=60)
    before = store.path.read_bytes()
    with pytest.raises(WorkConflictError, match="control_not_ready"):
        create(store, binding, spec)
    assert store.path.read_bytes() == before
    clock[0] += 60
    assert create(store, binding, spec)["generation"] == 2


def test_unknown_retry_does_not_bypass_required_old_delivery(tmp_path):
    store, _, _, _, binding, spec = prepared(tmp_path, ready=True, purpose="retry_unknown_answer")
    before = store.path.read_bytes()
    with pytest.raises(WorkConflictError, match="control_not_ready"):
        create(store, binding, spec)
    assert store.path.read_bytes() == before


def test_independent_comparison_does_not_ignore_any_other_unresolved_generation(tmp_path):
    store, _, source, _, binding, spec = prepared(tmp_path, ready=True)
    other = dict(spec, generation=3)
    store.create_or_attach(**other, run_id="LEGACY_OTHER", scan_id="LEGACY_OTHER")
    before = store.path.read_bytes()
    with pytest.raises(WorkConflictError, match="prior_work_unresolved"):
        create(store, binding, spec)
    assert store.path.read_bytes() == before and store.get_item(source["work_item_id"]) == source


def test_old_binding_dialect_never_accepts_new_control_fields(tmp_path):
    store, _, _, _, binding, spec = prepared(tmp_path)
    binding["protocol"] = "stockqa.owner_refresh_binding/2.0.0"
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="binding_invalid"):
        create(store, binding, spec)
    assert store.path.read_bytes() == before
