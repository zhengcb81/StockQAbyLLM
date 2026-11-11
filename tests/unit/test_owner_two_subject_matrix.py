"""Two subjects, two tasks, mixed progress in one owner work store.

The paid-recovery chain proves per-member states; this file pins the
cross-subject property behind that chain: two companies in one owner store
never share a work item, a lease or a checkpoint, and one company's progress
projection never reports or charges the other company's work.

No model request, no search call and no real credential is involved.
"""

import pytest

from src.utils.quick_scan_work_store import QuickScanWorkStore

IDENTITY_SHA = "c" * 64
QUESTION_SHA = "d" * 64
ROUTING_SHA = "e" * 64


def _attach(store, *, entity_id, question_id, run_id="RUN_SYNTHETIC", scan_id="SCAN_SYNTHETIC"):
    return store.create_or_attach(
        entity_id=entity_id,
        question_id=question_id,
        generation=1,
        scope="entity",
        scope_id=entity_id,
        identity_revision=1,
        source_binding_version=1,
        identity_state="verified",
        source_binding_ref="BND_FIXTURE",
        source_binding_refs=["BND_FIXTURE"],
        identity_snapshot_sha256=IDENTITY_SHA,
        question_fingerprint=QUESTION_SHA,
        routing_fingerprint=ROUTING_SHA,
        run_id=run_id,
        scan_id=scan_id,
    )


@pytest.fixture
def store(tmp_path):
    clock = [1800000000.0]
    return QuickScanWorkStore(tmp_path / "work.sqlite", clock=lambda: clock[0])


def test_same_question_under_two_companies_gets_distinct_work_identity(store):
    a = _attach(store, entity_id="ENT_ALPHA", question_id="IQS_01")
    b = _attach(store, entity_id="ENT_BETA", question_id="IQS_01")

    assert a["work_item_id"] != b["work_item_id"]
    assert a["entity_id"] == "ENT_ALPHA" and b["entity_id"] == "ENT_BETA"

    # A lease on one company's work never fences the other company's work:
    # both claims succeed while the first is still held.
    lease_a = store.claim(a["work_item_id"], lease_seconds=120)
    lease_b = store.claim(b["work_item_id"], lease_seconds=120)
    assert lease_a.lease_token != lease_b.lease_token

    # Re-attaching the same run binds to its own logical work item again.
    again_a = _attach(store, entity_id="ENT_ALPHA", question_id="IQS_01")
    again_b = _attach(store, entity_id="ENT_BETA", question_id="IQS_01")
    assert again_a["work_item_id"] == a["work_item_id"]
    assert again_b["work_item_id"] == b["work_item_id"]


def test_mixed_progress_projection_never_merges_two_companies(store):
    """One answered task plus one pending task: progress stays per subject."""
    answered = _attach(store, entity_id="ENT_ALPHA", question_id="IQS_01")
    pending = _attach(store, entity_id="ENT_BETA", question_id="IQS_02")

    store.claim(answered["work_item_id"], lease_seconds=120)

    alpha = store.company_scan_progress([answered["work_item_id"]])
    beta = store.company_scan_progress([pending["work_item_id"]])

    assert [row["question_id"] for row in alpha["rows"]] == ["IQS_01"]
    assert [row["question_id"] for row in beta["rows"]] == ["IQS_02"]
    assert alpha["rows"][0]["checkpoint_id"] is None
    assert beta["rows"][0]["checkpoint_id"] is None
    assert beta["rows"][0]["latest_attempt"] is None
    # Neither task counted a physical model request yet, and the pending
    # company contributed no checkpoint.
    assert alpha["model_requests"] == 0
    assert beta["model_requests"] == 0
    assert store.get_answer_checkpoint(pending["work_item_id"]) is None
