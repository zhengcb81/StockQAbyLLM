"""Pure adapter seam tests; check spies are synthetic, not native owner proofs."""

import copy
import inspect
import time
from contextlib import closing, nullcontext
from types import SimpleNamespace

import pytest

from src.core.models import Question
from src.runners.llm_runner import QuickScanWorkLifecycle
from src.utils.quick_scan_owner_refresh import OwnerRefreshSession
from src.utils.quick_scan_owner_research import OwnerResearchServices
from src.utils.quick_scan_refresh_admission import AdmissionRejected
from src.utils.quick_scan_refresh_execution import ApprovedRefreshSession
from src.utils.quick_scan_refresh_inspect import ReadOnlyWorkView
from src.utils.quick_scan_work_store import BudgetAdmissionError, WorkConflictError
from src.utils.quick_scan_work_transport import _OWNER_REFRESH_GUARD
from tests.unit.owner_refresh_port_test_support import approved_owner_refresh_port
from tests.unit.test_owner_batch_claim import invoke, prepare, rows, sha


def synthetic_adapter(prepared):
    """No real permission granted: only original journal + a counted check spy."""
    store, _, confirmation, _, items = prepared
    read_registration = ReadOnlyWorkView(store.path).get_registered_refresh_action
    record = read_registration(confirmation)
    owner = object.__new__(ApprovedRefreshSession)
    owner.native = object.__new__(OwnerRefreshSession)
    owner.native.store = store
    owner.native.observation_context = None
    owner.service = SimpleNamespace(store=store, read_registration=read_registration)
    owner.proof = {"confirmation": {"confirmation_id": confirmation}}
    owner.record = record
    owner.entries = {
        item["question_id"]: {
            "kind": "work",
            "work_item_id": item["work_item_id"],
            "specification": spec,
        }
        for item, spec in zip(items, record["inputs"]["specifications"])
    }
    owner.fields = {qid: {"decision": "resume_existing_work"} for qid in owner.entries}
    owner.deferred = {}
    calls = []
    owner.check = lambda: calls.append("fresh native check seam")
    owner.plan = {"perimeter_sha256": sha("perimeter")}
    return owner, calls


def lifecycle(prepared, owner):
    identity = {
        k: prepared[0].get_item(prepared[-1][0]["work_item_id"])[k]
        for k in (
            "entity_id",
            "identity_revision",
            "source_binding_version",
            "identity_state",
            "source_binding_ref",
            "identity_snapshot_sha256",
        )
    }
    identity["source_binding_refs"] = ["BND_US_FIX"]
    context = {
        "schema": "stockqa.quick_scan_observation_context/2.0.0",
        "questions": {
            qid: {
                "subject_binding": {
                    "protocol": "stockqa.subject_context/1.0.0",
                    "subject_ref": {
                        "entity_id": "ENT_fixture",
                        "scope": "entity",
                        "scope_id": "ENT_fixture",
                        "perimeter_sha256": sha("perimeter"),
                    },
                    "question_contract": {"prompt": qid},
                },
                "work_prompt_sha256": sha(qid),
            }
            for qid in owner.entries
        },
    }
    return QuickScanWorkLifecycle(
        prepared[0],
        entity_id="ENT_fixture",
        run_id="BATCH",
        scan_id="BATCH",
        identity=identity,
        transport_managed=True,
        owner_refresh_session=owner,
        observation_context=context,
    )


def test_local_claim_leases_exact_group_without_requesting_send_permission(tmp_path):
    prepared = prepare(tmp_path, 3)
    owner, calls = synthetic_adapter(prepared)
    life = lifecycle(prepared, owner)
    before = _OWNER_REFRESH_GUARD.get()
    result = life.before_registered_questions(
        [Question(qid, question_id=qid) for qid in owner.entries]
    )
    assert calls == [] and len(result) == 3 and all(r["claimed"] is True for r in result)
    assert _OWNER_REFRESH_GUARD.get() is before


@pytest.mark.parametrize(
    "drift",
    [
        "foreign_qid",
        "reference",
        "deferred",
        "prompt",
        "identity",
        "generation",
        "subject",
    ],
)
def test_adapter_rejects_drift_without_partial_claims(tmp_path, drift):
    prepared = prepare(tmp_path)
    owner, calls = synthetic_adapter(prepared)
    life = lifecycle(prepared, owner)
    questions = [Question(qid, question_id=qid) for qid in owner.entries]
    if drift == "foreign_qid":
        questions[-1] = Question("foreign", question_id="FOREIGN")
    elif drift == "reference":
        owner.entries[questions[-1].question_id]["kind"] = "reference"
    elif drift == "deferred":
        owner.deferred[questions[-1].question_id] = "superseded"
    elif drift == "prompt":
        questions[-1] = Question("changed", question_id=questions[-1].question_id)
    elif drift == "identity":
        life._identity["identity_revision"] = 2
    elif drift == "generation":
        life._generation = 2
    else:
        life._observation_context["questions"][questions[-1].question_id]["subject_binding"][
            "subject_ref"
        ]["perimeter_sha256"] = sha("another perimeter")
    before = rows(prepared[0])
    with pytest.raises((ValueError, AdmissionRejected, WorkConflictError)):
        life.before_registered_questions(questions)
    assert rows(prepared[0]) == before


def test_send_permission_rejection_is_owned_by_transport_after_atomic_claim(tmp_path):
    from src.utils.quick_scan_work_transport import (
        QuickScanWorkPersistenceError,
        _check_owner_refresh_guard,
        bind_owner_refresh_guard,
        bind_quick_scan_work,
    )

    prepared = prepare(tmp_path)
    owner, calls = synthetic_adapter(prepared)
    life = lifecycle(prepared, owner)

    def revoked():
        calls.append("current-send-check")
        raise AdmissionRejected("native permission revoked")

    owner.check = revoked
    handles = life.before_registered_questions(
        [Question(qid, question_id=qid) for qid in owner.entries]
    )
    assert calls == [] and all(handle["claimed"] for handle in handles)
    with bind_quick_scan_work(prepared[0], handles[0]["work_item_id"], handles[0]["lease"]):
        with bind_owner_refresh_guard(life._owner_refresh):
            with pytest.raises(QuickScanWorkPersistenceError) as error:
                _check_owner_refresh_guard()
    assert isinstance(error.value.__cause__, AdmissionRejected)
    assert calls == ["current-send-check"]


def test_registration_read_crossing_original_deadline_leaves_all_pending(tmp_path, monkeypatch):
    prepared = prepare(tmp_path)
    owner, _ = synthetic_adapter(prepared)
    life = lifecycle(prepared, owner)
    monotonic = [10.0]
    monkeypatch.setattr(time, "monotonic", lambda: monotonic[0])
    life._deadline = 15.0
    read_registration = owner.service.read_registration

    def slow_registration(key):
        record = read_registration(key)
        monotonic[0] = 20.0
        return record

    owner.service.read_registration = slow_registration
    before = rows(prepared[0])
    with pytest.raises(WorkConflictError, match="deadline"):
        life.before_registered_questions([Question(qid, question_id=qid) for qid in owner.entries])
    assert rows(prepared[0]) == before


def test_ordinary_object_is_not_an_original_approved_session(tmp_path):
    prepared = prepare(tmp_path)
    owner, _ = synthetic_adapter(prepared)
    life = lifecycle(prepared, owner)
    life._owner_refresh = SimpleNamespace(
        service=SimpleNamespace(store=prepared[0]), check=lambda: None
    )
    with pytest.raises(ValueError, match="original_approved"):
        life.before_registered_questions([Question("Q0", question_id="Q0")])


def test_explicit_service_opt_in_defaults_off():
    assert inspect.signature(OwnerResearchServices).parameters["owner_batch_claim"].default is False


@pytest.mark.parametrize("enabled", [False, True])
def test_service_uses_selected_branch_without_extra_or_partial_handle_claims(enabled):
    services = object.__new__(OwnerResearchServices)
    services.owner = approved_owner_refresh_port()
    services._validate_envelope = lambda e: None
    services.request_scope = None
    services._active = False
    services.native_session = False
    services.owner_batch_claim = enabled
    services._handles = {}
    services._responded_work_ids = set()
    services.questions = {q: Question(q, question_id=q) for q in ("Q0", "Q1")}
    calls = []

    def singleton(q):
        calls.append(("singleton", q.question_id))
        return {"claimed": True, "work_item_id": q.question_id, "lease": "synthetic"}

    def batch(qs):
        calls.append(("batch", tuple(q.question_id for q in qs)))
        return [{"claimed": True, "work_item_id": q.question_id, "lease": "synthetic"} for q in qs]

    def stop(handles):
        raise RuntimeError("synthetic stop before original transport boundary")

    services.lifecycle = SimpleNamespace(
        registered_research_work=lambda: SimpleNamespace(
            budget_policy={"synthetic": "budget"},
            budget_route={"quota_group": "synthetic"},
        ),
        before_question=singleton,
        before_registered_questions=batch,
        _budget_policy={"synthetic": "budget"},
        _budget_route={"quota_group": "synthetic"},
        dispatch_question_pack_context=stop,
    )
    with pytest.raises(RuntimeError, match="synthetic stop"):
        with services.generation_scope(
            {"dynamic_context": {"requested_question_ids": ["Q0", "Q1"]}}
        ):
            pass
    assert calls == (
        [("batch", ("Q0", "Q1"))] if enabled else [("singleton", "Q0"), ("singleton", "Q1")]
    )
    assert set(services._handles) == {"Q0", "Q1"} and services._active is False


def test_batch_claim_does_not_clear_unknown_budget_or_count_a_send(tmp_path):
    prepared = prepare(tmp_path)
    store = prepared[0]
    policy = {
        "configured": True,
        "policy_id": "same_ledger",
        "policy_version": "v1",
        "budget": {
            "currency": "USD",
            "max_cost": 25,
            "max_requests": 350,
            "max_cost_per_attempt": 0.1,
        },
        "cost_policy": {
            "pricing_basis": "verified_rate_card",
            "pricing_ref": "synthetic",
            "reserve_before_dispatch": True,
            "unknown_actual_cost_action": "retain_reservation_and_pause",
        },
        "dispatch": {"max_in_flight_total": 2},
        "quota_groups": [{"id": "group", "max_in_flight": 2}],
        "routes": [
            {
                "id": "route",
                "provider_config_ref": "alias",
                "model": "model",
                "quota_group": "group",
                "max_in_flight": 2,
                "eligible": True,
                "unavailable_reason": None,
            }
        ],
    }
    store.configure_quick_scan_budget(policy)
    for q in ("unknown", "unpriced"):
        store.reserve_budget_attempt(
            policy,
            budget_attempt_id="DISPATCH_" + q,
            route_id="route",
            provider="alias",
            model_requested="model",
            quota_group="group",
        )
    store.record_budget_outcome("DISPATCH_unknown", outcome="unknown", http_status_code=None)
    store.record_budget_outcome(
        "DISPATCH_unpriced", outcome="response_available", http_status_code=200
    )
    with closing(store._connect()) as db:
        before = [
            tuple(r)
            for r in db.execute(
                "SELECT * FROM quick_scan_budget_attempt ORDER BY budget_attempt_id"
            )
        ]
    invoke(prepared)
    with closing(store._connect()) as db:
        assert before == [
            tuple(r)
            for r in db.execute(
                "SELECT * FROM quick_scan_budget_attempt ORDER BY budget_attempt_id"
            )
        ]
        assert db.execute("SELECT COUNT(*) FROM attempt").fetchone()[0] == 0
    with pytest.raises(BudgetAdmissionError, match="reconciliation_required"):
        store.reserve_budget_attempt(
            policy,
            budget_attempt_id="DISPATCH_new",
            route_id="route",
            provider="alias",
            model_requested="model",
            quota_group="group",
        )


@pytest.mark.parametrize("mutation", ["swapped", "specification"])
def test_original_entry_cannot_be_relabelled_to_another_question(tmp_path, mutation):
    prepared = prepare(tmp_path)
    owner, _ = synthetic_adapter(prepared)
    if mutation == "swapped":
        owner.entries["Q0"], owner.entries["Q1"] = owner.entries["Q1"], owner.entries["Q0"]
    else:
        owner.entries["Q0"]["specification"] = copy.deepcopy(owner.entries["Q0"]["specification"])
        owner.entries["Q0"]["specification"]["routing_fingerprint"] = sha("changed entry")
    before = rows(prepared[0])
    with pytest.raises(AdmissionRejected, match="original_entry"):
        owner.claim_registered_question_batch(
            ["Q0", "Q1"], run_id="BATCH", scan_id="BATCH", lease_seconds=600
        )
    assert rows(prepared[0]) == before
