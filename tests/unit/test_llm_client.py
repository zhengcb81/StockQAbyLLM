#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试 LLM 客户端。"""

import asyncio
import io
import json
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
import requests
from urllib3.response import HTTPResponse


def _q10_response(model):
    response = Mock(status_code=200)
    response.headers = {"x-request-id": "q10-request"}
    response.json.return_value = {
        "id": "q10-response",
        "status": "completed",
        "model": model,
        "output": [
            {
                "type": "web_search_call",
                "id": "q10-search",
                "status": "completed",
                "action": {"type": "search", "sources": [{"url": "https://example.org/source"}]},
            },
            {
                "type": "message",
                "status": "completed",
                "content": [{"type": "output_text", "text": "synthetic answer"}],
            },
        ],
    }
    return response


def test_q10_unregistered_openai_model_does_not_verify(monkeypatch):
    session = Mock()
    session.post.return_value = _q10_response("fixture-resolved")
    monkeypatch.setattr(
        "src.providers.llm_client.http_client_manager.get_sync_session", lambda: session
    )
    result = LLMClient(
        "synthetic", "fixture-requested", "https://api.openai.com/v1/responses"
    ).send_search_request("synthetic")
    assert result.actual_model == "fixture-resolved"
    assert result.search_verified is False


def test_q10_registered_openai_alias_preserves_both_models(monkeypatch):
    session = Mock()
    session.post.return_value = _q10_response("fixture-resolved")
    monkeypatch.setattr(
        "src.providers.llm_client.http_client_manager.get_sync_session", lambda: session
    )
    policy = {
        "schema_version": "1.0.0",
        "aliases": [
            {
                "provider": "openai",
                "protocol": "responses",
                "requested_model": "fixture-requested",
                "resolved_model": "fixture-resolved",
            }
        ],
    }
    client = LLMClient(
        "synthetic",
        "fixture-requested",
        "https://api.openai.com/v1/responses",
        model_resolution=policy,
    )
    result = client.send_search_request("synthetic")
    assert result.search_verified is True
    assert session.post.call_args.kwargs["json"]["model"] == "fixture-requested"
    assert result.execution_metadata["actual_model"] == "fixture-resolved"
    assert len(result.execution_metadata["response_sha256"]) == 64


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "provider,protocol,requested,url",
    [
        ("openai", "responses", "synthetic-requested", "https://api.openai.com/v1/responses"),
        ("minimax", "responses", "MiniMax-M3", "https://api.minimax.io/v1/responses"),
        (
            "minimax",
            "anthropic_messages",
            "MiniMax-M3",
            "https://api.minimax.io/anthropic/v1/messages",
        ),
        (
            "mimo",
            "mimo_chat_completions",
            "mimo-v2.6-flash",
            "https://api.xiaomimimo.com/v1/chat/completions",
        ),
    ],
)
@pytest.mark.parametrize("case", ["exact", "registered", "unregistered", "wrong_scope"])
def test_q10_native_protocols_share_exact_or_registered_resolution(
    monkeypatch, asynchronous, provider, protocol, requested, url, case
):
    resolved = requested if case == "exact" else "synthetic-resolved"
    response = _q10_response(resolved)
    if protocol == "anthropic_messages":
        response.json.return_value = {
            "id": "q10-response",
            "model": resolved,
            "stop_reason": "end_turn",
            "content": [
                {"type": "server_tool_use", "id": "q10-search", "name": "web_search"},
                {
                    "type": "web_search_tool_result",
                    "tool_use_id": "q10-search",
                    "content": [{"type": "web_search_result", "url": "https://example.org/source"}],
                },
                {"type": "text", "text": "synthetic answer"},
            ],
        }
    elif protocol == "mimo_chat_completions":
        response.json.return_value = {
            "id": "q10-response",
            "model": resolved,
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": "synthetic answer",
                        "annotations": [
                            {"type": "url_citation", "url": "https://example.org/source"}
                        ],
                    },
                }
            ],
        }
    aliases = []
    if case in {"registered", "wrong_scope"}:
        mapped_provider, mapped_protocol = provider, protocol
        if case == "wrong_scope":
            mapped_provider, mapped_protocol = (
                ("minimax", "anthropic_messages")
                if provider != "minimax"
                else (
                    "minimax",
                    "responses" if protocol == "anthropic_messages" else "anthropic_messages",
                )
            )
        aliases = [
            {
                "provider": mapped_provider,
                "protocol": mapped_protocol,
                "requested_model": requested,
                "resolved_model": resolved,
            }
        ]
    policy = {"schema_version": "1.0.0", "aliases": aliases}
    session = Mock()
    if asynchronous:
        session.post = AsyncMock(return_value=response)
        monkeypatch.setattr(
            "src.providers.llm_client.http_client_manager.get_async_client",
            AsyncMock(return_value=session),
        )
        client = AsyncLLMClient(
            "synthetic", requested, url, provider_name=provider, model_resolution=policy
        )
        result = asyncio.run(client.send_search_request_async("synthetic"))
        assert session.post.await_count == 1
    else:
        session.post.return_value = response
        monkeypatch.setattr(
            "src.providers.llm_client.http_client_manager.get_sync_session", lambda: session
        )
        client = LLMClient(
            "synthetic", requested, url, provider_name=provider, model_resolution=policy
        )
        result = client.send_search_request("synthetic")
        assert session.post.call_count == 1
    assert result.search_verified is (case in {"exact", "registered"})
    assert result.actual_model == resolved
    assert result.execution_metadata["requested_model"] == requested
    assert len(result.execution_metadata["response_sha256"]) == 64


@pytest.mark.parametrize(
    "raw",
    [b'{"model":"a","model":"b"}', b'{"model":"a","number":NaN}', b'{"model":"a","number":1e999}'],
)
def test_q10_raw_duplicate_or_nonfinite_json_cannot_be_success(monkeypatch, raw):
    response = _q10_response("fixture-requested")
    response.content = raw
    session = Mock()
    session.post.return_value = response
    monkeypatch.setattr(
        "src.providers.llm_client.http_client_manager.get_sync_session", lambda: session
    )
    with pytest.raises(LLMTransportAttemptError):
        LLMClient(
            "synthetic", "fixture-requested", "https://api.openai.com/v1/responses"
        ).send_search_request("synthetic")


def test_q10_strict_raw_json_is_the_model_source_even_if_json_method_disagrees(monkeypatch):
    response = _q10_response("forged-json-method-model")
    payload = dict(response.json.return_value)
    payload["model"] = "fixture-requested"
    response.content = json.dumps(payload).encode("utf-8")
    session = Mock()
    session.post.return_value = response
    monkeypatch.setattr(
        "src.providers.llm_client.http_client_manager.get_sync_session", lambda: session
    )
    result = LLMClient(
        "synthetic", "fixture-requested", "https://api.openai.com/v1/responses"
    ).send_search_request("synthetic")
    assert result.search_verified is True
    assert result.actual_model == "fixture-requested"
    assert result.execution_metadata["response_json_basis"] == "strict_http_json"


def test_q10_async_model_mutation_after_begin_cannot_change_frozen_http_request(
    tmp_path, monkeypatch
):
    from src.utils.quick_scan_work_store import QuickScanWorkStore
    from src.utils.quick_scan_work_transport import (
        bind_quick_scan_budget,
        bind_quick_scan_route,
        bind_quick_scan_work,
    )

    requested = "fixture-requested"
    store = QuickScanWorkStore(tmp_path / "synthetic-model-mutation.sqlite")
    item = store.create_or_attach(
        entity_id="ENT_SYNTHETIC",
        question_id="CORE_01",
        generation=1,
        scope="entity",
        scope_id="ENT_SYNTHETIC",
        identity_revision=1,
        source_binding_version=1,
        identity_state="verified",
        source_binding_ref="BND_SYNTHETIC",
        source_binding_refs=["BND_SYNTHETIC"],
        identity_snapshot_sha256="a" * 64,
        question_fingerprint="b" * 64,
        routing_fingerprint="c" * 64,
        run_id="RUN_SYNTHETIC",
        scan_id="SCAN_SYNTHETIC",
    )
    work_id = item["work_item_id"]
    lease = store.claim(work_id, lease_seconds=60)
    assert lease is not None
    route = {
        "id": "mutation-route",
        "provider_config_ref": "openai",
        "model": requested,
        "quota_group": "synthetic-account",
        "max_in_flight": 1,
        "eligible": True,
    }
    policy = {
        "configured": True,
        "policy_id": "synthetic-mutation-budget",
        "policy_version": "synthetic-mutation-budget@v1",
        "budget": {"currency": "USD", "max_cost": 20, "max_requests": 5, "max_cost_per_attempt": 2},
        "cost_policy": {
            "pricing_basis": "verified_rate_card",
            "pricing_ref": "synthetic-rates",
            "reserve_before_dispatch": True,
            "unknown_actual_cost_action": "retain_reservation_and_pause",
        },
        "dispatch": {"max_in_flight_total": 1},
        "quota_groups": [{"id": "synthetic-account", "max_in_flight": 1}],
        "routes": [route],
    }
    client = AsyncLLMClient("synthetic", requested, "https://api.openai.com/v1/responses")
    session = Mock()
    session.post = AsyncMock(return_value=_q10_response(requested))

    async def get_client():
        attempt = store.list_attempts(work_id)[0]
        assert attempt["phase"] == "send_intent"
        assert attempt["model_requested"] == requested
        assert store.get_quick_scan_budget_status(policy["policy_id"])["requests"] == 1
        client.model = "mutated-after-durable-intent"
        return session

    monkeypatch.setattr("src.providers.llm_client.http_client_manager.get_async_client", get_client)
    try:
        with bind_quick_scan_work(store, work_id, lease), bind_quick_scan_budget(store, policy):
            with bind_quick_scan_route(
                route_id=route["id"],
                provider="openai",
                model_requested=requested,
                quota_group="synthetic-account",
            ):
                result = asyncio.run(client.send_search_request_async("synthetic prompt"))
    finally:
        assert session.post.await_count == 1
        assert session.post.call_args.kwargs["json"]["model"] == requested
    assert result.search_verified
    assert result.execution_metadata["requested_model"] == requested
    attempt = store.list_attempts(work_id)[0]
    response = store.get_attempt_response(attempt["attempt_id"])
    assert response["model_requested"] == requested
    assert response["model_resolved"] == requested


from src.providers.llm_client import (
    AsyncLLMClient,
    LLMClient,
    LLMTransportAttemptError,
    SearchCapabilityUnavailable,
)
from src.utils.http_client import SyncHTTPClient


class TestLLMClient:
    """测试同步 LLM 客户端。"""

    @pytest.fixture
    def client(self):
        return LLMClient(
            api_key="test_key",
            model="test_model",
            base_url="https://api.example.com/v1/chat/completions",
        )

    @patch("src.providers.llm_client.http_client_manager")
    def test_send_request_success(self, mock_http_manager, client):
        """测试成功发送请求。"""
        # 模拟 HTTP 响应
        mock_session = Mock()
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"choices": [{"message": {"content": "测试回答"}}]}
        mock_session.post.return_value = mock_response
        mock_http_manager.get_sync_session.return_value = mock_session

        result = client.send_request("测试问题")

        assert result == "测试回答"
        mock_session.post.assert_called_once()

    @patch("src.providers.llm_client.http_client_manager")
    def test_send_request_with_custom_system_prompt(self, mock_http_manager, client):
        """测试使用自定义系统提示词。"""
        mock_session = Mock()
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"choices": [{"message": {"content": "回答"}}]}
        mock_session.post.return_value = mock_response
        mock_http_manager.get_sync_session.return_value = mock_session

        client.send_request("问题", system_prompt="自定义系统提示")

        # 验证请求包含自定义系统提示
        call_args = mock_session.post.call_args
        assert call_args.kwargs["json"]["messages"][0]["content"] == "自定义系统提示"

    @patch("src.providers.llm_client.http_client_manager")
    def test_send_request_http_error(self, mock_http_manager, client):
        """测试HTTP错误处理。"""
        mock_session = Mock()
        mock_response = Mock()
        mock_response.raise_for_status.side_effect = RuntimeError("HTTP 500")
        mock_session.post.return_value = mock_response
        mock_http_manager.get_sync_session.return_value = mock_session

        with pytest.raises(RuntimeError):
            client.send_request("问题")

    @patch("src.providers.llm_client.http_client_manager")
    def test_send_request_timeout(self, mock_http_manager, client):
        """测试请求超时。"""
        mock_session = Mock()
        mock_session.post.side_effect = Exception("Timeout")
        mock_http_manager.get_sync_session.return_value = mock_session

        with pytest.raises(Exception, match="Timeout"):
            client.send_request("问题")

    def test_init_with_custom_timeout(self):
        """测试使用自定义超时时间初始化。"""
        client = LLMClient(
            api_key="test_key",
            model="test_model",
            base_url="https://api.example.com/v1",
            timeout=60.0,
        )
        assert client.timeout == 60.0

    def test_search_429_is_one_transport_post_and_retains_attempt_receipt(self):
        session = SyncHTTPClient.get_session()
        session.trust_env = False
        response = HTTPResponse(
            body=io.BytesIO(b'{"error":{"code":"rate_limit_exceeded"}}'),
            status=429,
            headers={"Retry-After": "18000", "x-request-id": "req-once"},
            preload_content=False,
        )
        client = LLMClient(
            api_key="fixture-key",
            model="fixture-model",
            base_url="https://api.openai.com/v1/responses",
            provider_name="openai",
        )
        try:
            with (
                patch(
                    "src.providers.llm_client.http_client_manager.get_sync_session",
                    return_value=session,
                ),
                patch(
                    "urllib3.connectionpool.HTTPSConnectionPool._make_request",
                    return_value=response,
                ) as send,
                patch(
                    "urllib3.util.retry.Retry.sleep",
                    side_effect=AssertionError("hidden transport sleep"),
                ),
            ):
                with pytest.raises(LLMTransportAttemptError) as error:
                    client.send_search_request("fixture question")
            assert send.call_count == 1
            receipt = error.value.attempt_receipt
            assert receipt["http_status_code"] == 429
            assert receipt["request_id"] == "req-once"
            assert receipt["retry_after_seconds"] == 18000
        finally:
            SyncHTTPClient.close_session()

    @patch("src.providers.llm_client.http_client_manager")
    def test_search_request_uses_openai_responses_tool_and_keeps_receipt(self, mock_manager):
        session = Mock()
        response = Mock()
        response.headers = {"x-request-id": "req_fixture_01"}
        response.status_code = 200
        response.json.return_value = {
            "id": "resp_fixture_01",
            "status": "completed",
            "model": "fixture-model",
            "usage": {
                "input_tokens": 15,
                "input_tokens_details": {"cached_tokens": 5},
                "output_tokens": 9,
                "output_tokens_details": {"reasoning_tokens": 2},
            },
            "output": [
                {
                    "type": "web_search_call",
                    "id": "ws_fixture_01",
                    "status": "completed",
                    "action": {
                        "type": "search",
                        "queries": ["Example Corp latest annual report"],
                        "sources": [{"type": "url", "url": "https://example.com/filing"}],
                    },
                },
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": '{"score":8,"description":"基于公开来源"}',
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://example.com/filing",
                                }
                            ],
                        }
                    ],
                },
            ],
        }
        session.post.return_value = response
        mock_manager.get_sync_session.return_value = session
        client = LLMClient(
            "fixture-key", "fixture-model", "https://api.openai.com/v1/chat/completions"
        )

        result = client.send_search_request("公司问题")

        assert result.content == '{"score":8,"description":"基于公开来源"}'
        assert result.search_verified is True
        assert result.request_id == "req_fixture_01"
        assert result.response_id == "resp_fixture_01"
        assert result.source_urls == ("https://example.com/filing",)
        call = session.post.call_args
        assert call.args[0] == "https://api.openai.com/v1/responses"
        assert call.kwargs["allow_redirects"] is False
        assert call.kwargs["json"]["tools"] == [{"type": "web_search"}]
        assert call.kwargs["json"]["tool_choice"] == "required"
        assert call.kwargs["json"]["include"] == ["web_search_call.action.sources"]
        assert "messages" not in call.kwargs["json"]
        assert result.execution_metadata["response_status"] == "completed"
        assert result.execution_metadata["http_status_code"] == 200
        assert result.execution_metadata["attempts"][-1]["http_status_code"] == 200
        assert result.execution_metadata["started_at"].endswith("Z")
        assert result.execution_metadata["completed_at"].endswith("Z")
        assert len(result.execution_metadata["prompt_sha256"]) == 64
        assert result.execution_metadata["attempt_id"]
        assert result.execution_metadata["search_receipt_id"] == "ws_fixture_01"
        assert result.execution_metadata["usage"] == {
            "schema": "quick-scan-usage-v1",
            "input_tokens": 10,
            "cached_input_tokens": 5,
            "cache_creation_input_tokens": 0,
            "output_tokens": 9,
            "reasoning_output_tokens": 2,
            "search_tool_calls": 1,
        }
        assert (
            result.execution_metadata["attempts"][-1]["usage"] == result.execution_metadata["usage"]
        )

    @patch("src.providers.llm_client.http_client_manager")
    def test_responses_usage_counts_search_event_without_provider_event_id(self, mock_manager):
        session = Mock()
        response = Mock()
        response.headers = {"x-request-id": "req_missing_search_id"}
        response.status_code = 200
        response.json.return_value = {
            "id": "resp_missing_search_id",
            "status": "completed",
            "model": "fixture-model",
            "usage": {"input_tokens": 10, "output_tokens": 2},
            "output": [
                {
                    "type": "web_search_call",
                    "status": "completed",
                    "action": {"type": "search", "sources": []},
                },
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "answer"}],
                },
            ],
        }
        session.post.return_value = response
        mock_manager.get_sync_session.return_value = session
        client = LLMClient(
            "fixture-key", "fixture-model", "https://api.openai.com/v1/chat/completions"
        )

        result = client.send_search_request("公司问题")

        assert result.search_verified is False
        assert result.execution_metadata["usage"]["search_tool_calls"] == 1

    @patch("src.providers.llm_client.http_client_manager")
    def test_responses_usage_rejects_search_total_below_observed_calls(self, mock_manager):
        session = Mock()
        response = Mock()
        response.headers = {"x-request-id": "req_inconsistent_search_usage"}
        response.status_code = 200
        response.json.return_value = {
            "id": "resp_inconsistent_search_usage",
            "status": "completed",
            "model": "fixture-model",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 2,
                "web_search_usage": {"tool_usage": 0},
            },
            "output": [
                {
                    "type": "web_search_call",
                    "status": "completed",
                    "action": {"type": "search", "sources": []},
                }
            ],
        }
        session.post.return_value = response
        mock_manager.get_sync_session.return_value = session
        client = LLMClient(
            "fixture-key", "fixture-model", "https://api.openai.com/v1/chat/completions"
        )

        result = client.send_search_request("公司问题")

        assert "usage" not in result.execution_metadata

    @patch("src.providers.llm_client.http_client_manager")
    def test_responses_usage_stays_unknown_for_unclassified_search_event(self, mock_manager):
        session = Mock()
        response = Mock()
        response.headers = {"x-request-id": "req_unknown_search_action"}
        response.status_code = 200
        response.json.return_value = {
            "id": "resp_unknown_search_action",
            "status": "completed",
            "model": "fixture-model",
            "usage": {"input_tokens": 10, "output_tokens": 2},
            "output": [{"type": "web_search_call", "status": "completed", "action": {}}],
        }
        session.post.return_value = response
        mock_manager.get_sync_session.return_value = session
        client = LLMClient(
            "fixture-key", "fixture-model", "https://api.openai.com/v1/chat/completions"
        )

        result = client.send_search_request("公司问题")

        assert "usage" not in result.execution_metadata

    @patch("src.providers.llm_client.http_client_manager")
    def test_search_request_without_execution_evidence_is_unverified(self, mock_manager):
        session = Mock()
        response = Mock()
        response.status_code = 200
        response.headers = {}
        response.json.return_value = {
            "id": "resp_without_search",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": '{"score":9,"description":"像是搜过"}',
                        }
                    ],
                }
            ],
        }
        session.post.return_value = response
        mock_manager.get_sync_session.return_value = session
        client = LLMClient(
            "fixture-key", "fixture-model", "https://api.openai.com/v1/chat/completions"
        )

        result = client.send_search_request("公司问题")

        assert result.search_verified is False
        assert result.execution_metadata["search_status"] == "unverified"

    @patch("src.providers.llm_client.http_client_manager")
    def test_malformed_provider_usage_is_not_published_as_billable_usage(self, mock_manager):
        session = Mock()
        response = Mock()
        response.headers = {"x-request-id": "req_bad_usage"}
        response.status_code = 200
        response.json.return_value = {
            "id": "resp_bad_usage",
            "status": "completed",
            "model": "fixture-model",
            "usage": {
                "input_tokens": 10,
                "input_tokens_details": {"cached_tokens": -1},
                "output_tokens": 5,
            },
            "output": [
                {
                    "type": "web_search_call",
                    "id": "search_bad_usage",
                    "status": "completed",
                    "action": {
                        "type": "search",
                        "sources": [{"type": "url", "url": "https://example.com/source"}],
                    },
                },
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "A verified answer."}],
                },
            ],
        }
        session.post.return_value = response
        mock_manager.get_sync_session.return_value = session
        client = LLMClient("fixture-key", "fixture-model", "https://api.openai.com/v1/responses")

        result = client.send_search_request("question")

        assert result.search_verified
        assert "usage" not in result.execution_metadata

    @patch("src.providers.llm_client.http_client_manager")
    def test_search_timeout_retains_sanitized_attempt_receipt(self, mock_manager):
        session = Mock()
        session.post.side_effect = requests.Timeout("private timeout detail")
        mock_manager.get_sync_session.return_value = session
        client = LLMClient("fixture-key", "fixture-model", "https://api.openai.com/v1/responses")

        with pytest.raises(LLMTransportAttemptError) as raised:
            client.send_search_request("private prompt text")

        receipt = raised.value.attempt_receipt
        assert raised.value.failure_type == "Timeout"
        assert "private timeout detail" not in str(raised.value)
        assert "private prompt text" not in str(receipt)
        assert receipt["attempt_id"]
        assert receipt["started_at"].endswith("Z")
        assert receipt["completed_at"].endswith("Z")
        assert len(receipt["prompt_sha256"]) == 64
        assert receipt["search_status"] == "unverified"
        assert receipt["failure_type"] == "Timeout"

    @patch("src.providers.llm_client.http_client_manager")
    def test_search_http_error_keeps_request_id_and_status_without_body(self, mock_manager):
        session = Mock()
        response = Mock()
        response.headers = {"x-request-id": "req_rate_limited"}
        response.status_code = 429
        response.raise_for_status.side_effect = requests.HTTPError("secret response body")
        session.post.return_value = response
        mock_manager.get_sync_session.return_value = session
        client = LLMClient("fixture-key", "fixture-model", "https://api.openai.com/v1/responses")

        with pytest.raises(LLMTransportAttemptError) as raised:
            client.send_search_request("question")

        receipt = raised.value.attempt_receipt
        assert receipt["request_id"] == "req_rate_limited"
        assert receipt["http_status_code"] == 429
        assert receipt["failure_type"] == "HTTPError"
        assert "secret response body" not in str(raised.value)

    @patch("src.providers.llm_client.http_client_manager")
    def test_search_response_incomplete_is_not_verified(self, mock_manager):
        session = Mock()
        response = Mock()
        response.status_code = 200
        response.headers = {"x-request-id": "req_incomplete"}
        response.json.return_value = {
            "id": "resp_incomplete",
            "status": "incomplete",
            "output": [
                {
                    "type": "web_search_call",
                    "id": "ws_completed_but_response_incomplete",
                    "status": "completed",
                    "action": {
                        "type": "search",
                        "sources": [{"type": "url", "url": "https://example.com/source"}],
                    },
                }
            ],
        }
        session.post.return_value = response
        mock_manager.get_sync_session.return_value = session
        client = LLMClient("fixture-key", "fixture-model", "https://api.openai.com/v1/responses")

        result = client.send_search_request("question")

        assert result.search_verified is False
        assert result.execution_metadata["response_status"] == "incomplete"

    @patch("src.providers.llm_client.http_client_manager")
    def test_search_sources_must_belong_to_a_completed_search_call(self, mock_manager):
        session = Mock()
        response = Mock()
        response.status_code = 200
        response.headers = {"x-request-id": "req_cross_call"}
        response.json.return_value = {
            "id": "resp_cross_call",
            "status": "completed",
            "output": [
                {
                    "type": "web_search_call",
                    "id": "ws_failed_with_sources",
                    "status": "incomplete",
                    "action": {
                        "type": "search",
                        "sources": [{"type": "url", "url": "https://example.com/failed"}],
                    },
                },
                {
                    "type": "web_search_call",
                    "id": "ws_completed_without_sources",
                    "status": "completed",
                    "action": {"type": "open_page", "url": "https://example.com/page"},
                },
            ],
        }
        session.post.return_value = response
        mock_manager.get_sync_session.return_value = session
        client = LLMClient("fixture-key", "fixture-model", "https://api.openai.com/v1/responses")

        result = client.send_search_request("question")

        assert result.search_verified is False
        assert result.source_urls == ()

    @pytest.mark.parametrize(
        "endpoint_url",
        [
            "https://user:pass@api.openai.com/v1/chat/completions",
            "https://api.openai.com:8443/v1/chat/completions",
            "https://api.openai.com/proxy/v1/chat/completions",
        ],
    )
    def test_ambiguous_openai_search_endpoint_is_rejected_before_transport(
        self, endpoint_url, monkeypatch
    ):
        mock_manager = Mock()
        monkeypatch.setattr("src.providers.llm_client.http_client_manager", mock_manager)
        client = LLMClient("fixture-key", "fixture-model", endpoint_url)
        assert not client.supports_web_search
        with pytest.raises(SearchCapabilityUnavailable):
            client.send_search_request("query")
        mock_manager.get_sync_session.assert_not_called()

    @patch("src.providers.llm_client.http_client_manager")
    def test_unsupported_endpoint_does_not_send_request(self, mock_manager):
        client = LLMClient(
            "fixture-key",
            "fixture-model",
            "https://api.example.com/v1/chat/completions",
        )
        with pytest.raises(SearchCapabilityUnavailable):
            client.send_search_request("公司问题")
        mock_manager.get_sync_session.assert_not_called()


class TestAsyncLLMClient:
    """测试异步 LLM 客户端。"""

    @pytest.fixture
    def client(self):
        return AsyncLLMClient(
            api_key="test_key",
            model="test_model",
            base_url="https://api.example.com/v1/chat/completions",
        )

    @pytest.mark.asyncio
    @patch("src.providers.llm_client.http_client_manager")
    async def test_send_request_async_success(self, mock_http_manager, client):
        """测试异步请求成功。"""
        # 模拟异步 HTTP 客户端
        mock_async_client = AsyncMock()
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"choices": [{"message": {"content": "异步回答"}}]}

        # 创建协程返回值的 mock
        async def mock_post(*args, **kwargs):
            return mock_response

        mock_async_client.post = mock_post
        mock_http_manager.get_async_client = AsyncMock(return_value=mock_async_client)

        result = await client.send_request_async("测试问题")

        assert result == "异步回答"

    @pytest.mark.asyncio
    @patch("src.providers.llm_client.http_client_manager")
    async def test_send_request_async_with_custom_system_prompt(self, mock_http_manager, client):
        """测试异步请求使用自定义系统提示。"""
        mock_async_client = AsyncMock()
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"choices": [{"message": {"content": "回答"}}]}

        # 记录调用参数
        call_params = {}

        async def mock_post(*args, **kwargs):
            call_params.update(kwargs)
            return mock_response

        mock_async_client.post = mock_post
        mock_http_manager.get_async_client = AsyncMock(return_value=mock_async_client)

        await client.send_request_async("问题", system_prompt="自定义系统提示")

        # 验证请求包含自定义系统提示
        assert call_params["json"]["messages"][0]["content"] == "自定义系统提示"

    @pytest.mark.asyncio
    @patch("src.providers.llm_client.http_client_manager")
    async def test_send_request_async_http_error(self, mock_http_manager, client):
        """测试异步请求HTTP错误。"""
        mock_async_client = AsyncMock()
        mock_response = Mock()
        mock_response.raise_for_status.side_effect = RuntimeError("HTTP 500")

        async def mock_post_with_error(*args, **kwargs):
            return mock_response

        mock_async_client.post = mock_post_with_error
        mock_http_manager.get_async_client = AsyncMock(return_value=mock_async_client)

        with pytest.raises(RuntimeError):
            await client.send_request_async("问题")

    @pytest.mark.asyncio
    @patch("src.providers.llm_client.http_client_manager")
    async def test_send_request_async_timeout(self, mock_http_manager, client):
        """测试异步请求超时。"""
        mock_async_client = AsyncMock()
        mock_async_client.post = Mock(side_effect=Exception("Async Timeout"))
        mock_http_manager.get_async_client = AsyncMock(return_value=mock_async_client)

        with pytest.raises(Exception, match="Async Timeout"):
            await client.send_request_async("问题")

    def test_async_init_with_custom_timeout(self):
        """测试异步客户端使用自定义超时初始化。"""
        client = AsyncLLMClient(
            api_key="test_key",
            model="test_model",
            base_url="https://api.example.com/v1",
            timeout=120.0,
        )
        assert client.timeout == 120.0

    @pytest.mark.asyncio
    @patch("src.providers.llm_client.http_client_manager")
    async def test_async_search_timeout_retains_sanitized_attempt_receipt(self, mock_manager):
        client = AsyncLLMClient(
            "fixture-key", "fixture-model", "https://api.openai.com/v1/responses"
        )
        http_client = AsyncMock()
        request = httpx.Request("POST", "https://api.openai.com/v1/responses?secret=token")
        http_client.post.side_effect = httpx.TimeoutException(
            "private async timeout detail", request=request
        )
        mock_manager.get_async_client = AsyncMock(return_value=http_client)

        with pytest.raises(LLMTransportAttemptError) as raised:
            await client.send_search_request_async("private prompt text")

        receipt = raised.value.attempt_receipt
        assert raised.value.failure_type == "TimeoutException"
        assert "private async timeout detail" not in str(raised.value)
        assert "secret=token" not in str(receipt)
        assert "private prompt text" not in str(receipt)
        assert receipt["attempt_id"]
        assert len(receipt["prompt_sha256"]) == 64
        assert receipt["search_status"] == "unverified"

    @pytest.mark.asyncio
    @patch("src.providers.llm_client.http_client_manager")
    async def test_async_search_uses_same_execution_semantics(self, mock_manager):
        client = AsyncLLMClient(
            "fixture-key", "fixture-model", "https://api.openai.com/v1/chat/completions"
        )
        http_client = AsyncMock()
        response = Mock()
        response.headers = {"x-request-id": "req_async_01"}
        response.status_code = 200
        response.json.return_value = {
            "id": "resp_async_01",
            "status": "completed",
            "model": "fixture-model",
            "output": [
                {
                    "type": "web_search_call",
                    "id": "ws_async_01",
                    "status": "completed",
                    "action": {
                        "type": "search",
                        "sources": [{"type": "url", "url": "https://example.com/source"}],
                    },
                },
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": '{"score":8,"description":"有依据"}',
                        }
                    ],
                },
            ],
        }
        http_client.post.return_value = response
        mock_manager.get_async_client = AsyncMock(return_value=http_client)

        result = await client.send_search_request_async("问题")

        assert result.search_verified is True
        assert result.request_id == "req_async_01"
        assert result.response_id == "resp_async_01"
        assert result.execution_metadata["http_status_code"] == 200
        assert result.execution_metadata["attempts"][-1]["http_status_code"] == 200
        assert http_client.post.call_args.args[0] == "https://api.openai.com/v1/responses"
        assert http_client.post.call_args.kwargs["follow_redirects"] is False
        assert http_client.post.call_args.kwargs["json"]["include"] == [
            "web_search_call.action.sources"
        ]
        assert result.execution_metadata["search_receipt_id"] == "ws_async_01"


def _minimax_search_response(
    *,
    event=True,
    event_status="completed",
    source="citation",
    actual_model="MiniMax-M3",
    response_id="resp_mm_01",
    response_status="completed",
):
    output = []
    if source == "before_event":
        output.append(
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "preamble",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url": "https://example.com/preamble",
                            }
                        ],
                    }
                ],
            }
        )
    if event:
        action = {"type": "search", "query": "Microsoft official annual report"}
        if source == "action":
            action["sources"] = [{"type": "url", "url": "https://example.com/source"}]
        output.append(
            {
                "type": "web_search_call",
                "id": "ws_mm_01",
                "status": event_status,
                "action": action,
            }
        )
    annotations = (
        [{"type": "url_citation", "url": "https://example.com/source"}]
        if source == "citation"
        else []
    )
    output.append(
        {
            "type": "message",
            "status": "completed",
            "content": [
                {
                    "type": "output_text",
                    "text": (
                        '{"score":8,"description":"https://example.com/prose; fake response id resp_mm_01"}'
                        if source == "prose"
                        else '{"score":8,"description":"来源可核"}'
                    ),
                    "annotations": annotations,
                }
            ],
        }
    )
    response = Mock()
    response.headers = {}
    response.status_code = 200
    response.json.return_value = {
        "id": response_id,
        "status": response_status,
        "model": actual_model,
        "output": output,
    }
    return response


def _minimax_anthropic_response(
    *,
    blocks=None,
    response_id="msg_mm_anthropic_01",
    actual_model="MiniMax-M3",
    stop_reason="end_turn",
    base_resp_status=0,
):
    if blocks is None:
        blocks = [
            {"type": "text", "text": "Searching official sources."},
            {
                "type": "server_tool_use",
                "id": "srvtoolu_mm_01",
                "name": "web_search",
                "input": {"query": "Example issuer annual report"},
            },
            {
                "type": "web_search_tool_result",
                "tool_use_id": "srvtoolu_mm_01",
                "content": [
                    {
                        "type": "web_search_result",
                        "title": "Official report",
                        "url": "https://example.com/annual-report",
                        "content": "Public source.",
                    }
                ],
            },
            {"type": "text", "text": '{"score":8,"description":"Grounded answer."}'},
        ]
    response = Mock()
    response.headers = {}
    response.status_code = 200
    response.json.return_value = {
        "id": response_id,
        "model": actual_model,
        "stop_reason": stop_reason,
        "content": blocks,
        "base_resp": {"status_code": base_resp_status},
    }
    return response


@patch("src.providers.llm_client.http_client_manager")
def test_sync_redirect_with_fake_completed_search_body_is_not_verified(mock_manager):
    payload = _minimax_search_response(source="action").json()
    payload["output"][-1]["content"][0]["text"] = '{"score":8,"description":"secret redirect body"}'
    response = requests.Response()
    response.status_code = 302
    response.url = "https://api.minimaxi.com/v1/responses"
    response.headers["Location"] = "https://example.invalid/redirected"
    response._content = json.dumps(payload).encode("utf-8")
    session = Mock()
    session.post.return_value = response
    mock_manager.get_sync_session.return_value = session

    client = LLMClient(
        "offline-fixture-key",
        "MiniMax-M3",
        "https://api.minimaxi.com/v1/responses",
        provider_name="minimax",
    )
    with pytest.raises(LLMTransportAttemptError) as raised:
        client.send_search_request("fixture")

    receipt = raised.value.attempt_receipt
    assert receipt["provider"] == "minimax"
    assert receipt["http_status_code"] == 302
    assert receipt["search_status"] == "unverified"
    assert receipt["response_id"] is None
    assert receipt["source_urls"] == []
    assert receipt["attempt_id"]
    assert session.post.call_args.kwargs["allow_redirects"] is False
    assert "offline-fixture-key" not in str(receipt)
    assert "secret redirect body" not in str(receipt)
    assert "https://example.invalid/redirected" not in str(receipt)
    assert "offline-fixture-key" not in str(raised.value)
    assert "secret redirect body" not in str(raised.value)


@pytest.mark.asyncio
@patch("src.providers.llm_client.http_client_manager")
async def test_async_redirect_with_fake_completed_search_body_is_not_verified(
    mock_manager,
):
    payload = _minimax_search_response(source="action").json()
    payload["output"][-1]["content"][0]["text"] = '{"score":8,"description":"secret redirect body"}'
    response = httpx.Response(
        302,
        headers={"Location": "https://example.invalid/redirected"},
        json=payload,
        request=httpx.Request("POST", "https://api.minimaxi.com/v1/responses"),
    )
    http_client = AsyncMock()
    http_client.post.return_value = response
    mock_manager.get_async_client = AsyncMock(return_value=http_client)

    client = AsyncLLMClient(
        "offline-fixture-key",
        "MiniMax-M3",
        "https://api.minimaxi.com/v1/responses",
        provider_name="minimax",
    )
    with pytest.raises(LLMTransportAttemptError) as raised:
        await client.send_search_request_async("fixture")

    receipt = raised.value.attempt_receipt
    assert receipt["provider"] == "minimax"
    assert receipt["http_status_code"] == 302
    assert receipt["search_status"] == "unverified"
    assert receipt["response_id"] is None
    assert receipt["source_urls"] == []
    assert receipt["attempt_id"]
    assert http_client.post.call_args.kwargs["follow_redirects"] is False
    assert "offline-fixture-key" not in str(receipt)
    assert "secret redirect body" not in str(receipt)
    assert "https://example.invalid/redirected" not in str(receipt)
    assert "offline-fixture-key" not in str(raised.value)
    assert "secret redirect body" not in str(raised.value)


@pytest.mark.parametrize("source", ["citation", "action"])
@patch("src.providers.llm_client.http_client_manager")
def test_minimax_search_requires_event_and_same_response_source(mock_manager, source):
    session = Mock()
    session.post.return_value = _minimax_search_response(source=source)
    mock_manager.get_sync_session.return_value = session
    result = LLMClient(
        "fixture-key", "MiniMax-M3", "https://api.minimaxi.com/v1/responses"
    ).send_search_request("Microsoft")
    assert result.search_verified
    assert result.execution_metadata["provider"] == "minimax"
    assert result.response_id == "resp_mm_01"
    assert result.request_id is None
    assert result.execution_metadata["attempt_id"]
    assert result.execution_metadata["search_receipt_id"] == "ws_mm_01"
    assert (
        result.execution_metadata["attempts"][0]["attempt_id"]
        == result.execution_metadata["attempt_id"]
    )
    assert result.execution_metadata["attempts"][0]["response_id"] == result.response_id
    assert result.execution_metadata["attempts"][0]["search_receipt_id"] == "ws_mm_01"
    assert result.source_urls == ("https://example.com/source",)
    assert session.post.call_args.args[0] == "https://api.minimaxi.com/v1/responses"
    assert session.post.call_args.kwargs["json"]["tools"] == [{"type": "web_search"}]


@patch("src.providers.llm_client.http_client_manager")
def test_minimax_responses_payload_keeps_system_text_out_of_input(mock_manager):
    """Q02 官方契约：system 走 instructions 字段，绝不拼进 input。

    回归锁定：曾把中文 system 行拼进 input 导致 web_search 被抑制（3/3 零搜索）。
    """
    session = Mock()
    session.post.return_value = _minimax_search_response(source="citation")
    mock_manager.get_sync_session.return_value = session

    LLMClient(
        "fixture-key", "MiniMax-M3", "https://api.minimaxi.com/v1/responses"
    ).send_search_request("Microsoft")

    payload = session.post.call_args.kwargs["json"]
    assert set(payload) == {"model", "instructions", "input", "tools"}
    assert payload["instructions"] == "你是一位专业的投资分析师，擅长分析公司的投资价值。"
    assert payload["input"] == "Microsoft"
    assert "投资分析师" not in payload["input"]
    assert payload["tools"] == [{"type": "web_search"}]
    # 官方 Responses tool_choice 枚举仅 none|auto；minimax 分支不发送该字段。
    assert "tool_choice" not in payload


@patch("src.providers.llm_client.http_client_manager")
def test_minimax_anthropic_messages_search_binds_tool_result_and_keeps_null_request_id(
    mock_manager,
):
    session = Mock()
    session.post.return_value = _minimax_anthropic_response()
    session.post.return_value.json.return_value["usage"] = {
        "input_tokens": 50,
        "cache_read_input_tokens": 20,
        "cache_creation_input_tokens": 10,
        "output_tokens": 30,
    }
    mock_manager.get_sync_session.return_value = session
    endpoint = "https://api.minimaxi.com/anthropic/v1/messages"
    client = LLMClient("fixture-key", "MiniMax-M3", endpoint, provider_name="minimax")

    result = client.send_search_request("Example issuer question", "Investment analyst.")

    assert result.search_verified
    assert result.request_id is None
    assert result.response_id == "msg_mm_anthropic_01"
    assert result.actual_model == "MiniMax-M3"
    assert result.source_urls == ("https://example.com/annual-report",)
    assert result.content == '{"score":8,"description":"Grounded answer."}'
    metadata = result.execution_metadata
    assert metadata["provider"] == "minimax"
    assert metadata["response_status"] == "completed"
    assert metadata["stop_reason"] == "end_turn"
    assert metadata["search_receipt_id"] == "srvtoolu_mm_01"
    assert metadata["usage"] == {
        "schema": "quick-scan-usage-v1",
        "input_tokens": 50,
        "cached_input_tokens": 20,
        "cache_creation_input_tokens": 10,
        "output_tokens": 30,
        "reasoning_output_tokens": 0,
        "search_tool_calls": 1,
    }
    assert metadata["attempt_id"]
    assert metadata["web_search_calls"] == [
        {
            "id": "srvtoolu_mm_01",
            "status": "completed",
            "action_type": "search",
            "source_urls": ["https://example.com/annual-report"],
            "sources": [
                {
                    "url": "https://example.com/annual-report",
                    "title": "Official report",
                    "published_date": None,
                }
            ],
        }
    ]
    call = session.post.call_args
    assert call.args[0] == endpoint
    assert call.kwargs["headers"]["x-api-key"] == "fixture-key"
    assert call.kwargs["headers"]["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in call.kwargs["headers"]
    assert call.kwargs["json"]["system"] == "Investment analyst."
    assert call.kwargs["json"]["messages"] == [
        {"role": "user", "content": "Example issuer question"}
    ]
    assert call.kwargs["json"]["max_tokens"] == 2048
    assert call.kwargs["json"]["tools"] == [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 1}
    ]
    # Official Messages API ToolChoice: only auto/none (Q02 conformance).
    assert call.kwargs["json"]["tool_choice"] == {"type": "auto"}


@pytest.mark.parametrize(
    ("actual_model", "response_id", "stop_reason", "base_resp_status"),
    [
        ("MiniMax-M3", "msg_mm_anthropic_01", "max_tokens", 0),
        ("MiniMax-M2.7", "msg_mm_anthropic_01", "end_turn", 0),
        ("MiniMax-M3", None, "end_turn", 0),
        ("MiniMax-M3", "msg_mm_anthropic_01", "end_turn", 1004),
        ("MiniMax-M3", "msg_mm_anthropic_01", "end_turn", False),
    ],
    ids=[
        "truncated",
        "wrong-model",
        "missing-response-id",
        "provider-error",
        "boolean-provider-status",
    ],
)
@patch("src.providers.llm_client.http_client_manager")
def test_minimax_anthropic_incomplete_or_mismatched_response_never_verifies(
    mock_manager, actual_model, response_id, stop_reason, base_resp_status
):
    session = Mock()
    session.post.return_value = _minimax_anthropic_response(
        actual_model=actual_model,
        response_id=response_id,
        stop_reason=stop_reason,
        base_resp_status=base_resp_status,
    )
    mock_manager.get_sync_session.return_value = session
    result = LLMClient(
        "fixture-key",
        "MiniMax-M3",
        "https://api.minimaxi.com/anthropic/v1/messages",
        provider_name="minimax",
    ).send_search_request("Example issuer question")
    assert not result.search_verified
    assert result.execution_metadata["search_status"] == "unverified"
    assert result.execution_metadata["search_receipt_id"] is None


@patch("src.providers.llm_client.http_client_manager")
def test_minimax_anthropic_unknown_server_tool_prevents_search_verification(
    mock_manager,
):
    response = _minimax_anthropic_response()
    payload = response.json.return_value
    payload["content"].insert(
        -1,
        {
            "type": "server_tool_use",
            "id": "srvtoolu_mm_unknown",
            "name": "web_fetch",
            "input": {"url": "https://example.com/annual-report"},
        },
    )
    session = Mock()
    session.post.return_value = response
    mock_manager.get_sync_session.return_value = session

    result = LLMClient(
        "fixture-key",
        "MiniMax-M3",
        "https://api.minimaxi.com/anthropic/v1/messages",
        provider_name="minimax",
    ).send_search_request("Example issuer question")

    assert not result.search_verified
    assert result.execution_metadata["search_status"] == "unverified"
    assert result.execution_metadata["search_receipt_id"] is None
    assert result.execution_metadata["web_search_calls"][0]["status"] == "completed"


@patch("src.providers.llm_client.http_client_manager")
def test_minimax_anthropic_multiple_search_calls_exceed_one_call_policy(mock_manager):
    blocks = [
        {
            "type": "server_tool_use",
            "id": "srvtoolu_mm_first",
            "name": "web_search",
            "input": {"query": "first query"},
        },
        {
            "type": "web_search_tool_result",
            "tool_use_id": "srvtoolu_mm_first",
            "content": [{"type": "web_search_result", "url": "https://example.com/first"}],
        },
        {
            "type": "server_tool_use",
            "id": "srvtoolu_mm_second",
            "name": "web_search",
            "input": {"query": "second query"},
        },
        {
            "type": "web_search_tool_result",
            "tool_use_id": "srvtoolu_mm_second",
            "content": [{"type": "web_search_result", "url": "https://example.com/second"}],
        },
        {"type": "text", "text": '{"score":8,"description":"Grounded answer."}'},
    ]
    session = Mock()
    session.post.return_value = _minimax_anthropic_response(blocks=blocks)
    mock_manager.get_sync_session.return_value = session

    result = LLMClient(
        "fixture-key",
        "MiniMax-M3",
        "https://api.minimaxi.com/anthropic/v1/messages",
        provider_name="minimax",
    ).send_search_request("Example issuer question")

    assert len(result.execution_metadata["web_search_calls"]) == 2
    assert all(
        call["status"] == "completed" for call in result.execution_metadata["web_search_calls"]
    )
    assert not result.search_verified
    assert result.execution_metadata["search_status"] == "unverified"


@pytest.mark.parametrize("malformed_base_resp", [None, "ok", [], 0])
@patch("src.providers.llm_client.http_client_manager")
def test_minimax_anthropic_malformed_present_base_resp_never_verifies(
    mock_manager, malformed_base_resp
):
    response = _minimax_anthropic_response()
    response.json.return_value["base_resp"] = malformed_base_resp
    session = Mock()
    session.post.return_value = response
    mock_manager.get_sync_session.return_value = session

    result = LLMClient(
        "fixture-key",
        "MiniMax-M3",
        "https://api.minimaxi.com/anthropic/v1/messages",
        provider_name="minimax",
    ).send_search_request("Example issuer question")

    assert not result.search_verified
    assert result.execution_metadata["search_status"] == "unverified"


@patch("src.providers.llm_client.http_client_manager")
def test_minimax_anthropic_absent_optional_base_resp_can_verify(mock_manager):
    response = _minimax_anthropic_response()
    response.json.return_value.pop("base_resp")
    session = Mock()
    session.post.return_value = response
    mock_manager.get_sync_session.return_value = session

    result = LLMClient(
        "fixture-key",
        "MiniMax-M3",
        "https://api.minimaxi.com/anthropic/v1/messages",
        provider_name="minimax",
    ).send_search_request("Example issuer question")

    assert result.search_verified


@pytest.mark.parametrize(
    "blocks",
    [
        [
            {
                "type": "server_tool_use",
                "id": "srvtoolu_mm_01",
                "name": "web_search",
                "input": {"query": "q"},
            },
            {
                "type": "web_search_tool_result",
                "tool_use_id": "foreign-id",
                "content": [{"type": "web_search_result", "url": "https://example.com/foreign"}],
            },
            {"type": "text", "text": "A result from another call."},
        ],
        [
            {
                "type": "server_tool_use",
                "id": "srvtoolu_mm_01",
                "name": "web_search",
                "input": {"query": "q"},
            },
            {
                "type": "web_search_tool_result",
                "tool_use_id": "srvtoolu_mm_01",
                "content": {
                    "type": "web_search_tool_result_error",
                    "error_code": "max_uses_exceeded",
                },
            },
            {"type": "text", "text": "No verified search."},
        ],
        [
            {
                "type": "server_tool_use",
                "id": "srvtoolu_mm_01",
                "name": "web_search",
                "input": {"query": "q"},
            },
            {
                "type": "web_search_tool_result",
                "tool_use_id": "srvtoolu_mm_01",
                "content": [],
            },
            {"type": "text", "text": "No sources."},
        ],
    ],
    ids=["unbound-result", "server-tool-error", "empty-sources"],
)
@patch("src.providers.llm_client.http_client_manager")
def test_minimax_anthropic_requires_correlated_search_result_sources(mock_manager, blocks):
    session = Mock()
    session.post.return_value = _minimax_anthropic_response(blocks=blocks)
    mock_manager.get_sync_session.return_value = session
    result = LLMClient(
        "fixture-key",
        "MiniMax-M3",
        "https://api.minimaxi.com/anthropic/v1/messages",
        provider_name="minimax",
    ).send_search_request("Example issuer question")
    assert not result.search_verified
    assert result.execution_metadata["search_status"] == "unverified"
    if isinstance(blocks[1].get("content"), dict):
        assert result.execution_metadata["web_search_calls"][0]["error_code"] == "max_uses_exceeded"


@pytest.mark.parametrize(
    ("base_url", "expected_support"),
    [
        ("https://api.minimaxi.com/v1/responses", True),
        ("https://api.minimax.io/v1/responses", True),
        ("https://api.minimaxi.com/anthropic/v1/messages", True),
        ("https://api.minimax.io/anthropic/v1/messages", True),
        ("https://api.minimax.cn/anthropic/v1/messages", False),
        ("https://api.minimax.cn:invalid/anthropic/v1/messages", False),
    ],
)
def test_minimax_search_capabilities_are_limited_to_allowlisted_routes(base_url, expected_support):
    assert (
        LLMClient(
            "fixture-key", "MiniMax-M3", base_url, provider_name="minimax"
        ).supports_web_search
        is expected_support
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"event": False, "source": "citation"},
        {"event": True, "source": "none"},
        {"event": True, "source": "prose", "response_id": None},
        {"event": True, "source": "before_event"},
        {"event": True, "source": "action", "event_status": "incomplete"},
        {"event": True, "source": "citation", "actual_model": "Other-M3"},
        {"event": True, "source": "citation", "response_id": None},
        {"event": True, "source": "citation", "response_status": "incomplete"},
    ],
)
@patch("src.providers.llm_client.http_client_manager")
def test_minimax_search_fails_closed_without_correlated_evidence(mock_manager, kwargs):
    session = Mock()
    session.post.return_value = _minimax_search_response(**kwargs)
    mock_manager.get_sync_session.return_value = session
    result = LLMClient(
        "fixture-key", "MiniMax-M3", "https://api.minimaxi.com/v1/responses"
    ).send_search_request("Microsoft")
    assert not result.search_verified
    assert result.execution_metadata["search_status"] == "unverified"


@patch("src.providers.llm_client.http_client_manager")
def test_minimax_citation_is_attributed_to_the_preceding_completed_call(mock_manager):
    response = _minimax_search_response(source="citation")
    response.json.return_value["output"].insert(
        0,
        {
            "type": "web_search_call",
            "id": "ws_earlier_without_source",
            "status": "completed",
            "action": {"type": "search", "query": "earlier search"},
        },
    )
    session = Mock()
    session.post.return_value = response
    mock_manager.get_sync_session.return_value = session
    result = LLMClient(
        "fixture-key", "MiniMax-M3", "https://api.minimaxi.com/v1/responses"
    ).send_search_request("Microsoft")
    assert result.search_verified
    assert result.execution_metadata["search_receipt_id"] == "ws_mm_01"
    assert result.execution_metadata["web_search_calls"][0]["source_urls"] == []
    assert result.execution_metadata["web_search_calls"][1]["source_urls"] == [
        "https://example.com/source"
    ]


@patch("src.providers.llm_client.http_client_manager")
def test_minimax_citation_after_incomplete_search_is_not_reused(mock_manager):
    response = _minimax_search_response(source="citation")
    response.json.return_value["output"].insert(
        1,
        {
            "type": "web_search_call",
            "id": "ws_failed_after_valid_call",
            "status": "incomplete",
            "action": {"type": "search", "query": "failed search"},
        },
    )
    session = Mock()
    session.post.return_value = response
    mock_manager.get_sync_session.return_value = session
    result = LLMClient(
        "fixture-key", "MiniMax-M3", "https://api.minimaxi.com/v1/responses"
    ).send_search_request("Microsoft")
    assert not result.search_verified
    assert result.source_urls == ()


@pytest.mark.parametrize(
    "url,model",
    [
        ("https://api.example.invalid/v1/responses", "MiniMax-M3"),
        ("https://api.minimaxi.com/v1/chat/completions", "MiniMax-M3"),
        ("https://api.minimaxi.com:8443/v1/responses", "MiniMax-M3"),
        ("https://user:pass@api.minimaxi.com/v1/responses", "MiniMax-M3"),
        ("https://api.minimaxi.com/v1/responses", "MiniMax-M2.7"),
        ("https://api.minimax.cn/v1/messages", "MiniMax-M3"),
        ("https://api.minimax.cn/anthropic/v1/messages", "MiniMax-M3"),
    ],
)
@patch("src.providers.llm_client.http_client_manager")
def test_minimax_unsupported_host_or_model_never_sends(mock_manager, url, model):
    client = LLMClient("fixture-key", model, url)
    assert not client.supports_web_search
    with pytest.raises(SearchCapabilityUnavailable):
        client.send_search_request("Microsoft")
    mock_manager.get_sync_session.assert_not_called()


@pytest.mark.parametrize(
    "configured_provider,url,model",
    [
        ("openai", "https://api.minimaxi.com/v1/responses", "MiniMax-M3"),
        ("minimax", "https://api.openai.com/v1/responses", "gpt-4.1-mini"),
    ],
)
@patch("src.providers.llm_client.http_client_manager")
def test_canonical_provider_endpoint_mismatch_never_sends(
    mock_manager, configured_provider, url, model
):
    client = LLMClient("fixture-key", model, url, provider_name=configured_provider)
    assert not client.supports_web_search
    with pytest.raises(SearchCapabilityUnavailable):
        client.send_search_request("Microsoft")
    mock_manager.get_sync_session.assert_not_called()


@pytest.mark.parametrize("status", [400, 401, 429])
@patch("src.providers.llm_client.http_client_manager")
def test_minimax_http_failure_keeps_sanitized_provider_attempt(mock_manager, status):
    session = Mock()
    response = Mock()
    response.headers = {"x-request-id": f"failed-{status}"}
    response.status_code = status
    response.json.return_value = {"error": {"code": "rate_limit_exceeded"}} if status == 429 else {}
    response.raise_for_status.side_effect = requests.HTTPError("secret response body")
    session.post.return_value = response
    mock_manager.get_sync_session.return_value = session
    client = LLMClient("fixture-key", "MiniMax-M3", "https://api.minimaxi.com/v1/responses")
    with pytest.raises(LLMTransportAttemptError) as raised:
        client.send_search_request("private prompt")
    receipt = raised.value.attempt_receipt
    assert receipt["provider"] == "minimax"
    assert receipt["http_status_code"] == status
    assert receipt["request_id"] == f"failed-{status}"
    assert "private prompt" not in str(receipt)
    assert "secret response body" not in str(raised.value)


@pytest.mark.asyncio
@patch("src.providers.llm_client.http_client_manager")
async def test_async_minimax_search_uses_same_receipt_semantics(mock_manager):
    client = AsyncLLMClient("fixture-key", "MiniMax-M3", "https://api.minimaxi.com/v1/responses")
    http_client = AsyncMock()
    http_client.post.return_value = _minimax_search_response(source="citation")
    mock_manager.get_async_client = AsyncMock(return_value=http_client)
    result = await client.send_search_request_async("Microsoft")
    assert result.search_verified
    assert result.execution_metadata["provider"] == "minimax"
    assert result.execution_metadata["search_receipt_id"] == "ws_mm_01"
    assert http_client.post.call_args.args[0] == "https://api.minimaxi.com/v1/responses"


@pytest.mark.asyncio
@patch("src.providers.llm_client.http_client_manager")
async def test_async_minimax_anthropic_search_uses_same_receipt_semantics(mock_manager):
    endpoint = "https://api.minimaxi.com/anthropic/v1/messages"
    client = AsyncLLMClient("fixture-key", "MiniMax-M3", endpoint, provider_name="minimax")
    http_client = AsyncMock()
    http_client.post.return_value = _minimax_anthropic_response()
    mock_manager.get_async_client = AsyncMock(return_value=http_client)

    result = await client.send_search_request_async("Example issuer question")

    assert result.search_verified
    assert result.request_id is None
    assert result.execution_metadata["search_receipt_id"] == "srvtoolu_mm_01"
    assert http_client.post.call_args.args[0] == endpoint
    assert http_client.post.call_args.kwargs["headers"]["x-api-key"] == "fixture-key"
    assert http_client.post.call_args.kwargs["json"]["tools"][0]["max_uses"] == 1


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.xiaomimimo.com/v1",
        "https://token-plan-cn.xiaomimimo.com/v1",
        "https://api.xiaomimimo.com/v1/chat/completions",
    ],
)
@patch("src.providers.llm_client.http_client_manager")
def test_mimo_chat_search_binds_only_same_response_url_citations(mock_manager, base_url):
    response = Mock()
    response.headers = {"x-request-id": "req_mimo_01"}
    response.status_code = 200
    response.json.return_value = {
        "id": "chatcmpl_mimo_01",
        "object": "chat.completion",
        "model": "mimo-v2.6-flash",
        "usage": {
            "prompt_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 25},
            "completion_tokens": 40,
            "completion_tokens_details": {"reasoning_tokens": 5},
            "web_search_usage": {"tool_usage": 2, "page_usage": 3},
        },
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": '{"score":8,"description":"来自联网来源"}',
                    "annotations": [
                        {"type": "url_citation", "url": "https://example.com/source"},
                        {"type": "url_citation", "url": "https://example.com/source"},
                        {"type": "other", "url": "https://example.com/not-a-citation"},
                    ],
                    "tool_calls": None,
                },
            }
        ],
    }
    session = Mock()
    session.post.return_value = response
    mock_manager.get_sync_session.return_value = session
    client = LLMClient("fixture-key", "mimo-v2.6-flash", base_url, provider_name="mimo")

    result = client.send_search_request("issuer question", system_prompt="system context")

    assert result.search_verified
    assert result.source_urls == ("https://example.com/source",)
    assert result.execution_metadata["provider"] == "mimo"
    assert result.execution_metadata["search_receipt_id"] == "chatcmpl_mimo_01"
    assert result.execution_metadata["usage"] == {
        "schema": "quick-scan-usage-v1",
        "input_tokens": 75,
        "cached_input_tokens": 25,
        "cache_creation_input_tokens": 0,
        "output_tokens": 40,
        "reasoning_output_tokens": 5,
        "search_tool_calls": 2,
    }
    assert result.execution_metadata["web_search_calls"] == [
        {
            "id": "chatcmpl_mimo_01",
            "status": "completed",
            "action_type": "search",
            "source_urls": ["https://example.com/source"],
            "sources": [
                {"url": "https://example.com/source", "title": None, "published_date": None}
            ],
            "evidence_basis": "url_citation_annotations",
        }
    ]
    request = session.post.call_args
    assert (
        request.args[0]
        == "https://"
        + ("token-plan-cn.xiaomimimo.com" if "token-plan" in base_url else "api.xiaomimimo.com")
        + "/v1/chat/completions"
    )
    assert request.kwargs["headers"]["Authorization"] == "Bearer fixture-key"
    assert request.kwargs["json"]["messages"] == [
        {"role": "system", "content": "system context"},
        {"role": "user", "content": "issuer question"},
    ]
    assert request.kwargs["json"]["tools"] == [
        {"type": "web_search", "max_keyword": 2, "force_search": True, "limit": 3}
    ]
    assert request.kwargs["json"]["tool_choice"] == "auto"
    assert request.kwargs["json"]["thinking"] == {"type": "disabled"}
    assert request.kwargs["allow_redirects"] is False


@pytest.mark.parametrize(
    "response_overrides",
    [
        {"annotations": []},
        {"actual_model": "mimo-v2.6-pro"},
        {"finish_reason": "length"},
        {"response_id": None},
    ],
)
@patch("src.providers.llm_client.http_client_manager")
def test_mimo_chat_search_without_full_same_response_evidence_is_unverified(
    mock_manager, response_overrides
):
    message = {
        "role": "assistant",
        "content": '{"score":9,"description":"I searched the web"}',
        "annotations": [{"type": "url_citation", "url": "https://example.com/source"}],
    }
    choice = {"finish_reason": "stop", "message": message}
    body = {"id": "chatcmpl_mimo_02", "model": "mimo-v2.6-flash", "choices": [choice]}
    if "annotations" in response_overrides:
        message["annotations"] = response_overrides["annotations"]
    if "actual_model" in response_overrides:
        body["model"] = response_overrides["actual_model"]
    if "finish_reason" in response_overrides:
        choice["finish_reason"] = response_overrides["finish_reason"]
    if "response_id" in response_overrides:
        body["id"] = response_overrides["response_id"]
    response = Mock()
    response.headers = {}
    response.status_code = 200
    response.json.return_value = body
    session = Mock()
    session.post.return_value = response
    mock_manager.get_sync_session.return_value = session
    client = LLMClient(
        "fixture-key",
        "mimo-v2.6-flash",
        "https://api.xiaomimimo.com/v1",
        provider_name="mimo",
    )

    result = client.send_search_request("issuer question")

    assert not result.search_verified
    assert result.execution_metadata["search_status"] == "unverified"
    assert result.execution_metadata["search_receipt_id"] is None


@pytest.mark.parametrize(
    "base_url,model,provider",
    [
        ("https://api.xiaomimimo.com/v1/responses", "mimo-v2.6-flash", "mimo"),
        ("https://api.xiaomimimo.com/v1/chat/completions", "mimo-v2.5", "mimo"),
        (
            "https://api.xiaomimimo.com/v1/chat/completions",
            "mimo-v2.6-flash",
            "deepseek",
        ),
    ],
)
@patch("src.providers.llm_client.http_client_manager")
def test_mimo_search_rejects_unsupported_path_model_or_provider_before_http(
    mock_manager, base_url, model, provider
):
    client = LLMClient("fixture-key", model, base_url, provider_name=provider)

    assert not client.supports_web_search
    with pytest.raises(SearchCapabilityUnavailable):
        client.send_search_request("issuer question")
    mock_manager.get_sync_session.assert_not_called()


@pytest.mark.asyncio
@patch("src.providers.llm_client.http_client_manager")
async def test_async_mimo_search_uses_same_citation_contract(mock_manager):
    response = Mock()
    response.headers = {"x-request-id": "req_mimo_async_01"}
    response.status_code = 200
    response.json.return_value = {
        "id": "chatcmpl_mimo_async_01",
        "model": "mimo-v2.6-flash",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": '{"score":7,"description":"引用可核验"}',
                    "annotations": [{"type": "url_citation", "url": "https://example.com/async"}],
                },
            }
        ],
    }
    http_client = AsyncMock()
    http_client.post.return_value = response
    mock_manager.get_async_client = AsyncMock(return_value=http_client)
    client = AsyncLLMClient(
        "fixture-key",
        "mimo-v2.6-flash",
        "https://api.xiaomimimo.com/v1",
        provider_name="mimo",
    )

    result = await client.send_search_request_async("issuer question")

    assert result.search_verified
    assert result.execution_metadata["provider"] == "mimo"
    assert result.source_urls == ("https://example.com/async",)
    assert http_client.post.call_args.args[0] == "https://api.xiaomimimo.com/v1/chat/completions"
    assert http_client.post.call_args.kwargs["json"]["tools"][0]["force_search"] is True
    assert http_client.post.call_args.kwargs["follow_redirects"] is False
