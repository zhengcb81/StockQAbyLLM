"""Public facade contract; the Company Agent is consumed as a public double.

The real Company Agent package owns task semantics, so this file installs a
public fake of its facade. The real-package combination check lives in
``tests/integration/test_company_agent_facade_material.py``.
"""

import sys

import pytest

from src.utils import quick_scan_company_api as api
from tests.unit import company_agent_public_fake as company_agent_fake


@pytest.fixture(autouse=True)
def _company_agent_facade(monkeypatch):
    """Install the public Company Agent double for the whole module."""
    monkeypatch.setitem(sys.modules, "iqs_company_agent_lab.api", company_agent_fake)
    return company_agent_fake


create_owner_task = company_agent_fake.create_owner_task
task_snapshot = company_agent_fake.task_snapshot


def test_company_scan_public_api_is_versioned_and_explicit():
    assert api.PUBLIC_API_PROTOCOL == "stockqa.company-scan-public-api/1.8.0"
    assert len(api.__all__) == len(set(api.__all__))
    assert all(not name.startswith("_") for name in api.__all__)
    assert api.COMPANY_SCAN_TASK_PORT_PROTOCOL == "stockqa.company_scan_task_port/1.0.0"
    assert api.COMPANY_SCAN_RUNTIME_PORT_PROTOCOL == "stockqa.company_scan_runtime_port/2.2.0"
    assert api.CompanyScanTaskPort is not None
    assert api.company_scan_inputs is not None
    assert api.company_scan_contract is not None
    assert api.create_company_scan_task is not None
    assert api.create_native_task is not None
    assert api.COMPANY_SCAN_CONTRACT_PROTOCOL == "stockqa.company_scan_contract/1.0.0"
    assert api.create_company_research_runtime is not None
    assert api.supports_public_api(
        "iqs.agent-runtime-public-api/1.4.0", "iqs.agent-runtime-public-api", (1, 4, 0)
    )
    assert api.supports_public_api(
        "iqs.agent-runtime-public-api/1.5.0", "iqs.agent-runtime-public-api", (1, 4, 0)
    )
    assert not api.supports_public_api(
        "iqs.agent-runtime-public-api/2.0.0", "iqs.agent-runtime-public-api", (1, 4, 0)
    )
    from types import SimpleNamespace

    facade = SimpleNamespace(
        PUBLIC_API_PROTOCOL="iqs.agent-runtime-public-api/1.4.0",
        __all__=["published"],
        published=lambda: None,
        callable_but_private=lambda: None,
    )
    assert api.supports_public_api_operations(
        facade,
        "iqs.agent-runtime-public-api",
        (1, 4, 0),
        ("published",),
    )
    assert not api.supports_public_api_operations(
        facade,
        "iqs.agent-runtime-public-api",
        (1, 4, 0),
        ("callable_but_private",),
    )
    assert api.RUNTIME_SERVICES_PROTOCOL == "iqs.agent-runtime-services/1.0.0"
    assert api.company_agent_public_api is not None
    assert "CompanyResearchRuntime" not in api.__all__
    try:
        api._implementation_detail
    except AttributeError:
        pass
    else:
        raise AssertionError("private or unregistered API names must not resolve")


def test_runtime_facade_exposes_owner_operations_without_consumer_store_access(monkeypatch):
    from src.utils import quick_scan_company_runtime

    raw = {
        "protocol": "iqs.company-scan-task/1.0.0",
        "run_id": "runtime-facade-test",
        "company_ref": {"path": "company.json", "bytes": 1, "sha256": "a" * 64},
        "information_cutoff": "2026-10-15",
        "limits": {},
        "strategy_id": "free_agent",
        "question_refs": [],
    }
    task = create_owner_task(
        raw,
        [],
        {"origin": "owner_public"},
        {"records": []},
        {"provider": "test", "model": "test-model"},
        {},
    )

    class Services:
        native_session = object()
        native_search = False

        def attempt_observations(self, *, transport_origin):
            return [{"transport_origin": transport_origin}]

        def recover_intent(self, intent):
            return {"intent": intent}

        def request_resume(self, attempt_ref):
            return {"attempt_ref": attempt_ref}

        def inspect_attempt(self, attempt_ref):
            return {"attempt_ref": attempt_ref}

        def validate_registered_inputs(self):
            return True

        def bind_task_owner_binding(self, _binding):
            return None

    services = Services()
    services.task = task
    contract = task_snapshot(task)
    monkeypatch.setattr(
        quick_scan_company_runtime,
        "is_admitted_research_service",
        lambda candidate: candidate is services,
    )
    runtime = api.create_company_research_runtime(services, task_contract=contract)
    assert not hasattr(runtime, "task")
    assert not hasattr(runtime, "_services")
    assert not hasattr(runtime, "__dict__")
    assert runtime.is_bound_to_task_contract(contract)
    changed = dict(contract)
    changed["company_doc"] = {"origin": "synthetic"}
    assert not runtime.is_bound_to_task_contract(changed)
    assert runtime.has_native_session()
    assert runtime.attempt_observations(transport_origin="test") == [{"transport_origin": "test"}]
    assert runtime.supports_intent_recovery()
    assert runtime.recover_intent({"id": "i-1"}) == {"intent": {"id": "i-1"}}
    assert runtime.request_resume("attempt-1") == {"attempt_ref": "attempt-1"}
    assert runtime.inspect_attempt("attempt-1") == {"attempt_ref": "attempt-1"}
    assert runtime.validate_registered_inputs() is True
    description = runtime.describe()
    assert description["protocol"] == "iqs.agent-runtime-services/1.0.0"
    assert description["capabilities"]["original_semantic_resources"] is True


def test_runtime_semantic_resources_use_the_owner_facade_and_return_a_detached_dto(monkeypatch):
    import copy
    from types import SimpleNamespace

    import src.utils.quick_scan_company_runtime as runtime_impl
    from src.utils import quick_scan_company_context, quick_scan_owner_refresh_api
    from src.utils.quick_scan_company_runtime import CompanyResearchRuntime

    context = {"schema": "owner-context/1", "questions": {"Q1": {"metadata": {}}}}
    projected = {"protocol": "iqs.answer-semantic-resources/1.0.0", "resource_digests": {}}
    owner = object()
    services = SimpleNamespace(
        owner=owner,
        lifecycle=object(),
        bind_task_owner_binding=lambda _binding: None,
    )
    task_contract = {"protocol": "task/1", "owner_binding": {"id": "owner-1"}}
    monkeypatch.setattr(runtime_impl, "is_admitted_research_service", lambda _: True)
    monkeypatch.setattr(runtime_impl, "_service_task_contract", lambda _: task_contract)
    monkeypatch.setattr(
        quick_scan_company_context,
        "company_scan_contract",
        lambda _: SimpleNamespace(observation_context=copy.deepcopy(context)),
    )
    calls = []
    monkeypatch.setattr(
        quick_scan_owner_refresh_api,
        "original_semantic_resources",
        lambda candidate, observed: calls.append((candidate, observed)) or projected,
    )

    runtime = CompanyResearchRuntime(services, task_contract=task_contract)
    result = runtime.original_semantic_resources()

    assert calls == [(owner, context)]
    assert result == projected and result is not projected
    result["resource_digests"]["mutated"] = "x"
    assert projected["resource_digests"] == {}


def test_runtime_factory_rejects_unregistered_owner_service():
    import pytest

    with pytest.raises(ValueError, match="original_owner_research_services_required"):
        api.create_company_research_runtime(object())


def test_owner_research_uses_detached_company_task_dto_projection():
    import copy

    import pytest

    from src.utils.quick_scan_owner_research import (
        OwnerResearchRejected,
        _company_task_projection,
    )

    subject_ref = {"entity_id": "SYNTHETIC", "scope": "entity", "scope_id": "SYNTHETIC"}
    task = create_owner_task(
        {
            "protocol": "iqs.company-scan-task/1.0.0",
            "run_id": "task-projection-test",
            "company_ref": {"path": "company.json", "bytes": 1, "sha256": "a" * 64},
            "information_cutoff": "2026-10-15",
            "limits": {"max_model_requests_per_session": 2},
            "strategy_id": "free_agent",
            "question_refs": [],
        },
        [{"question_id": "Q1", "question": {"id": "Q1"}}],
        {"origin": "owner_public", "subject": {"subject_ref": subject_ref}},
        {"records": []},
        {"provider": "synthetic-test", "model": "synthetic-test"},
        {},
        owner_binding={
            "protocol": "iqs.native-company-owner-binding/1.0.0",
            "synthetic": False,
            "subject_ref": subject_ref,
            "authority_ref": {"id": "synthetic-authority"},
        },
    )
    contract = task_snapshot(task)
    before = copy.deepcopy(contract)

    projected_contract, projected = _company_task_projection(task)

    assert projected_contract == before
    assert projected.raw == before["raw"]
    assert projected.question_ids == before["question_ids"]
    assert projected.company_doc == before["company_doc"]
    projected_contract["raw"]["run_id"] = "mutated-copy"
    assert projected.raw["run_id"] == "task-projection-test"

    malformed = copy.deepcopy(before)
    malformed["contract_sha256"] = "0" * 64
    with pytest.raises(OwnerResearchRejected, match="versioned_company_task_contract_required"):
        _company_task_projection(malformed)
