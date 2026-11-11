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
    for key in ("iqs_root", "company_agent_root", "runtime_bridge_root"):
        base = tmp_path / key
        base.mkdir(parents=True)
        config[key] = str(base)
    return config, tmp_path / "config.json"


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "version",
        "relative",
        "dependency",
        "company_root",
        "runtime_root",
        "inline_key",
        "boards",
    ],
)
def test_explicit_shared_config_never_silently_falls_back(configured, fault):
    config, path = configured
    if fault == "version":
        config["schema_version"] = "unknown"
    elif fault == "relative":
        config["state_root"] = "relative"
    elif fault == "dependency":
        config["iqs_root"] += "/missing"
    elif fault == "company_root":
        config["company_agent_root"] += "/missing"
    elif fault == "runtime_root":
        config["runtime_bridge_root"] += "/missing"
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


@pytest.mark.parametrize(
    "api, expected",
    [
        (None, "company_agent_public_api_unavailable"),
        (
            SimpleNamespace(
                PUBLIC_API_PROTOCOL="iqs.company-agent-public-api/2.2.0",
                validate_evidence_record=lambda record, excerpt_cap: record,
            ),
            "company_agent_public_api_version_unsupported",
        ),
        (
            SimpleNamespace(
                PUBLIC_API_PROTOCOL="iqs.company-agent-public-api/2.3.0",
                __all__=["validate_evidence_record"],
            ),
            "company_agent_public_api_operation_missing",
        ),
    ],
)
def test_evidence_binding_rejects_missing_or_incompatible_public_facade(monkeypatch, api, expected):
    if api is None:
        monkeypatch.setitem(sys.modules, "iqs_company_agent_lab.api", None)
    else:
        monkeypatch.setitem(sys.modules, "iqs_company_agent_lab.api", api)
    subject = {"entity_id": "ENT_A", "scope": "entity"}
    config = {"evidence_boards": [{"subject_ref": subject, "records": [{"excerpt": "x"}]}]}
    with pytest.raises(ImportError, match=expected):
        _bound_board(
            config,
            subject=subject,
            company_ref={"path": "native/company.json", "sha256": "a" * 64, "bytes": 1},
            excerpt_cap=2000,
        )


@pytest.mark.parametrize(
    "protocol, supported",
    [
        ("stockqa.llm-research-client-module/1.0.1", True),
        ("stockqa.llm-research-client-module/1.4.0", True),
        ("stockqa.llm-research-client-module/2.0.0", False),
        ("stockqa.llm-research-client-module/0.9.9", False),
    ],
)
def test_research_capabilities_accept_same_major_compatible_protocols(
    monkeypatch, protocol, supported
):
    from src.providers import llm_research_client
    from src.utils.quick_scan_company_context import company_scan_capabilities

    client = SimpleNamespace(
        provider_name="mimo",
        base_url="https://api.xiaomimimo.com/v1",
        model="mimo-v2.6-pro",
        supports_web_search=True,
    )
    monkeypatch.setattr(llm_research_client, "MODULE_API_PROTOCOL", protocol)
    monkeypatch.setattr(
        llm_research_client,
        "MODULE_API_EXPORTS",
        frozenset({"research_channel_capabilities"}),
    )
    if supported:
        assert company_scan_capabilities(client)["native_web_search"]
    else:
        with pytest.raises(ValueError, match="research_capabilities_provider_protocol_unsupported"):
            company_scan_capabilities(client)


def test_company_agent_facade_rejects_callable_operation_not_declared_public(monkeypatch):
    from src.utils.quick_scan_company_api import company_agent_public_api

    api = SimpleNamespace(
        PUBLIC_API_PROTOCOL="iqs.company-agent-public-api/2.3.0",
        __all__=["task_snapshot"],
        task_snapshot=lambda task: task,
        load_checkpoint_task=lambda path: path,
    )
    monkeypatch.setitem(sys.modules, "iqs_company_agent_lab.api", api)
    with pytest.raises(ImportError, match="company_agent_public_api_operation_not_exported"):
        company_agent_public_api("load_checkpoint_task")


def test_original_checkpoint_loader_uses_versioned_company_agent_facade(monkeypatch):
    from src.utils.quick_scan_shared_company import load_original_company_task

    task = object()
    calls = []
    api = SimpleNamespace(
        PUBLIC_API_PROTOCOL="iqs.company-agent-public-api/2.3.0",
        __all__=["load_checkpoint_task"],
        load_checkpoint_task=lambda checkpoint: calls.append(checkpoint) or task,
    )
    monkeypatch.setitem(sys.modules, "iqs_company_agent_lab.api", api)
    assert load_original_company_task("checkpoint.json") is task
    assert calls == ["checkpoint.json"]


def test_reference_records_bind_only_exact_original_subject_without_mutating_input(monkeypatch):
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "iqs_company_agent_lab.api",
        SimpleNamespace(
            PUBLIC_API_PROTOCOL="iqs.company-agent-public-api/2.3.0",
            __all__=["validate_evidence_record"],
            validate_evidence_record=lambda r, cap: calls.append(copy.deepcopy(r)),
        ),
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


def _companion_facade(protocol):
    return SimpleNamespace(PUBLIC_API_PROTOCOL=protocol)


def test_modules_consumes_companions_through_public_facades_and_checks_engine_root(
    tmp_path, monkeypatch
):
    from pathlib import Path

    from src.utils import quick_scan_shared_company as module

    config = {}
    for key in ("iqs_root", "company_agent_root", "runtime_bridge_root"):
        root = tmp_path / key
        root.mkdir()
        config[key] = str(root)
    loaded = {
        "shared_company_engine": SimpleNamespace(
            __file__=str(Path(config["iqs_root"]) / "scripts" / "shared_company_engine.py")
        ),
        "iqs_company_agent_lab.api": _companion_facade("iqs.company-agent-public-api/2.3.0"),
        "iqs_agent_runtime_lab.api": _companion_facade("iqs.agent-runtime-public-api/1.4.0"),
    }
    monkeypatch.setattr(module.sys, "path", [])
    monkeypatch.setattr(module.importlib, "import_module", lambda name: loaded[name])

    # The composition root needs no knowledge of companion file layout; the
    # engine stays anchored to the configured IQS root.
    engine = module._modules(config)
    assert engine == loaded["shared_company_engine"]

    loaded["shared_company_engine"].__file__ = str(tmp_path / "shadow" / "engine.py")
    with pytest.raises(
        ValueError, match="shared_company_dependency_shadowed:shared_company_engine"
    ):
        module._modules(config)


def test_modules_reports_a_missing_companion_facade(tmp_path, monkeypatch):
    from pathlib import Path

    from src.utils import quick_scan_shared_company as module

    config = {}
    for key in ("iqs_root", "company_agent_root", "runtime_bridge_root"):
        root = tmp_path / key
        root.mkdir()
        config[key] = str(root)
    engine = SimpleNamespace(__file__=str(Path(config["iqs_root"]) / "scripts" / "engine.py"))
    runtime_api = _companion_facade("iqs.agent-runtime-public-api/1.4.0")

    def import_module(name):
        if name == "iqs_company_agent_lab.api":
            raise ModuleNotFoundError("companion facade not installed")
        return {"shared_company_engine": engine, "iqs_agent_runtime_lab.api": runtime_api}[name]

    monkeypatch.setattr(module.sys, "path", [])
    monkeypatch.setattr(module.importlib, "import_module", import_module)
    with pytest.raises(
        ValueError, match="shared_company_dependency_unavailable:iqs_company_agent_lab"
    ):
        module._modules(config)


def test_modules_rejects_incompatible_companion_public_protocols(tmp_path, monkeypatch):
    from pathlib import Path

    from src.utils import quick_scan_shared_company as module

    config = {}
    for key in ("iqs_root", "company_agent_root", "runtime_bridge_root"):
        root = tmp_path / key
        root.mkdir()
        config[key] = str(root)
    engine = SimpleNamespace(__file__=str(Path(config["iqs_root"]) / "scripts" / "engine.py"))
    loaded = {
        "shared_company_engine": engine,
        "iqs_company_agent_lab.api": _companion_facade("iqs.company-agent-public-api/2.2.0"),
        "iqs_agent_runtime_lab.api": _companion_facade("iqs.agent-runtime-public-api/1.4.0"),
    }
    monkeypatch.setattr(module.sys, "path", [])
    monkeypatch.setattr(module.importlib, "import_module", lambda name: loaded[name])

    # A companion may move its files freely; only its declared public
    # protocol decides whether this composition may consume it.
    with pytest.raises(
        ValueError, match="shared_company_dependency_protocol_unsupported:iqs_company_agent_lab"
    ):
        module._modules(config)


def test_modules_uses_explicit_import_roots_for_companions(tmp_path, monkeypatch):
    from pathlib import Path

    from src.utils import quick_scan_shared_company as module

    config = {}
    for key in ("iqs_root", "company_agent_root", "runtime_bridge_root"):
        root = tmp_path / key
        root.mkdir()
        config[key] = str(root)
    # The import root is explicit; the companion source layout is private.
    config["company_agent_import_root"] = str(tmp_path / "companion-installed")
    config["runtime_import_root"] = str(tmp_path / "runtime-installed")
    loaded = {
        "shared_company_engine": SimpleNamespace(
            __file__=str(Path(config["iqs_root"]) / "scripts" / "engine.py")
        ),
        "iqs_company_agent_lab.api": _companion_facade("iqs.company-agent-public-api/2.3.0"),
        "iqs_agent_runtime_lab.api": _companion_facade("iqs.agent-runtime-public-api/1.4.0"),
    }
    monkeypatch.setattr(module.sys, "path", [])
    monkeypatch.setattr(module.importlib, "import_module", lambda name: loaded[name])

    module._modules(config)
    assert str(tmp_path / "companion-installed") in module.sys.path
    assert str(tmp_path / "runtime-installed") in module.sys.path


@pytest.mark.parametrize(
    "protocol,exports,error",
    [
        ("stockqa.company-scan-public-api/1.7.0", None, None),
        ("stockqa.company-scan-public-api/1.8.2", None, None),
        (
            "stockqa.company-scan-public-api/1.6.9",
            None,
            "shared_company_owner_api_version_unsupported",
        ),
        (
            "stockqa.company-scan-public-api/1.5.9",
            None,
            "shared_company_owner_api_version_unsupported",
        ),
        (
            "stockqa.company-scan-public-api/2.0.0",
            None,
            "shared_company_owner_api_version_unsupported",
        ),
        (
            "stockqa.company-scan-public-api/1.7.0",
            ["create_company_scan_task"],
            "shared_company_owner_api_operations_incomplete",
        ),
    ],
)
def test_owner_task_factory_is_resolved_through_versioned_facade(
    monkeypatch, protocol, exports, error
):
    from src.utils import quick_scan_shared_company as module

    required = ["company_scan_contract", "company_scan_capabilities", "create_company_scan_task"]
    api = SimpleNamespace(
        PUBLIC_API_PROTOCOL=protocol,
        __all__=required if exports is None else exports,
        company_scan_contract=lambda *_: None,
        company_scan_capabilities=lambda *_args, **_kwargs: {},
        create_company_scan_task=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(module.importlib, "import_module", lambda name: api)
    if error is None:
        assert module._company_scan_task_api() is api
    else:
        with pytest.raises(ValueError, match=error):
            module._company_scan_task_api()


def test_shared_engine_version_contract_accepts_only_same_major_public_protocol():
    from src.utils.quick_scan_shared_company import require_shared_engine_capabilities

    expected_operations = {
        "execute_shared_engine",
        "resume_shared_engine",
        "finalize_shared_engine",
    }
    for version in ("iqs.shared-company-engine/1.0.0", "iqs.shared-company-engine/1.2.7"):
        caps = {"protocol": version}
        assert require_shared_engine_capabilities(caps, expected_operations) == caps
    for version in (
        "iqs.shared-company-engine/0.9.9",
        "iqs.shared-company-engine/2.0.0",
        "iqs.shared-company-engine/1.x.0",
        None,
    ):
        with pytest.raises(ValueError, match="shared_company_engine_protocol_unsupported"):
            require_shared_engine_capabilities({"protocol": version}, expected_operations)
    with pytest.raises(ValueError, match="shared_company_engine_operations_incomplete"):
        require_shared_engine_capabilities(
            {"protocol": "iqs.shared-company-engine/1.0.0"},
            expected_operations,
            available_operations={"execute_shared_engine"},
        )


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
    from src.interfaces.quick_scan_owner_refresh import OwnerRefreshSnapshot
    from src.utils import quick_scan_company_api as owner_api
    from src.utils import quick_scan_shared_company as module

    subject = {"entity_id": "ENT_A", "scope": "entity"}
    entries = {"Q1": {"kind": "work"}, "Q2": {"kind": "work"}}
    proof = {"confirmation": {"confirmation_id": "original-confirmation"}}
    owner = SimpleNamespace(
        entries=entries,
        proof=proof,
        input_raw={"identity.json": b'{"payload":{"entity_id":"ENT_A"}}'},
        snapshot=lambda: OwnerRefreshSnapshot(
            kind="approved",
            plan={},
            proof=proof,
            record=None,
            bundle=None,
            entries=entries,
            fields={},
            work={},
            deferred={},
            scope_bindings={},
            observation_context=None,
            ordered_route_targets=None,
            requested_question_ids=("Q1", "Q2"),
            needs_new_dispatch=False,
            recovery_only=False,
            recovery_work_item_ids=(),
            remaining_work_item_ids=(),
        ),
        identity_payload=lambda: {"payload": {"entity_id": "ENT_A"}},
    )
    work = SimpleNamespace(
        owner=owner,
        observation_context={
            "questions": {q: {"subject_binding": {"subject_ref": subject}} for q in ("Q1", "Q2")}
        },
    )
    factory_port = object()
    scan_contract = SimpleNamespace(
        requested_question_ids=("Q1", "Q2"),
        eligible_question_ids=("Q1", "Q2"),
        identity_payload={
            "entity_id": "ENT_A",
            "canonical_name": "Synthetic issuer",
            "listings": [{"market": "US", "ticker": "SYN"}],
        },
        observation_context={
            "questions": {q: {"subject_binding": {"subject_ref": subject}} for q in ("Q1", "Q2")}
        },
    )
    monkeypatch.setattr(owner_api, "PUBLIC_API_PROTOCOL", "stockqa.company-scan-public-api/1.7.0")
    monkeypatch.setattr(owner_api, "company_scan_contract", lambda _lifecycle: scan_contract)
    monkeypatch.setattr(
        owner_api,
        "create_company_scan_task",
        lambda _lifecycle, **kwargs: original_task,
    )
    monkeypatch.setattr(
        owner_api,
        "company_scan_capabilities",
        lambda _client, native_search=True: {
            "native_web_search": True,
            "question_packs": True,
            "max_output_tokens": 32768,
        },
    )
    lifecycle = SimpleNamespace(
        registered_research_work=lambda: work,
        company_scan_uses_native_search=lambda *, recovery_only: not recovery_only,
        company_scan_task_port=lambda: factory_port,
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
    company_api = SimpleNamespace(
        PUBLIC_API_PROTOCOL="iqs.company-agent-public-api/2.3.0",
        __all__=[
            "task_snapshot",
            "native_company_projection",
            "canonical_json",
            "sha256_hex",
            "scan_limits",
            "validate_evidence_record",
            "load_checkpoint_task",
        ],
        task_snapshot=lambda task: {
            "question_ids": list(task.question_ids),
            "limits": dict(task.limits),
            "raw": dict(task.raw),
        },
        native_company_projection=lambda identity, ref: identity,
        canonical_json=canonical,
        sha256_hex=digest,
        scan_limits=lambda value: limits,
        validate_evidence_record=lambda record, excerpt_cap: record,
        load_checkpoint_task=lambda path: original_task,
    )
    monkeypatch.setitem(sys.modules, "iqs_company_agent_lab.api", company_api)

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
        capabilities=lambda: {"protocol": "iqs.shared-company-engine/1.0.0"},
        execute_shared_engine=execute,
        resume_shared_engine=resume,
        finalize_shared_engine=finalize,
    )
    monkeypatch.setattr(module, "_modules", lambda config: engine)
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
    from src.interfaces.quick_scan_owner_refresh import OwnerRefreshSnapshot
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
        snapshot=lambda: OwnerRefreshSnapshot(
            kind="approved",
            plan={},
            proof={},
            record={},
            bundle={},
            entries={"Q1": {"kind": "work"}},
            fields={},
            work={},
            deferred={},
            scope_bindings={},
            observation_context=None,
            ordered_route_targets=None,
            requested_question_ids=("Q1",),
            needs_new_dispatch=False,
            recovery_only=True,
            recovery_work_item_ids=(),
            remaining_work_item_ids=(),
        ),
        existing_work=lambda q: {"work_item_id": "W1"},
        is_bound_to=lambda candidate: candidate is store,
        execution_routes=lambda p: routes,
    )
    assert registered_recovery_routes(session, {}, store=store) == [routes[1]]
    assert routes[1]["eligible"] is False
    store.get_attempt_response = lambda a: None
    with pytest.raises(ValueError, match="original_route_required"):
        registered_recovery_routes(session, {}, store=store)


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
    lifecycle = QuickScanWorkLifecycle(
        store,
        entity_id="ENT_A",
        run_id="original-run",
        scan_id="original-scan",
        identity={},
        owner_refresh_session=owner,
        model_requested="original",
        transport_managed=True,
        observation_context=context,
        route_id="r",
    )

    def forbidden(*a, **k):
        raise AssertionError("pure projection must not perform I/O or current-permission checks")

    owner.check = forbidden
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    projected = company_scan_inputs(lifecycle)
    assert not hasattr(projected, "check")
    port = lifecycle.company_scan_task_port()
    assert company_scan_inputs(port) == projected
    for name in ("_store", "_owner_refresh", "_observation_context", "registered_research_work"):
        with pytest.raises(AttributeError):
            getattr(port, name)
    projected.entries["Q1"]["kind"] = "changed"
    projected.plan["model"] = "changed"
    projected.observation_context["questions"]["Q1"]["provider"] = "changed"
    assert owner.entries["Q1"]["kind"] == "work" and owner.plan["model"] == "original"
    assert lifecycle.registered_research_work().observation_context["questions"]["Q1"] == {}
    with pytest.raises(ValueError, match="original_native_lifecycle_required"):
        company_scan_inputs(SimpleNamespace(**vars(lifecycle)))
    lifecycle._owner_refresh = SimpleNamespace(**vars(owner))
    with pytest.raises(ValueError, match="original_admitted_native_owner_required"):
        company_scan_inputs(lifecycle)
