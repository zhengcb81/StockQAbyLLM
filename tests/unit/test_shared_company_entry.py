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
def test_partial_export_is_not_published_and_resumes_original_checkpoint(
    tmp_path, monkeypatch, resume_complete
):
    # This is the entry coordinator boundary. Native checkpoint authenticity,
    # SQL and SDK are covered by the separate original-owner software E2E.
    import hashlib

    import src.utils.quick_scan_owner_research as services_module
    from src.core.models import Question
    from src.utils import quick_scan_shared_company as module

    subject = {"entity_id": "ENT_A", "scope": "entity"}
    owner = SimpleNamespace(
        entries={"Q1": {"kind": "work"}},
        proof={"confirmation": {"confirmation_id": "original-confirmation"}},
        input_raw={"identity.json": b'{"payload":{"entity_id":"ENT_A"}}'},
    )
    lifecycle = SimpleNamespace(
        _owner_refresh=owner,
        _external_retrieval=None,
        _observation_context={"questions": {"Q1": {"subject_binding": {"subject_ref": subject}}}},
    )
    client = SimpleNamespace(
        provider_name="mimo",
        model="mimo-v2.6-pro",
        base_url="https://api.xiaomimimo.com/v1",
        supports_web_search=True,
    )
    limits = dict(
        excerpt_chars_per_source=2000,
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
    factory = SimpleNamespace(
        create_company_scan_task=lambda *a, **k: SimpleNamespace(limits=limits)
    )
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
        session_dir.mkdir()
        output.mkdir()
        (output / "complete.txt").write_text("completed original export")
        return {"all_slots_completed": resume_complete}

    def finalize(task, services, output):
        calls.append("finalize")
        assert (output / "complete.txt").is_file()
        return {"status": "completed"}

    engine = SimpleNamespace(
        execute_shared_engine=execute, resume_shared_engine=resume, finalize_shared_engine=finalize
    )
    monkeypatch.setattr(module, "_modules", lambda config: (engine, factory))
    monkeypatch.setattr(services_module, "OwnerResearchServices", lambda *a, **k: SimpleNamespace())
    config = {"state_root": str(tmp_path)}
    args = dict(config=config, questions=[Question("original question", question_id="Q1")])
    with pytest.raises(OSError, match="scripted_partial_export"):
        module.run_shared_company_scan(lifecycle, client, **args)
    scope = next(tmp_path.glob("scan-*"))
    assert not (scope / "export").exists() and (scope / "export-0001/partial.txt").is_file()
    if not resume_complete:
        with pytest.raises(ValueError, match="scan_incomplete"):
            module.run_shared_company_scan(lifecycle, client, **args)
        assert not (scope / "export").exists()
        assert (scope / "export-0002/complete.txt").is_file()
        assert calls == ["execute", "resume"]
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
