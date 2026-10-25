"""Native-handle mapper units, never owner-admission or financial positives.

The mapper object follows the existing native-inspection unit convention. It
uses a real temporary original work store and original durable responses; only
the already-reviewed registered-input guard is replaced for this narrow unit.
"""

from contextlib import closing
from types import SimpleNamespace

import pytest

from src.core.models import Question
from src.utils.quick_scan_owner_research import (
    OwnerResearchRejected,
    OwnerResearchServices,
)
from src.utils.quick_scan_work_store import LeaseFencedError
from tests.unit.test_quick_scan_work_store import (
    _created,
    _store,
    _successful_search_attempt,
)


@pytest.fixture
def native(tmp_path):
    clock = [1_800_000_000.0]
    store = _store(tmp_path, clock)
    item = _created(store)
    lease, attempt, receipt = _successful_search_attempt(store, item["work_item_id"])
    mapper = OwnerResearchServices.__new__(OwnerResearchServices)
    mapper._assert_registered_inputs = lambda: None  # Mapper-only unit, not production authority.
    mapper.store, mapper._handles = store, {}
    mapper.owner = SimpleNamespace(
        entries={"IQS_05": {"kind": "work", "work_item_id": item["work_item_id"]}}
    )
    mapper.questions = {"IQS_05": Question("synthetic mapper fixture", question_id="IQS_05")}
    return mapper, item, lease, attempt, receipt, clock


def test_restore_exact_original_live_lease_changes_no_durable_state(native):
    mapper, item, lease, attempt, receipt, _ = native
    before_item = mapper.store.get_item(item["work_item_id"])
    before_events = mapper.store.list_events(item["work_item_id"])
    handle = mapper.restore_finalization_handle(
        "IQS_05", native_reference="stockqa:" + receipt["attempt_id"]
    )
    assert handle == {
        "claimed": True,
        "reason": "restored_original_finalization_handle",
        "work_item_id": item["work_item_id"],
        "lease": lease,
    }
    assert mapper._handles["IQS_05"] == handle
    assert mapper.store.get_item(item["work_item_id"]) == before_item
    assert mapper.store.list_events(item["work_item_id"]) == before_events
    assert len(mapper.store.list_attempts(item["work_item_id"])) == 1
    assert (
        mapper.restore_finalization_handle(
            "IQS_05", native_reference="stockqa:" + receipt["attempt_id"]
        )
        == handle
    )


@pytest.mark.parametrize(
    "question,reference",
    [("foreign", "original"), ("IQS_05", "stockqa:unknown"), ("IQS_05", "foreign-reference")],
)
def test_restore_refuses_foreign_question_or_reference(native, question, reference):
    mapper, _, _, _, receipt, _ = native
    if reference == "original":
        reference = "stockqa:" + receipt["attempt_id"]
    with pytest.raises(OwnerResearchRejected):
        mapper.restore_finalization_handle(question, native_reference=reference)
    assert mapper._handles == {}


def test_restore_foreign_batch_member_receipt_is_not_authority(native):
    mapper, _, _, _, receipt, _ = native
    foreign = _created(mapper.store, question_id="IQS_06")
    mapper.owner.entries["IQS_06"] = {"kind": "work", "work_item_id": foreign["work_item_id"]}
    mapper.questions["IQS_06"] = Question("synthetic foreign member", question_id="IQS_06")
    with pytest.raises(OwnerResearchRejected, match="member"):
        mapper.restore_finalization_handle(
            "IQS_06", native_reference="stockqa:" + receipt["attempt_id"]
        )
    assert mapper._handles == {}


def test_restore_expired_lease_never_renews_or_reclaims(native):
    mapper, item, _, _, receipt, clock = native
    clock[0] += 61
    before = mapper.store.get_item(item["work_item_id"])
    with pytest.raises(LeaseFencedError):
        mapper.restore_finalization_handle(
            "IQS_05", native_reference="stockqa:" + receipt["attempt_id"]
        )
    assert mapper._handles == {} and mapper.store.get_item(item["work_item_id"]) == before


@pytest.mark.parametrize(
    "field,value", [("lease_token", "different-owner-token"), ("lease_epoch", 99)]
)
def test_restore_refuses_replaced_current_lease(native, field, value):
    mapper, item, _, _, receipt, _ = native
    with closing(mapper.store._connect()) as connection, connection:
        # Synthetic database corruption only; production code never writes leases.
        connection.execute(
            "UPDATE work_item SET " + field + "=? WHERE work_item_id=?",
            (value, item["work_item_id"]),
        )
    with pytest.raises(OwnerResearchRejected, match="lease"):
        mapper.restore_finalization_handle(
            "IQS_05", native_reference="stockqa:" + receipt["attempt_id"]
        )
    assert mapper._handles == {}


def test_existing_checkpoint_reseals_without_new_lease_or_attempt(native, monkeypatch):
    from tests.unit.test_quick_scan_work_store import _answer_for

    mapper, item, lease, attempt, receipt, _ = native
    body = dict(question_id="IQS_05", status="scored", score=8, summary="Synthetic mapper fixture")
    checkpoint = mapper.store.save_answer_checkpoint(
        item["work_item_id"],
        lease,
        attempt["attempt_id"],
        answer=_answer_for(item, description=body["summary"]),
        execution_receipt=receipt,
    )
    # This narrow mapper unit supplies the standard-body accessor. The public
    # software E2E verifies the actual complete standard-answer side table.
    monkeypatch.setattr(mapper.store, "get_standard_answer", lambda _: {"answer": body})
    seals = []

    def seal(work_id):
        seals.append(work_id)
        if len(seals) == 1:
            raise RuntimeError("synthetic failure after original checkpoint")

    mapper.lifecycle = SimpleNamespace(_seal_delivery=seal)
    before_events = mapper.store.list_events(item["work_item_id"])
    with pytest.raises(RuntimeError, match="after original checkpoint"):
        mapper.commit_standard_answers([body], native_reference="stockqa:" + receipt["attempt_id"])
    assert mapper._handles == {}
    result = mapper.commit_standard_answers(
        [body], native_reference="stockqa:" + receipt["attempt_id"]
    )
    assert result["saved_question_ids"] == ["IQS_05"]
    assert mapper.store.get_answer_checkpoint(item["work_item_id"]) == checkpoint
    assert mapper.store.list_events(item["work_item_id"]) == before_events
    assert len(mapper.store.list_attempts(item["work_item_id"])) == 1
    assert seals == [item["work_item_id"]] * 2
    changed = dict(body, summary="different output")
    with pytest.raises(OwnerResearchRejected, match="existing_original_answer_conflict"):
        mapper.commit_standard_answers(
            [changed], native_reference="stockqa:" + receipt["attempt_id"]
        )
    assert len(seals) == 2


def _scope_lifecycle(native, *, owner=None, budget=True):
    from src.runners.llm_runner import QuickScanWorkLifecycle
    from tests.unit.test_quick_scan_budget import _policy

    mapper, item, lease, _, _, _ = native
    policy = _policy(model="mimo-v2.6-flash") if budget else None
    identity = {
        key: item[key]
        for key in (
            "identity_revision",
            "source_binding_version",
            "identity_state",
            "source_binding_ref",
            "identity_snapshot_sha256",
        )
    }
    identity["source_binding_refs"] = [item["source_binding_ref"]]
    lifecycle = QuickScanWorkLifecycle(
        mapper.store,
        entity_id=item["entity_id"],
        run_id="UNIT_SCOPE",
        scan_id="UNIT_SCOPE",
        identity=identity,
        transport_managed=True,
        budget_policy=policy,
        budget_route=None if policy is None else policy["routes"][0],
        owner_refresh_session=owner,
    )
    return lifecycle, {"work_item_id": item["work_item_id"], "lease": lease}, policy


def test_research_scope_keeps_native_budget_guard_and_default_q09_reservation(native):
    from src.utils.quick_scan_owner_refresh import OwnerRefreshSession
    from src.utils.quick_scan_refresh_execution import ApprovedRefreshSession
    from src.utils.quick_scan_work_transport import (
        QuickScanWorkPersistenceError,
        bind_quick_scan_budget,
        own_reservation_held,
        require_budgeted_research_send,
    )

    mapper = native[0]
    # Guard-only owner object; the opt-in public E2E covers real admission.
    owner = ApprovedRefreshSession.__new__(ApprovedRefreshSession)
    owner.service = SimpleNamespace(store=mapper.store)
    owner.native = OwnerRefreshSession.__new__(OwnerRefreshSession)
    owner.native.store = mapper.store
    owner.check = lambda: None
    lifecycle, handle, policy = _scope_lifecycle(native, owner=owner)
    with lifecycle.dispatch_context(handle):
        assert own_reservation_held() is True
    assert own_reservation_held() is False
    with lifecycle.dispatch_context(handle, research_budget_managed=True):
        assert own_reservation_held() is False
        with pytest.raises(QuickScanWorkPersistenceError, match="durable_budget_binding"):
            require_budgeted_research_send()
        with bind_quick_scan_budget(mapper.store, policy):
            require_budgeted_research_send()
    assert own_reservation_held() is False
    assert len(mapper.store.list_attempts(native[1]["work_item_id"])) == 1


@pytest.mark.parametrize("missing", ["owner", "budget", "foreign_store", "foreign_native"])
def test_research_scope_refuses_missing_budget_or_foreign_owner(native, missing):
    from src.utils.quick_scan_owner_refresh import OwnerRefreshSession
    from src.utils.quick_scan_refresh_execution import ApprovedRefreshSession

    owner = ApprovedRefreshSession.__new__(ApprovedRefreshSession)
    owner.service = SimpleNamespace(
        store=object() if missing == "foreign_store" else native[0].store
    )
    owner.native = OwnerRefreshSession.__new__(OwnerRefreshSession)
    owner.native.store = object() if missing == "foreign_native" else native[0].store
    owner.check = lambda: None
    lifecycle, handle, _ = _scope_lifecycle(
        native, owner=None if missing == "owner" else owner, budget=missing != "budget"
    )
    with pytest.raises(ValueError, match="original_owner_budget_required"):
        lifecycle.dispatch_context(handle, research_budget_managed=True)
