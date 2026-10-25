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
