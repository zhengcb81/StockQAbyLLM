"""Provider-level tests for search execution evidence and score propagation."""

import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from src.core.models import Question
from src.providers.async_llm_provider import AsyncLLMProvider
from src.providers.llm_client import LLMSearchResponse, LLMTransportAttemptError
from src.providers.llm_provider import LLMProvider


def _config(tmp_path, base_url="https://api.openai.com/v1/chat/completions", repair_budget=0):
    path = tmp_path / "offline-provider-config.json"
    path.write_text(
        json.dumps(
            {
                "default_provider": "openai",
                "providers": {
                    "openai": {
                        "enabled": True,
                        "model": "fixture-model",
                        "base_url": base_url,
                        "api_key": "",
                        "format_repair_budget": repair_budget,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def _search_response(content, *, verified=True):
    metadata = {
        "provider": "openai",
        "search_status": "executed" if verified else "unverified",
        "response_status": "completed",
        "started_at": "2026-09-24T00:00:00Z",
        "completed_at": "2026-09-24T00:00:01Z",
        "attempt_id": "attempt_fixture_01",
        "prompt_sha256": "a" * 64,
        "search_receipt_id": "ws_fixture_01" if verified else None,
        "request_id": "req_fixture_01" if verified else None,
        "response_id": "resp_fixture_01",
        "actual_model": "fixture-model-2026-09",
        "web_search_calls": [
            {
                "id": "ws_fixture_01" if verified else None,
                "status": "completed" if verified else "incomplete",
                "action_type": "search",
                "source_urls": ["https://example.com/source"] if verified else [],
            }
        ],
        "source_urls": ["https://example.com/source"] if verified else [],
        "attempts": [
            {
                "provider": "openai",
                "request_id": "req_fixture_01" if verified else None,
                "response_id": "resp_fixture_01",
                "actual_model": "fixture-model-2026-09",
                "search_status": "executed" if verified else "unverified",
                "response_status": "completed",
                "started_at": "2026-09-24T00:00:00Z",
                "completed_at": "2026-09-24T00:00:01Z",
                "attempt_id": "attempt_fixture_01",
                "prompt_sha256": "a" * 64,
                "search_receipt_id": "ws_fixture_01" if verified else None,
                "source_urls": ["https://example.com/source"] if verified else [],
                "web_search_calls": [],
            }
        ],
    }
    return LLMSearchResponse(
        content=content,
        request_id=metadata["request_id"],
        response_id=metadata["response_id"],
        actual_model=metadata["actual_model"],
        source_urls=tuple(metadata["source_urls"]),
        execution_metadata=metadata,
    )


def test_sync_provider_keeps_score_eight_and_search_receipt(tmp_path):
    provider = LLMProvider(
        provider_name="openai",
        api_key="offline-fixture-key",
        model="fixture-model",
        config_file=_config(tmp_path),
        require_search=True,
    )
    provider.client.send_search_request = Mock(
        return_value=_search_response('{"score":8,"description":"有公开来源"}')
    )

    result = provider.search("公司竞争优势")[0]

    assert result.score == 8
    assert result.status == "scored"
    assert result.metadata["search_status"] == "executed"
    assert result.metadata["request_id"] == "req_fixture_01"
    assert result.metadata["source_urls"] == ["https://example.com/source"]


def test_sync_provider_repairs_wrong_question_id_once_and_keeps_both_receipts(tmp_path):
    provider = LLMProvider(
        provider_name="openai",
        api_key="offline-fixture-key",
        model="fixture-model",
        config_file=_config(tmp_path, repair_budget=1),
        require_search=True,
    )
    provider.max_retries = 1
    provider.client.send_search_request = Mock(
        side_effect=[
            _search_response('{"question_id":"IQS_06","score":9,"description":"wrong item"}'),
            _search_response('{"question_id":"IQS_05","score":8,"description":"repaired item"}'),
        ]
    )

    result = provider.search_question(Question("Issuer quality", question_id="IQS_05"))[0]

    assert result.score == 8
    assert result.status == "scored"
    assert result.metadata["format_repair"]["status"] == "repaired"
    assert [item["request_id"] for item in result.metadata["attempts"]] == [
        "req_fixture_01",
        "req_fixture_01",
    ]
    assert provider.client.send_search_request.call_count == 2
    assert "IQS_05" in provider.client.send_search_request.call_args_list[1].args[0]


def test_sync_provider_failed_single_repair_remains_unscored_error(tmp_path):
    provider = LLMProvider(
        provider_name="openai",
        api_key="offline-fixture-key",
        model="fixture-model",
        config_file=_config(tmp_path, repair_budget=1),
        require_search=True,
    )
    provider.max_retries = 1
    provider.client.send_search_request = Mock(
        side_effect=[
            _search_response('{"question_id":"IQS_06","score":9,"description":"bad"}'),
            _search_response('{"question_id":"IQS_06","score":10,"description":"still bad"}'),
        ]
    )

    result = provider.search_question(Question("Issuer quality", question_id="IQS_05"))[0]

    assert result.score is None
    assert result.status == "unknown"
    assert result.metadata["format_repair"]["status"] == "failed"
    assert provider.client.send_search_request.call_count == 2


def test_sync_quick_scan_does_not_repeat_invalid_successful_response_without_repair_budget(
    tmp_path,
):
    provider = LLMProvider(
        provider_name="openai",
        api_key="offline-fixture-key",
        model="fixture-model",
        config_file=_config(tmp_path, repair_budget=0),
        require_search=True,
        entity_id="issuer:target",
        company_name="Target Corp",
    )
    provider.max_retries = 3
    provider.client.send_search_request = Mock(
        return_value=_search_response(
            'prefix {"entity_id":"issuer:target","company_name":"Target Corp",'
            '"question_id":"IQS_05","score":8,"description":"would pass regex"}'
        )
    )

    result = provider.search_question(Question("Quality", question_id="IQS_05"))[0]

    assert result.status == "unknown"
    assert result.score is None
    assert result.metadata["format_repair"]["status"] == "not_enabled"
    assert provider.client.send_search_request.call_count == 1


def test_sync_quick_scan_rejects_wrong_issuer_identity(tmp_path):
    provider = LLMProvider(
        provider_name="openai",
        api_key="offline-fixture-key",
        model="fixture-model",
        config_file=_config(tmp_path),
        require_search=True,
        entity_id="issuer:target",
        company_name="Target Corp",
    )
    provider.client.send_search_request = Mock(
        return_value=_search_response(
            '{"entity_id":"issuer:other","company_name":"Other Corp",'
            '"question_id":"IQS_05","score":10,"description":"other issuer"}'
        )
    )

    result = provider.search_question(Question("Quality", question_id="IQS_05"))[0]

    assert result.score is None
    assert result.status == "unknown"
    assert result.metadata["format_repair"]["failure_type"] == "invalid_answer"
    prompt = provider.client.send_search_request.call_args.args[0]
    assert "issuer:target" in prompt
    assert "Target Corp" in prompt


def test_sync_provider_preserves_failed_transport_attempt_before_success(tmp_path):
    provider = LLMProvider(
        provider_name="openai",
        api_key="offline-fixture-key",
        model="fixture-model",
        config_file=_config(tmp_path),
        require_search=True,
    )
    provider.max_retries = 2
    provider.retry_strategy.get_wait_time = Mock(return_value=0)
    failed_attempt = {
        "provider": "openai",
        "request_id": "req_timeout",
        "response_id": None,
        "search_status": "unverified",
        "response_status": None,
        "http_status_code": None,
        "started_at": "2026-09-24T00:00:00Z",
        "completed_at": "2026-09-24T00:00:01Z",
        "attempt_id": "attempt_timeout",
        "prompt_sha256": "b" * 64,
        "search_receipt_id": None,
        "source_urls": [],
        "web_search_calls": [],
        "failure_type": "Timeout",
    }
    provider.client.send_search_request = Mock(
        side_effect=[
            LLMTransportAttemptError("Timeout", failed_attempt),
            _search_response('{"score":8,"description":"recovered by retry"}'),
        ]
    )

    result = provider.search("company quality")[0]

    assert result.score == 8
    assert [item["attempt_id"] for item in result.metadata["attempts"]] == [
        "attempt_timeout",
        "attempt_fixture_01",
    ]
    assert result.metadata["attempts"][0]["failure_type"] == "Timeout"
    assert provider.client.send_search_request.call_count == 2


def test_sync_provider_exhausted_transport_attempts_reach_result_metadata(tmp_path):
    provider = LLMProvider(
        provider_name="openai",
        api_key="offline-fixture-key",
        model="fixture-model",
        config_file=_config(tmp_path),
        require_search=True,
    )
    provider.max_retries = 2
    provider.retry_strategy.get_wait_time = Mock(return_value=0)
    failed_attempts = [
        {
            "attempt_id": f"attempt_{index}",
            "failure_type": "Timeout",
            "request_id": None,
            "started_at": f"2026-09-24T00:00:0{index}Z",
            "completed_at": f"2026-09-24T00:00:1{index}Z",
            "prompt_sha256": "c" * 64,
            "search_status": "unverified",
            "source_urls": [],
            "web_search_calls": [],
        }
        for index in (1, 2)
    ]
    provider.client.send_search_request = Mock(
        side_effect=[
            LLMTransportAttemptError("Timeout", failed_attempts[0]),
            LLMTransportAttemptError("Timeout", failed_attempts[1]),
        ]
    )

    result = provider.search_question(Question("Quality", question_id="IQS_05"))[0]

    assert result.score is None
    assert result.status == "error"
    assert [item["attempt_id"] for item in result.metadata["attempts"]] == [
        "attempt_1",
        "attempt_2",
    ]
    assert provider.client.send_search_request.call_count == 2


def test_sync_provider_discards_score_without_search_execution_receipt(tmp_path):
    provider = LLMProvider(
        provider_name="openai",
        api_key="offline-fixture-key",
        model="fixture-model",
        config_file=_config(tmp_path),
        require_search=True,
    )
    provider.client.send_search_request = Mock(
        return_value=_search_response('{"score":10,"description":"模型自称已搜索"}', verified=False)
    )

    result = provider.search("公司竞争优势")[0]

    assert result.score is None
    assert result.status == "insufficient_evidence"
    assert result.metadata["search_status"] == "unverified"


def test_sync_provider_sanitizes_unexpected_transport_error_details(tmp_path):
    provider = LLMProvider(
        provider_name="openai",
        api_key="offline-fixture-key",
        model="fixture-model",
        config_file=_config(tmp_path),
    )
    provider.max_retries = 1
    provider.client.send_request = Mock(side_effect=RuntimeError("secret response body"))

    result = provider.search("Issuer quality")[0]

    assert result.score is None
    assert result.status == "error"
    assert "secret response body" not in result.snippet
    assert "RuntimeError" in result.snippet


def test_sync_provider_skips_unsupported_search_capability_without_sending(tmp_path):
    provider = LLMProvider(
        provider_name="deepseek",
        api_key="offline-fixture-key",
        model="fixture-model",
        config_file=_config(tmp_path, "https://api.deepseek.com/v1/chat/completions"),
        require_search=True,
    )
    provider.client.send_search_request = Mock()

    result = provider.search("公司竞争优势")[0]

    assert result.score is None
    assert result.status == "insufficient_evidence"
    assert result.metadata["search_status"] == "unavailable"
    provider.client.send_search_request.assert_not_called()


def test_sync_provider_binds_response_to_explicit_question_id(tmp_path):
    provider = LLMProvider(
        provider_name="openai",
        api_key="offline-fixture-key",
        model="fixture-model",
        config_file=_config(tmp_path),
        require_search=True,
    )
    provider.max_retries = 1
    provider.client.send_search_request = Mock(
        return_value=_search_response('{"question_id":"IQS_05","score":8,"description":"同题回答"}')
    )

    result = provider.search_question(Question("公司竞争优势", question_id="IQS_05"))[0]

    assert result.score == 8
    assert result.metadata["question_id"] == "IQS_05"
    assert '"question_id": "IQS_05"' in provider.client.send_search_request.call_args.args[0]


def test_sync_provider_drops_answer_with_wrong_question_id(tmp_path):
    provider = LLMProvider(
        provider_name="openai",
        api_key="offline-fixture-key",
        model="fixture-model",
        config_file=_config(tmp_path),
        require_search=True,
    )
    provider.max_retries = 1
    provider.client.send_search_request = Mock(
        return_value=_search_response(
            '{"question_id":"IQS_06","score":10,"description":"错题高分"}'
        )
    )

    result = provider.search_question(Question("公司竞争优势", question_id="IQS_05"))[0]

    assert result.score is None
    assert result.status == "unknown"


@pytest.mark.asyncio
async def test_async_quick_scan_rejects_wrong_issuer_identity(tmp_path):
    provider = AsyncLLMProvider(
        provider_name="openai",
        api_key="offline-fixture-key",
        model="fixture-model",
        config_file=_config(tmp_path),
        require_search=True,
        entity_id="issuer:target",
        company_name="Target Corp",
    )
    provider.client.send_search_request_async = AsyncMock(
        return_value=_search_response(
            '{"entity_id":"issuer:other","company_name":"Other Corp",'
            '"question_id":"IQS_05","score":10,"description":"other issuer"}'
        )
    )

    result = await provider.search_question_async(Question("Quality", question_id="IQS_05"))

    assert result[0].score is None
    assert result[0].status == "unknown"
    assert provider.client.send_search_request_async.call_count == 1


@pytest.mark.asyncio
async def test_async_provider_preserves_failed_transport_attempt_before_success(tmp_path):
    provider = AsyncLLMProvider(
        provider_name="openai",
        api_key="offline-fixture-key",
        model="fixture-model",
        config_file=_config(tmp_path),
        require_search=True,
    )
    provider.max_retries = 2
    provider.retry_strategy.get_wait_time = Mock(return_value=0)
    failed_attempt = {
        "provider": "openai",
        "request_id": "req_async_timeout",
        "response_id": None,
        "search_status": "unverified",
        "response_status": None,
        "http_status_code": None,
        "started_at": "2026-09-24T00:00:00Z",
        "completed_at": "2026-09-24T00:00:01Z",
        "attempt_id": "attempt_async_timeout",
        "prompt_sha256": "d" * 64,
        "search_receipt_id": None,
        "source_urls": [],
        "web_search_calls": [],
        "failure_type": "TimeoutException",
    }
    provider.client.send_search_request_async = AsyncMock(
        side_effect=[
            LLMTransportAttemptError("TimeoutException", failed_attempt),
            _search_response(
                '{"question_id":"IQS_05","score":8,"description":"async retry recovered"}'
            ),
        ]
    )

    result = await provider.search_question_async(Question("Quality", question_id="IQS_05"))

    assert result[0].score == 8
    assert [item["attempt_id"] for item in result[0].metadata["attempts"]] == [
        "attempt_async_timeout",
        "attempt_fixture_01",
    ]
    assert result[0].metadata["attempts"][0]["failure_type"] == "TimeoutException"
    assert provider.client.send_search_request_async.call_count == 2


@pytest.mark.asyncio
async def test_async_transport_error_does_not_leak_provider_error_details(tmp_path, caplog):
    provider = AsyncLLMProvider(
        provider_name="openai",
        api_key="offline-fixture-key",
        model="fixture-model",
        config_file=_config(tmp_path),
        require_search=True,
        entity_id="issuer:target",
        company_name="Target Corp",
    )
    request = httpx.Request("POST", "https://api.openai.com/v1/responses?secret=token")
    provider.client.send_search_request_async = AsyncMock(
        side_effect=httpx.RequestError("private upstream body", request=request)
    )
    provider.max_retries = 1

    with pytest.raises(Exception) as raised:
        await provider.search_question_async(Question("Quality", question_id="IQS_05"))

    assert "private upstream body" not in str(raised.value)
    assert "secret=token" not in str(raised.value)
    assert "private upstream body" not in caplog.text
    assert "secret=token" not in caplog.text


@pytest.mark.asyncio
async def test_async_provider_uses_same_parser_and_search_receipt(tmp_path):
    provider = AsyncLLMProvider(
        provider_name="openai",
        api_key="offline-fixture-key",
        model="fixture-model",
        config_file=_config(tmp_path),
        require_search=True,
    )
    provider.client.send_search_request_async = AsyncMock(
        return_value=_search_response('{"score":8,"description":"有公开来源"}')
    )

    result = (await provider.search_async("公司竞争优势"))[0]

    assert result.score == 8
    assert result.status == "scored"
    assert result.metadata["search_status"] == "executed"
    assert result.metadata["request_id"] == "req_fixture_01"


@pytest.mark.asyncio
async def test_async_provider_uses_evidenced_provider_for_configured_alias(tmp_path):
    config = tmp_path / "alias-provider-config.json"
    config.write_text(
        json.dumps(
            {
                "default_provider": "backup",
                "providers": {
                    "backup": {
                        "enabled": True,
                        "model": "mimo-v2.6-flash",
                        "base_url": "https://api.xiaomimimo.com/v1",
                        "api_key": "",
                        "format_repair_budget": 0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    provider = AsyncLLMProvider(
        provider_name="backup",
        api_key="offline-fixture-key",
        model="mimo-v2.6-flash",
        config_file=str(config),
        require_search=True,
    )
    response = _search_response('{"score":8,"description":"MiMo source"}')
    response.execution_metadata["provider"] = "mimo"
    response.execution_metadata["actual_model"] = "mimo-v2.6-flash"
    response.execution_metadata["attempts"][0]["provider"] = "mimo"
    provider.client.send_search_request_async = AsyncMock(return_value=response)

    result = (await provider.search_async("公司竞争优势"))[0]

    assert result.metadata["provider"] == "mimo"
    assert result.metadata["provider_config_ref"] == "backup"
    assert result.metadata["execution"]["provider"] == "mimo"
    assert result.metadata["execution"]["provider_config_ref"] == "backup"
    assert result.metadata["attempts"][0]["provider"] == "mimo"
    assert result.metadata["attempts"][0]["provider_config_ref"] == "backup"


@pytest.mark.asyncio
async def test_async_provider_rejects_unverified_search_and_keeps_null_score(tmp_path):
    provider = AsyncLLMProvider(
        provider_name="openai",
        api_key="offline-fixture-key",
        model="fixture-model",
        config_file=_config(tmp_path),
        require_search=True,
    )
    provider.client.send_search_request_async = AsyncMock(
        return_value=_search_response('{"score":10,"description":"模型自称已搜索"}', verified=False)
    )

    result = (await provider.search_async("公司竞争优势"))[0]

    assert result.score is None
    assert result.status == "insufficient_evidence"


@pytest.mark.asyncio
async def test_async_provider_repairs_invalid_response_once(tmp_path):
    provider = AsyncLLMProvider(
        provider_name="openai",
        api_key="offline-fixture-key",
        model="fixture-model",
        config_file=_config(tmp_path, repair_budget=1),
        require_search=True,
    )
    provider.max_retries = 1
    provider.client.send_search_request_async = AsyncMock(
        side_effect=[
            _search_response('{"question_id":"IQS_06","score":9,"description":"bad"}'),
            _search_response('{"question_id":"IQS_05","score":8,"description":"fixed"}'),
        ]
    )

    result = (
        await provider.search_question_async(Question("Issuer quality", question_id="IQS_05"))
    )[0]

    assert result.score == 8
    assert result.status == "scored"
    assert result.metadata["format_repair"]["status"] == "repaired"
    assert provider.client.send_search_request_async.call_count == 2


@pytest.mark.asyncio
async def test_async_provider_binds_response_to_explicit_question_id(tmp_path):
    provider = AsyncLLMProvider(
        provider_name="openai",
        api_key="offline-fixture-key",
        model="fixture-model",
        config_file=_config(tmp_path),
        require_search=True,
    )
    provider.max_retries = 1
    provider.client.send_search_request_async = AsyncMock(
        return_value=_search_response('{"question_id":"IQS_05","score":8,"description":"同题回答"}')
    )

    result = await provider.search_question_async(Question("公司竞争优势", question_id="IQS_05"))

    assert result[0].score == 8
    assert result[0].metadata["question_id"] == "IQS_05"
