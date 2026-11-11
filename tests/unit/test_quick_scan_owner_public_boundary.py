"""Owner public boundary: public fakes, detached DTOs and limited projections.

StockQA is the single execution owner. These counterexamples pin the boundary
rules this package must keep:

* the owner runtime and the owner services run against a fake that carries no
  private runner, store or client attribute, and touching one fails the test;
* a returned DTO is detached, so mutating its nested containers cannot change
  owner state;
* a returned handle does not expose undeclared state such as ``store``,
  ``client``, ``task`` or ``__dict__``;
* the Company Agent facade hands back only the operations a caller asked for,
  never the owner module object.

No provider is contacted and no paid model request is made.
"""

from __future__ import annotations

import copy
import sys
from types import SimpleNamespace

import pytest

from src.utils.quick_scan_company_api import company_agent_public_api


def _fake_company_agent_facade():
    """A stand-in for the Company Agent public facade (protocol 2.3.0)."""
    return SimpleNamespace(
        PUBLIC_API_PROTOCOL="iqs.company-agent-public-api/2.3.0",
        __all__=["task_snapshot", "task_from_snapshot", "LIMIT_CAPS"],
        task_snapshot=lambda task: {"task": task},
        task_from_snapshot=lambda snapshot: snapshot,
        LIMIT_CAPS={"max_model_requests_per_session": 12},
    )


def test_company_agent_facade_returns_only_the_requested_operations(monkeypatch):
    facade = _fake_company_agent_facade()
    monkeypatch.setitem(sys.modules, "iqs_company_agent_lab.api", facade)

    projection = company_agent_public_api("task_snapshot")

    assert projection.task_snapshot is facade.task_snapshot
    # The owner module object itself never leaves the owner package.
    assert projection is not facade
    for hidden in ("task_from_snapshot", "LIMIT_CAPS", "__all__", "PUBLIC_API_PROTOCOL"):
        assert not hasattr(projection, hidden), hidden
    with pytest.raises(AttributeError):
        projection.task_from_snapshot


def test_company_agent_facade_accepts_declared_public_values(monkeypatch):
    facade = _fake_company_agent_facade()
    monkeypatch.setitem(sys.modules, "iqs_company_agent_lab.api", facade)

    projection = company_agent_public_api("task_snapshot", "LIMIT_CAPS")

    assert projection.LIMIT_CAPS == {"max_model_requests_per_session": 12}
    assert projection.task_snapshot is facade.task_snapshot


def test_company_agent_facade_still_rejects_undeclared_and_unresolvable_names(monkeypatch):
    facade = _fake_company_agent_facade()
    monkeypatch.setitem(sys.modules, "iqs_company_agent_lab.api", facade)

    with pytest.raises(ImportError, match="company_agent_public_api_operation_not_exported"):
        company_agent_public_api("load_checkpoint_task")

    del facade.task_from_snapshot
    with pytest.raises(ImportError, match="company_agent_public_api_operation_missing"):
        company_agent_public_api("task_from_snapshot")


class _PublicOnlyServices:
    """Owner services double exposing public operations only.

    Any private attribute access is a test failure: the owner boundary must
    never need the runner, the store or the client through a private name.
    """

    native_session = object()
    native_search = False

    def __init__(self, task_contract):
        self.task_contract = task_contract
        self.bound = []

    def bind_task_owner_binding(self, binding):
        self.bound.append(binding)

    def client_for(self, envelope):
        return SimpleNamespace(provider_name="synthetic", model="synthetic-model")

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AssertionError(f"private owner state accessed: {name}")
        raise AttributeError(name)


def _runtime_for(services, monkeypatch):
    from src.utils import quick_scan_company_runtime

    monkeypatch.setitem(sys.modules, "iqs_company_agent_lab.api", _fake_company_agent_facade())
    monkeypatch.setattr(
        quick_scan_company_runtime, "is_admitted_research_service", lambda candidate: True
    )
    return quick_scan_company_runtime.CompanyResearchRuntime(services)


def test_owner_runtime_runs_against_a_fake_without_private_owner_state(monkeypatch):
    services = _PublicOnlyServices(task_contract={"protocol": "task/1.0.0"})

    runtime = _runtime_for(services, monkeypatch)

    assert runtime.has_native_session() is True
    assert runtime.describe()["protocol"] == "iqs.agent-runtime-services/1.0.0"
    assert services.bound == [None]


def test_owner_runtime_handle_hides_undeclared_state(monkeypatch):
    services = _PublicOnlyServices(task_contract={"protocol": "task/1.0.0"})

    runtime = _runtime_for(services, monkeypatch)

    for hidden in ("store", "client", "task", "__dict__", "_services", "services"):
        assert not hasattr(runtime, hidden), hidden


def test_owner_contract_dto_is_detached_from_the_owner_mapping():
    from src.interfaces.quick_scan_company import CompanyScanContract

    identity = {"entity_id": "SYNTHETIC", "listings": [{"market": "US", "ticker": "SYN"}]}
    observation = {"questions": {"Q1": {"metadata": {"scope": "entity"}}}}
    contract = CompanyScanContract(
        protocol="stockqa.company_scan_contract/1.0.0",
        requested_question_ids=("Q1",),
        eligible_question_ids=("Q1",),
        question_contracts={"Q1": {"id": "Q1"}},
        module_locks=({"module_id": "M", "version": "1"},),
        identity_payload=identity,
        native_input_references={
            name: {"path": f"native-input/{name}", "bytes": 1, "sha256": "a" * 64}
            for name in (
                "manifest.json",
                "identity.json",
                "authority.json",
                "owner-config.json",
                "questions.json",
            )
        },
        observation_context=observation,
        information_cutoff="2026-10-15",
        route_id="route-1",
        route_provider="synthetic",
        route_model="synthetic-model",
    )

    contract.observation_context["questions"]["Q1"]["metadata"]["scope"] = "mutated"
    contract.identity_payload["listings"][0]["ticker"] = "MUTATED"

    assert observation["questions"]["Q1"]["metadata"]["scope"] == "entity"
    assert identity["listings"][0]["ticker"] == "SYN"


def test_owner_refresh_port_snapshot_is_detached_and_hides_the_live_owner():
    from tests.unit.owner_refresh_port_test_support import approved_owner_refresh_port

    port = approved_owner_refresh_port()

    snapshot = port.snapshot()
    snapshot.plan["subject_key"] = "MUTATED"
    snapshot.entries["Q1"]["work_item_id"] = "MUTATED"

    assert port.snapshot().plan["subject_key"] == "SUBJ_1"
    assert port.snapshot().entries["Q1"]["work_item_id"] == "W1"
    for hidden in ("owner", "store", "client", "_owner", "__dict__"):
        assert not hasattr(port, hidden), hidden
    for hidden in ("owner", "store", "client", "_owner"):
        assert not hasattr(snapshot, hidden), hidden
