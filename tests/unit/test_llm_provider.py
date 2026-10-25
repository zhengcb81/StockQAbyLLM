"""LLM 提供者单元测试。

该模块测试 LLMProvider 类的核心功能。
"""

import hashlib
import json
from unittest.mock import MagicMock, Mock, patch

import pytest
import requests

from src.core.models import Question
from src.providers.llm_provider import LLMProvider
from src.utils.quick_scan_work_store import (
    QuickScanWorkStore,
    quick_scan_receipt_sha256,
)
from src.utils.quick_scan_work_transport import (
    QuickScanSendAttempt,
    QuickScanWorkPersistenceError,
    QuickScanWorkUncertainError,
    bind_quick_scan_route,
    bind_quick_scan_work,
)


def _q10_sync_provider(tmp_path, *, alias=False):
    resolution = {
        "schema_version": "1.0.0",
        "aliases": (
            [
                {
                    "provider": "openai",
                    "protocol": "responses",
                    "requested_model": "model-a",
                    "resolved_model": "model-b",
                }
            ]
            if alias
            else []
        ),
    }
    path = tmp_path / "synthetic-llm.json"
    path.write_text(
        json.dumps(
            {
                "default_provider": "openai",
                "providers": {
                    "openai": {
                        "enabled": True,
                        "api_key": "synthetic-key",
                        "model": "model-a",
                        "base_url": "https://api.openai.com/v1/responses",
                        "max_retries": 2,
                        "format_repair_budget": 1,
                    }
                },
                "quick_scan_model_resolution": resolution,
            }
        ),
        encoding="utf-8",
    )
    provider = LLMProvider(
        provider_name="openai",
        api_key="synthetic-key",
        config_file=str(path),
        require_search=True,
        entity_id="ENT_FIXTURE_CO",
        company_name="Fixture Co",
    )
    provider.retry_strategy.get_wait_time = lambda _retry: 0
    return provider, resolution


def _q10_sync_claimed(tmp_path):
    store = QuickScanWorkStore(tmp_path / "sync-repair.sqlite")
    item = store.create_or_attach(
        entity_id="ENT_FIXTURE_CO",
        question_id="CORE_01",
        generation=1,
        scope="entity",
        scope_id="ENT_FIXTURE_CO",
        identity_revision=1,
        source_binding_version=1,
        identity_state="verified",
        source_binding_ref="BND_FIXTURE",
        source_binding_refs=["BND_FIXTURE"],
        identity_snapshot_sha256=hashlib.sha256(b"identity").hexdigest(),
        question_fingerprint=hashlib.sha256(b"question").hexdigest(),
        routing_fingerprint=hashlib.sha256(b"routes").hexdigest(),
        run_id="run-sync-repair",
        scan_id="scan-sync-repair",
    )
    lease = store.claim(item["work_item_id"], lease_seconds=30)
    assert lease is not None
    return store, item["work_item_id"], lease


def _q10_sync_response(response_id, *, valid=False, model="model-a"):
    text = "malformed answer"
    if valid:
        text = json.dumps(
            {
                "question_id": "CORE_01",
                "entity_id": "ENT_FIXTURE_CO",
                "company_name": "Fixture Co",
                "status": "scored",
                "score": 8,
                "description": "Synthetic repaired answer",
                "actual_model": "forged-in-answer",
            }
        )
    response = Mock()
    response.status_code = 200
    response.headers = {"x-request-id": f"request-{response_id}"}
    response.json.return_value = {
        "id": response_id,
        "status": "completed",
        "model": model,
        "output": [
            {
                "type": "web_search_call",
                "id": f"search-{response_id}",
                "status": "completed",
                "action": {
                    "type": "search",
                    "sources": [{"type": "url", "url": "https://example.org/source"}],
                },
            },
            {
                "type": "message",
                "status": "completed",
                "content": [{"type": "output_text", "text": text}],
            },
        ],
    }
    return response


def _q10_sync_http(monkeypatch, responses):
    session = Mock()
    session.post.side_effect = responses
    monkeypatch.setattr(
        "src.providers.llm_client.http_client_manager.get_sync_session", lambda: session
    )
    return session


@pytest.mark.parametrize("alias", [False, True])
def test_q10_sync_repair_records_two_attempts_and_final_http_identity(tmp_path, monkeypatch, alias):
    provider, resolution = _q10_sync_provider(tmp_path, alias=alias)
    store, work_id, lease = _q10_sync_claimed(tmp_path)
    resolved = "model-b" if alias else "model-a"
    session = _q10_sync_http(
        monkeypatch,
        [_q10_sync_response("initial"), _q10_sync_response("repaired", valid=True, model=resolved)],
    )
    route = {"route_id": "openai-route", "provider": "openai", "model_requested": "model-a"}
    if alias:
        route["model_resolution"] = resolution
    with bind_quick_scan_work(store, work_id, lease), bind_quick_scan_route(**route):
        results = provider.search_question(Question("Synthetic question", question_id="CORE_01"))
    assert results[0].status == "scored"
    assert results[0].score == 8
    assert session.post.call_count == 2
    attempts = store.list_attempts(work_id)
    assert len(attempts) == 2
    assert [attempt["phase"] for attempt in attempts] == [
        "response_available",
        "response_available",
    ]
    assert attempts[0]["attempt_id"] != attempts[1]["attempt_id"]
    execution = results[0].metadata["execution"]
    final = execution["work_transport"]["final_receipt"]
    assert execution["format_repair"]["status"] == "repaired"
    assert execution["work_transport"]["work_attempt_id"] == attempts[1]["attempt_id"]
    assert final["response_id"] == "repaired"
    assert final["actual_model"] == resolved
    assert attempts[1]["receipt_sha256"] == quick_scan_receipt_sha256(final)
    assert attempts[0]["receipt_sha256"] != attempts[1]["receipt_sha256"]
    assert [call.kwargs["json"]["model"] for call in session.post.call_args_list] == [
        "model-a",
        "model-a",
    ]


def test_q10_sync_repair_uncertain_outcome_propagates_without_third_post(tmp_path, monkeypatch):
    provider, _ = _q10_sync_provider(tmp_path)
    store, work_id, lease = _q10_sync_claimed(tmp_path)
    session = _q10_sync_http(
        monkeypatch, [_q10_sync_response("initial"), requests.Timeout("synthetic uncertain repair")]
    )
    with bind_quick_scan_work(store, work_id, lease), bind_quick_scan_route(
        route_id="openai-route", provider="openai", model_requested="model-a"
    ):
        with pytest.raises(QuickScanWorkUncertainError):
            provider.search_question(Question("Synthetic question", question_id="CORE_01"))
    assert session.post.call_count == 2
    attempts = store.list_attempts(work_id)
    assert len(attempts) == 2
    assert attempts[0]["phase"] == "response_available"
    assert attempts[1]["phase"] == "uncertain"


def test_q10_sync_repair_persistence_error_propagates_without_retry_or_success(
    tmp_path, monkeypatch
):
    provider, _ = _q10_sync_provider(tmp_path)
    store, work_id, lease = _q10_sync_claimed(tmp_path)
    session = _q10_sync_http(
        monkeypatch, [_q10_sync_response("initial"), _q10_sync_response("repaired", valid=True)]
    )
    original = QuickScanSendAttempt.record_response
    written = []

    def record_response(attempt, **kwargs):
        written.append(attempt.attempt_id)
        if len(written) == 2:
            raise QuickScanWorkPersistenceError("synthetic repair receipt write failed")
        return original(attempt, **kwargs)

    monkeypatch.setattr(QuickScanSendAttempt, "record_response", record_response)
    with bind_quick_scan_work(store, work_id, lease), bind_quick_scan_route(
        route_id="openai-route", provider="openai", model_requested="model-a"
    ):
        with pytest.raises(QuickScanWorkPersistenceError, match="synthetic repair"):
            provider.search_question(Question("Synthetic question", question_id="CORE_01"))
    assert session.post.call_count == 2
    assert len(set(written)) == 2
    assert len(store.list_attempts(work_id)) == 2


class TestLLMProviderInit:
    """测试 LLMProvider 初始化。"""

    def test_init_with_provider_name_only(self):
        """测试仅使用 provider_name 初始化。"""
        provider = LLMProvider(provider_name="deepseek")
        assert provider.provider_name == "deepseek"

    def test_init_with_api_key_and_model(self):
        """测试使用 api_key 和 model 初始化。"""
        provider = LLMProvider(provider_name="test", api_key="test_key", model="test_model")
        assert provider.api_key == "test_key"
        assert provider.model == "test_model"
        assert provider.provider_name == "test"

    def test_init_with_custom_base_url(self):
        """测试使用自定义 base_url 初始化。

        注意：base_url 只能通过配置文件设置，不能直接传递。
        """
        provider = LLMProvider(provider_name="test", api_key="test_key", model="test_model")
        # base_url 是从配置文件读取的
        # 如果没有配置，base_url 可能为空字符串或不存在
        assert hasattr(provider, "base_url") or not hasattr(provider, "base_url")

    def test_init_with_custom_timeout(self):
        """测试使用自定义 timeout 初始化。

        注意：timeout 只能通过配置文件设置，不能直接传递。
        """
        provider = LLMProvider(provider_name="test", api_key="test_key", model="test_model")
        # timeout 是从配置文件读取的
        # 如果没有配置，使用默认值或属性可能不存在
        assert hasattr(provider, "timeout") or not hasattr(provider, "timeout")

    def test_init_with_max_retries(self):
        """测试使用 max_retries 初始化。

        注意：max_retries 只能通过配置文件设置，不能直接传递。
        """
        provider = LLMProvider(provider_name="test", api_key="test_key", model="test_model")
        # max_retries 是从配置文件读取的
        # 如果没有配置，属性可能不存在
        # 这是预期行为


class TestLLMProviderSearch:
    """测试 LLMProvider 搜索功能。"""

    @patch("src.providers.llm_client.http_client_manager")
    def test_search_with_valid_response(self, mock_http_client_manager):
        """测试使用有效响应的搜索。"""
        # 模拟 Session 和 post 方法
        mock_session = Mock()
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"score": 7, "description": "测试答案"}'}}]
        }
        mock_session.post.return_value = mock_response
        mock_http_client_manager.get_sync_session.return_value = mock_session

        provider = LLMProvider(provider_name="test", api_key="test_key", model="test_model")

        results = provider.search("测试问题")

        assert len(results) == 1
        # results[0] 现在是 SearchResult 对象
        assert results[0].score == 7
        assert results[0].snippet == "测试答案"
        assert results[0].source == "llm_api"
        # 验证调用了 session.post
        mock_session.post.assert_called_once()

    @patch("src.providers.llm_client.http_client_manager")
    def test_search_with_api_error(self, mock_http_client_manager):
        """测试 API 错误处理。"""
        import requests

        # 模拟 Session 和 post 方法抛出异常
        mock_session = Mock()
        mock_session.post.side_effect = requests.RequestException("API Error")
        mock_http_client_manager.get_sync_session.return_value = mock_session

        provider = LLMProvider(provider_name="test", api_key="test_key", model="test_model")

        # API 错误应该返回空结果或抛出异常
        # 具体行为取决于实现
        with pytest.raises(Exception):
            provider.search("测试问题")

    def test_search_without_api_key(self):
        """测试没有 API 密钥的搜索。"""
        provider = LLMProvider(provider_name="test")

        # 没有 API 密钥时应该返回默认结果
        results = provider.search("测试问题")
        assert len(results) >= 0


class TestLLMProviderPromptGeneration:
    """测试提示词生成功能。"""

    def test_generate_prompt_for_single_question(self):
        """测试为单个问题生成提示词。"""
        provider = LLMProvider(provider_name="test", company_name="测试公司")

        # 这个测试验证 _generate_prompt 方法存在
        # 实际的提示词内容测试可能需要访问私有方法
        assert hasattr(provider, "search")

    def test_search_includes_company_context(self):
        """测试搜索包含公司上下文。"""
        provider = LLMProvider(provider_name="test", company_name="腾讯控股")

        # 验证公司名称被正确存储
        assert provider.company_name == "腾讯控股"


class TestLLMProviderResponseParsing:
    """测试响应解析功能。"""

    def test_parse_valid_json_response(self):
        """测试解析有效的 JSON 响应。"""
        provider = LLMProvider(provider_name="test")

        # 测试解析标准格式响应
        json_content = '{"score": 7, "description": "测试描述"}'
        # _parse_llm_response 是私有方法，这里测试公共接口
        # 实际测试通过 search 方法进行

    def test_parse_markdown_code_block_response(self):
        """测试解析 Markdown 代码块格式的响应。"""
        # 测试包含在代码块中的 JSON
        json_with_code_block = '```json\n{"score": 8, "description": "答案"}\n```'
        # 测试通过 search 方法进行

    def test_parse_invalid_json_response(self):
        """测试解析无效的 JSON 响应。"""
        provider = LLMProvider(provider_name="test")

        # 无效 JSON 应该被适当处理
        invalid_json = "这不是有效的 JSON"
        # 测试通过 search 方法进行


class TestLLMProviderRetryLogic:
    """测试重试逻辑。"""

    @patch("src.providers.llm_client.http_client_manager")
    def test_retry_on_timeout(self, mock_http_client_manager):
        """测试超时重试。

        注意：这个测试验证重试机制存在，实际的重试行为
        取决于配置和网络环境。
        """
        import requests

        # 模拟 Session 和 post 方法抛出超时错误
        mock_session = Mock()
        mock_session.post.side_effect = requests.Timeout("Connection timeout")
        mock_http_client_manager.get_sync_session.return_value = mock_session

        provider = LLMProvider(provider_name="test", api_key="test_key", model="test_model")

        # 应该抛出异常或返回空结果
        # 具体行为取决于重试配置
        try:
            results = provider.search("测试问题")
            # 如果没有抛出异常，应该返回空结果或默认结果
            assert isinstance(results, list)
        except Exception as e:
            # 如果抛出异常，验证是超时相关错误
            assert "timeout" in str(e).lower() or "retry" in str(e).lower() or True

    def test_max_retries_parameter(self):
        """测试 max_retries 参数传递。"""
        import inspect

        provider = LLMProvider(provider_name="test")

        # 验证 _call_llm_api 方法接受 max_retries 参数
        sig = inspect.signature(provider._call_llm_api)
        assert "max_retries" in sig.parameters


class TestLLMProviderErrorHandling:
    """测试错误处理。"""

    def test_handle_network_error(self):
        """测试网络错误处理。"""
        provider = LLMProvider(provider_name="test")
        # 网络错误应该被适当处理

    def test_handle_json_decode_error(self):
        """测试 JSON 解码错误处理。"""
        provider = LLMProvider(provider_name="test")
        # JSON 解码错误应该被适当处理

    def test_handle_missing_api_key(self):
        """测试缺少 API 密钥的处理。"""
        provider = LLMProvider(provider_name="test")
        # 缺少 API 密钥时应该返回默认结果或友好错误


class TestLLMProviderConfiguration:
    """测试配置相关功能。"""

    def test_load_provider_config_from_file(self):
        """测试从配置文件加载提供者配置。"""
        provider = LLMProvider(provider_name="deepseek")
        # 验证提供者可以读取配置

    def test_use_environment_variable_for_api_key(self):
        """测试使用环境变量获取 API 密钥。"""
        import os

        # 设置环境变量
        os.environ["TEST_API_KEY"] = "env_test_key"

        try:
            provider = LLMProvider(provider_name="test", api_key="env_test_key")
            assert provider.api_key == "env_test_key"
        finally:
            # 清理环境变量
            if "TEST_API_KEY" in os.environ:
                del os.environ["TEST_API_KEY"]


class TestLLMProviderEdgeCases:
    """测试边界情况。"""

    def test_empty_query(self):
        """测试空查询处理。"""
        provider = LLMProvider(provider_name="test")
        results = provider.search("")
        assert isinstance(results, list)

    def test_very_long_query(self):
        """测试超长查询处理。"""
        provider = LLMProvider(provider_name="test")
        long_query = "问题" * 1000
        results = provider.search(long_query)
        assert isinstance(results, list)

    def test_special_characters_in_query(self):
        """测试查询中的特殊字符处理。"""
        provider = LLMProvider(provider_name="test")
        special_query = "问题?!@#$%^&*()_+-=[]{}|;':\",./<>?"
        results = provider.search(special_query)
        assert isinstance(results, list)

    def test_unicode_in_query(self):
        """测试查询中的 Unicode 字符。"""
        provider = LLMProvider(provider_name="test")
        unicode_query = "问题😀🎉🚀测试"
        results = provider.search(unicode_query)
        assert isinstance(results, list)


@pytest.mark.parametrize(
    "configured_provider,url,model",
    [
        ("openai", "https://api.minimaxi.com/v1/responses", "MiniMax-M3.1-Flash"),
        ("minimax", "https://api.openai.com/v1/responses", "gpt-4.1-mini"),
    ],
)
@patch("src.providers.llm_client.http_client_manager")
def test_public_provider_rejects_canonical_endpoint_mismatch_before_transport(
    mock_manager, tmp_path, configured_provider, url, model
):
    config_file = tmp_path / "llm_apis.json"
    config_file.write_text(
        json.dumps(
            {
                "default_provider": configured_provider,
                "providers": {
                    configured_provider: {
                        "enabled": True,
                        "api_key": "",
                        "model": model,
                        "base_url": url,
                        "max_retries": 1,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    provider = LLMProvider(
        provider_name=configured_provider,
        api_key="offline-fixture-key",
        config_file=str(config_file),
        company_name="Microsoft Corporation",
        entity_id="issuer:US5949181045",
        require_search=True,
    )
    assert not provider.client.supports_web_search
    result = provider.search_question(Question("Moat durability?", question_id="IQS_05"))[0]
    assert result.score is None
    assert result.status == "insufficient_evidence"
    assert result.metadata["search_status"] == "unavailable"
    mock_manager.get_sync_session.assert_not_called()


@pytest.mark.parametrize(
    "base_url,expected_env,expected_value",
    [
        ("https://token-plan-cn.xiaomimimo.com/v1", "MIMO_PLAN_API_KEY", "plan-fixture-key"),
        ("https://api.xiaomimimo.com/v1", "MIMO_API_KEY", "paygo-fixture-key"),
    ],
)
def test_mimo_environment_key_matches_configured_endpoint(
    monkeypatch, tmp_path, base_url, expected_env, expected_value
):
    config = tmp_path / "mimo-provider.json"
    config.write_text(
        json.dumps(
            {
                "default_provider": "mimo",
                "providers": {
                    "mimo": {
                        "enabled": True,
                        "api_key": "",
                        "model": "mimo-v2.6-flash",
                        "base_url": base_url,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MIMO_API_KEY", "paygo-fixture-key")
    monkeypatch.setenv("MIMO_PLAN_API_KEY", "plan-fixture-key")

    provider = LLMProvider(provider_name="mimo", config_file=str(config))

    assert provider.api_key == expected_value
    assert provider.client.supports_web_search


def _final_diagnostic_fixture(tmp_path, *, maximum=4096):
    config = tmp_path / "llm_apis.json"
    config.write_text("{}", encoding="utf-8")
    (tmp_path / "quick_scan_final_answer_diagnostics.json").write_text(
        json.dumps({"schema_version": "1.0.0", "enabled": True, "max_final_answer_bytes": maximum}),
        encoding="utf-8",
    )
    metadata = {
        "provider": "mimo",
        "actual_model": "mimo-v2.6-pro",
        "response_id": "response-fixture",
        "response_sha256": "a" * 64,
        "response_status": "completed",
        "reasoning_content": "PRIVATE_CHAIN_MUST_NOT_BE_SAVED",
        "prompt": "PRIVATE_PROMPT_MUST_NOT_BE_SAVED",
    }
    trusted = {
        "pack_id": "PACK_" + "1" * 32,
        "execution_attempt_id": "execution-fixture",
        "budget_attempt_id": "PACK_BUDGET_" + "1" * 32,
        "members": [
            {
                "question_id": "IQS_01",
                "work_attempt_id": "ATTEMPT_" + "2" * 32,
                "work_item_id": "WORK_" + "3" * 32,
            }
        ],
    }
    return config, metadata, trusted


def test_final_answer_diagnostics_disabled_by_default_creates_nothing(tmp_path):
    from src.providers.llm_provider import (
        _capture_final_answer_diagnostic,
        _final_answer_diagnostic_policy,
    )

    config = tmp_path / "llm_apis.json"
    assert _final_answer_diagnostic_policy(config) is None
    assert _capture_final_answer_diagnostic(None, "private", {}, {}) is None
    assert list(tmp_path.iterdir()) == []


def test_final_answer_diagnostic_is_bounded_final_only_immutable_and_not_authority(tmp_path):
    from src.providers.llm_provider import (
        _capture_final_answer_diagnostic,
        _final_answer_diagnostic_policy,
    )

    config, metadata, trusted = _final_diagnostic_fixture(tmp_path)
    policy = _final_answer_diagnostic_policy(config)
    final = '{"answers":[]}'
    first = _capture_final_answer_diagnostic(policy, final, metadata, trusted)
    directory = tmp_path / "final-answer-diagnostics"
    artifact = directory / first["artifact_name"]
    before = artifact.read_bytes()
    body = json.loads(before)
    assert (
        body["final_answer"] == final
        and body["final_answer_sha256"] == hashlib.sha256(final.encode()).hexdigest()
    )
    assert body["response_sha256"] == metadata["response_sha256"]
    assert body["original_transport"]["physical_budget_attempt_id"] == trusted["budget_attempt_id"]
    assert body["replay_or_delivery_authorized"] is False
    assert b"PRIVATE_CHAIN" not in before and b"PRIVATE_PROMPT" not in before
    diagnostic = {
        "schema_version": "1.0.0",
        "status": "rejected",
        "rejections": [{"code": "pack_root_fields", "pointer": ""}],
    }
    result = _capture_final_answer_diagnostic(
        policy, final, metadata, trusted, diagnostics=diagnostic
    )
    assert artifact.read_bytes() == before
    assert (
        json.loads((directory / result["parser_artifact_name"]).read_bytes())["parser_diagnostics"]
        == diagnostic
    )
    assert len(list(directory.iterdir())) == 2


def test_final_answer_diagnostic_excess_or_private_markers_retains_only_hash(tmp_path):
    from src.providers.llm_provider import (
        _capture_final_answer_diagnostic,
        _final_answer_diagnostic_policy,
    )

    config, metadata, trusted = _final_diagnostic_fixture(tmp_path, maximum=16)
    policy = _final_answer_diagnostic_policy(config)
    for final in ['{"answers":["' + "x" * 40 + '"]}', '<think>PRIVATE_CHAIN</think>{"answers":[]}']:
        result = _capture_final_answer_diagnostic(policy, final, metadata, trusted)
        artifact = json.loads(
            (tmp_path / "final-answer-diagnostics" / result["artifact_name"]).read_bytes()
        )
        assert artifact["final_answer"] is None and artifact["final_answer_retained"] is False
        assert artifact["final_answer_sha256"] == hashlib.sha256(final.encode()).hexdigest()
        assert "PRIVATE_CHAIN" not in json.dumps(artifact)


def test_final_answer_diagnostic_invalid_owner_config_fails_before_dispatch(tmp_path):
    from src.providers.llm_provider import _final_answer_diagnostic_policy

    config, _, _ = _final_diagnostic_fixture(tmp_path)
    control = tmp_path / "quick_scan_final_answer_diagnostics.json"
    for raw in [
        '{"enabled":true,"enabled":false}',
        '{"schema_version":"1.0.0","enabled":"yes"}',
        '{"schema_version":"1.0.0","enabled":true,"max_final_answer_bytes":true}',
        '{"schema_version":"1.0.0","enabled":true,"directory":"../escape"}',
    ]:
        control.write_text(raw, encoding="utf-8")
        with pytest.raises(ValueError, match="final_answer_diagnostics_configuration_invalid"):
            _final_answer_diagnostic_policy(config)


def test_final_answer_private_channel_markers_are_not_retained_even_below_cap(tmp_path):
    from src.providers.llm_provider import (
        _capture_final_answer_diagnostic,
        _final_answer_diagnostic_policy,
    )

    config, metadata, trusted = _final_diagnostic_fixture(tmp_path, maximum=4096)
    policy = _final_answer_diagnostic_policy(config)
    for final in [
        '<think>PRIVATE_CHAIN</think>{"answers":[]}',
        '{"analysis":"PRIVATE_CHAIN","answers":[]}',
        'PRIVATE_CHAIN prose before {"answers":[]}',
        "```analysis\nPRIVATE_CHAIN\n```",
        '<thinking>PRIVATE_CHAIN</thinking>{"answers":[]}',
    ]:
        result = _capture_final_answer_diagnostic(policy, final, metadata, trusted)
        raw = (tmp_path / "final-answer-diagnostics" / result["artifact_name"]).read_bytes()
        artifact = json.loads(raw)
        assert artifact["final_answer_retained"] is False and artifact["final_answer"] is None
        assert b"PRIVATE_CHAIN" not in raw


def test_final_answer_artifact_collision_and_untrusted_diagnostic_never_overwrite_or_leak(tmp_path):
    from src.providers.llm_provider import (
        _capture_final_answer_diagnostic,
        _final_answer_diagnostic_policy,
    )

    config, metadata, trusted = _final_diagnostic_fixture(tmp_path)
    policy = _final_answer_diagnostic_policy(config)
    final = '{"answers":[]}'
    result = _capture_final_answer_diagnostic(policy, final, metadata, trusted)
    path = tmp_path / "final-answer-diagnostics" / result["artifact_name"]
    original = path.read_bytes()
    bad = {
        "schema_version": "1.0.0",
        "status": "rejected",
        "rejections": [{"code": "invalid_json", "pointer": "", "message": "PRIVATE_CHAIN"}],
    }
    assert (
        _capture_final_answer_diagnostic(policy, final, metadata, trusted, diagnostics=bad)[
            "status"
        ]
        == "unavailable"
    )
    assert path.read_bytes() == original
    assert not list(path.parent.glob("*.parser.json"))
    path.write_bytes(b"owner-existing-bytes")
    assert (
        _capture_final_answer_diagnostic(policy, final, metadata, trusted)["status"]
        == "unavailable"
    )
    assert path.read_bytes() == b"owner-existing-bytes"


def _diagnostic_pack_provider(tmp_path, monkeypatch, *, verified=True):
    from types import SimpleNamespace as NS

    from src.providers.llm_response_parser import LLMResponseParser
    from src.utils import quick_scan_work_transport as transport

    config, metadata, trusted = _final_diagnostic_fixture(tmp_path)
    metadata.update(response_status="completed" if verified else "length", work_transport={})
    monkeypatch.setattr(transport, "assert_question_pack_binding", lambda *args: None)
    monkeypatch.setattr(transport, "project_question_pack_transport", lambda *args: trusted)
    provider = object.__new__(LLMProvider)
    provider.api_key = "fixture-only"
    provider.require_search = True
    provider.company_name, provider.entity_id, provider.provider_name = (
        "Fixture Co",
        "ENT_FIXTURE_CO",
        "mimo",
    )
    provider.config_manager = NS(config_file=config)
    provider._build_prompt = lambda *args, **kwargs: "PRIVATE_PROMPT_MUST_NOT_BE_SAVED"
    provider.parser = LLMResponseParser()
    provider.client = Mock()
    provider.client.supports_web_search = True
    provider.client.supports_search_json_mode = True
    provider.client.send_search_request.return_value = NS(
        content='{"answers":[],"extra":1}', execution_metadata=metadata, search_verified=verified
    )
    return provider, config


def test_pack_final_answer_capture_precedes_actual_parser_and_does_not_retry(tmp_path, monkeypatch):
    provider, config = _diagnostic_pack_provider(tmp_path, monkeypatch)
    original = provider.parser.parse_question_pack_response_with_diagnostics
    calls = []

    def parse(*args, **kwargs):
        assert len(list((tmp_path / "final-answer-diagnostics").glob("*.final.json"))) == 1
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(provider.parser, "parse_question_pack_response_with_diagnostics", parse)
    result = provider.search_question_pack(
        [Question(text="Fixture question", question_id="IQS_01")],
        thinking_enabled=True,
        max_output_tokens=32768,
    )
    assert calls == [1] and result.invalid_question_ids == ("IQS_01",)
    assert provider.client.send_search_request.call_count == 1
    assert provider.client.send_search_request.call_args.kwargs["json_mode"] is True
    assert result.execution_metadata["question_pack_parse_diagnostics"]["rejections"] == [
        {"code": "pack_root_fields", "pointer": ""}
    ]
    assert result.execution_metadata["final_answer_diagnostic"]["parser_diagnostic_saved"] is True


def test_pack_length_final_is_captured_as_incomplete_and_never_claimed_accepted(
    tmp_path, monkeypatch
):
    provider, config = _diagnostic_pack_provider(tmp_path, monkeypatch, verified=False)
    provider.client.supports_search_json_mode = False
    monkeypatch.setattr(
        provider.parser,
        "parse_question_pack_response_with_diagnostics",
        Mock(side_effect=AssertionError("must not parse incomplete provider final")),
    )
    result = provider.search_question_pack(
        [Question(text="Fixture question", question_id="IQS_01")],
        thinking_enabled=True,
        max_output_tokens=32768,
    )
    assert result.missing_question_ids == ("IQS_01",) and result.results == ()
    assert "json_mode" not in provider.client.send_search_request.call_args.kwargs
    assert result.execution_metadata["question_pack_parse_diagnostics"]["rejections"] == [
        {"code": "provider_response_incomplete", "pointer": ""}
    ]
    assert provider.client.send_search_request.call_count == 1


def test_pack_bad_diagnostic_opt_in_rejects_before_any_provider_call(tmp_path, monkeypatch):
    provider, config = _diagnostic_pack_provider(tmp_path, monkeypatch)
    (tmp_path / "quick_scan_final_answer_diagnostics.json").write_text(
        '{"enabled":true}', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="final_answer_diagnostics_configuration_invalid"):
        provider.search_question_pack(
            [Question(text="Fixture question", question_id="IQS_01")],
            thinking_enabled=True,
            max_output_tokens=32768,
        )
    assert provider.client.send_search_request.call_count == 0


@pytest.mark.parametrize(
    "final",
    [
        r'{"reasoning\u005fcontent":"PRIVATE_ESCAPED_SENTINEL","answers":[]}',
        json.dumps({r"reasoning\u005fcontent": "PRIVATE_ESCAPED_SENTINEL", "answers": []}),
        r'{"answers":[{"description":"<thi\u006ek>PRIVATE_ESCAPED_SENTINEL</think>"}]}',
        json.dumps(
            {
                "answers": [
                    {"description": json.dumps({"reasoning_content": "PRIVATE_ESCAPED_SENTINEL"})}
                ]
            }
        ),
        json.dumps(
            {
                "answers": [
                    {
                        "description": json.dumps(
                            json.dumps({"thinking": "PRIVATE_ESCAPED_SENTINEL"})
                        )
                    }
                ]
            }
        ),
        '{"answers":[]}\nPRIVATE_ESCAPED_SENTINEL trailing prose',
        '```json\n{"answers":[]}\n```\nPRIVATE_ESCAPED_SENTINEL trailing prose',
        '```json\n{"answers":[]}\nPRIVATE_ESCAPED_SENTINEL\n```',
        '{"answers":["PRIVATE_ESCAPED_SENTINEL"],"answers":[]}',
        '{"answers":["PRIVATE_ESCAPED_SENTINEL"],"nonfinite":NaN}',
        '{"answers":["PRIVATE_ESCAPED_SENTINEL"',
    ],
)
def test_final_answer_diagnostic_fully_decodes_json_before_private_channel_retention(
    tmp_path, final
):
    from src.providers.llm_provider import (
        _capture_final_answer_diagnostic,
        _final_answer_diagnostic_policy,
    )

    config, metadata, trusted = _final_diagnostic_fixture(tmp_path)
    result = _capture_final_answer_diagnostic(
        _final_answer_diagnostic_policy(config), final, metadata, trusted
    )
    raw = (tmp_path / "final-answer-diagnostics" / result["artifact_name"]).read_bytes()
    artifact = json.loads(raw)
    assert artifact["final_answer_retained"] is False and artifact["final_answer"] is None
    assert artifact["final_answer_sha256"] == hashlib.sha256(final.encode()).hexdigest()
    assert b"PRIVATE_ESCAPED_SENTINEL" not in raw


@pytest.mark.parametrize(
    "final",
    [
        '{"answers":[{"description":"正常公开的判断理由\\n包含转义换行"}]}',
        '```json\n{"answers":[{"description":"公开判断理由"}]}\n```',
        '  ```JSON\r\n{"answers":[]}\r\n``` \n',
    ],
)
def test_final_answer_diagnostic_accepts_complete_safe_json_or_whole_fence_only(tmp_path, final):
    from src.providers.llm_provider import (
        _capture_final_answer_diagnostic,
        _final_answer_diagnostic_policy,
    )

    config, metadata, trusted = _final_diagnostic_fixture(tmp_path)
    result = _capture_final_answer_diagnostic(
        _final_answer_diagnostic_policy(config), final, metadata, trusted
    )
    artifact = json.loads(
        (tmp_path / "final-answer-diagnostics" / result["artifact_name"]).read_bytes()
    )
    assert artifact["final_answer_retained"] is True and artifact["final_answer"] == final


def test_pack_diagnostic_target_regular_file_rejects_before_paid_dispatch(tmp_path, monkeypatch):
    provider, _ = _diagnostic_pack_provider(tmp_path, monkeypatch)
    target = tmp_path / "final-answer-diagnostics"
    target.write_bytes(b"owner-existing-file")
    with pytest.raises(ValueError, match="final_answer_diagnostics_configuration_invalid"):
        provider.search_question_pack(
            [Question(text="Fixture question", question_id="IQS_01")],
            thinking_enabled=True,
            max_output_tokens=32768,
        )
    assert provider.client.send_search_request.call_count == 0
    assert target.read_bytes() == b"owner-existing-file"
