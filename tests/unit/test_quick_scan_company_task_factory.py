"""Task creation stays inside StockQA and rejects scope changes before delegation."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from src.interfaces.quick_scan_company import CompanyScanContract
from src.utils import _quick_scan_company_task_factory as factory
from src.utils import quick_scan_company_api as api
from src.utils import quick_scan_company_context as context


def _contract():
    question = {"id": "Q1", "module_id": "base", "prompt": "Synthetic question"}
    subject = {
        "entity_id": "ENT_SYNTHETIC",
        "scope": "entity",
        "scope_id": "ENT_SYNTHETIC",
        "primary_issuer_id": "ISS_SYNTHETIC",
    }
    return CompanyScanContract(
        protocol="stockqa.company_scan_contract/1.0.0",
        requested_question_ids=("Q1", "Q2"),
        eligible_question_ids=("Q1",),
        question_contracts={
            "Q1": question,
            "Q2": {"id": "Q2", "module_id": "base", "prompt": "Other question"},
        },
        module_locks=({"module_id": "base", "version": "1.0.0"},),
        identity_payload={
            "entity_id": "ENT_SYNTHETIC",
            "canonical_name": "Synthetic issuer",
            "listings": [{"market": "US", "ticker": "SYN"}],
        },
        native_input_references={
            name: {"path": "native-input/" + name, "bytes": 4, "sha256": str(i + 1) * 64}
            for i, name in enumerate(
                (
                    "manifest.json",
                    "identity.json",
                    "authority.json",
                    "owner-config.json",
                    "questions.json",
                )
            )
        },
        observation_context={
            "questions": {
                "Q1": {
                    "subject_binding": {
                        "question_contract": question,
                        "subject_ref": subject,
                    }
                },
                "Q2": {
                    "subject_binding": {
                        "question_contract": {
                            "id": "Q2",
                            "module_id": "base",
                            "prompt": "Other question",
                        },
                        "subject_ref": subject,
                    }
                },
            }
        },
        information_cutoff="2026-11-10",
        route_id="route-synthetic",
        route_provider="synthetic",
        route_model="unit-model",
    )


def _wire(monkeypatch, contract_values=None):
    contract = _contract()
    calls = []
    owner = object()
    lifecycle = SimpleNamespace(registered_research_work=lambda: SimpleNamespace(owner=owner))

    def contract_for_owner(_lifecycle):
        if contract_values is not None:
            return contract_values.pop(0)
        return contract

    monkeypatch.setattr(factory, "company_scan_contract", contract_for_owner)
    monkeypatch.setattr(factory, "validate_company_scan_task", lambda _lifecycle, _task: None)
    monkeypatch.setattr(context, "company_scan_pack_group_key", lambda *args: "base")
    monkeypatch.setattr(factory, "company_scan_pack_group_key", lambda *args: "base")

    def canonical(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    def digest(value):
        payload = value if isinstance(value, bytes) else canonical(value).encode()
        return hashlib.sha256(payload).hexdigest()

    limits = {
        "max_model_requests_per_session": 1,
        "max_search_queries_per_session": 2,
        "logical_max_output_tokens_including_reasoning_per_request": 8192,
        "session_input_tokens": 1000,
        "session_output_tokens_including_reasoning": 1000,
        "company_wall_seconds": 60,
    }
    api_impl = SimpleNamespace(
        canonical_json=canonical,
        sha256_hex=digest,
        create_owner_task=lambda *args, **kwargs: calls.append((args, kwargs))
        or SimpleNamespace(raw=args[0]),
        native_company_projection=lambda identity, subject: {
            "entity_id": identity["entity_id"],
            "subject_ref": subject,
        },
        scan_limits=lambda selected, search_exposure=None: dict(limits),
        build_scan_profile=lambda questions, **kwargs: {
            "resource_limits": dict(limits),
            "packs": [[q["question_id"] for q in questions]],
        },
        validate_scan_task=lambda task: task,
        LIMIT_CAPS={
            "max_model_requests_per_session": 2,
            "max_search_queries_per_session": 4,
        },
        QUESTION_IDS=("Q1", "Q2"),
        limits_within_registration=lambda _limits: False,
    )
    monkeypatch.setattr(api, "company_agent_public_api", lambda *operations: api_impl)
    return lifecycle, contract, calls, api_impl


def test_company_scan_task_factory_keeps_owner_references_inside_task(monkeypatch):
    lifecycle, contract, calls, _api_impl = _wire(monkeypatch)

    task = factory.create_company_scan_task(
        lifecycle,
        experiment_id="synthetic",
        run_id="run-1",
        question_ids=["Q1"],
    )

    assert len(calls) == 1
    raw = task.raw
    assert raw["protocol"] == "iqs.company-scan-task/1.0.0"
    assert raw["native_inputs"] == dict(contract.native_input_references)
    assert raw["owner_binding"]["subject_ref"]["entity_id"] == "ENT_SYNTHETIC"
    assert raw["module_locks"] == [dict(lock) for lock in contract.module_locks]
    assert "input_raw" not in raw and "bundle" not in raw


def test_company_task_factory_rejects_reference_question_before_agent_call(monkeypatch):
    lifecycle, _contract_value, calls, _api_impl = _wire(monkeypatch)

    with pytest.raises(ValueError, match="nonempty_unique_original_authorized_questions_required"):
        factory.create_company_scan_task(
            lifecycle,
            experiment_id="synthetic",
            run_id="run-1",
            question_ids=["Q2"],
        )

    assert calls == []


def test_native_task_factory_uses_the_same_owner_contract_and_never_returns_source_bytes(
    monkeypatch,
):
    lifecycle, contract, calls, _api_impl = _wire(monkeypatch)

    task = factory.create_native_task(
        lifecycle,
        experiment_id="synthetic",
        run_id="run-native",
        question_ids=["Q1"],
    )

    assert len(calls) == 1
    raw = task.raw
    assert raw["protocol"] == "iqs.company-agent-task/3.0.0"
    assert raw["strategy_id"] == "free_agent"
    assert raw["native_inputs"] == dict(contract.native_input_references)
    assert raw["limits"]["max_search_queries_per_session"] == 0
    assert "input_raw" not in raw


def test_company_task_factory_rejects_forged_identity_projection_before_agent_call(
    monkeypatch,
):
    lifecycle, contract, calls, _api_impl = _wire(monkeypatch)
    forged = CompanyScanContract(
        **{
            **{field: getattr(contract, field) for field in contract.__dataclass_fields__},
            "identity_payload": {
                **contract.identity_payload,
                "entity_id": "ENT_OTHER",
            },
        }
    )
    monkeypatch.setattr(factory, "company_scan_contract", lambda _lifecycle: forged)

    with pytest.raises(ValueError, match="original_identity_subject_required"):
        factory.create_company_scan_task(
            lifecycle,
            experiment_id="synthetic",
            run_id="run-1",
            question_ids=["Q1"],
        )

    assert calls == []


@pytest.mark.parametrize(
    "create_task",
    [factory.create_company_scan_task, factory.create_native_task],
)
def test_task_factory_reads_original_registration_without_live_dispatch_permission(
    monkeypatch, create_task
):
    lifecycle, _contract, calls, _api = _wire(monkeypatch)

    def dispatch_check_unavailable(_lifecycle):
        raise RuntimeError("current dispatch permission service unavailable")

    monkeypatch.setattr(
        factory, "assert_company_scan_admission", dispatch_check_unavailable, raising=False
    )

    task = create_task(
        lifecycle, experiment_id="synthetic", run_id="run-registered", question_ids=["Q1"]
    )

    assert len(calls) == 1
    assert task.raw["run_id"] == "run-registered"


def test_company_task_factory_rechecks_changed_owner_before_agent_call(monkeypatch):
    original = _contract()
    changed = _contract()
    changed = CompanyScanContract(
        **{
            **{field: getattr(changed, field) for field in changed.__dataclass_fields__},
            "route_model": "changed-model",
        }
    )
    lifecycle, _contract_value, calls, _api_impl = _wire(monkeypatch, [original, changed])

    with pytest.raises(ValueError, match="original_owner_changed_during_task_creation"):
        factory.create_company_scan_task(
            lifecycle,
            experiment_id="synthetic",
            run_id="run-1",
            question_ids=["Q1"],
        )

    assert calls == []


@pytest.mark.parametrize(
    "create_task",
    [factory.create_company_scan_task, factory.create_native_task],
)
def test_task_factory_rejects_identical_owner_replacement_before_agent_call(
    monkeypatch, create_task
):
    lifecycle, contract, calls, _api_impl = _wire(monkeypatch)
    owner_a, owner_b = object(), object()
    owner_index = 0

    def registered_work():
        nonlocal owner_index
        owner = owner_a if owner_index == 0 else owner_b
        owner_index += 1
        return SimpleNamespace(owner=owner)

    lifecycle.registered_research_work = registered_work
    monkeypatch.setattr(
        factory,
        "company_scan_contract",
        lambda _lifecycle: (registered_work(), contract)[1],
    )

    with pytest.raises(ValueError, match="original_owner_changed_during_task_creation"):
        create_task(
            lifecycle, experiment_id="synthetic", run_id="run-owner-swap", question_ids=["Q1"]
        )

    assert calls == []


@pytest.mark.parametrize(
    "create_task",
    [factory.create_company_scan_task, factory.create_native_task],
)
def test_task_factory_does_not_return_task_if_owner_changes_during_creation(
    monkeypatch, create_task
):
    lifecycle, contract, calls, api_impl = _wire(monkeypatch)
    original_owner, replacement_owner = object(), object()
    current_owner = original_owner
    lifecycle.registered_research_work = lambda: SimpleNamespace(owner=current_owner)
    monkeypatch.setattr(factory, "company_scan_contract", lambda _lifecycle: contract)

    def create_owner_task(*args, **kwargs):
        nonlocal current_owner
        calls.append((args, kwargs))
        current_owner = replacement_owner
        return SimpleNamespace(raw=args[0])

    api_impl.create_owner_task = create_owner_task

    with pytest.raises(ValueError, match="original_owner_changed_during_task_creation"):
        create_task(
            lifecycle,
            experiment_id="synthetic",
            run_id="run-owner-during-create",
            question_ids=["Q1"],
        )

    assert len(calls) == 1
