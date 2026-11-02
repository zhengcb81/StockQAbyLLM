"""Shared-entry configuration and exact evidence subject binding; no model I/O."""

import copy
import json
import sys
from types import SimpleNamespace

import pytest

from src.utils.quick_scan_shared_company import _bound_board, read_shared_company_config


@pytest.fixture
def configured(tmp_path):
    config = {"schema_version": "1.0.0", "state_root": str(tmp_path / "state")}
    for key, member in (
        ("iqs_root", "scripts/shared_company_engine.py"),
        ("company_agent_root", "src/iqs_company_agent_lab/task.py"),
        ("runtime_bridge_root", "src/iqs_agent_runtime_lab/port.py"),
    ):
        base = tmp_path / key
        target = base / member
        target.parent.mkdir(parents=True)
        target.write_text("# synthetic dependency location", encoding="utf8")
        config[key] = str(base)
    return config, tmp_path / "config.json"


@pytest.mark.parametrize(
    "fault", [None, "version", "relative", "dependency", "inline_key", "boards"]
)
def test_explicit_shared_config_never_silently_falls_back(configured, fault):
    config, path = configured
    if fault == "version":
        config["schema_version"] = "unknown"
    elif fault == "relative":
        config["state_root"] = "relative"
    elif fault == "dependency":
        config["iqs_root"] += "/missing"
    elif fault == "inline_key":
        config["api_key"] = "synthetic-not-a-secret"
    elif fault == "boards":
        config["evidence_boards"] = [{"records": []}]
    path.write_text(json.dumps(config), encoding="utf8")
    if fault is None:
        assert read_shared_company_config(path) == config
    else:
        with pytest.raises(ValueError):
            read_shared_company_config(path)


def test_reference_records_bind_only_exact_original_subject_without_mutating_input(monkeypatch):
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "iqs_company_agent_lab.evidence",
        SimpleNamespace(validate_evidence_record=lambda r, cap: calls.append(copy.deepcopy(r))),
    )
    subject = {"entity_id": "ENT_A", "scope": "entity", "perimeter": "original"}
    ref = {"path": "native-derived/company.json", "sha256": "a" * 64, "bytes": 10}
    record = {"excerpt": "Synthetic original input", "scope": "original"}
    config = {"evidence_boards": [{"subject_ref": subject, "records": [record]}]}
    original = copy.deepcopy(config)
    board = _bound_board(config, subject=subject, company_ref=ref, excerpt_cap=2000)
    assert board["records"][0]["issuer_ref"] == ref and config == original and len(calls) == 1
    wrong = copy.deepcopy(config)
    wrong["evidence_boards"][0]["subject_ref"]["perimeter"] = "different"
    assert (
        _bound_board(wrong, subject=subject, company_ref=ref, excerpt_cap=2000) == {"records": []}
        and len(calls) == 1
    )
    config["evidence_boards"][0]["records"][0]["issuer_ref"] = {**ref, "sha256": "b" * 64}
    with pytest.raises(ValueError, match="issuer_mismatch"):
        _bound_board(config, subject=subject, company_ref=ref, excerpt_cap=2000)


def test_original_cli_forwards_optional_shared_config_without_starting_clients(
    tmp_path, monkeypatch
):
    import main_with_llm
    import src.runners.llm_runner as runner

    calls = []

    class Runner:
        def __init__(self, **kwargs):
            pass

        def run(self, **kwargs):
            calls.append(kwargs)
            return 0

    monkeypatch.setattr(runner, "LLMRunner", Runner)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "main_with_llm.py",
            "--company",
            "Synthetic issuer",
            "--shared-company-config",
            str(tmp_path / "shared.json"),
        ],
    )
    assert main_with_llm.main() == 0
    assert calls[0]["shared_company_config"] == str(tmp_path / "shared.json")


@pytest.mark.parametrize("resume_complete", [True, False])
@pytest.mark.parametrize("completed_on_refusal", [False, True])
def test_partial_export_is_not_published_and_resumes_original_checkpoint(
    tmp_path, monkeypatch, resume_complete, completed_on_refusal
):
    # This is the entry coordinator boundary. Native checkpoint authenticity,
    # SQL and SDK are covered by the separate original-owner software E2E.
    import hashlib

    import src.utils.quick_scan_owner_research as services_module
    from src.core.models import Question
    from src.utils import quick_scan_shared_company as module

    subject = {"entity_id": "ENT_A", "scope": "entity"}
    owner = SimpleNamespace(
        entries={"Q1": {"kind": "work"}, "Q2": {"kind": "work"}},
        proof={"confirmation": {"confirmation_id": "original-confirmation"}},
        input_raw={"identity.json": b'{"payload":{"entity_id":"ENT_A"}}'},
    )
    lifecycle = SimpleNamespace(
        _owner_refresh=owner,
        _external_retrieval=None,
        _observation_context={
            "questions": {q: {"subject_binding": {"subject_ref": subject}} for q in ("Q1", "Q2")}
        },
    )
    client = SimpleNamespace(
        provider_name="mimo",
        model="mimo-v2.6-pro",
        base_url="https://api.xiaomimimo.com/v1",
        supports_web_search=True,
    )
    limits = dict(
        excerpt_chars_per_source=2000,
        logical_max_output_tokens_including_reasoning_per_request=8192,
        session_input_tokens=240000,
        session_output_tokens_including_reasoning=48000,
        company_wall_seconds=900,
    )
    canonical = lambda v: json.dumps(v, sort_keys=True, separators=(",", ":"))
    digest = lambda v: hashlib.sha256(
        v if isinstance(v, bytes) else canonical(v).encode()
    ).hexdigest()
    monkeypatch.setitem(
        sys.modules,
        "iqs_company_agent_lab.canonical",
        SimpleNamespace(canonical_json=canonical, sha256_hex=digest),
    )
    monkeypatch.setitem(
        sys.modules,
        "iqs_company_agent_lab.scan_profile",
        SimpleNamespace(
            native_company_projection=lambda identity, ref: identity,
            scan_limits=lambda value: limits,
        ),
    )
    original_task = SimpleNamespace(
        limits=limits,
        question_ids=["Q1", "Q2"],
        raw={"execution_profile": {"packs": [["Q1"], ["Q2"]]}},
    )
    factory = SimpleNamespace(create_company_scan_task=lambda *a, **k: original_task)

    def read_original(path):
        assert path.read_text() == "original-checkpoint"
        return original_task

    monkeypatch.setattr(module, "load_original_company_task", read_original)
    calls = []

    def execute(task, services, config, output, *, session_dir, **kwargs):
        calls.append("execute")
        session_dir.mkdir()
        (session_dir / "checkpoint.json").write_text("original-checkpoint")
        output.mkdir()
        (output / "partial.txt").write_text("interrupted original export")
        raise OSError("scripted_partial_export")

    def resume(task, services, config, checkpoint, output, *, session_dir, **kwargs):
        calls.append("resume")
        assert checkpoint.read_text() == "original-checkpoint"
        assert config["max_output_tokens"] == 8192  # frozen, never the new 32768 setting
        session_dir.mkdir()
        output.mkdir()
        (output / "complete.txt").write_text("completed original export")
        (output / "checkpoint.json").write_text("original-checkpoint")
        return {
            "all_slots_completed": resume_complete,
            "run": {
                "slots": [
                    (
                        {
                            "question_id": "Q1",
                            "execution_status": "completed",
                            "answer": {"original": True},
                        }
                        if completed_on_refusal
                        else {
                            "question_id": "Q1",
                            "execution_status": "not_attempted",
                            "answer": None,
                        }
                    ),
                    {"question_id": "Q2", "execution_status": "failed", "answer": None},
                ]
            },
        }

    def finalize(task, services, output, **selection):
        if selection:
            assert selection == {"question_ids": ["Q1"]}
            calls.append("finalize_partial")
        else:
            calls.append("finalize")
        assert (output / "complete.txt").is_file()
        return {"status": "completed"}

    engine = SimpleNamespace(
        execute_shared_engine=execute, resume_shared_engine=resume, finalize_shared_engine=finalize
    )
    monkeypatch.setattr(module, "_modules", lambda config: (engine, factory))
    monkeypatch.setattr(services_module, "OwnerResearchServices", lambda *a, **k: SimpleNamespace())
    config = {"state_root": str(tmp_path)}
    args = dict(
        config=config,
        questions=[Question("original question", question_id=q) for q in ("Q1", "Q2")],
    )
    with pytest.raises(OSError, match="scripted_partial_export"):
        module.run_shared_company_scan(lifecycle, client, **args)
    scope = next(tmp_path.glob("scan-*"))
    args["max_output_tokens"] = 32768
    assert not (scope / "export").exists() and (scope / "export-0001/partial.txt").is_file()
    if not resume_complete:
        with pytest.raises(ValueError, match="scan_incomplete"):
            module.run_shared_company_scan(lifecycle, client, **args)
        assert not (scope / "export").exists()
        assert (scope / "export-0002/complete.txt").is_file()
        assert calls == ["execute", "resume"] + (
            ["finalize_partial"] if completed_on_refusal else []
        )
        with pytest.raises(ValueError, match="scan_incomplete"):
            module.run_shared_company_scan(lifecycle, client, recovery_only=True, **args)
        assert calls == ["execute", "resume"] + (
            ["finalize_partial"] if completed_on_refusal else []
        ) + ["resume"] + (["finalize_partial"] if completed_on_refusal else [])
        assert (scope / "export-0002/complete.txt").is_file()
        return
    assert module.run_shared_company_scan(lifecycle, client, **args)["status"] == "completed"
    assert (scope / "export-0001/partial.txt").is_file() and (
        scope / "export/complete.txt"
    ).is_file()
    module.run_shared_company_scan(lifecycle, client, **args)
    assert calls == ["execute", "resume", "finalize", "finalize"]


def test_shared_runner_requires_original_approval_before_reading_config_or_creating_clients(
    tmp_path, monkeypatch
):
    from src.runners.llm_runner import LLMRunner

    monkeypatch.chdir(tmp_path)
    runner = LLMRunner()
    with pytest.raises(ValueError, match="original_approved_single_company"):
        runner.run(company="Synthetic issuer", shared_company_config=str(tmp_path / "missing.json"))
    assert not list(tmp_path.glob("*.sqlite"))


def test_shared_received_recovery_uses_original_single_entry_without_new_spend(
    monkeypatch, tmp_path
):
    import src.runners.llm_runner as module

    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(module, "load_identity_snapshot", lambda *a, **k: {"original": True})
    runner = module.LLMRunner()
    monkeypatch.setattr(runner, "_run_single_company", lambda **kwargs: calls.append(kwargs) or 0)
    assert (
        runner.run(
            company="Synthetic issuer",
            entity_id="E1",
            require_search=True,
            config="questions.json",
            identity_snapshot="identity.json",
            question_manifest="manifest.json",
            c06_authority="authority.json",
            owner_refresh_config="owner.json",
            refresh_admission_config="admission.json",
            refresh_confirmation_id="original",
            seal_deliveries=True,
            recover_checkpoints=True,
            shared_company_config="shared.json",
            question_pack_size=1,
            thinking_enabled=True,
            max_output_tokens=16384,
        )
        == 0
    )
    assert len(calls) == 1 and calls[0]["shared_company_recovery_only"] is True
    assert calls[0]["identity_payload"] == {"original": True}
    assert calls[0].get("spend_authorization") is None
    assert calls[0]["max_output_tokens"] == 16384
    assert not list(tmp_path.glob("*.sqlite"))


def test_received_recovery_uses_original_nonfirst_keyless_route():
    from src.utils.quick_scan_shared_company import registered_recovery_routes

    routes = [
        dict(id="first", provider_config_ref="a", model="m1", eligible=True),
        dict(id="original", provider_config_ref="b", model="m2", eligible=False),
    ]
    store = SimpleNamespace(
        list_attempts=lambda w: [
            dict(
                attempt_id="A1",
                phase="response_available",
                route_id="original",
                provider="b",
                model_requested="m2",
            )
        ],
        get_attempt_response=lambda a: {"receipt": "original"},
    )
    session = SimpleNamespace(
        requested_question_ids=["Q1"],
        entries={"Q1": {"kind": "work"}},
        existing_work=lambda q: {"work_item_id": "W1"},
        service=SimpleNamespace(store=store),
        execution_routes=lambda p: routes,
    )
    assert registered_recovery_routes(session, {}) == [routes[1]]
    assert routes[1]["eligible"] is False
    store.get_attempt_response = lambda a: None
    with pytest.raises(ValueError, match="original_route_required"):
        registered_recovery_routes(session, {})


def test_comparison_scope_read_does_not_require_new_dispatch_key():
    from src.utils.quick_scan_refresh_admission import AdmissionRejected
    from src.utils.quick_scan_refresh_execution import ApprovedRefreshSession
    from src.utils.quick_scan_result_outbox import canonical_sha256

    policy = {
        "policy_id": "original",
        "routes": [dict(id="r", provider_config_ref="p", model="m", eligible=False)],
    }
    expected = dict(
        provider="p",
        model="m",
        qa=dict(route_id="r", provider_config_ref="p", policy_id="original"),
    )
    target = dict(
        provider="p",
        model="m",
        route_id="r",
        provider_config_ref="p",
        policy_id="original",
        owner_policy_sha256=canonical_sha256(expected),
    )
    session = ApprovedRefreshSession.__new__(ApprovedRefreshSession)
    session.proof = {"request": {"protocol": "iqs.refresh_request/1.3.0"}, "owner_policy": expected}
    session.work = {"Q1": {"status": "leased"}}
    session.deferred = set()
    session.entries = {
        "Q1": {
            "kind": "work",
            "specification": {
                "owner_refresh_binding": {"owner_control": {"comparison_target": target}}
            },
        }
    }
    assert session.execution_routes(policy) == policy["routes"]
    assert policy["routes"][0]["eligible"] is False
    session.work["Q1"]["status"] = "pending"
    with pytest.raises(AdmissionRejected, match="route_unavailable"):
        session.execution_routes(policy)
    session.work["Q1"]["status"] = "leased"
    target["route_id"] = "foreign"
    with pytest.raises(AdmissionRejected, match="not_approved"):
        session.execution_routes(policy)


def test_replay_only_internal_entry_never_falls_back_to_regular_questions(tmp_path, monkeypatch):
    from src.runners.llm_runner import LLMRunner

    monkeypatch.chdir(tmp_path)
    runner = LLMRunner()
    with pytest.raises(ValueError, match="no_legacy_fallback"):
        runner._run_single_company(
            company="Synthetic",
            provider="mimo",
            config="missing.json",
            output="out.json",
            config_format="json",
            shared_company_recovery_only=True,
        )
    assert not list(tmp_path.iterdir())


def test_scan_capabilities_follow_original_sdk_not_provider_brand():
    from src.providers.llm_client import LLMClient
    from src.utils.quick_scan_company_context import company_scan_capabilities

    client = LLMClient(
        api_key="synthetic",
        model="mimo-v2.6-pro",
        base_url="https://api.xiaomimimo.com/v1",
        provider_name="mimo",
    )
    caps = company_scan_capabilities(client)
    assert caps["question_packs"] and caps["native_web_search"]
    assert caps["max_output_tokens"] >= 32768
    # Generic SDK search may recognize a host while the research adapter does
    # not implement its configured protocol/provider. Neither may overclaim.
    client.provider_name = "unsupported"
    assert not company_scan_capabilities(client)["native_web_search"]
    assert not company_scan_capabilities(client)["question_packs"]
    client.base_url = "https://example.invalid/v1"
    assert not company_scan_capabilities(client)["native_web_search"]
    assert not company_scan_capabilities(client)["question_packs"]


def test_company_scan_input_projection_is_detached_and_has_no_io(monkeypatch):
    # These uninitialized real classes test data projection ONLY, never consent
    # or paid execution. The original executor still authenticates before send.
    from pathlib import Path

    from src.runners.llm_runner import QuickScanWorkLifecycle
    from src.utils.quick_scan_company_context import company_scan_inputs
    from src.utils.quick_scan_owner_refresh import OwnerRefreshSession
    from src.utils.quick_scan_refresh_execution import ApprovedRefreshSession

    context = {"schema": "stockqa.quick_scan_observation_context/2.0.0", "questions": {"Q1": {}}}
    store = object()
    native = OwnerRefreshSession.__new__(OwnerRefreshSession)
    native.store, native.observation_context = store, copy.deepcopy(context)
    owner = ApprovedRefreshSession.__new__(ApprovedRefreshSession)
    owner.native, owner.service = native, SimpleNamespace(store=store)
    owner.entries = {"Q1": {"kind": "work"}}
    owner.bundle = {"manifest": {"questions": [{"id": "Q1"}]}}
    owner.input_raw, owner.plan, owner.proof = (
        {"identity.json": b"original"},
        {"model": "original", "provider": "mimo"},
        {"request": {"protocol": "iqs.refresh_request/1.1.0"}},
    )
    lifecycle = QuickScanWorkLifecycle.__new__(QuickScanWorkLifecycle)
    lifecycle._owner_refresh, lifecycle._store = owner, store
    lifecycle._model_requested = "original"
    lifecycle._transport_managed, lifecycle._observation_context, lifecycle._route_id = (
        True,
        context,
        "r",
    )

    def forbidden(*a, **k):
        raise AssertionError("pure projection must not perform I/O or current-permission checks")

    owner.check = forbidden
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    projected = company_scan_inputs(lifecycle)
    assert not hasattr(projected, "check")
    projected.entries["Q1"]["kind"] = "changed"
    projected.plan["model"] = "changed"
    assert owner.entries["Q1"]["kind"] == "work" and owner.plan["model"] == "original"
    with pytest.raises(ValueError, match="original_native_lifecycle_required"):
        company_scan_inputs(SimpleNamespace(**vars(lifecycle)))
    lifecycle._owner_refresh = SimpleNamespace(**vars(owner))
    with pytest.raises(ValueError, match="original_admitted_native_owner_required"):
        company_scan_inputs(lifecycle)
