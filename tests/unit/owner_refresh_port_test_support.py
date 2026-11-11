"""Shared synthetic owner session fixtures for transport boundary tests."""

from types import SimpleNamespace

from src.utils.quick_scan_owner_refresh import OwnerRefreshSession
from src.utils.quick_scan_owner_refresh_api import adapt_owner_refresh
from src.utils.quick_scan_refresh_execution import ApprovedRefreshSession


def approved_owner_refresh_session(
    *, store=None, check=None, work_item_ids=("W1",), question_ids=None
):
    store = object() if store is None else store
    native = object.__new__(OwnerRefreshSession)
    native.store = store
    native.observation_context = {"schema": "context-v1", "questions": {"Q1": {}}}
    native.current = {"snapshot": {"subject_binding": {"subject": {"entity_id": "ENT_1"}}}}

    owner = object.__new__(ApprovedRefreshSession)
    owner.native = native
    owner.service = SimpleNamespace(store=store, budget={"configured": True})
    owner.plan = {"subject_key": "SUBJ_1", "perimeter_sha256": "a" * 64, "model": "m1"}
    owner.proof = {
        "request": {"protocol": "iqs.refresh_request/1.6.0"},
        "preview": {"ordered_route_targets": [{"route_id": "r1"}]},
        "confirmation": {"confirmation_id": "C1"},
    }
    owner.record = {"inputs": {"run_id": "RUN_1"}}
    question_ids = tuple(
        question_ids or (f"Q{index}" for index in range(1, len(work_item_ids) + 1))
    )
    if len(question_ids) != len(work_item_ids):
        raise ValueError("question_ids_must_match_work_item_ids")
    owner.bundle = {
        "manifest": {"questions": [{"id": qid} for qid in question_ids]},
        "authority": {"id": "A1"},
    }
    owner.entries = {
        qid: {"kind": "work", "work_item_id": work_item_id}
        for qid, work_item_id in zip(question_ids, work_item_ids)
    }
    owner.fields = {qid: {"decision": "resume_existing_work"} for qid in question_ids}
    owner.work = {qid: {"status": "pending"} for qid in question_ids}
    owner.deferred = {}
    owner.scope_bindings = {qid: {"scope": "entity", "scope_id": "ENT_1"} for qid in question_ids}
    owner.recovery_only = False
    owner.recovery_work_item_ids = []
    owner.remaining_work_item_ids = list(work_item_ids)
    owner.input_raw = {"identity.json": b'{"payload":{"entity_id":"ENT_1"}}'}
    owner.check = check or (lambda: None)
    return owner


def approved_owner_refresh_port(
    *, store=None, check=None, work_item_ids=("W1",), question_ids=None
):
    owner = approved_owner_refresh_session(
        store=store,
        check=check,
        work_item_ids=work_item_ids,
        question_ids=question_ids,
    )
    return adapt_owner_refresh(owner)
