"""Receipt inspection mapper only; this bypasses no production admission.

The unit object is intentionally not constructed/admitted as a production
OwnerResearchServices. A real temporary native SQLite contains a recorded
synthetic refusal; no owner permission, HTTP or financial positive is claimed.
"""

import copy
from types import SimpleNamespace

import pytest

from src.core.models import Question
from src.utils.quick_scan_owner_research import (
    OwnerResearchRejected,
    OwnerResearchServices,
)
from src.utils.quick_scan_result_outbox import canonical_sha256
from src.utils.quick_scan_work_store import (
    QuickScanWorkStore,
    quick_scan_receipt_sha256,
)
from tests.unit.test_quick_scan_budget import _policy
from tests.unit.test_quick_scan_work_store import (
    _created,
    _prepared,
    _synthetic_search_receipt,
)


def test_known_refusal_json_is_not_reported_as_successful_native_attempt(tmp_path):
    store = QuickScanWorkStore(tmp_path / "native-inspection-only.sqlite")
    item = _created(store)
    work_id = item["work_item_id"]
    lease = store.claim(work_id, lease_seconds=300)
    attempt = _prepared(store, work_id, lease)
    store.mark_send_intent(work_id, lease, attempt["attempt_id"])
    native = _synthetic_search_receipt()
    native.update(http_status_code=401, response_status="failed", search_status="not_requested")
    store.record_attempt_outcome(
        work_id,
        lease,
        attempt["attempt_id"],
        outcome="confirmed_failure",
        http_status_code=401,
        failure_category="authentication_rejected",
        receipt_sha256=quick_scan_receipt_sha256(native),
        execution_receipt=native,
    )
    mapper = OwnerResearchServices.__new__(OwnerResearchServices)
    mapper._assert_registered_inputs = lambda: None  # Mapper-only unit; never owner admission.
    mapper.store = store
    mapper.owner = SimpleNamespace(
        check=lambda: None, entries={"IQS_05": {"kind": "work", "work_item_id": work_id}}
    )
    mapper.questions = {"IQS_05": Question("synthetic mapper fixture", question_id="IQS_05")}
    mapper._observations, mapper._replayed_refs = {}, {}
    result = mapper.inspect_attempt("stockqa:" + native["attempt_id"])
    assert result["known"] is True
    assert result["outcome_recorded"] == "confirmed_failure"
    assert result["response_saved"] is None
    assert (
        mapper.request_resume("stockqa:" + native["attempt_id"])["decision"]
        == "pause_for_inspection"
    )
    assert store.list_attempts(work_id)[0]["phase"] == "confirmed_failure"


@pytest.mark.parametrize("drift", ["company_ref", "endpoint", "limits"])
@pytest.mark.parametrize(
    "entrypoint",
    ["inspect", "resume", "observe", "observations", "checkpoint", "intent", "finalization_handle"],
)
def test_every_cache_and_observer_entrypoint_refuses_separate_carrier_drift(drift, entrypoint):
    # Guard-only unit object, not an admitted owner. The positive assertion
    # checks exact unchanged carriers; every drift must stop before DB access.
    policy = _policy()
    task = SimpleNamespace(
        raw={"run_id": "guard-only"},
        questions=[],
        company_doc={"origin": "owner_public"},
        company_ref={"sha256": "a" * 64},
        limits={"max_model_requests_per_session": 2},
    )
    lifecycle = SimpleNamespace(
        _model_requested="fixture",
        _route_id="route",
        _provider_name="alias",
        _budget_route={"id": "route"},
        _model_resolution=None,
        _observation_context={},
        _budget_policy=policy,
    )
    mapper = OwnerResearchServices.__new__(OwnerResearchServices)
    mapper.task, mapper.lifecycle = task, lifecycle
    mapper.client = SimpleNamespace(
        model="fixture", provider_name="provider", base_url="https://original.invalid"
    )
    mapper.owner = SimpleNamespace(
        plan={"provider": "provider"},
        service=SimpleNamespace(budget=copy.deepcopy(policy)),
        check=lambda: None,
    )
    mapper.native_search = mapper._native_search_snapshot = False
    mapper.owner_batch_claim = mapper._owner_batch_claim_snapshot = False
    mapper._task_sha256 = canonical_sha256(task.raw)
    mapper._questions_snapshot = copy.deepcopy(task.questions)
    mapper._company_snapshot = copy.deepcopy(task.company_doc)
    mapper._company_ref_snapshot = copy.deepcopy(task.company_ref)
    mapper._limits_snapshot = copy.deepcopy(task.limits)
    mapper._client_endpoint_snapshot = mapper.client.base_url
    mapper._route_snapshot = (
        lifecycle._route_id,
        lifecycle._provider_name,
        copy.deepcopy(lifecycle._budget_route),
        None,
    )
    mapper._context_sha256 = canonical_sha256(lifecycle._observation_context)
    mapper._assert_registered_inputs()
    if drift == "company_ref":
        task.company_ref = {"sha256": "b" * 64}
    elif drift == "endpoint":
        mapper.client.base_url = "https://changed.invalid"
    else:
        task.limits = {"max_model_requests_per_session": 200}
    actions = {
        "inspect": lambda: mapper.inspect_attempt("stockqa:unknown"),
        "resume": lambda: mapper.request_resume("stockqa:unknown"),
        "observe": lambda: mapper.observe_native_step({"receipt": {}}),
        "observations": lambda: mapper.attempt_observations(transport_origin="scripted_wire"),
        "checkpoint": lambda: mapper.commit_standard_answers(
            [], native_reference="stockqa:unknown"
        ),
        "intent": lambda: mapper.recover_intent({"question_ids": ["other"]}),
        "finalization_handle": lambda: mapper.restore_finalization_handle(
            "other", native_reference="stockqa:unknown"
        ),
    }
    with pytest.raises(OwnerResearchRejected, match="registered_runtime_inputs_changed"):
        actions[entrypoint]()


def test_task_binding_is_pure_but_generation_scope_still_checks_current_revocation():
    policy = _policy()
    task = SimpleNamespace(
        raw={"run_id": "local-binding"},
        questions=[],
        company_doc={"subject": {"subject_ref": {"entity_id": "ENT_FIXTURE"}}},
        company_ref={"sha256": "a" * 64},
        limits={},
    )
    lifecycle = SimpleNamespace(
        _model_requested="fixture",
        _route_id="r",
        _provider_name="alias",
        _budget_route={"route_id": "r"},
        _model_resolution=None,
        _observation_context={},
        _budget_policy=policy,
    )
    mapper = OwnerResearchServices.__new__(OwnerResearchServices)
    mapper.task, mapper.lifecycle = task, lifecycle
    mapper.client = SimpleNamespace(
        model="fixture", provider_name="provider", base_url="https://original.invalid"
    )
    calls = []

    def revoked():
        calls.append("current")
        raise ValueError("native_actor_revoked")

    mapper.owner = SimpleNamespace(
        plan={"provider": "provider"},
        service=SimpleNamespace(budget=copy.deepcopy(policy)),
        check=revoked,
    )
    mapper.native_search = mapper._native_search_snapshot = False
    mapper.owner_batch_claim = mapper._owner_batch_claim_snapshot = False
    mapper._task_sha256 = canonical_sha256(task.raw)
    mapper._questions_snapshot = copy.deepcopy(task.questions)
    mapper._company_snapshot = copy.deepcopy(task.company_doc)
    mapper._company_ref_snapshot = copy.deepcopy(task.company_ref)
    mapper._limits_snapshot = {}
    mapper._client_endpoint_snapshot = mapper.client.base_url
    mapper._route_snapshot = ("r", "alias", copy.deepcopy(lifecycle._budget_route), None)
    mapper._context_sha256 = canonical_sha256({})
    mapper._task_binding = None
    binding = dict(
        synthetic=False,
        subject_ref=copy.deepcopy(task.company_doc["subject"]["subject_ref"]),
        authority_ref={"sha256": "b" * 64},
    )
    mapper.bind_task_owner_binding(binding)
    mapper._observations, mapper._replayed_refs = {}, {}
    assert mapper.attempt_observations(transport_origin="scripted_wire") == []
    assert calls == [] and mapper._task_binding == binding
    binding["subject_ref"]["entity_id"] = "ENT_OTHER"
    assert mapper._task_binding["subject_ref"]["entity_id"] == "ENT_FIXTURE"
    with pytest.raises(ValueError, match="native_actor_revoked"):
        with mapper.generation_scope({}):
            pytest.fail("revoked actor reached actual generation scope")
    assert calls == ["current"]


@pytest.mark.parametrize("release", ["explicit", "expiry", "mixed"])
def test_recover_intent_classifies_only_store_proven_unsent_scope(tmp_path, release):
    # Original SQLite release/expiry and inspection; no native owner admission
    # positive is claimed by this narrowly constructed service carrier.
    clock = [1800000000.0]
    store = QuickScanWorkStore(tmp_path / "unsent-scope.sqlite", clock=lambda: clock[0])
    qids = ["IQS_05", "IQS_06"]
    entries = {}
    leases = []
    for qid in qids:
        work_id = _created(store, question_id=qid)["work_item_id"]
        entries[qid] = dict(kind="work", work_item_id=work_id)
        leases.append(store.claim(work_id, lease_seconds=10))
    if release == "expiry":
        clock[0] += 11
    for index, (entry, lease) in enumerate(zip(entries.values(), leases)):
        if release == "mixed" and index == 1:
            continue
        if release in {"explicit", "mixed"}:
            assert store.release_unsent(entry["work_item_id"], lease) == "pending"
        else:
            assert store.recover_expired(entry["work_item_id"]) == "pending"
    mapper = OwnerResearchServices.__new__(OwnerResearchServices)
    mapper._assert_registered_inputs = lambda: None  # Scope classification seam only.
    mapper.store, mapper.owner = store, SimpleNamespace(entries=entries)
    mapper.questions = {qid: object() for qid in qids}
    mapper._task_sha256 = "a" * 64
    before = [
        (store.get_item(e["work_item_id"]), store.list_events(e["work_item_id"]))
        for e in entries.values()
    ]
    recovered = mapper.recover_intent(dict(kind="model_step", question_ids=qids))
    if release == "mixed":
        assert recovered is None
    else:
        assert recovered["decision"] == "not_sent" and recovered["saved_receipt"] is None
    assert [
        (store.get_item(e["work_item_id"]), store.list_events(e["work_item_id"]))
        for e in entries.values()
    ] == before


@pytest.mark.parametrize("state", ["new_pending", "leased", "new_claim", "send_intent", "unknown"])
def test_recover_intent_does_not_turn_missing_output_into_send_permission(tmp_path, state):
    store = QuickScanWorkStore(tmp_path / "not-proven-unsent.sqlite")
    work_id = _created(store)["work_item_id"]
    if state != "new_pending":
        lease = store.claim(work_id, lease_seconds=300)
        if state == "new_claim":
            store.release_unsent(work_id, lease)
            store.claim(work_id, lease_seconds=300)
        elif state in {"send_intent", "unknown"}:
            attempt = _prepared(store, work_id, lease)
            store.mark_send_intent(work_id, lease, attempt["attempt_id"])
            if state == "unknown":
                store.record_attempt_outcome(
                    work_id, lease, attempt["attempt_id"], outcome="unknown"
                )
    mapper = OwnerResearchServices.__new__(OwnerResearchServices)
    mapper._assert_registered_inputs = lambda: None  # Same classifier seam only.
    mapper.store, mapper.owner = store, SimpleNamespace(
        entries={"IQS_05": dict(kind="work", work_item_id=work_id)}
    )
    mapper.questions = {"IQS_05": object()}
    mapper._task_sha256 = "a" * 64
    assert mapper.recover_intent(dict(kind="model_step", question_ids=["IQS_05"])) is None
