"""QA-NET-01 offline CLI E2E through the real public entry.

Covers the package's CLI row: native answer path end-to-end from
``main_with_llm.py`` -> QAEngine -> Q07 checkpoint -> sealed C06 package, the
Q13 frozen-manifest incremental plan (cold then warm), the Q10 restart seal
entry with zero model calls, and a tampered manifest refused before any HTTP.
Only the HTTP boundary is stubbed; the runner, store, adapter and outbox are
the real implementations. The temporary root is pytest's ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

import main_with_llm

UUID_ENTITY = "ENT_1b2a4d3e-0000-4a1b-8c2d-000000000001"
MODEL = "actual-fixture-model"
QUESTIONS = [
    {"question_id": "IQS_01", "text": "问题 IQS_01 的题面"},
    {"question_id": "IQS_02", "text": "问题 IQS_02 的题面"},
]


def _load_harness():
    harness_path = Path(__file__).resolve().parent / "test_quick_scan_cli.py"
    spec = importlib.util.spec_from_file_location("qa_net01_cli_harness", harness_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HARNESS = _load_harness()


def _manifest() -> dict:
    questions = []
    for item in QUESTIONS:
        questions.append(
            {
                "id": item["question_id"],
                "module_id": "common",
                "scope": "entity",
                "prompt": item["text"],
                "prompt_sha256": hashlib.sha256(item["text"].encode("utf-8")).hexdigest(),
                "semantic_sha256": hashlib.sha256(
                    ("sem:" + item["question_id"]).encode()
                ).hexdigest(),
                "definition_sha256": hashlib.sha256(
                    ("def:" + item["question_id"]).encode()
                ).hexdigest(),
                "metric_contract": {
                    "question_id": item["question_id"],
                    "replacement_for": None,
                },
            }
        )
    return {
        "schema_version": "3.1.0",
        "template_version": "3.2.0",
        "question_count": len(questions),
        "questions": questions,
        "modules": ["common"],
        "module_locks": [{"module_id": "common", "version": "3.0.0"}],
        "replacements": {},
        "module_package_id": "pkg_" + "b" * 64,
    }


def _authority() -> dict:
    return {
        "schema_version": "1.0.0",
        "contract_versions": {
            "identity_schema": "2.1.0",
            "answer_schema": "quick-scan-answer-v1",
            "observation_schema": "1.0.0",
            "question_catalog": "iqs-2026-10",
            "model_policy_schema": "2.0.0",
        },
        "capabilities": [
            "entity_security_identity_v1",
            "standard_observation_v1",
            "score_v1",
        ],
        "producer": {"component_version": "1.0.0", "build_id": "stockqa-offline-e2e"},
    }


def _setup(
    tmp_path: Path, *, manifest: dict | None = None, with_authority: bool = True
) -> dict[str, Path]:
    (tmp_path / "logs").mkdir(exist_ok=True)
    question_file = tmp_path / "questions.json"
    question_file.write_text(
        json.dumps(
            {"categories": [{"category": "quality", "questions": QUESTIONS}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(
        json.dumps(manifest if manifest is not None else _manifest(), ensure_ascii=False),
        encoding="utf-8",
    )
    (tmp_path / "llm_apis.json").write_text(
        json.dumps(
            {
                "default_provider": "openai",
                "providers": {
                    "openai": {
                        "enabled": True,
                        "api_key": "offline-fixture-key",
                        "model": MODEL,
                        "base_url": "https://api.openai.com/v1/chat/completions",
                        "max_retries": 1,
                        "format_repair_budget": 0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    authority_file = tmp_path / "quick_scan_c06_authority.json"
    if with_authority:
        authority_file.write_text(json.dumps(_authority(), ensure_ascii=False), encoding="utf-8")
    (tmp_path / "spend_authorization.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "currency": "USD",
                "hard_cap": 25,
                "pricing_snapshot_ref": "fixture-pricing-snapshot",
                "authorized_at": "2026-10-06T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "object_type": "entity",
                "schema_version": "2.2.0",
                "payload": {
                    "entity_id": UUID_ENTITY,
                    "identity_revision": 1,
                    "identity_state": "verified",
                    "listings": [{"source_binding_ref": "BND_fixture_net01"}],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return {
        "questions": question_file,
        "manifest": manifest_file,
        "snapshot": snapshot,
        "authority": authority_file,
        "spend": tmp_path / "spend_authorization.json",
        "output": tmp_path / "result.json",
    }


def _run(
    monkeypatch,
    tmp_path,
    files,
    *,
    manifest: bool = True,
    extra=None,
    with_authority=True,
):
    session = HARNESS.Mock()
    answered = {"count": 0}

    def _post(*args, **kwargs):
        index = answered["count"]
        answered["count"] += 1
        question_id = QUESTIONS[index % len(QUESTIONS)]["question_id"]
        content = (
            '{"entity_id":"'
            + UUID_ENTITY
            + '","company_name":"Fixture Corp","question_id":"'
            + question_id
            + '","score":8,"description":"基于公开来源的判断"}'
        )
        return HARNESS._response(content=content)

    session.post.side_effect = _post
    manager = HARNESS.Mock()
    manager.get_sync_session.return_value = session
    monkeypatch.setattr("src.providers.llm_client.http_client_manager", manager)
    monkeypatch.chdir(tmp_path)
    argv = [
        "main_with_llm.py",
        "--company",
        "Fixture Corp",
        "--entity-id",
        UUID_ENTITY,
        "--provider",
        "openai",
        "--config",
        str(files["questions"]),
        "--output",
        str(files["output"]),
        "--require-search",
        "--identity-snapshot",
        str(files["snapshot"]),
        "--spend-authorization",
        str(files["spend"]),
    ]
    if with_authority:
        argv.extend(["--c06-authority", str(files["authority"])])
    if manifest:
        argv.extend(["--question-manifest", str(files["manifest"])])
    if extra:
        argv.extend(extra)
    monkeypatch.setattr(sys, "argv", argv)
    code = main_with_llm.main()
    return code, session


def _plans(captured: str) -> list[dict]:
    plans = []
    for line in captured.splitlines():
        line = line.strip()
        if line.startswith('{"contract_id": "stockqa.consumes_iqs_question_manifest'):
            plans.append(json.loads(line))
    return plans


def test_cli_e2e_native_path_seals_c06_and_warm_run_sends_zero(
    monkeypatch, tmp_path, capsys
) -> None:
    files = _setup(tmp_path)

    # cold run: one HTTP per question, incremental plan says dispatch both
    code, session = _run(monkeypatch, tmp_path, files)
    cold_out = capsys.readouterr().out
    assert code == 0, cold_out
    assert session.post.call_count == 2
    plans = _plans(cold_out)
    assert plans, cold_out
    cold_plan = plans[-1]
    assert cold_plan["schema"] == "stockqa.question_manifest_plan/1.0.0"
    assert cold_plan["counts"] == {"dispatch": 2}
    assert cold_plan["model_calls_planned"] == 2
    assert cold_plan["entity_id"] == UUID_ENTITY

    # real store: both questions checkpointed and sealed into valid C06 packages
    from src.utils.quick_scan_result_outbox import validate_exchange_package
    from src.utils.quick_scan_work_store import QuickScanWorkStore

    store = QuickScanWorkStore(tmp_path / "quick_scan_work.sqlite")
    con = sqlite3.connect(store.path)
    try:
        rows = con.execute(
            "SELECT question_id,status FROM work_item ORDER BY question_id"
        ).fetchall()
    finally:
        con.close()
    assert rows == [("IQS_01", "result_ready"), ("IQS_02", "result_ready")]
    deliveries = store.list_result_deliveries(states=["ready"], limit=10)
    assert len(deliveries) == 2
    for delivery in deliveries:
        package = delivery["package"]
        validate_exchange_package(package)
        observation = package["items"][0]["observation"]
        assert observation["entity_id"] == UUID_ENTITY
        assert observation["execution"]["model_resolved"] == MODEL
        assert observation["execution"]["search_status"] == "executed"
        assert package["contract_versions"]["observation_schema"] == "1.0.0"

    # warm run: hydration reuses both answers — zero additional HTTP
    code, warm_session = _run(monkeypatch, tmp_path, files)
    warm_out = capsys.readouterr().out
    assert code == 0, warm_out
    assert warm_session.post.call_count == 0
    warm_plan = _plans(warm_out)[-1]
    assert warm_plan["counts"] == {"reuse": 2}
    assert warm_plan["model_calls_planned"] == 0

    # restart seal entry: models 0, packages already sealed
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "main_with_llm.py",
            "--seal-deliveries",
            "--c06-authority",
            str(files["authority"]),
        ],
    )
    seal_code = main_with_llm.main()
    seal_out = capsys.readouterr().out
    assert seal_code == 0, seal_out
    seal_report = json.loads(seal_out.strip().splitlines()[-1])
    assert seal_report["schema"] == "stockqa.seal_deliveries/1.0.0"
    assert seal_report["model_calls"] == 0
    assert seal_report["sealed"] == []
    assert len(seal_report["already_sealed"]) == 2
    assert warm_session.post.call_count == 0


def test_cli_e2e_tampered_manifest_is_refused_before_any_http(
    monkeypatch, tmp_path, capsys
) -> None:
    tampered = _manifest()
    tampered["questions"][0]["prompt"] = "被篡改的题面"
    files = _setup(tmp_path, manifest=tampered)

    code, session = _run(monkeypatch, tmp_path, files)
    captured = capsys.readouterr()
    assert code == 1
    assert session.post.call_count == 0
    # refused before dispatch: no plan was even computed, no request sent
    assert _plans(captured.out) == []
    assert not files["output"].exists()


def _search_policy(**over) -> dict:
    document = {
        "schema_version": "1.0.0",
        "policy_id": "quick-scan-search-e2e",
        "selection": {
            "mode": "external_context",
            "external_search_priority": [{"route_id": "brave-primary"}],
            "answer_model_priority_source": "existing_stockqa_model_policy",
            "hybrid_plan_reference": None,
        },
        "external_routes": [
            {
                "route_id": "brave-primary",
                "kind": "brave",
                "endpoint": "https://api.search.example/v1/web/search",
                "credential_env": "BRAVE_API_KEY",
                "enabled": True,
                "cost_bound_verified": True,
                "pricing": {
                    "basis": "per_request",
                    "currency": "USD",
                    "unit_cost_micros": 3000,
                    "per_request_cap_micros": 3000,
                    "quota_group": None,
                },
                "storage_rights": {
                    "confirmed": True,
                    "entitlement_ref": "ent-brave-2026",
                },
            }
        ],
        "retrieval": {
            "max_snippet_unicode_characters": 500,
            "max_company_evidence_unicode_characters": 30000,
            "max_json_unwrap_layers": 3,
            "max_in_memory_response_bytes": 1000000,
            "raw_response_persistence": False,
        },
        "budget": {
            "source": "existing_stockqa_shared_budget",
            "include_search_model_repair_probe_and_discovery_requests": True,
            "unknown_cost_action": "hold_reservation_and_reconcile",
            "reset_on_restart": False,
        },
    }
    for key, value in over.items():
        document[key] = value
    return document


def test_cli_e2e_search_policy_admission_gates_before_any_http(
    monkeypatch, tmp_path, capsys
) -> None:
    """An external mode whose routes are not admitted fails closed with zero
    HTTP; an admitted route prints its admission receipt before dispatch."""
    import os

    files = _setup(tmp_path, with_authority=False)
    policy_file = tmp_path / "search_policy.json"
    policy_file.write_text(json.dumps(_search_policy()), encoding="utf-8")

    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    code, session = _run(
        monkeypatch,
        tmp_path,
        files,
        manifest=False,
        with_authority=False,
        extra=["--search-policy", str(policy_file)],
    )
    captured = capsys.readouterr()
    assert code == 1
    assert session.post.call_count == 0
    assert '"external_dispatch_enabled": false' in captured.out
    assert "no_admitted_external_route" in captured.out

    monkeypatch.setenv("BRAVE_API_KEY", "offline-fixture-key")
    code, admitted_session = _run(
        monkeypatch,
        tmp_path,
        files,
        manifest=False,
        with_authority=False,
        extra=["--search-policy", str(policy_file)],
    )
    captured = capsys.readouterr()
    assert code == 0, captured.out
    assert admitted_session.post.call_count == 2
    assert '"admitted_routes": ["brave-primary"]' in captured.out
    assert '"external_dispatch_enabled": true' in captured.out


def test_cli_e2e_search_policy_template_marker_is_refused(monkeypatch, tmp_path, capsys) -> None:
    files = _setup(tmp_path, with_authority=False)
    template = _search_policy()
    template["template_only"] = True
    template["execution_enabled"] = False
    policy_file = tmp_path / "template_policy.json"
    policy_file.write_text(json.dumps(template), encoding="utf-8")

    code, session = _run(
        monkeypatch,
        tmp_path,
        files,
        manifest=False,
        with_authority=False,
        extra=["--search-policy", str(policy_file)],
    )
    capsys.readouterr()
    assert code == 1
    assert session.post.call_count == 0


def test_cli_e2e_missing_authority_blocks_then_restart_seals(monkeypatch, tmp_path, capsys) -> None:
    files = _setup(tmp_path, with_authority=False)
    code, session = _run(monkeypatch, tmp_path, files, with_authority=False)
    capsys.readouterr()
    assert code == 0
    assert session.post.call_count == 2

    from src.utils.quick_scan_work_store import QuickScanWorkStore

    store = QuickScanWorkStore(tmp_path / "quick_scan_work.sqlite")
    blocked = store.list_result_deliveries(states=["blocked"], limit=10)
    assert len(blocked) == 2
    assert {item["block_code"] for item in blocked} == {"c06_authority_unavailable"}
    con = sqlite3.connect(store.path)
    try:
        statuses = {row[0] for row in con.execute("SELECT status FROM work_item")}
    finally:
        con.close()
    assert statuses == {"result_ready"}  # answers kept, nothing re-asked

    # the authority arrives later: seal without a single model call
    files["authority"].write_text(json.dumps(_authority()), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "main_with_llm.py",
            "--seal-deliveries",
            "--c06-authority",
            str(files["authority"]),
        ],
    )
    assert main_with_llm.main() == 0
    seal_report = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert seal_report["model_calls"] == 0
    assert len(seal_report["sealed"]) == 2
    assert len(store.list_result_deliveries(states=["ready"], limit=10)) == 2
    assert session.post.call_count == 2  # no new model request on the restart
