"""Owner-issued scan contract: detached, versioned, and free of raw inputs."""

import copy
from types import SimpleNamespace

import pytest

from src.interfaces.quick_scan_company import CompanyScanContract
from src.utils import quick_scan_company_context as context


def _contract(**changes):
    question = {"id": "Q1", "module_id": "base", "prompt": "Synthetic question"}
    subject = {
        "entity_id": "ENT_SYNTHETIC",
        "scope": "entity",
        "scope_id": "ENT_SYNTHETIC",
        "primary_issuer_id": "ISS_SYNTHETIC",
    }
    values = dict(
        protocol="stockqa.company_scan_contract/1.0.0",
        requested_question_ids=("Q1",),
        eligible_question_ids=("Q1",),
        question_contracts={"Q1": question},
        module_locks=({"module_id": "base", "version": "1.0.0"},),
        identity_payload={
            "entity_id": "ENT_SYNTHETIC",
            "canonical_name": "Synthetic issuer",
            "listings": [{"market": "US", "ticker": "SYN"}],
        },
        native_input_references={
            name: {
                "path": "native-input/" + name,
                "bytes": 7,
                "sha256": (str(index + 1) * 64),
            }
            for index, name in enumerate(
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
            "schema": "stockqa.quick_scan_observation_context/2.0.0",
            "questions": {
                "Q1": {
                    "subject_binding": {
                        "question_contract": question,
                        "subject_ref": subject,
                    }
                }
            },
        },
        information_cutoff="2026-11-10",
        route_id="route-synthetic",
        route_provider="synthetic",
        route_model="unit-model",
    )
    values.update(changes)
    return CompanyScanContract(**values)


@pytest.mark.parametrize(
    "changes,error",
    [
        ({"protocol": "stockqa.company_scan_contract/2.0.0"}, "protocol_unsupported"),
        ({"requested_question_ids": ("Q1", "Q1")}, "question_scope_invalid"),
        ({"eligible_question_ids": ("Q2",)}, "eligible_scope_invalid"),
        (
            {
                "native_input_references": {
                    **_contract().native_input_references,
                    "identity.json": {
                        "path": "C:/issuer/identity.json",
                        "bytes": 7,
                        "sha256": "b" * 64,
                    },
                }
            },
            "native_input_reference_invalid",
        ),
        ({"information_cutoff": ""}, "route_or_cutoff_invalid"),
        ({"route_model": ""}, "route_or_cutoff_invalid"),
    ],
)
def test_company_scan_contract_rejects_incompatible_or_out_of_scope_values(changes, error):
    with pytest.raises(ValueError, match=error):
        _contract(**changes)


def test_company_scan_contract_has_no_raw_bytes_and_detaches_nested_values():
    identity = {
        "entity_id": "ENT_SYNTHETIC",
        "canonical_name": "Synthetic issuer",
        "listings": [{"market": "US", "ticker": "SYN"}],
    }
    refs = _contract().native_input_references
    contract = _contract(identity_payload=identity, native_input_references=refs)
    identity["listings"][0]["ticker"] = "MUTATED"
    assert contract.identity_payload["listings"][0]["ticker"] == "SYN"
    assert not hasattr(contract, "input_raw")
    assert not hasattr(contract, "bundle")
    assert not hasattr(contract, "proof")
    assert all("bytes_data" not in ref for ref in contract.native_input_references.values())


def test_company_scan_contract_projects_only_registered_questions_and_owner_refs(monkeypatch):
    from src.utils.quick_scan_result_outbox import canonical_sha256

    q1 = {"id": "Q1", "module_id": "base", "prompt": "Question 1"}
    q2 = {"id": "Q2", "module_id": "growth", "prompt": "Question 2"}
    context_value = {
        "schema": "stockqa.quick_scan_observation_context/2.0.0",
        "questions": {
            "Q1": {"subject_binding": {"question_contract": q1}},
            "Q2": {"subject_binding": {"question_contract": q2}},
        },
    }
    owner = SimpleNamespace(
        snapshot=lambda: SimpleNamespace(
            observation_context=copy.deepcopy(context_value),
            bundle={
                "manifest": {
                    "questions": [q1, q2],
                    "module_locks": [
                        {"module_id": "base", "version": "1.0.0"},
                        {"module_id": "growth", "version": "2.0.0"},
                    ],
                }
            },
            requested_question_ids=("Q1", "Q2"),
            entries={"Q1": {"kind": "work"}, "Q2": {"kind": "reference"}},
            plan={"information_cutoff": "2026-11-10"},
        ),
        identity_payload=lambda: {
            "entity_id": "ENT_SYNTHETIC",
            "canonical_name": "Synthetic issuer",
            "listings": [{"market": "US", "ticker": "SYN"}],
            "internal_note": "must not cross the public contract",
        },
        native_input_references=lambda: _contract().native_input_references,
    )
    work = SimpleNamespace(
        owner=owner,
        observation_context=context_value,
        route_id="route-synthetic",
        model_requested="unit-model",
        route_model="unit-model",
        provider_actual=lambda: "synthetic",
    )
    monkeypatch.setattr(context, "_admitted_work", lambda _lifecycle: work)

    result = context.company_scan_contract(object())

    assert result.requested_question_ids == ("Q1", "Q2")
    assert result.eligible_question_ids == ("Q1",)
    assert set(result.question_contracts) == {"Q1", "Q2"}
    assert result.identity_payload == {
        "entity_id": "ENT_SYNTHETIC",
        "canonical_name": "Synthetic issuer",
        "listings": [{"market": "US", "ticker": "SYN"}],
    }
    assert "semantic_release_root" not in CompanyScanContract.__dataclass_fields__
    assert not hasattr(result, "semantic_release_root")
    assert result.route_id == "route-synthetic"
    assert canonical_sha256(result.observation_context) == canonical_sha256(context_value)
    assert not hasattr(result, "input_raw")


def test_company_scan_contract_does_not_request_or_publish_host_release_path(monkeypatch):
    question = {"id": "Q1", "module_id": "base", "prompt": "Question 1"}
    subject = {"entity_id": "ENT_SYNTHETIC", "scope": "entity", "scope_id": "ENT_SYNTHETIC"}
    context_value = {
        "schema": "stockqa.quick_scan_observation_context/2.0.0",
        "questions": {
            "Q1": {"subject_binding": {"question_contract": question, "subject_ref": subject}}
        },
    }
    owner = SimpleNamespace(
        snapshot=lambda: SimpleNamespace(
            observation_context=copy.deepcopy(context_value),
            bundle={
                "manifest": {
                    "questions": [question],
                    "module_locks": [{"module_id": "base", "version": "1.0.0"}],
                }
            },
            requested_question_ids=("Q1",),
            entries={"Q1": {"kind": "work"}},
            plan={"information_cutoff": "2026-11-10"},
        ),
        identity_payload=lambda: {
            "entity_id": "ENT_SYNTHETIC",
            "listings": [{"market": "US", "ticker": "SYN"}],
        },
        native_input_references=lambda: _contract().native_input_references,
    )
    work = SimpleNamespace(
        owner=owner,
        observation_context=context_value,
        route_id="route-synthetic",
        model_requested="unit-model",
        provider_actual=lambda: "synthetic",
    )
    monkeypatch.setattr(context, "_admitted_work", lambda _lifecycle: work)

    result = context.company_scan_contract(object())

    assert result.requested_question_ids == ("Q1",)
    assert result.identity_payload["entity_id"] == "ENT_SYNTHETIC"


def test_admitted_work_rejects_forged_lifecycle_and_mismatched_owner_store(monkeypatch):
    from src.utils import quick_scan_owner_refresh_api

    class TestLifecyclePort:
        pass

    owner = SimpleNamespace(
        snapshot=lambda: SimpleNamespace(kind="approved"),
        is_bound_to=lambda store: store is expected_store,
    )
    expected_store = object()
    work = SimpleNamespace(
        owner=owner,
        store=expected_store,
        transport_managed=True,
    )
    lifecycle = TestLifecyclePort()
    lifecycle.registered_research_work = lambda: work
    monkeypatch.setattr(context, "QuickScanWorkLifecyclePort", TestLifecyclePort)
    monkeypatch.setattr(quick_scan_owner_refresh_api, "is_owner_refresh_port", lambda _value: False)

    with pytest.raises(ValueError, match="original_admitted_native_owner_required"):
        context._admitted_work(lifecycle)

    monkeypatch.setattr(
        quick_scan_owner_refresh_api, "is_owner_refresh_port", lambda value: value is owner
    )
    owner.is_bound_to = lambda _store: False
    with pytest.raises(ValueError, match="original_admitted_native_owner_required"):
        context._admitted_work(lifecycle)


def test_admitted_work_accepts_only_a_registered_owner_port_bound_to_its_store(monkeypatch):
    from src.utils import quick_scan_owner_refresh_api

    class TestLifecyclePort:
        pass

    store = object()
    owner = SimpleNamespace(
        snapshot=lambda: SimpleNamespace(kind="approved"),
        is_bound_to=lambda candidate: candidate is store,
    )
    work = SimpleNamespace(owner=owner, store=store, transport_managed=True)
    lifecycle = TestLifecyclePort()
    lifecycle.registered_research_work = lambda: work
    monkeypatch.setattr(context, "QuickScanWorkLifecyclePort", TestLifecyclePort)
    monkeypatch.setattr(
        quick_scan_owner_refresh_api, "is_owner_refresh_port", lambda value: value is owner
    )

    assert context._admitted_work(lifecycle) is work
