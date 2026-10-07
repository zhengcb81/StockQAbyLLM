"""True public CLI/transport regressions; synthetic HTTP and rate-card fixtures.

No provider price guarantee, real identity golden, or live request is claimed.
Unlike the earlier lifecycle-only F2 case, each receipt originates at HTTP.
"""

import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest
import requests

RUNTIME = Path(__file__).resolve().parents[2]


def helper(name, relative):
    spec = importlib.util.spec_from_file_location(name, RUNTIME / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CLI = helper("iqs_transport_owner_cli", "tests/integration/test_qa_net01_cli_e2e.py")


def setup(monkeypatch, tmp_path, *, repair=False, max_requests=100):
    monkeypatch.setattr(CLI, "QUESTIONS", [{"question_id": "IQS_01", "text": "题面"}])
    files = CLI._setup(tmp_path)
    config = json.loads((tmp_path / "llm_apis.json").read_text(encoding="utf-8"))
    providers = CLI.HARNESS._ordered_provider_configs()
    for provider in providers.values():
        provider.update(max_retries=1, format_repair_budget=int(repair))
    policy = CLI.HARNESS._ordered_policy()
    policy["budget"]["max_requests"] = max_requests
    config.update(default_provider="primary", providers=providers, quick_scan_model_policy=policy)
    if repair:
        # Existing configured v2 deliberately disables format repair. Exercise
        # the explicit legacy single-route repair contract without enabling it.
        config.pop("quick_scan_model_policy")
        providers["primary"]["model"] = "primary-model"
    (tmp_path / "llm_apis.json").write_text(json.dumps(config), encoding="utf-8")
    registry = {
        "schema_version": "1.0.0",
        "rate_cards": [
            {
                "pricing_ref": "fixture-cli-rate-card-v1",
                "provider": "openai",
                "model": model,
                "currency": "USD",
                "source_ref": "offline-fixture-price-not-real-provider-billing",
                "source_checked_at": "2026-10-07",
                "rates": {
                    "input_per_million_tokens": 2,
                    "cached_input_per_million_tokens": 1,
                    "cache_creation_input_per_million_tokens": 4,
                    "output_per_million_tokens": 10,
                    "search_tool_call": 0.25,
                },
            }
            for model in ("primary-model", "backup-model")
        ],
    }
    (tmp_path / "quick_scan_rate_cards.json").write_text(json.dumps(registry), encoding="utf-8")
    return files


def invoke(
    monkeypatch, tmp_path, files, *, failure="reported_usage", repair=False, assert_intent=True
):
    import main_with_llm
    from src.utils.quick_scan_work_store import QuickScanWorkStore

    session = CLI.HARNESS.Mock()
    models = []

    def post(*args, **kwargs):
        model = kwargs["json"]["model"]
        models.append(model)
        store = QuickScanWorkStore(tmp_path / "quick_scan_work.sqlite")
        item = store.find_work_items(entity_id=CLI.UUID_ENTITY, question_ids=["IQS_01"])["IQS_01"][
            0
        ]
        attempts = store.list_attempts(item["work_item_id"])
        if assert_intent:
            assert len(attempts) == len(models)
            current = attempts[-1]
            assert current["phase"] == "send_intent"
            assert current["model_requested"] == model
            if not repair:
                assert current["route_id"] == (
                    "primary-route" if model == "primary-model" else "backup-route"
                )
            with sqlite3.connect(store.path) as con:
                budget = con.execute(
                    "SELECT status,in_flight FROM quick_scan_budget_attempt WHERE budget_attempt_id=?",
                    (current["attempt_id"],),
                ).fetchone()
            if not repair:
                assert budget == (
                    "in_flight",
                    1,
                ), "Actual HTTP must have its own budget reservation"
            else:
                assert budget is None  # Legacy single route has no configured budget policy.
        if len(models) == 1 and failure == "timeout":
            raise requests.Timeout("offline synthetic timeout")
        if len(models) == 1 and failure not in {"none", "repair", "oversize_description"}:
            response = CLI.HARNESS.Mock(status_code=429)
            response.headers = {"x-request-id": "req-primary-rejected"}
            payload = {"error": {"code": "insufficient_quota"}}
            if failure in {
                "reported_usage",
                "nonzero_usage",
                "malformed_usage",
                "missing_search_count",
                "conflicting_usage",
                "bad_usage_details",
                "conflicting_cache_details",
            }:
                payload.update(
                    model=model,
                    usage={
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "web_search_usage": {"tool_usage": 0},
                    },
                )
                if failure == "malformed_usage":
                    payload["usage"]["input_tokens"] = -1
                elif failure == "nonzero_usage":
                    payload["usage"].update(input_tokens=100, output_tokens=50)
                elif failure == "missing_search_count":
                    payload["usage"].pop("web_search_usage")
                elif failure == "conflicting_usage":
                    payload["usage"]["prompt_tokens"] = 10
                elif failure == "bad_usage_details":
                    payload["usage"]["output_tokens_details"] = []
                elif failure == "conflicting_cache_details":
                    payload["usage"].update(
                        input_tokens=100,
                        prompt_tokens=100,
                        output_tokens=50,
                        input_tokens_details={"cached_tokens": 100},
                        prompt_tokens_details={"cached_tokens": 0},
                    )
            response.json.return_value = payload
            response.raise_for_status.side_effect = requests.HTTPError(
                "offline rejected", response=response
            )
            return response
        content = json.dumps(
            {
                "entity_id": CLI.UUID_ENTITY,
                "company_name": "Fixture Corp",
                "question_id": "IQS_01",
                "score": 8,
                "description": "有来源的合成答案",
            },
            ensure_ascii=False,
        )
        if failure == "oversize_description":
            document = json.loads(content)
            document["description"] = "x" * 5001
            content = json.dumps(document)
        if failure == "repair" and len(models) == 1:
            content = "synthetic invalid JSON"
        response = CLI.HARNESS._response(
            content=content,
            request_id="req-" + str(len(models)),
            response_id="resp-" + str(len(models)),
            search_call_id="search-" + str(len(models)),
            usage={"input_tokens": 100, "output_tokens": 50},
        )
        response.json.return_value["model"] = model
        # Final repair receipt has a different exact source set from aggregate history.
        if failure == "repair" and len(models) == 2:
            response.json.return_value["output"][0]["action"]["sources"][0][
                "url"
            ] = "https://example.com/repair"
        return response

    session.post.side_effect = post
    manager = CLI.HARNESS.Mock()
    manager.get_sync_session.return_value = session
    monkeypatch.setattr("src.providers.llm_client.http_client_manager", manager)
    monkeypatch.chdir(tmp_path)
    argv = [
        "main_with_llm.py",
        "--company",
        "Fixture Corp",
        "--entity-id",
        CLI.UUID_ENTITY,
        "--provider",
        "primary",
        "--config",
        str(files["questions"]),
        "--output",
        str(files["output"]),
        "--require-search",
        "--identity-snapshot",
        str(files["snapshot"]),
        "--spend-authorization",
        str(files["spend"]),
        "--question-manifest",
        str(files["manifest"]),
        "--c06-authority",
        str(files["authority"]),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    return main_with_llm.main(), session, models


def test_actual_two_route_fallback_checkpoint_seal_and_zero_http_resume(
    monkeypatch, tmp_path, capsys
):
    from src.utils.quick_scan_work_store import QuickScanWorkStore

    files = setup(monkeypatch, tmp_path)
    code, session, models = invoke(monkeypatch, tmp_path, files)
    captured = capsys.readouterr()
    assert code == 0, captured.out
    assert session.post.call_count == 2 and models == ["primary-model", "backup-model"]
    store = QuickScanWorkStore(tmp_path / "quick_scan_work.sqlite")
    item = store.find_work_items(entity_id=CLI.UUID_ENTITY, question_ids=["IQS_01"])["IQS_01"][0]
    attempts = store.list_attempts(item["work_item_id"])
    assert [a["phase"] for a in attempts] == ["confirmed_failure", "response_available"]
    checkpoint = store.get_answer_checkpoint(item["work_item_id"])
    assert checkpoint and checkpoint["attempt_id"] == attempts[1]["attempt_id"]
    provenance = checkpoint["payload"]["provenance"]
    assert provenance["actual_model"] == "backup-model" and provenance["route_id"] == "backup-route"
    delivery = store.get_result_delivery(item["work_item_id"])
    assert delivery["state"] == "ready"
    before = json.dumps(delivery["package"], sort_keys=True)
    budget_before = store.get_quick_scan_budget_status("quick-scan-test-policy")
    assert budget_before["requests"] == 2 and budget_before["unreconciled_attempts"] == 0
    files["output"] = tmp_path / "warm-independent-output.json"
    code, warm, _ = invoke(monkeypatch, tmp_path, files)
    captured = capsys.readouterr()
    assert code == 0, captured.out
    assert warm.post.call_count == 0
    assert store.list_attempts(item["work_item_id"]) == attempts
    assert (
        json.dumps(store.get_result_delivery(item["work_item_id"])["package"], sort_keys=True)
        == before
    )
    assert store.get_quick_scan_budget_status("quick-scan-test-policy") == budget_before


@pytest.mark.parametrize(
    "failure",
    [
        "timeout",
        "unpriced",
        "malformed_usage",
        "missing_search_count",
        "conflicting_usage",
        "bad_usage_details",
        "conflicting_cache_details",
    ],
)
def test_uncertain_or_unpriced_primary_never_blindly_sends_backup(
    monkeypatch, tmp_path, capsys, failure
):
    from src.utils.quick_scan_work_store import QuickScanWorkStore

    files = setup(monkeypatch, tmp_path)
    _, session, models = invoke(monkeypatch, tmp_path, files, failure=failure)
    capsys.readouterr()
    assert session.post.call_count == 1 and models == ["primary-model"]
    store = QuickScanWorkStore(tmp_path / "quick_scan_work.sqlite")
    item = store.find_work_items(entity_id=CLI.UUID_ENTITY, question_ids=["IQS_01"])["IQS_01"][0]
    attempts = store.list_attempts(item["work_item_id"])
    sent = [a for a in attempts if a["phase"] != "prepared"]
    assert len(sent) == 1
    assert sent[0]["phase"] == ("uncertain" if failure == "timeout" else "confirmed_failure")
    assert store.get_answer_checkpoint(item["work_item_id"]) is None
    status = store.get_quick_scan_budget_status("quick-scan-test-policy")
    assert status["requests"] == 1 and status["unreconciled_attempts"] == 1
    assert status["reserved_micros"] > 0


def test_one_explicit_format_repair_keeps_final_http_receipt_hash(monkeypatch, tmp_path, capsys):
    from src.utils.quick_scan_work_store import QuickScanWorkStore

    files = setup(monkeypatch, tmp_path, repair=True)
    code, session, models = invoke(monkeypatch, tmp_path, files, failure="repair", repair=True)
    captured = capsys.readouterr()
    assert code == 0, captured.out
    assert session.post.call_count == 2 and models == ["primary-model", "primary-model"]
    store = QuickScanWorkStore(tmp_path / "quick_scan_work.sqlite")
    item = store.find_work_items(entity_id=CLI.UUID_ENTITY, question_ids=["IQS_01"])["IQS_01"][0]
    attempts = store.list_attempts(item["work_item_id"])
    checkpoint = store.get_answer_checkpoint(item["work_item_id"])
    assert checkpoint and checkpoint["attempt_id"] == attempts[-1]["attempt_id"]
    provenance = checkpoint["payload"]["provenance"]
    assert provenance["receipt_sha256"] == attempts[-1]["receipt_sha256"]
    assert provenance["source_urls"] == ["https://example.com/repair"]
    assert store.get_result_delivery(item["work_item_id"])["state"] == "ready"


def test_rejected_primary_with_reported_nonzero_usage_is_not_free(monkeypatch, tmp_path, capsys):
    from src.utils.quick_scan_work_store import QuickScanWorkStore

    files = setup(monkeypatch, tmp_path)
    code, session, models = invoke(monkeypatch, tmp_path, files, failure="nonzero_usage")
    captured = capsys.readouterr()
    assert code == 0, captured.out
    assert models == ["primary-model", "backup-model"] and session.post.call_count == 2
    store = QuickScanWorkStore(tmp_path / "quick_scan_work.sqlite")
    with sqlite3.connect(store.path) as con:
        costs = con.execute(
            "SELECT actual_cost_micros FROM quick_scan_budget_attempt ORDER BY created_at,budget_attempt_id"
        ).fetchall()
    assert sorted(cost[0] for cost in costs) == [700, 250700]
    assert (
        store.get_quick_scan_budget_status("quick-scan-test-policy")["unreconciled_attempts"] == 0
    )


@pytest.mark.parametrize("tamper", ["attempt_id", "model", "receipt_hash"])
def test_transport_checkpoint_refuses_mutated_binding_without_re_recording(
    monkeypatch, tmp_path, tamper
):
    from src.core.models import Answer, QAResult, Question
    from src.providers.llm_client import LLMClient
    from src.runners.llm_runner import QuickScanWorkLifecycle
    from src.utils.quick_scan_work_store import QuickScanWorkStore

    seal = helper("iqs_seal_tamper_helper", "tests/unit/test_qa_net01_c06_seal.py")
    store = QuickScanWorkStore(tmp_path / "tamper.sqlite")
    lifecycle = QuickScanWorkLifecycle(
        store,
        entity_id=seal.UUID_ENTITY,
        run_id="tamper",
        scan_id="tamper",
        identity=seal.IDENTITY,
        model_requested="primary-model",
        transport_managed=True,
    )
    question = Question(text="题面", question_id="IQS_01")
    handle = lifecycle.before_question(question)
    response = CLI.HARNESS._response(
        content="synthetic", usage={"input_tokens": 1, "output_tokens": 1}
    )
    response.json.return_value["model"] = "primary-model"
    session = CLI.HARNESS.Mock()
    session.post.return_value = response
    manager = CLI.HARNESS.Mock()
    manager.get_sync_session.return_value = session
    monkeypatch.setattr("src.providers.llm_client.http_client_manager", manager)
    with lifecycle.dispatch_context(handle):
        received = LLMClient(
            "offline-key", "primary-model", "https://api.openai.com/v1/chat/completions"
        ).send_search_request("synthetic")
    original_attempts = store.list_attempts(handle["work_item_id"])
    execution = received.execution_metadata
    private = execution["work_transport"]
    if tamper == "attempt_id":
        private["work_attempt_id"] = "ATTEMPT_unknown"
    elif tamper == "model":
        private["final_receipt"]["actual_model"] = "other-model"
    else:
        private["final_receipt"]["source_urls"] = ["https://example.com/tampered"]
    result = QAResult(
        question=question,
        answer=Answer(text="合成答案", score=8, status="scored", metadata={"execution": execution}),
    )
    from src.core.exceptions import ProcessingError

    with pytest.raises(ProcessingError, match="检查点"):
        lifecycle.after_question(handle, result)
    assert store.get_answer_checkpoint(handle["work_item_id"]) is None
    assert store.list_attempts(handle["work_item_id"]) == original_attempts


def test_checkpoint_refusal_is_public_error_without_losing_transport(monkeypatch, tmp_path, capsys):
    from src.utils.quick_scan_work_store import QuickScanWorkStore

    files = setup(monkeypatch, tmp_path)
    code, session, models = invoke(monkeypatch, tmp_path, files, failure="oversize_description")
    capsys.readouterr()
    assert code == 1 and session.post.call_count == 1
    answer = json.loads(files["output"].read_text(encoding="utf-8"))["answers"]["IQS_01"]
    assert answer["status"] == "error" and answer["score"] is None
    store = QuickScanWorkStore(tmp_path / "quick_scan_work.sqlite")
    item = store.find_work_items(entity_id=CLI.UUID_ENTITY, question_ids=["IQS_01"])["IQS_01"][0]
    attempts = store.list_attempts(item["work_item_id"])
    assert len(attempts) == 1 and attempts[0]["phase"] == "response_available"
    assert store.get_answer_checkpoint(item["work_item_id"]) is None
    assert store.get_result_delivery(item["work_item_id"]) is None
    assert (
        store.get_quick_scan_budget_status("quick-scan-test-policy")["unreconciled_attempts"] == 0
    )


@pytest.mark.parametrize(
    "field,value",
    [("search_status", "unverified"), ("response_status", "incomplete"), ("actual_model", None)],
)
def test_private_http_receipt_still_requires_checkpoint_quality(field, value):
    from src.core.models import execution_receipt_for_checkpoint

    receipt = {
        "provider": "openai",
        "search_status": "executed",
        "response_status": "completed",
        "http_status_code": 200,
        "completed_at": "2026-10-07T00:00:00Z",
        "actual_model": "primary-model",
        "response_id": "response",
        "attempt_id": "provider-attempt",
        "search_receipt_id": "search",
    }
    receipt[field] = value
    assert (
        execution_receipt_for_checkpoint(
            {"execution": {"work_transport": {"final_receipt": receipt}}}
        )
        is None
    )
