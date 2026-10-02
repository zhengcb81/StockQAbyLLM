"""Public CLI quick-scan integration tests with isolated provider transport."""

import hashlib
import json
import sqlite3
from contextlib import closing
from unittest.mock import Mock

import pytest
import requests

import main_with_llm
import src.runners.llm_runner as llm_runner_module
from src.utils.quick_scan_work_store import QuickScanWorkStore

_ORIGINAL_BIND_QUICK_SCAN_BUDGET = llm_runner_module.bind_quick_scan_budget


def _assert_unpriced_budget_reservation(tmp_path):
    status = QuickScanWorkStore(tmp_path / "quick_scan_work.sqlite").get_quick_scan_budget_status(
        "quick-scan-test-policy"
    )
    assert status["reserved_micros"] == 10_000_000
    assert status["requests"] == 1
    assert status["in_flight"] == 0
    assert status["unreconciled_attempts"] == 1


def _write_inputs(
    tmp_path,
    *,
    with_ids=True,
    repair_budget=0,
    question_count=1,
    max_retries=1,
    provider_configs=None,
    model_policy=None,
    rate_cards=None,
):
    questions = (
        [
            {
                "question_id": f"IQS_{index + 5:02d}",
                "text": f"公司的第{index + 1}项核心优势是否持久？",
            }
            for index in range(question_count)
        ]
        if with_ids
        else ["公司的核心优势是否持久？"]
    )
    question_file = tmp_path / "questions.json"
    question_file.write_text(
        json.dumps(
            {"categories": [{"category": "quality", "questions": questions}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    config_file = tmp_path / "llm_apis.json"
    providers = provider_configs or {
        "openai": {
            "enabled": True,
            "api_key": "offline-fixture-key",
            "model": "requested-fixture-model",
            "base_url": "https://api.openai.com/v1/chat/completions",
            "max_retries": max_retries,
            "format_repair_budget": repair_budget,
        }
    }
    llm_config = {
        "default_provider": "openai" if "openai" in providers else next(iter(providers)),
        "providers": providers,
    }
    if model_policy is not None:
        llm_config["quick_scan_model_policy"] = model_policy
    config_file.write_text(json.dumps(llm_config), encoding="utf-8")
    if rate_cards is not None:
        (tmp_path / "quick_scan_rate_cards.json").write_text(
            json.dumps(rate_cards), encoding="utf-8"
        )
    return question_file, config_file


def _response(
    *,
    searched=True,
    content=None,
    question_id="IQS_05",
    request_id="req_e2e_01",
    response_id="resp_e2e_01",
    search_call_id="ws_e2e_01",
    usage=None,
):
    if content is None:
        content = (
            '{"entity_id":"issuer:fixture","company_name":"Fixture Corp",'
            f'"question_id":"{question_id}","score":8,"description":"基于公开来源的判断"}}'
        )
    output = []
    if searched:
        output.append(
            {
                "type": "web_search_call",
                "id": search_call_id,
                "status": "completed",
                "action": {
                    "type": "search",
                    "sources": [{"type": "url", "url": "https://example.com/issuer"}],
                },
            }
        )
    output.append(
        {
            "type": "message",
            "content": [{"type": "output_text", "text": content}],
        }
    )
    response = Mock()
    response.headers = {"x-request-id": request_id}
    response.status_code = 200
    payload = {
        "id": response_id,
        "status": "completed",
        "model": "actual-fixture-model",
        "output": output,
    }
    if usage is not None:
        payload["usage"] = usage
    response.json.return_value = payload
    return response


def _minimax_response(*, searched=True, cited=True, actual_model="MiniMax-M3"):
    response = _response(searched=searched)
    payload = response.json.return_value
    payload["model"] = actual_model
    if searched:
        payload["output"][0]["action"].pop("sources")
    if cited:
        payload["output"][-1]["content"][0]["annotations"] = [
            {"type": "url_citation", "url": "https://example.com/issuer"}
        ]
    response.headers = {}
    return response


def _minimax_provider_config(
    *, base_url="https://api.minimaxi.com/v1/responses", model="MiniMax-M3"
):
    return {
        "minimax": {
            "enabled": True,
            "api_key": "offline-fixture-key",
            "model": model,
            "base_url": base_url,
            "max_retries": 1,
            "format_repair_budget": 0,
        }
    }


def _minimax_anthropic_response():
    response = Mock()
    response.headers = {}
    response.status_code = 200
    response.json.return_value = {
        "id": "msg_mm_cli_01",
        "model": "MiniMax-M3",
        "stop_reason": "end_turn",
        "base_resp": {"status_code": 0},
        "content": [
            {
                "type": "server_tool_use",
                "id": "srvtoolu_cli_01",
                "name": "web_search",
                "input": {"query": "Fixture Corp official source"},
            },
            {
                "type": "web_search_tool_result",
                "tool_use_id": "srvtoolu_cli_01",
                "content": [
                    {
                        "type": "web_search_result",
                        "title": "Official source",
                        "url": "https://example.com/issuer",
                    }
                ],
            },
            {
                "type": "text",
                "text": (
                    '{"entity_id":"issuer:fixture","company_name":"Fixture Corp",'
                    '"question_id":"IQS_05","score":8,"description":"Verified source."}'
                ),
            },
        ],
    }
    return response


def _http_failure(status_code, *, retry_after=None, error_code=None):
    response = Mock()
    response.headers = {"x-request-id": f"failed-{status_code}"}
    if retry_after is not None:
        response.headers["Retry-After"] = retry_after
    response.status_code = status_code
    response.json.return_value = {"error": {"code": error_code}} if error_code else {}
    response.raise_for_status.side_effect = requests.HTTPError("private provider response")
    return response


def _ordered_policy(*, max_attempts=3):
    return {
        "schema_version": "2.0.0",
        "policy_id": "quick-scan-test-policy",
        "execution_owner": "StockQAbyLLM",
        "configured": True,
        "dispatch": {
            "max_in_flight_total": 4,
            "max_questions_per_pack": 32,
            "one_entity_per_pack": True,
            "separate_score_and_fact_packs": True,
            "required_capabilities": ["web_search", "structured_output"],
            "on_preferred_capacity_full": "wait",
            "speculative_racing": False,
        },
        "budget": {
            "currency": "USD",
            "max_cost": 100,
            "max_requests": 100,
            "max_cost_per_attempt": 10,
            "reset_on_restart": False,
        },
        "cost_policy": {
            "pricing_basis": "verified_rate_card",
            "pricing_ref": "fixture-cli-rate-card-v1",
            "include_search_charges": True,
            "include_failed_attempts": True,
            "reserve_before_dispatch": True,
            "unknown_actual_cost_action": "retain_reservation_and_pause",
        },
        "quota_groups": [
            {
                "id": "account-primary",
                "max_in_flight": 1,
                "window_seconds_hint": None,
                "unknown_reset_cooldown_seconds": 18000,
                "half_open_probe_limit": 1,
            },
            {
                "id": "account-backup",
                "max_in_flight": 1,
                "window_seconds_hint": None,
                "unknown_reset_cooldown_seconds": 18000,
                "half_open_probe_limit": 1,
            },
        ],
        "fallback": {
            "on_quota_exhausted": "cooldown_group_then_next",
            "on_rate_limit": "respect_retry_after_then_next",
            "on_server_error": "bounded_retry_then_next",
            "on_auth_error": "disable_route_then_next",
            "on_invalid_request": "stop_without_fallback",
            "on_malformed_response": "bounded_retry_then_next",
            "on_missing_capability": "skip_before_dispatch",
            "on_timeout": "reconcile_receipt_before_retry",
            "on_low_score": "accept",
            "on_unknown_answer": "accept_with_gap",
            "on_fallback_success": "stop_dispatch_keep_primary_health",
            "max_attempts_per_dispatch_round": max_attempts,
        },
        "comparison": {
            "enabled": False,
            "max_models_per_question": 2,
            "max_cost": 0,
            "max_requests": 0,
            "same_input_and_cutoff_required": True,
            "separate_from_primary_fallback": True,
        },
        "resume": {
            "success_checkpoint": "per_question",
            "replay_outbox_before_dispatch": True,
            "preserve_uncertain_requests": True,
            "persist_cooldowns_and_budget": True,
        },
        "models": [
            {
                "id": "primary-route",
                "enabled": True,
                "provider_config_ref": "primary",
                "model": "primary-model",
                "quota_group": "account-primary",
                "max_in_flight": 1,
            },
            {
                "id": "backup-route",
                "enabled": True,
                "provider_config_ref": "backup",
                "model": "backup-model",
                "quota_group": "account-backup",
                "max_in_flight": 1,
            },
        ],
    }


def _ordered_provider_configs(*, primary_url="https://api.openai.com/v1/chat/completions"):
    return {
        "primary": {
            "enabled": True,
            "api_key": "offline-primary-key",
            "model": "legacy-primary",
            "base_url": primary_url,
            "max_retries": 3,
        },
        "backup": {
            "enabled": True,
            "api_key": "offline-backup-key",
            "model": "legacy-backup",
            "base_url": "https://api.openai.com/v1/chat/completions",
            "max_retries": 3,
        },
    }


def _invoke(
    monkeypatch,
    tmp_path,
    *,
    searched=True,
    with_ids=True,
    entity_id="issuer:fixture",
    repair_budget=0,
    question_count=1,
    max_retries=1,
    responses=None,
    provider_configs=None,
    model_policy=None,
    provider_name="openai",
    fixture_cost_resolver=True,
    rate_cards=None,
):
    question_file, _ = _write_inputs(
        tmp_path,
        with_ids=with_ids,
        repair_budget=repair_budget,
        question_count=question_count,
        max_retries=max_retries,
        provider_configs=provider_configs,
        model_policy=model_policy,
        rate_cards=rate_cards,
    )
    if model_policy is not None and fixture_cost_resolver:

        def bind_with_fixture_cost(store, policy, **kwargs):
            kwargs["cost_resolver"] = lambda receipt: {
                "actual_cost": 0.01,
                "pricing_ref": "fixture-cli-rate-card-v1",
                "source_ref": f"offline-provider-usage-{receipt.get('http_status_code', 'unknown')}",
            }
            return _ORIGINAL_BIND_QUICK_SCAN_BUDGET(store, policy, **kwargs)

        monkeypatch.setattr(llm_runner_module, "bind_quick_scan_budget", bind_with_fixture_cost)
    (tmp_path / "logs").mkdir(exist_ok=True)
    session = Mock()
    if responses is None:
        session.post.return_value = _response(searched=searched)
    else:
        session.post.side_effect = responses
    manager = Mock()
    manager.get_sync_session.return_value = session
    monkeypatch.setattr("src.providers.llm_client.http_client_manager", manager)
    monkeypatch.chdir(tmp_path)
    output_file = tmp_path / "result.json"
    argv = [
        "main_with_llm.py",
        "--company",
        "Fixture Corp",
        "--entity-id",
        entity_id,
        "--provider",
        provider_name,
        "--config",
        str(question_file),
        "--output",
        str(output_file),
        "--require-search",
    ]
    monkeypatch.setattr("sys.argv", argv)
    exit_code = main_with_llm.main()
    return exit_code, output_file, session


def test_public_cli_reopens_durable_quota_cooldown_without_reasking_primary(monkeypatch, tmp_path):
    policy = _ordered_policy()
    first_code, first_output, first_session = _invoke(
        monkeypatch,
        tmp_path,
        provider_name="primary",
        provider_configs=_ordered_provider_configs(),
        model_policy=policy,
        responses=[
            _http_failure(429, error_code="insufficient_quota"),
            _response(),
        ],
    )
    assert first_code == 0
    first_receipt = json.loads(first_output.read_text(encoding="utf-8"))["execution_receipts"][
        "IQS_05"
    ]
    assert [a["http_status_code"] for a in first_receipt["attempts"]] == [429, 200]
    assert first_session.post.call_count == 2
    health_path = tmp_path / "quick_scan_health.sqlite"
    assert health_path.exists()

    second_code, second_output, second_session = _invoke(
        monkeypatch,
        tmp_path,
        provider_name="primary",
        provider_configs=_ordered_provider_configs(),
        model_policy=policy,
        responses=[_response(request_id="backup-after-restart")],
    )
    assert second_code == 0
    second_receipt = json.loads(second_output.read_text(encoding="utf-8"))["execution_receipts"][
        "IQS_05"
    ]
    assert second_receipt["attempts"][-1]["route_trace"][0]["decision"] == (
        "skipped_quota_group_cooldown"
    )
    assert second_receipt["attempts"][-1]["provider_config_ref"] == "backup"
    assert second_session.post.call_count == 1
    raw = health_path.read_bytes()
    for forbidden in (b"offline-primary-key", b"https://", b"Fixture Corp"):
        assert forbidden not in raw


def test_public_cli_fake_health_schema_rejects_before_http(monkeypatch, tmp_path):
    health_path = tmp_path / "quick_scan_health.sqlite"
    with closing(sqlite3.connect(health_path)) as connection, connection:
        connection.execute("CREATE TABLE group_health(group_id TEXT PRIMARY KEY)")
        connection.execute("PRAGMA user_version=1")
    before = health_path.read_bytes()
    code, output, session = _invoke(
        monkeypatch,
        tmp_path,
        provider_name="primary",
        provider_configs=_ordered_provider_configs(),
        model_policy=_ordered_policy(),
        responses=[_response()],
    )
    assert code == 1
    assert session.post.call_count == 0
    assert not output.exists()
    assert health_path.read_bytes() == before


def test_public_cli_exports_eight_score_entity_timestamp_model_and_search_receipt(
    monkeypatch, tmp_path
):
    exit_code, output_file, session = _invoke(monkeypatch, tmp_path)

    assert exit_code == 0
    result = json.loads(output_file.read_text(encoding="utf-8"))
    assert result["schema_version"] == "stockqa.quick_scan_result/1.0.0"
    assert result["entity"] == {"entity_id": "issuer:fixture", "name": "Fixture Corp"}
    assert result["observed_at"].endswith("Z")
    assert result["provider"] == {
        "name": "openai",
        "requested_model": "requested-fixture-model",
    }
    answer = result["answers"]["IQS_05"]
    assert answer["score"] == 8
    assert answer["status"] == "scored"
    assert answer["check_level"] == "unverified_model_output"
    assert answer["question_id"] == "IQS_05"
    assert "response_kind" not in answer
    receipt = result["execution_receipts"]["IQS_05"]
    assert receipt["request_id"] == "req_e2e_01"
    assert receipt["response_id"] == "resp_e2e_01"
    assert receipt["actual_model"] == "actual-fixture-model"
    assert receipt["search_status"] == "executed"
    assert receipt["response_status"] == "completed"
    assert receipt["http_status_code"] == 200
    assert receipt["started_at"].endswith("Z")
    assert receipt["completed_at"].endswith("Z")
    assert receipt["attempt_id"]
    assert len(receipt["prompt_sha256"]) == 64
    expected_question = "公司的第1项核心优势是否持久？"
    assert (
        receipt["input_question_sha256"]
        == hashlib.sha256(expected_question.encode("utf-8")).hexdigest()
    )
    assert (
        receipt["answer_sha256"]
        == hashlib.sha256(
            json.dumps(
                answer,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
    )
    assert receipt["attempts"][-1]["http_status_code"] == 200
    assert receipt["attempts"][-1]["provider"] == "openai"
    assert receipt["attempts"][-1]["provider_config_ref"] == "openai"
    assert receipt["search_receipt_id"] == "ws_e2e_01"
    assert receipt["answered_at"].endswith("Z")
    assert receipt["source_urls"] == ["https://example.com/issuer"]
    request = session.post.call_args.kwargs["json"]
    assert request["tools"] == [{"type": "web_search"}]
    assert request["include"] == ["web_search_call.action.sources"]
    assert "IQS_05" in request["input"]
    assert "issuer:fixture" in request["input"]
    assert "Fixture Corp" in request["input"]


def test_public_cli_falls_back_after_quota_rejection_and_records_ordered_attempts(
    monkeypatch, tmp_path
):
    policy = _ordered_policy()
    exit_code, output_file, session = _invoke(
        monkeypatch,
        tmp_path,
        provider_name="primary",
        provider_configs=_ordered_provider_configs(),
        model_policy=policy,
        responses=[
            _http_failure(429, error_code="insufficient_quota"),
            _response(request_id="backup-request"),
        ],
    )

    assert exit_code == 0
    assert session.post.call_count == 2
    output = json.loads(output_file.read_text(encoding="utf-8"))
    answer = output["answers"]["IQS_05"]
    receipt = output["execution_receipts"]["IQS_05"]
    assert answer["score"] == 8
    assert receipt["provider"] == "openai"
    assert receipt["requested_model"] == "backup-model"
    assert [attempt["provider"] for attempt in receipt["attempts"]] == [
        "openai",
        "openai",
    ]
    assert [attempt["provider_config_ref"] for attempt in receipt["attempts"]] == [
        "primary",
        "backup",
    ]
    assert [attempt["http_status_code"] for attempt in receipt["attempts"]] == [
        429,
        200,
    ]
    assert all(
        attempt["policy_version"].startswith("quick-scan-test-policy@")
        for attempt in receipt["attempts"]
    )
    route_trace = receipt["attempts"][-1]["route_trace"]
    assert [route["decision"] for route in route_trace] == [
        "provider_failure",
        "accepted_answer",
    ]


def test_public_cli_pauses_after_unpriced_attempt_without_sending_backup(monkeypatch, tmp_path):
    exit_code, output_file, session = _invoke(
        monkeypatch,
        tmp_path,
        provider_name="primary",
        provider_configs=_ordered_provider_configs(),
        model_policy=_ordered_policy(),
        responses=[
            _http_failure(429, error_code="insufficient_quota"),
            _response(request_id="must-not-be-sent"),
        ],
        fixture_cost_resolver=False,
    )

    assert exit_code == 0
    assert session.post.call_count == 1
    result = json.loads(output_file.read_text(encoding="utf-8"))
    receipt = result["execution_receipts"]["IQS_05"]
    assert [attempt["http_status_code"] for attempt in receipt["attempts"]] == [429]
    assert receipt["dispatch_outcome"]["state"] == "budget_deferred"
    assert (
        receipt["dispatch_outcome"]["resume_condition"]
        == "budget_reconciled_or_dispatch_capacity_available"
    )
    _assert_unpriced_budget_reservation(tmp_path)


def test_public_cli_settles_provider_usage_against_matching_local_rate_card(monkeypatch, tmp_path):
    registry = {
        "schema_version": "1.0.0",
        "rate_cards": [
            {
                "pricing_ref": "fixture-cli-rate-card-v1",
                "provider": "openai",
                "model": "actual-fixture-model",
                "currency": "USD",
                "source_ref": "offline-fixture-rate-card",
                "source_checked_at": "2026-09-27",
                "rates": {
                    "input_per_million_tokens": 2,
                    "cached_input_per_million_tokens": 0.5,
                    "cache_creation_input_per_million_tokens": 4,
                    "output_per_million_tokens": 10,
                    "search_tool_call": 0.25,
                },
            }
        ],
    }
    response = _response(
        usage={
            "input_tokens": 1000,
            "input_tokens_details": {"cached_tokens": 100},
            "output_tokens": 500,
            "output_tokens_details": {"reasoning_tokens": 20},
        }
    )
    exit_code, output_file, session = _invoke(
        monkeypatch,
        tmp_path,
        provider_name="primary",
        provider_configs=_ordered_provider_configs(),
        model_policy=_ordered_policy(),
        responses=[response],
        fixture_cost_resolver=False,
        rate_cards=registry,
    )

    assert exit_code == 0
    assert session.post.call_count == 1
    result = json.loads(output_file.read_text(encoding="utf-8"))
    usage = result["execution_receipts"]["IQS_05"]["attempts"][0]["usage"]
    assert usage["input_tokens"] == 900
    assert usage["cached_input_tokens"] == 100
    status = QuickScanWorkStore(tmp_path / "quick_scan_work.sqlite").get_quick_scan_budget_status(
        "quick-scan-test-policy"
    )
    assert status["spent_micros"] == 256850
    assert status["reserved_micros"] == 0
    assert status["unreconciled_attempts"] == 0


def test_public_cli_does_not_fallback_on_unrecognized_429(monkeypatch, tmp_path):
    exit_code, output_file, session = _invoke(
        monkeypatch,
        tmp_path,
        provider_name="primary",
        provider_configs=_ordered_provider_configs(),
        model_policy=_ordered_policy(),
        responses=[_http_failure(429), _response(request_id="must-not-be-sent")],
        fixture_cost_resolver=False,
    )

    assert exit_code == 1
    assert session.post.call_count == 1
    result = json.loads(output_file.read_text(encoding="utf-8"))
    receipt = result["execution_receipts"]["IQS_05"]
    assert [attempt["http_status_code"] for attempt in receipt["attempts"]] == [429]
    assert receipt["dispatch_outcome"]["state"] == "uncertain"
    assert receipt["attempts"][0]["requested_model"] == "primary-model"
    assert receipt["dispatch_outcome"]["resume_condition"] == "reconcile_same_attempt_before_retry"
    _assert_unpriced_budget_reservation(tmp_path)


def test_public_cli_mixed_vendor_fallback_separates_actual_provider_from_route_alias(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    policy = _ordered_policy()
    policy["models"][1]["model"] = "MiniMax-M3"
    providers = _ordered_provider_configs()
    providers["backup"]["base_url"] = "https://api.minimaxi.com/v1/responses"
    exit_code, output_file, session = _invoke(
        monkeypatch,
        tmp_path,
        provider_name="primary",
        provider_configs=providers,
        model_policy=policy,
        responses=[
            _http_failure(429, error_code="insufficient_quota"),
            _minimax_response(),
        ],
    )
    assert exit_code == 0
    assert session.post.call_count == 2
    output = json.loads(output_file.read_text(encoding="utf-8"))
    assert output["answers"]["IQS_05"]["score"] == 8
    assert output["provider"] == {"name": None, "requested_model": None}
    receipt = output["execution_receipts"]["IQS_05"]
    assert receipt["provider"] == "minimax"
    assert [attempt["provider"] for attempt in receipt["attempts"]] == [
        "openai",
        "minimax",
    ]
    assert [attempt["provider_config_ref"] for attempt in receipt["attempts"]] == [
        "primary",
        "backup",
    ]
    assert [attempt["route_id"] for attempt in receipt["attempts"]] == [
        "primary-route",
        "backup-route",
    ]
    assert [item["provider_config_ref"] for item in receipt["attempts"][-1]["route_trace"]] == [
        "primary",
        "backup",
    ]


@pytest.mark.parametrize(
    ("provider_name", "base_url", "model"),
    [
        ("openai", "https://api.minimaxi.com/v1/responses", "MiniMax-M3"),
        ("minimax", "https://api.openai.com/v1/responses", "gpt-4.1-mini"),
    ],
)
def test_public_cli_rejects_canonical_provider_host_mismatch_before_request(
    monkeypatch, tmp_path, provider_name, base_url, model
):
    provider_configs = {
        provider_name: {
            "enabled": True,
            "api_key": "offline-fixture-key",
            "model": model,
            "base_url": base_url,
            "max_retries": 1,
            "format_repair_budget": 0,
        }
    }
    _exit_code, output_file, session = _invoke(
        monkeypatch,
        tmp_path,
        provider_name=provider_name,
        provider_configs=provider_configs,
    )

    session.post.assert_not_called()
    output = json.loads(output_file.read_text(encoding="utf-8"))
    assert output["provider"] == {"name": None, "requested_model": None}
    receipt = output["execution_receipts"]["IQS_05"]
    assert receipt["provider"] is None
    assert receipt["search_status"] == "unavailable"
    assert receipt["attempts"] == []


def test_public_cli_accepts_low_score_without_trying_backup(monkeypatch, tmp_path):
    content = (
        '{"entity_id":"issuer:fixture","company_name":"Fixture Corp",'
        '"question_id":"IQS_05","score":2,"description":"有证据支持低分"}'
    )
    exit_code, output_file, session = _invoke(
        monkeypatch,
        tmp_path,
        provider_name="primary",
        provider_configs=_ordered_provider_configs(),
        model_policy=_ordered_policy(),
        responses=[_response(content=content)],
    )

    assert exit_code == 0
    assert session.post.call_count == 1
    output = json.loads(output_file.read_text(encoding="utf-8"))
    assert output["answers"]["IQS_05"]["score"] == 2
    assert output["execution_receipts"]["IQS_05"]["provider"] == "openai"
    assert (
        output["execution_receipts"]["IQS_05"]["attempts"][0]["route_trace"][-1]["decision"]
        == "accepted_answer"
    )


def test_public_cli_does_not_fallback_on_unrecognized_400(monkeypatch, tmp_path):
    exit_code, output_file, session = _invoke(
        monkeypatch,
        tmp_path,
        provider_name="primary",
        provider_configs=_ordered_provider_configs(),
        model_policy=_ordered_policy(),
        responses=[_http_failure(400), _response(request_id="must-not-be-used")],
        fixture_cost_resolver=False,
    )

    assert exit_code == 1
    assert session.post.call_count == 1
    output = json.loads(output_file.read_text(encoding="utf-8"))
    answer = output["answers"]["IQS_05"]
    receipt = output["execution_receipts"]["IQS_05"]
    assert answer["status"] == "error"
    assert answer["score"] is None
    assert receipt["attempts"][0]["http_status_code"] == 400
    assert receipt["dispatch_outcome"]["state"] == "uncertain"
    _assert_unpriced_budget_reservation(tmp_path)


def test_public_cli_preflights_search_capability_without_sending_to_unsupported_route(
    monkeypatch, tmp_path
):
    exit_code, output_file, session = _invoke(
        monkeypatch,
        tmp_path,
        provider_name="primary",
        provider_configs=_ordered_provider_configs(
            primary_url="https://provider.example/v1/chat/completions"
        ),
        model_policy=_ordered_policy(),
        responses=[_response(request_id="backup-only")],
    )

    assert exit_code == 0
    assert session.post.call_count == 1
    output = json.loads(output_file.read_text(encoding="utf-8"))
    receipt = output["execution_receipts"]["IQS_05"]
    assert receipt["provider"] == "openai"
    assert receipt["requested_model"] == "backup-model"
    route_trace = receipt["attempts"][0]["route_trace"]
    assert route_trace[0]["decision"] == "skipped_missing_web_search"
    assert route_trace[1]["decision"] == "accepted_answer"


def test_public_cli_emits_same_answer_schema_but_null_when_search_receipt_is_missing(
    monkeypatch, tmp_path
):
    exit_code, output_file, _ = _invoke(monkeypatch, tmp_path, searched=False)

    assert exit_code == 0
    result = json.loads(output_file.read_text(encoding="utf-8"))
    answer = result["answers"]["IQS_05"]
    receipt = result["execution_receipts"]["IQS_05"]
    assert answer["status"] == "insufficient_evidence"
    assert answer["score"] is None
    assert receipt["search_status"] == "unverified"
    assert set(answer) == {
        "question_id",
        "status",
        "score",
        "description",
        "source_urls",
        "published_date",
        "information_as_of",
        "check_level",
        "check_level_receipt_id",
    }


def test_public_cli_refuses_to_infer_question_id(monkeypatch, tmp_path):
    exit_code, output_file, session = _invoke(monkeypatch, tmp_path, with_ids=False)

    assert exit_code == 1
    assert not output_file.exists()
    session.post.assert_not_called()


def test_public_cli_repairs_one_wrong_id_and_records_each_attempt(monkeypatch, tmp_path):
    responses = [
        _response(
            content='{"entity_id":"issuer:fixture","company_name":"Fixture Corp","question_id":"IQS_06","score":9,"description":"wrong answer"}',
            request_id="req_first",
            response_id="resp_first",
            search_call_id="ws_first",
        ),
        _response(
            content='{"entity_id":"issuer:fixture","company_name":"Fixture Corp","question_id":"IQS_05","score":8,"description":"repaired answer"}',
            request_id="req_repair",
            response_id="resp_repair",
            search_call_id="ws_repair",
        ),
    ]
    exit_code, output_file, session = _invoke(
        monkeypatch, tmp_path, repair_budget=1, responses=responses
    )

    assert exit_code == 0
    output = json.loads(output_file.read_text(encoding="utf-8"))
    assert output["answers"]["IQS_05"]["score"] == 8
    receipt = output["execution_receipts"]["IQS_05"]
    assert receipt["format_repair"] == {
        "attempted": True,
        "status": "repaired",
        "failure_type": None,
    }
    assert [item["request_id"] for item in receipt["attempts"]] == [
        "req_first",
        "req_repair",
    ]
    assert session.post.call_count == 2


def test_public_cli_persists_every_failed_transport_retry_without_error_details(
    monkeypatch, tmp_path
):
    monkeypatch.setattr("src.providers.llm_provider.time.sleep", lambda _seconds: None)
    exit_code, output_file, session = _invoke(
        monkeypatch,
        tmp_path,
        max_retries=2,
        responses=[
            requests.Timeout("private provider details"),
            requests.HTTPError("private response body"),
        ],
    )

    assert exit_code == 1
    output_text = output_file.read_text(encoding="utf-8")
    assert "private provider details" not in output_text
    assert "private response body" not in output_text
    result = json.loads(output_text)
    answer = result["answers"]["IQS_05"]
    receipt = result["execution_receipts"]["IQS_05"]
    assert answer["status"] == "error"
    assert answer["score"] is None
    assert receipt["search_status"] == "unverified"
    assert receipt["failure_type"] == "HTTPError"
    assert len(receipt["attempts"]) == 2
    assert [item["failure_type"] for item in receipt["attempts"]] == [
        "Timeout",
        "HTTPError",
    ]
    assert all(item["attempt_id"] for item in receipt["attempts"])
    assert all(len(item["prompt_sha256"]) == 64 for item in receipt["attempts"])
    assert session.post.call_count == 2


def test_public_cli_repairs_wrong_company_identity_once(monkeypatch, tmp_path):
    responses = [
        _response(
            content='{"entity_id":"issuer:other","company_name":"Other Corp",'
            '"question_id":"IQS_05","score":10,"description":"wrong issuer"}',
            request_id="req_wrong_entity",
            response_id="resp_wrong_entity",
            search_call_id="ws_wrong_entity",
        ),
        _response(
            content='{"entity_id":"issuer:fixture","company_name":"Fixture Corp",'
            '"question_id":"IQS_05","score":8,"description":"correct issuer"}',
            request_id="req_entity_repair",
            response_id="resp_entity_repair",
            search_call_id="ws_entity_repair",
        ),
    ]

    exit_code, output_file, session = _invoke(
        monkeypatch, tmp_path, repair_budget=1, responses=responses
    )

    assert exit_code == 0
    result = json.loads(output_file.read_text(encoding="utf-8"))
    assert result["answers"]["IQS_05"]["score"] == 8
    assert result["execution_receipts"]["IQS_05"]["format_repair"]["status"] == "repaired"
    assert session.post.call_count == 2
    repair_prompt = session.post.call_args.kwargs["json"]["input"]
    assert "issuer:fixture" in repair_prompt
    assert "Fixture Corp" in repair_prompt


def test_public_cli_accepts_single_bound_json_after_preamble_without_retry(monkeypatch, tmp_path):
    """Q02/Q03 联合批次：厂商前导文本后的唯一完整绑定 JSON 被接受且不触发重试。"""
    wrapped = (
        'extra prose {"entity_id":"issuer:fixture","company_name":"Fixture Corp",'
        '"question_id":"IQS_05","score":8,"description":"single bound object"}'
    )
    exit_code, output_file, session = _invoke(
        monkeypatch, tmp_path, responses=[_response(content=wrapped)]
    )

    assert exit_code == 0
    result = json.loads(output_file.read_text(encoding="utf-8"))
    assert result["answers"]["IQS_05"]["status"] == "scored"
    assert result["answers"]["IQS_05"]["score"] == 8
    assert result["execution_receipts"]["IQS_05"]["format_repair"] is None
    assert session.post.call_count == 1


def test_public_cli_rejects_ambiguous_multi_json_after_preamble_without_retry(
    monkeypatch, tmp_path
):
    """严格模式对多候选 fail-closed：仍拒绝且不触发修复重试。"""
    ambiguous = (
        'extra prose {"entity_id":"issuer:fixture","company_name":"Fixture Corp",'
        '"question_id":"IQS_05","score":8,"description":"first"} '
        'and {"question_id":"IQS_05","score":2,"description":"second"}'
    )
    exit_code, output_file, session = _invoke(
        monkeypatch, tmp_path, responses=[_response(content=ambiguous)]
    )

    assert exit_code == 1
    result = json.loads(output_file.read_text(encoding="utf-8"))
    assert result["answers"]["IQS_05"]["status"] == "unknown"
    assert result["answers"]["IQS_05"]["score"] is None
    assert result["execution_receipts"]["IQS_05"]["format_repair"]["status"] == "not_enabled"
    assert session.post.call_count == 1


@pytest.mark.parametrize("invalid_status", [[], {}], ids=["array-status", "object-status"])
def test_public_cli_retains_completed_search_receipt_for_invalid_status(
    monkeypatch, tmp_path, invalid_status
):
    content = json.dumps(
        {
            "entity_id": "issuer:fixture",
            "company_name": "Fixture Corp",
            "question_id": "IQS_05",
            "status": invalid_status,
            "score": None,
            "description": "模型状态字段格式错误。",
        },
        ensure_ascii=False,
    )
    exit_code, output_file, session = _invoke(
        monkeypatch, tmp_path, responses=[_response(content=content)]
    )

    assert exit_code == 1
    result = json.loads(output_file.read_text(encoding="utf-8"))
    answer = result["answers"]["IQS_05"]
    assert answer["score"] is None
    assert answer["status"] == "unknown"
    receipt = result["execution_receipts"]["IQS_05"]
    assert receipt["request_id"] == "req_e2e_01"
    assert receipt["response_id"] == "resp_e2e_01"
    assert receipt["search_status"] == "executed"
    assert receipt["search_receipt_id"] == "ws_e2e_01"
    assert receipt["source_urls"] == ["https://example.com/issuer"]
    assert receipt["web_search_calls"] == [
        {
            "id": "ws_e2e_01",
            "status": "completed",
            "action_type": "search",
            "source_urls": ["https://example.com/issuer"],
        }
    ]
    assert receipt["format_repair"]["status"] == "not_enabled"
    assert session.post.call_count == 1


def test_public_cli_preserves_good_answer_and_returns_failure_for_unrepaired_item(
    monkeypatch, tmp_path
):
    responses = [
        _response(
            content='{"entity_id":"issuer:fixture","company_name":"Fixture Corp","question_id":"IQS_05","score":8,"description":"valid answer"}',
            request_id="req_good",
            response_id="resp_good",
            search_call_id="ws_good",
        ),
        _response(
            content='{"entity_id":"issuer:fixture","company_name":"Fixture Corp","question_id":"IQS_05","score":9,"description":"wrong item id"}',
            request_id="req_bad",
            response_id="resp_bad",
            search_call_id="ws_bad",
        ),
        _response(
            content='{"entity_id":"issuer:fixture","company_name":"Fixture Corp","question_id":"IQS_05","score":10,"description":"still wrong item id"}',
            request_id="req_failed_repair",
            response_id="resp_failed_repair",
            search_call_id="ws_failed_repair",
        ),
    ]
    exit_code, output_file, session = _invoke(
        monkeypatch,
        tmp_path,
        repair_budget=1,
        question_count=2,
        responses=responses,
    )

    assert exit_code == 1
    output = json.loads(output_file.read_text(encoding="utf-8"))
    assert output["answers"]["IQS_05"]["score"] == 8
    failed = output["answers"]["IQS_06"]
    assert failed["score"] is None
    assert failed["status"] == "unknown"
    assert output["execution_receipts"]["IQS_06"]["format_repair"]["status"] == "failed"
    assert session.post.call_count == 3


def test_public_cli_falls_back_without_blocking_on_retry_after(monkeypatch, tmp_path):
    sleeps = []
    monkeypatch.setattr("src.utils.llm_integration.time.sleep", sleeps.append)
    exit_code, output_file, session = _invoke(
        monkeypatch,
        tmp_path,
        provider_name="primary",
        provider_configs=_ordered_provider_configs(),
        model_policy=_ordered_policy(),
        responses=[
            _http_failure(429, retry_after="7", error_code="rate_limit_exceeded"),
            _response(),
        ],
    )

    assert exit_code == 0
    result = json.loads(output_file.read_text(encoding="utf-8"))
    receipt = result["execution_receipts"]["IQS_05"]
    assert result["provider"] == {"name": "openai", "requested_model": None}
    assert receipt["provider"] == "openai"
    assert receipt["requested_model"] == "backup-model"
    assert sleeps == []
    assert [attempt["http_status_code"] for attempt in receipt["attempts"]] == [
        429,
        200,
    ]
    assert receipt["attempts"][0]["provider_error_code"] == "rate_limit_exceeded"
    assert receipt["attempts"][0]["retry_after_seconds"] == 7.0
    assert session.post.call_count == 2


def test_public_cli_parses_http_date_retry_after_and_does_not_persist_raw_header(
    monkeypatch, tmp_path
):
    sleeps = []
    raw_header = "Thu, 01 Jan 2099 00:00:00 GMT"
    monkeypatch.setattr("src.utils.llm_integration.time.sleep", sleeps.append)
    exit_code, output_file, _session = _invoke(
        monkeypatch,
        tmp_path,
        provider_name="primary",
        provider_configs=_ordered_provider_configs(),
        model_policy=_ordered_policy(),
        responses=[
            _http_failure(429, retry_after=raw_header, error_code="rate_limit_exceeded"),
            _response(),
        ],
    )

    assert exit_code == 0
    output_text = output_file.read_text(encoding="utf-8")
    receipt = json.loads(output_text)["execution_receipts"]["IQS_05"]
    retry_after_seconds = receipt["attempts"][0]["retry_after_seconds"]
    assert retry_after_seconds > 0
    assert sleeps == []
    assert raw_header not in output_text


def test_public_cli_quota_exhaustion_cools_shared_group_for_current_run(monkeypatch, tmp_path):
    policy = _ordered_policy()
    alias = dict(policy["models"][0])
    alias.update(id="same-account-alias", model="primary-alt")
    policy["models"].insert(1, alias)
    backup_q6 = _response(
        question_id="IQS_06",
        request_id="backup-q6",
        response_id="resp-backup-q6",
        search_call_id="ws-backup-q6",
    )

    exit_code, output_file, session = _invoke(
        monkeypatch,
        tmp_path,
        provider_name="primary",
        question_count=2,
        provider_configs=_ordered_provider_configs(),
        model_policy=policy,
        responses=[
            _http_failure(429, error_code="insufficient_quota"),
            _response(),
            backup_q6,
        ],
    )

    assert exit_code == 0
    result = json.loads(output_file.read_text(encoding="utf-8"))
    receipt = result["execution_receipts"]["IQS_05"]
    decisions = [entry["decision"] for entry in receipt["attempts"][-1]["route_trace"]]
    assert decisions == [
        "provider_failure",
        "skipped_quota_group_cooldown",
        "accepted_answer",
    ]
    assert receipt["attempts"][0]["failure_category"] == "quota_exhausted"
    next_trace = result["execution_receipts"]["IQS_06"]["attempts"][-1]["route_trace"]
    assert [entry["decision"] for entry in next_trace] == [
        "skipped_quota_group_cooldown",
        "skipped_quota_group_cooldown",
        "accepted_answer",
    ]
    assert session.post.call_count == 3


def test_public_cli_disables_rejected_auth_route_for_later_questions(monkeypatch, tmp_path):
    backup_q5 = _response(
        request_id="backup-q5",
        response_id="resp-backup-q5",
        search_call_id="ws-backup-q5",
    )
    backup_q5.json.return_value["output"][-1]["content"][0]["text"] = (
        '{"entity_id":"issuer:fixture","company_name":"Fixture Corp",'
        '"question_id":"IQS_05","score":8,"description":"backup q5"}'
    )
    backup_q6 = _response(
        request_id="backup-q6",
        response_id="resp-backup-q6",
        search_call_id="ws-backup-q6",
    )
    backup_q6.json.return_value["output"][-1]["content"][0]["text"] = (
        '{"entity_id":"issuer:fixture","company_name":"Fixture Corp",'
        '"question_id":"IQS_06","score":7,"description":"backup q6"}'
    )
    exit_code, output_file, session = _invoke(
        monkeypatch,
        tmp_path,
        provider_name="primary",
        question_count=2,
        provider_configs=_ordered_provider_configs(),
        model_policy=_ordered_policy(),
        responses=[_http_failure(401), backup_q5, backup_q6],
    )

    assert exit_code == 0
    result = json.loads(output_file.read_text(encoding="utf-8"))
    assert result["answers"]["IQS_05"]["score"] == 8
    assert result["answers"]["IQS_06"]["score"] == 7
    route_trace = result["execution_receipts"]["IQS_06"]["attempts"][-1]["route_trace"]
    assert [entry["decision"] for entry in route_trace] == [
        "skipped_auth_disabled",
        "accepted_answer",
    ]
    assert session.post.call_count == 3


def test_public_cli_does_not_retry_uncertain_server_error_without_reconciliation(
    monkeypatch, tmp_path
):
    exit_code, output_file, session = _invoke(
        monkeypatch,
        tmp_path,
        provider_name="primary",
        provider_configs=_ordered_provider_configs(),
        model_policy=_ordered_policy(max_attempts=3),
        responses=[_http_failure(503), _http_failure(503), _response()],
        fixture_cost_resolver=False,
    )

    assert exit_code == 1
    assert session.post.call_count == 1
    result = json.loads(output_file.read_text(encoding="utf-8"))
    receipt = result["execution_receipts"]["IQS_05"]
    assert [attempt["http_status_code"] for attempt in receipt["attempts"]] == [503]
    assert receipt["attempts"][0]["requested_model"] == "primary-model"
    assert receipt["dispatch_outcome"]["state"] == "uncertain"
    assert (
        receipt["dispatch_outcome"]["uncertain_attempt_id"] == receipt["attempts"][0]["attempt_id"]
    )
    assert result["answers"]["IQS_05"]["status"] == "error"
    _assert_unpriced_budget_reservation(tmp_path)


@pytest.mark.parametrize(
    "content",
    [
        json.dumps(
            {
                "entity_id": "issuer:fixture",
                "company_name": "Fixture Corp",
                "question_id": "IQS_05",
                "score": 5,
                "description": json.dumps(
                    {
                        "id": "IQS_05",
                        "status": "insufficient_evidence",
                        "score": None,
                        "confidence": "medium",
                        "rationale": "No disclosed evidence",
                    }
                ),
            }
        ),
        json.dumps(
            {
                "entity_id": "issuer:fixture",
                "company_name": "Fixture Corp",
                "question_id": "IQS_05",
                "score": 5,
                "description": json.dumps(
                    {
                        "id": "IQS_05",
                        "status": "scored",
                        "score": 8,
                        "rationale": "inner 8",
                    }
                ),
            }
        ),
        json.dumps(
            {
                "entity_id": "issuer:fixture",
                "company_name": "Fixture Corp",
                "question_id": "IQS_05",
                "score": 8,
                "description": json.dumps(
                    {
                        "id": "IQS_06",
                        "status": "scored",
                        "score": 8,
                        "rationale": "wrong item",
                    }
                ),
            }
        ),
        (
            '{"entity_id":"issuer:fixture","company_name":"Fixture Corp",'
            '"question_id":"IQS_06","question_id":"IQS_05","score":8,'
            '"description":"ambiguous ID"}'
        ),
        (
            '{"entity_id":"issuer:fixture","company_name":"Fixture Corp",'
            '"question_id":"IQS_06","question_id":"IQS_05","score":8,'
            '"description":"ambiguous outer answer",'
            '"metadata":{"question_id":"IQS_05","score":9,"description":"not the answer"}}'
        ),
    ],
    ids=[
        "legacy-unknown-outer-five",
        "inner-outer-score-conflict",
        "inner-question-conflict",
        "duplicate-question-key",
        "duplicate-question-key-nested-metadata",
    ],
)
def test_public_cli_never_scores_ambiguous_or_nested_legacy_answers(
    monkeypatch, tmp_path, capsys, content
):
    exit_code, output_file, session = _invoke(
        monkeypatch,
        tmp_path,
        repair_budget=0,
        responses=[_response(content=content)],
    )

    assert output_file.exists()
    answer = json.loads(output_file.read_text(encoding="utf-8"))["answers"]["IQS_05"]
    assert answer["score"] is None
    assert answer["status"] != "scored"
    assert session.post.call_count == 1
    assert "[OK] 平均评分:" not in capsys.readouterr().out


def test_public_cli_minimax_search_receipt_scores_with_completed_event_and_citation(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    exit_code, output_file, session = _invoke(
        monkeypatch,
        tmp_path,
        provider_name="minimax",
        provider_configs=_minimax_provider_config(),
        responses=[_minimax_response()],
    )
    assert exit_code == 0
    assert session.post.call_count == 1
    result = json.loads(output_file.read_text(encoding="utf-8"))
    assert result["provider"]["name"] == "minimax"
    assert result["answers"]["IQS_05"]["score"] == 8
    receipt = result["execution_receipts"]["IQS_05"]
    assert receipt["provider"] == "minimax"
    assert receipt["requested_model"] == "MiniMax-M3"
    assert receipt["actual_model"] == "MiniMax-M3"
    assert receipt["search_status"] == "executed"
    assert receipt["response_id"] == "resp_e2e_01"
    assert receipt["attempt_id"]
    assert receipt["search_receipt_id"] == "ws_e2e_01"
    assert receipt["source_urls"] == ["https://example.com/issuer"]
    assert session.post.call_args.args[0] == "https://api.minimaxi.com/v1/responses"


def test_public_cli_minimax_anthropic_search_receipt_scores_with_correlated_server_result(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    endpoint = "https://api.minimaxi.com/anthropic/v1/messages"
    exit_code, output_file, session = _invoke(
        monkeypatch,
        tmp_path,
        provider_name="minimax",
        provider_configs=_minimax_provider_config(base_url=endpoint),
        responses=[_minimax_anthropic_response()],
    )

    assert exit_code == 0
    assert session.post.call_count == 1
    result = json.loads(output_file.read_text(encoding="utf-8"))
    assert result["answers"]["IQS_05"]["score"] == 8
    receipt = result["execution_receipts"]["IQS_05"]
    assert receipt["provider"] == "minimax"
    assert receipt["requested_model"] == "MiniMax-M3"
    assert receipt["actual_model"] == "MiniMax-M3"
    assert receipt["request_id"] is None
    assert receipt["response_id"] == "msg_mm_cli_01"
    assert receipt["attempt_id"]
    assert receipt["search_status"] == "executed"
    assert receipt["search_receipt_id"] == "srvtoolu_cli_01"
    assert receipt["source_urls"] == ["https://example.com/issuer"]
    call = session.post.call_args
    assert call.args[0] == endpoint
    assert call.kwargs["headers"]["x-api-key"] == "offline-fixture-key"
    assert call.kwargs["headers"]["anthropic-version"] == "2023-06-01"
    assert call.kwargs["json"]["tools"][0]["max_uses"] == 1
    # Official Messages API ToolChoice: only auto/none (Q02 conformance).
    assert call.kwargs["json"]["tool_choice"] == {"type": "auto"}


def test_public_cli_minimax_anthropic_refuses_score_without_bound_search_result(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    response = _minimax_anthropic_response()
    response.json.return_value["content"][1]["tool_use_id"] = "foreign_tool_call"

    exit_code, output_file, session = _invoke(
        monkeypatch,
        tmp_path,
        provider_name="minimax",
        provider_configs=_minimax_provider_config(
            base_url="https://api.minimaxi.com/anthropic/v1/messages"
        ),
        responses=[response],
    )

    assert exit_code == 0
    result = json.loads(output_file.read_text(encoding="utf-8"))
    answer = result["answers"]["IQS_05"]
    receipt = result["execution_receipts"]["IQS_05"]
    assert answer["score"] is None
    assert receipt["search_status"] == "unverified"
    assert receipt["search_receipt_id"] is None
    assert receipt["source_urls"] == []
    assert session.post.call_count == 1


@pytest.mark.parametrize(
    "searched,cited,actual_model",
    [
        (False, True, "MiniMax-M3"),
        (True, False, "MiniMax-M3"),
        (True, True, "wrong-model"),
    ],
)
def test_public_cli_minimax_refuses_score_without_correlated_search_receipt(
    monkeypatch, tmp_path, searched, cited, actual_model
):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    exit_code, output_file, session = _invoke(
        monkeypatch,
        tmp_path,
        provider_name="minimax",
        provider_configs=_minimax_provider_config(),
        responses=[_minimax_response(searched=searched, cited=cited, actual_model=actual_model)],
    )
    assert exit_code == 0
    result = json.loads(output_file.read_text(encoding="utf-8"))
    assert result["answers"]["IQS_05"]["score"] is None
    assert result["execution_receipts"]["IQS_05"]["search_status"] == "unverified"
    assert session.post.call_count == 1


@pytest.mark.parametrize(
    "base_url,model",
    [
        ("https://api.minimax.cn/v1/chat/completions", "MiniMax-M3"),
        ("https://api.minimaxi.com/v1/responses", "MiniMax-M2.7"),
    ],
)
def test_public_cli_minimax_wrong_host_or_model_is_preflight_unavailable(
    monkeypatch, tmp_path, base_url, model
):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    exit_code, output_file, session = _invoke(
        monkeypatch,
        tmp_path,
        provider_name="minimax",
        provider_configs=_minimax_provider_config(base_url=base_url, model=model),
    )
    assert exit_code == 0
    assert session.post.call_count == 0
    result = json.loads(output_file.read_text(encoding="utf-8"))
    assert result["answers"]["IQS_05"]["score"] is None
    assert result["execution_receipts"]["IQS_05"]["search_status"] == "unavailable"


@pytest.mark.parametrize("http_status", [400, 401, 429])
def test_public_cli_minimax_http_failure_never_scores(monkeypatch, tmp_path, http_status):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    exit_code, output_file, session = _invoke(
        monkeypatch,
        tmp_path,
        provider_name="minimax",
        provider_configs=_minimax_provider_config(),
        responses=[_http_failure(http_status)],
    )
    assert exit_code == 1
    assert session.post.call_count == 1
    result = json.loads(output_file.read_text(encoding="utf-8"))
    assert result["answers"]["IQS_05"]["score"] is None
    attempt = result["execution_receipts"]["IQS_05"]["attempts"][0]
    assert attempt["provider"] == "minimax"
    assert attempt["http_status_code"] == http_status


def _mimo_response(*, cited=True, actual_model="mimo-v2.6-flash"):
    response = Mock()
    response.headers = {"x-request-id": "req_mimo_cli_01"}
    response.status_code = 200
    annotations = (
        [
            {
                "type": "url_citation",
                "url": "https://example.com/issuer",
                "title": "Issuer source",
            }
        ]
        if cited
        else []
    )
    response.json.return_value = {
        "id": "chatcmpl_mimo_cli_01",
        "object": "chat.completion",
        "model": actual_model,
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": (
                        '{"entity_id":"issuer:fixture","company_name":"Fixture Corp",'
                        '"question_id":"IQS_05","score":8,"description":"基于MiMo联网引用。"}'
                    ),
                    "annotations": annotations,
                    "tool_calls": None,
                },
            }
        ],
    }
    return response


def test_public_cli_supports_mimo_web_search_and_exports_response_citation_receipt(
    monkeypatch, tmp_path
):
    provider_configs = {
        "mimo": {
            "enabled": True,
            "api_key": "offline-mimo-key",
            "model": "mimo-v2.6-flash",
            "base_url": "https://api.xiaomimimo.com/v1",
            "max_retries": 1,
            "format_repair_budget": 0,
        }
    }
    exit_code, output_file, session = _invoke(
        monkeypatch,
        tmp_path,
        provider_name="mimo",
        provider_configs=provider_configs,
        responses=[_mimo_response()],
    )

    assert exit_code == 0
    output = json.loads(output_file.read_text(encoding="utf-8"))
    receipt = output["execution_receipts"]["IQS_05"]
    assert output["provider"] == {"name": "mimo", "requested_model": "mimo-v2.6-flash"}
    assert output["answers"]["IQS_05"]["score"] == 8
    assert receipt["provider"] == "mimo"
    assert receipt["actual_model"] == "mimo-v2.6-flash"
    assert receipt["search_status"] == "executed"
    assert receipt["search_receipt_id"] == "chatcmpl_mimo_cli_01"
    assert receipt["source_urls"] == ["https://example.com/issuer"]
    assert receipt["web_search_calls"][0]["evidence_basis"] == "url_citation_annotations"
    request = session.post.call_args
    assert request.args[0] == "https://api.xiaomimimo.com/v1/chat/completions"
    assert request.kwargs["json"]["tools"][0]["force_search"] is True
    assert (
        "Target stable entity_id: issuer:fixture"
        in request.kwargs["json"]["messages"][1]["content"]
    )
    assert "issuer:fixture" in request.kwargs["json"]["messages"][1]["content"]


def test_public_cli_rejects_mimo_answer_without_citations(monkeypatch, tmp_path):
    provider_configs = {
        "mimo": {
            "enabled": True,
            "api_key": "offline-mimo-key",
            "model": "mimo-v2.6-flash",
            "base_url": "https://api.xiaomimimo.com/v1",
            "max_retries": 1,
            "format_repair_budget": 0,
        }
    }
    exit_code, output_file, session = _invoke(
        monkeypatch,
        tmp_path,
        provider_name="mimo",
        provider_configs=provider_configs,
        responses=[_mimo_response(cited=False)],
    )

    assert exit_code == 0
    output = json.loads(output_file.read_text(encoding="utf-8"))
    answer = output["answers"]["IQS_05"]
    receipt = output["execution_receipts"]["IQS_05"]
    assert session.post.call_count == 1
    assert answer["status"] == "insufficient_evidence"
    assert answer["score"] is None
    assert receipt["search_status"] == "unverified"
    assert receipt["source_urls"] == []


def test_public_cli_next_run_policy_keeps_startup_order_for_remaining_questions(
    monkeypatch, tmp_path
):
    """A saved B>A policy without immediate mode leaves the current run untouched."""
    policy = _ordered_policy()
    import json as jsonlib

    swapped_bodies = {"done": False}

    def post(_url, *, json, **_kwargs):
        payload_input = json["input"] if isinstance(json.get("input"), str) else ""
        question_id = "IQS_06" if "IQS_06" in payload_input else "IQS_05"
        model = json["model"]
        if model == "primary-model" and not swapped_bodies["done"]:
            swapped_bodies["done"] = True
            raw = jsonlib.loads((tmp_path / "llm_apis.json").read_text(encoding="utf-8"))
            swapped = dict(policy)
            swapped["models"] = [
                {
                    "id": "backup-route",
                    "enabled": True,
                    "provider_config_ref": "backup",
                    "model": "backup-model",
                    "quota_group": "account-backup",
                    "max_in_flight": 1,
                },
                {
                    "id": "primary-route",
                    "enabled": True,
                    "provider_config_ref": "primary",
                    "model": "primary-model",
                    "quota_group": "account-primary",
                    "max_in_flight": 1,
                },
            ]
            raw["quick_scan_model_policy"] = swapped
            (tmp_path / "llm_apis.json").write_text(jsonlib.dumps(raw), encoding="utf-8")
        return _response(request_id=f"req-next-run-{model}", question_id=question_id)

    _exit_code, output_file, session = _invoke(
        monkeypatch,
        tmp_path,
        provider_name="primary",
        provider_configs=_ordered_provider_configs(),
        model_policy=policy,
        question_count=2,
        responses=post,
    )

    assert _exit_code == 0
    requested_models = [call.kwargs["json"]["model"] for call in session.post.call_args_list]
    assert requested_models == ["primary-model", "primary-model"]
    result = json.loads(output_file.read_text(encoding="utf-8"))
    receipts = result["execution_receipts"]
    versions = {
        receipts["IQS_05"]["attempts"][-1]["policy_version"],
        receipts["IQS_06"]["attempts"][-1]["policy_version"],
    }
    assert len(versions) == 1
    assert all(
        "policy_transition" not in attempt
        for receipt in receipts.values()
        for attempt in receipt["attempts"]
    )


def test_public_cli_immediate_policy_swaps_at_next_undispatched_boundary(monkeypatch, tmp_path):
    """Explicit immediate mode adopts B>A at the next question boundary only."""
    policy = _ordered_policy()
    import json as jsonlib

    swapped_bodies = {"done": False}

    def post(_url, *, json, **_kwargs):
        payload_input = json["input"] if isinstance(json.get("input"), str) else ""
        question_id = "IQS_06" if "IQS_06" in payload_input else "IQS_05"
        model = json["model"]
        if model == "primary-model" and not swapped_bodies["done"]:
            swapped_bodies["done"] = True
            raw = jsonlib.loads((tmp_path / "llm_apis.json").read_text(encoding="utf-8"))
            swapped = dict(policy)
            swapped["models"] = [
                {
                    "id": "backup-route",
                    "enabled": True,
                    "provider_config_ref": "backup",
                    "model": "backup-model",
                    "quota_group": "account-backup",
                    "max_in_flight": 1,
                },
                {
                    "id": "primary-route",
                    "enabled": True,
                    "provider_config_ref": "primary",
                    "model": "primary-model",
                    "quota_group": "account-primary",
                    "max_in_flight": 1,
                },
            ]
            raw["quick_scan_model_policy"] = swapped
            raw["quick_scan_model_policy_effective_mode"] = "immediate"
            (tmp_path / "llm_apis.json").write_text(jsonlib.dumps(raw), encoding="utf-8")
        return _response(request_id=f"req-immediate-{model}", question_id=question_id)

    _exit_code, output_file, session = _invoke(
        monkeypatch,
        tmp_path,
        provider_name="primary",
        provider_configs=_ordered_provider_configs(),
        model_policy=policy,
        question_count=2,
        responses=post,
    )

    requested_models = [call.kwargs["json"]["model"] for call in session.post.call_args_list]
    assert requested_models == ["primary-model", "backup-model"]
    result = json.loads(output_file.read_text(encoding="utf-8"))
    receipts = result["execution_receipts"]
    first_version = receipts["IQS_05"]["attempts"][-1]["policy_version"]
    second_version = receipts["IQS_06"]["attempts"][-1]["policy_version"]
    assert first_version != second_version
    assert second_version.startswith("quick-scan-test-policy@")
    assert receipts["IQS_06"]["attempts"][-1]["policy_transition"]["at_boundary"] is True
