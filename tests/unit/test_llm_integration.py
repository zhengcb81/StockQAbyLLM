#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试 LLM 集成增强模块。"""

import copy
import hashlib
import json
import time
from unittest.mock import Mock

import pytest

from src.config.llm_config import LLMConfig
from src.core.models import Question, SearchResult
from src.providers.llm_provider import LLMProvider
from src.providers.model_resolution import model_resolution_sha256
from src.utils.llm_integration import (
    OrderedSearchProviderCascade,
    ProviderCascade,
    RequestCache,
    RequestContext,
    RequestCost,
    TokenTracker,
    TokenUsage,
    cached_llm_request,
    create_request_context,
    generate_request_id,
    get_global_request_cache,
    get_global_token_tracker,
    get_request_context,
    set_request_context,
)
from src.utils.quick_scan_provider_health import QuickScanProviderHealth
from src.utils.quick_scan_work_store import QuickScanWorkStore
from src.utils.quick_scan_work_transport import bind_quick_scan_work


def _q10_integration_resolution(resolved="model-resolved"):
    return {
        "schema_version": "1.0.0",
        "aliases": [
            {
                "provider": "openai",
                "protocol": "responses",
                "requested_model": "model-a",
                "resolved_model": resolved,
            }
        ],
    }


def _q10_integration_response(question_id, actual_model):
    response = Mock()
    response.status_code = 200
    response.headers = {"x-request-id": f"request-{question_id}"}
    response.json.return_value = {
        "id": f"response-{question_id}",
        "status": "completed",
        "model": actual_model,
        "output": [
            {
                "type": "web_search_call",
                "id": f"search-{question_id}",
                "status": "completed",
                "action": {
                    "type": "search",
                    "sources": [{"type": "url", "url": "https://example.org/source"}],
                },
            },
            {
                "type": "message",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(
                            {
                                "question_id": question_id,
                                "entity_id": "ENT_SYNTHETIC",
                                "company_name": "Synthetic Co",
                                "status": "scored",
                                "score": 8,
                                "description": "Synthetic answer",
                                "actual_model": "forged-answer-model",
                            }
                        ),
                    }
                ],
            },
        ],
    }
    return response


def _q10_integration_item(store, question_id, policy_version):
    item = store.create_or_attach(
        entity_id="ENT_SYNTHETIC",
        question_id=question_id,
        generation=1,
        scope="entity",
        scope_id="ENT_SYNTHETIC",
        identity_revision=1,
        source_binding_version=1,
        identity_state="verified",
        source_binding_ref="BND_SYNTHETIC",
        source_binding_refs=["BND_SYNTHETIC"],
        identity_snapshot_sha256=hashlib.sha256(b"synthetic-identity").hexdigest(),
        question_fingerprint=hashlib.sha256(question_id.encode("utf-8")).hexdigest(),
        routing_fingerprint=hashlib.sha256(policy_version.encode("utf-8")).hexdigest(),
        run_id="run-q10-integration",
        scan_id="scan-q10-integration",
    )
    lease = store.claim(item["work_item_id"], lease_seconds=60)
    assert lease is not None
    return item, lease


def _q10_integration_config(tmp_path, *, aliases=True):
    path = tmp_path / "synthetic-llm.json"
    policy = _ordered_policy()
    policy["models"][1]["enabled"] = False
    content = {
        "providers": {
            "primary": {
                "enabled": True,
                "api_key": "synthetic-key",
                "model": "model-a",
                "base_url": "https://api.openai.com/v1/responses",
                "max_retries": 1,
                "format_repair_budget": 0,
            }
        },
        "quick_scan_model_policy": policy,
        "quick_scan_model_resolution": (
            _q10_integration_resolution() if aliases else {"schema_version": "1.0.0", "aliases": []}
        ),
    }
    path.write_text(json.dumps(content), encoding="utf-8")
    return path, LLMConfig(str(path))


def test_q10_next_run_alias_snapshot_stays_frozen_through_factory_and_http(tmp_path, monkeypatch):
    path, config = _q10_integration_config(tmp_path)
    snapshot = config.get_quick_scan_model_policy()
    frozen_resolution = copy.deepcopy(snapshot["routes"][0]["model_resolution"])
    config.config["quick_scan_model_resolution"] = _q10_integration_resolution(
        "current-config-model"
    )
    path.write_text(json.dumps(config.config), encoding="utf-8")
    calls, providers = [], []

    def factory(frozen_snapshot):
        calls.append(frozen_snapshot["policy_version"])
        provider = LLMProvider(
            provider_name="primary",
            api_key="synthetic-key",
            model="model-a",
            config_file=str(path),
            require_search=True,
            entity_id="ENT_SYNTHETIC",
            company_name="Synthetic Co",
            model_resolution=frozen_snapshot["routes"][0]["model_resolution"],
        )
        assert provider.client.model_resolution == frozen_resolution
        providers.append(provider)
        return [provider]

    policy_reads = []

    def policy_source():
        policy_reads.append(True)
        return snapshot

    cascade = OrderedSearchProviderCascade(
        providers=[],
        routes=[],
        policy_version=snapshot["policy_version"],
        max_attempts_per_dispatch_round=1,
        policy_source={
            "policy_provider": policy_source,
            "provider_factory": factory,
            "effective_mode": "next_run",
        },
    )
    store = QuickScanWorkStore(tmp_path / "q10-integration.sqlite")
    session = Mock()
    posts = []

    def post(_url, **kwargs):
        posts.append(kwargs["json"]["model"])
        if len(posts) == 1:
            # This happens after the first POST's durable intent was frozen.
            config.config["quick_scan_model_resolution"] = _q10_integration_resolution(
                "changed-during-http"
            )
            path.write_text(json.dumps(config.config), encoding="utf-8")
            snapshot["routes"][0]["model_resolution"]["aliases"][0][
                "resolved_model"
            ] = "mutated-source-snapshot"
            providers[0].client.model_resolution = _q10_integration_resolution("mutated-client")
        return _q10_integration_response(f"CORE_{len(posts):02d}", "model-resolved")

    session.post.side_effect = post
    monkeypatch.setattr(
        "src.providers.llm_client.http_client_manager.get_sync_session", lambda: session
    )
    checkpoints = []
    for question_id in ("CORE_01", "CORE_02"):
        item, lease = _q10_integration_item(store, question_id, snapshot["policy_version"])
        with bind_quick_scan_work(store, item["work_item_id"], lease):
            result = cascade.search_question(
                Question("Synthetic question", question_id=question_id)
            )[0]
        assert result.status == "scored"
        final = result.metadata["execution"]["work_transport"]
        assert final["final_receipt"]["actual_model"] == "model-resolved"
        assert final["final_receipt"]["requested_model"] == "model-a"
        assert final["final_receipt"]["model_resolution_sha256"] == model_resolution_sha256(
            frozen_resolution
        )
        durable = store.get_attempt_response(final["work_attempt_id"])
        assert durable["model_resolution"] == frozen_resolution
        checkpoints.append(
            store.save_answer_checkpoint(
                item["work_item_id"],
                lease,
                final["work_attempt_id"],
                answer={
                    "entity_id": item["entity_id"],
                    "question_id": question_id,
                    "status": result.status,
                    "score": result.score,
                    "description": result.snippet,
                },
                execution_receipt=final["final_receipt"],
            )
        )
    assert calls == [snapshot["policy_version"]]
    assert len(policy_reads) == 1
    assert posts == ["model-a", "model-a"]
    assert all(
        checkpoint["payload"]["provenance"]["actual_model"] == "model-resolved"
        for checkpoint in checkpoints
    )


def test_q10_current_factory_config_cannot_register_alias_for_frozen_empty_route(
    tmp_path, monkeypatch
):
    path, config = _q10_integration_config(tmp_path, aliases=False)
    snapshot = config.get_quick_scan_model_policy()
    config.config["quick_scan_model_resolution"] = _q10_integration_resolution()
    path.write_text(json.dumps(config.config), encoding="utf-8")
    provider = LLMProvider(
        provider_name="primary",
        api_key="synthetic-key",
        model="model-a",
        config_file=str(path),
        require_search=True,
        entity_id="ENT_SYNTHETIC",
        company_name="Synthetic Co",
        model_resolution=snapshot["routes"][0]["model_resolution"],
    )
    assert provider.client.model_resolution["aliases"] == []
    cascade = OrderedSearchProviderCascade(
        providers=[provider],
        routes=snapshot["routes"],
        policy_version=snapshot["policy_version"],
        max_attempts_per_dispatch_round=1,
    )
    session = Mock()
    session.post.return_value = _q10_integration_response("CORE_01", "model-resolved")
    monkeypatch.setattr(
        "src.providers.llm_client.http_client_manager.get_sync_session", lambda: session
    )
    store = QuickScanWorkStore(tmp_path / "q10-empty-snapshot.sqlite")
    item, lease = _q10_integration_item(store, "CORE_01", snapshot["policy_version"])
    with bind_quick_scan_work(store, item["work_item_id"], lease):
        result = cascade.search_question(Question("Synthetic question", question_id="CORE_01"))[0]
    assert result.status != "scored"
    assert result.score is None
    final = result.metadata["execution"]["work_transport"]
    response = store.get_attempt_response(final["work_attempt_id"])
    assert response["model_resolved"] == "model-resolved"
    assert response["model_resolution"]["aliases"] == []
    assert store.get_answer_checkpoint(item["work_item_id"]) is None
    session.post.assert_called_once()


class TestTokenUsage:
    """测试 Token 使用统计。"""

    def test_token_usage_creation(self):
        """测试创建 TokenUsage。"""
        usage = TokenUsage(prompt_tokens=100, completion_tokens=50)
        assert usage.prompt_tokens == 100
        assert usage.completion_tokens == 50
        assert usage.total_tokens == 150

    def test_token_usage_add(self):
        """测试累加 TokenUsage。"""
        usage1 = TokenUsage(prompt_tokens=100, completion_tokens=50)
        usage2 = TokenUsage(prompt_tokens=200, completion_tokens=100)
        usage1.add(usage2)
        assert usage1.prompt_tokens == 300
        assert usage1.completion_tokens == 150
        assert usage1.total_tokens == 450

    def test_token_usage_to_dict(self):
        """测试转换为字典。"""
        usage = TokenUsage(prompt_tokens=100, completion_tokens=50)
        result = usage.to_dict()
        assert result == {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        }


class TestRequestCost:
    """测试请求成本统计。"""

    def test_cost_calculation(self):
        """测试成本计算。"""
        cost = RequestCost()
        cost.calculate_from_tokens(prompt_tokens=1000, completion_tokens=500)
        assert cost.input_cost > 0
        assert cost.output_cost > 0
        assert cost.total_cost == cost.input_cost + cost.output_cost

    def test_cost_add(self):
        """测试累加成本。"""
        cost1 = RequestCost()
        cost1.calculate_from_tokens(1000, 500)
        cost2 = RequestCost()
        cost2.calculate_from_tokens(2000, 1000)
        cost1.add(cost2)
        assert cost1.input_cost > cost2.input_cost
        assert cost1.total_cost == cost1.input_cost + cost1.output_cost

    def test_cost_to_dict(self):
        """测试转换为字典。"""
        cost = RequestCost()
        cost.calculate_from_tokens(1000, 500)
        result = cost.to_dict()
        assert "total_cost" in result
        assert result["total_cost"] > 0


class TestTokenTracker:
    """测试 Token 追踪器。"""

    def test_record_request(self):
        """测试记录请求。"""
        tracker = TokenTracker()
        tracker.record_request("deepseek", prompt_tokens=100, completion_tokens=50)
        assert tracker.get_request_count() == 1

        total = tracker.get_total_usage()
        assert total.prompt_tokens == 100
        assert total.completion_tokens == 50

    def test_multiple_providers(self):
        """测试多个提供商。"""
        tracker = TokenTracker()
        tracker.record_request("deepseek", 100, 50)
        tracker.record_request("minimax", 200, 100)

        usage_by_provider = tracker.get_usage_by_provider()
        assert "deepseek" in usage_by_provider
        assert "minimax" in usage_by_provider

        total = tracker.get_total_usage()
        assert total.prompt_tokens == 300

    def test_reset(self):
        """测试重置。"""
        tracker = TokenTracker()
        tracker.record_request("deepseek", 100, 50)
        tracker.reset()
        assert tracker.get_request_count() == 0
        assert tracker.get_total_usage().total_tokens == 0


class TestRequestCache:
    """测试请求缓存。"""

    def test_cache_set_and_get(self):
        """测试缓存设置和获取。"""
        cache = RequestCache(max_size=10)
        cache.set("deepseek", "test prompt", "system", "result")

        result = cache.get("deepseek", "test prompt", "system")
        assert result == "result"

    def test_cache_miss(self):
        """测试缓存未命中。"""
        cache = RequestCache()
        result = cache.get("deepseek", "nonexistent", "system")
        assert result is None

    def test_cache_expiration(self):
        """测试缓存过期。"""
        cache = RequestCache(ttl_seconds=1)
        cache.set("deepseek", "prompt", "system", "result")

        # 等待过期
        time.sleep(1.1)

        result = cache.get("deepseek", "prompt", "system")
        assert result is None

    def test_cache_stats(self):
        """测试缓存统计。"""
        cache = RequestCache()
        cache.set("deepseek", "prompt1", "system", "result1")
        cache.get("deepseek", "prompt1", "system")  # hit
        cache.get("deepseek", "prompt2", "system")  # miss

        stats = cache.get_stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1

    def test_cache_clear(self):
        """测试清空缓存。"""
        cache = RequestCache()
        cache.set("deepseek", "prompt", "system", "result")
        cache.clear()

        result = cache.get("deepseek", "prompt", "system")
        assert result is None


class TestRequestContext:
    """测试请求上下文。"""

    def test_context_creation(self):
        """测试创建上下文。"""
        context = RequestContext()
        assert context.request_id is not None
        assert len(context.request_id) > 0
        assert context.start_time > 0

    def test_custom_request_id(self):
        """测试自定义请求 ID。"""
        context = RequestContext(request_id="test-id-123")
        assert context.request_id == "test-id-123"

    def test_elapsed_time(self):
        """测试获取已用时间。"""
        context = RequestContext()
        time.sleep(0.1)
        elapsed = context.get_elapsed_time()
        assert elapsed >= 0.1

    def test_metadata(self):
        """测试元数据。"""
        context = RequestContext()
        context.metadata["key"] = "value"
        assert context.metadata["key"] == "value"

    def test_global_context(self):
        """测试全局上下文。"""
        context = create_request_context()
        assert get_request_context() == context

        set_request_context(None)
        assert get_request_context() is None


class TestGenerateRequestId:
    """测试生成请求 ID。"""

    def test_generate_unique_ids(self):
        """测试生成唯一 ID。"""
        id1 = generate_request_id()
        id2 = generate_request_id()
        assert id1 != id2

    def test_id_format(self):
        """测试 ID 格式。"""
        request_id = generate_request_id()
        assert len(request_id) > 0


class TestProviderCascade:
    """测试 Provider 降级器。"""

    def test_initialization(self):
        """测试初始化。"""
        cascade = ProviderCascade(["deepseek", "minimax", "glm"])
        assert cascade.get_primary() == "deepseek"
        assert cascade.get_current() == "deepseek"

    def test_empty_providers_error(self):
        """测试空提供商列表错误。"""
        with pytest.raises(ValueError, match="不能为空"):
            ProviderCascade([])

    def test_mark_failure_triggers_cascade(self):
        """测试标记失败触发降级。"""
        cascade = ProviderCascade(["deepseek", "minimax"])
        cascade.mark_failure("deepseek")
        assert cascade.get_current() == "minimax"

    def test_mark_failure_on_non_current(self):
        """测试标记非当前 Provider 失败。"""
        cascade = ProviderCascade(["deepseek", "minimax"])
        cascade.mark_failure("minimax")
        assert cascade.get_current() == "deepseek"  # 不应切换

    def test_mark_success_restores_primary(self):
        """测试标记成功恢复到主 Provider。"""
        cascade = ProviderCascade(["deepseek", "minimax"])
        cascade.mark_failure("deepseek")
        assert cascade.get_current() == "minimax"

        cascade.mark_success("minimax")
        assert cascade.get_current() == "deepseek"

    def test_all_providers_failed(self):
        """测试所有 Provider 失败。"""
        cascade = ProviderCascade(["deepseek", "minimax"])
        cascade.mark_failure("deepseek")
        cascade.mark_failure("minimax")
        assert cascade.get_current() == "minimax"  # 最后一个

    def test_failure_counts(self):
        """测试失败计数。"""
        cascade = ProviderCascade(["deepseek", "minimax"])
        cascade.mark_failure("deepseek")
        cascade.mark_failure("deepseek")

        counts = cascade.get_failure_counts()
        assert counts["deepseek"] == 2

    def test_reset(self):
        """测试重置。"""
        cascade = ProviderCascade(["deepseek", "minimax"])
        cascade.mark_failure("deepseek")
        cascade.reset()

        assert cascade.get_current() == "deepseek"
        # reset 后 failure_counts 被清空，所以 deepseek 不在其中
        counts = cascade.get_failure_counts()
        assert "deepseek" not in counts or counts["deepseek"] == 0

    def test_failure_threshold_delays_cascade(self):
        """测试失败阈值延迟降级。"""
        cascade = ProviderCascade(
            ["deepseek", "minimax"], failure_threshold=3, recovery_threshold=1
        )
        # 失败2次，不应触发降级
        cascade.mark_failure("deepseek")
        cascade.mark_failure("deepseek")
        assert cascade.get_current() == "deepseek"

        # 第3次失败，触发降级
        cascade.mark_failure("deepseek")
        assert cascade.get_current() == "minimax"

    def test_recovery_threshold_delays_restore(self):
        """测试恢复阈值延迟恢复。"""
        cascade = ProviderCascade(
            ["deepseek", "minimax"], failure_threshold=1, recovery_threshold=3
        )
        # 降级到minimax
        cascade.mark_failure("deepseek")
        assert cascade.get_current() == "minimax"

        # 成功1次，不应恢复
        cascade.mark_success("minimax")
        assert cascade.get_current() == "minimax"

        # 成功2次，不应恢复
        cascade.mark_success("minimax")
        assert cascade.get_current() == "minimax"

        # 第3次成功，触发恢复
        cascade.mark_success("minimax")
        assert cascade.get_current() == "deepseek"

    def test_health_check_prevents_restore(self):
        """测试健康检查阻止恢复。"""

        def unhealthy_check(provider: str) -> bool:
            return provider != "deepseek"  # deepseek不健康

        cascade = ProviderCascade(
            ["deepseek", "minimax"],
            failure_threshold=1,
            recovery_threshold=1,
            health_check=unhealthy_check,
        )
        # 降级到minimax
        cascade.mark_failure("deepseek")
        assert cascade.get_current() == "minimax"

        # 尝试恢复，但健康检查阻止
        cascade.mark_success("minimax")
        assert cascade.get_current() == "minimax"  # 仍在minimax
        assert cascade.is_recovering() is True

    def test_success_counts_tracking(self):
        """测试成功计数追踪。"""
        cascade = ProviderCascade(
            ["deepseek", "minimax"],
            failure_threshold=1,
            recovery_threshold=3,
        )
        cascade.mark_failure("deepseek")
        assert cascade.get_current() == "minimax"

        cascade.mark_success("minimax")
        cascade.mark_success("minimax")
        counts = cascade.get_success_counts()
        assert counts["minimax"] == 2

    def test_health_check_setter(self):
        """测试健康检查setter。"""
        cascade = ProviderCascade(["deepseek", "minimax"])
        assert cascade.health_check is None

        def check(p: str) -> bool:
            return True

        cascade.health_check = check
        assert cascade.health_check is check


def _route(route_id, provider, model):
    return {
        "id": route_id,
        "provider_config_ref": provider,
        "model": model,
        "eligible": True,
    }


def _provider(name, model, result, *, supports_search=True):
    provider = Mock()
    provider.get_provider_name.return_value = name
    provider.model = model
    provider.client.supports_web_search = supports_search
    provider.search_question.return_value = [result]
    return provider


def _search_result(status, score=None, *, attempts=None):
    return SearchResult(
        title="fixture",
        snippet="fixture answer",
        source="llm_api",
        status=status,
        score=score,
        metadata={
            "attempts": attempts or [],
            "execution": {"search_status": "executed"},
        },
    )


def _ordered_cascade(primary, backup, *, max_attempts=2):
    return OrderedSearchProviderCascade(
        providers=[primary, backup],
        routes=[
            _route("primary", "primary", "model-a"),
            _route("backup", "backup", "model-b"),
        ],
        policy_version="policy-id@fixturehash",
        max_attempts_per_dispatch_round=max_attempts,
    )


class TestOrderedSearchProviderCascade:
    def test_annotation_preserves_transport_vendor_and_separates_route_alias(self):
        failed = _search_result(
            "error",
            attempts=[{"attempt_id": "a-429", "provider": "openai", "http_status_code": 429}],
        )
        accepted = _search_result(
            "scored",
            8,
            attempts=[{"attempt_id": "b-200", "provider": "minimax", "http_status_code": 200}],
        )
        accepted.metadata["provider"] = "minimax"
        accepted.metadata["execution"]["provider"] = "minimax"
        accepted.metadata["execution"]["response_id"] = "resp-minimax"
        primary = _provider("primary", "model-a", failed)
        backup = _provider("backup", "MiniMax-M3", accepted)

        result = _ordered_cascade(primary, backup).search_question(
            Question("Moat?", question_id="IQS_05")
        )[0]

        assert result.metadata["provider"] == "minimax"
        attempts = result.metadata["attempts"]
        assert [attempt["provider"] for attempt in attempts] == ["openai", "minimax"]
        assert [attempt["provider_config_ref"] for attempt in attempts] == [
            "primary",
            "backup",
        ]
        assert [attempt["route_id"] for attempt in attempts] == ["primary", "backup"]

    def test_429_then_backup_score_8_marks_dispatch_completed(self):
        primary = _provider(
            "primary",
            "model-a",
            _search_result("error", attempts=[{"attempt_id": "a-429", "http_status_code": 429}]),
        )
        backup = _provider("backup", "model-b", _search_result("scored", 8))

        result = _ordered_cascade(primary, backup).search_question(Question("Q?"))[0]

        assert result.score == 8
        assert result.metadata["execution"]["dispatch_outcome"] == {
            "scope": "provider_dispatch",
            "state": "completed",
            "wait_reason": None,
            "resume_condition": None,
        }
        primary.search_question.assert_called_once()
        backup.search_question.assert_called_once()

    def test_429_then_backup_without_results_marks_dispatch_completed(self):
        primary = _provider(
            "primary",
            "model-a",
            _search_result("error", attempts=[{"attempt_id": "a-429", "http_status_code": 429}]),
        )
        backup = _provider("backup", "model-b", _search_result("scored", 8))
        backup.search_question.return_value = []

        result = _ordered_cascade(primary, backup).search_question(Question("Q?"))[0]

        assert result.status == "insufficient_evidence"
        assert result.source == "no_results"
        assert result.score is None
        assert result.metadata["execution"]["route_trace"][-1]["decision"] == (
            "answered_without_results"
        )
        assert result.metadata["execution"]["dispatch_outcome"] == {
            "scope": "provider_dispatch",
            "state": "completed",
            "wait_reason": None,
            "resume_condition": None,
        }
        primary.search_question.assert_called_once()
        backup.search_question.assert_called_once()

    def test_falls_back_after_429_and_keeps_per_question_model_receipts(self):
        primary = _provider(
            "primary",
            "model-a",
            _search_result(
                "error",
                attempts=[
                    {
                        "attempt_id": "a-429",
                        "provider": "openai",
                        "http_status_code": 429,
                    }
                ],
            ),
        )
        backup = _provider(
            "backup",
            "model-b",
            _search_result(
                "scored",
                8,
                attempts=[
                    {
                        "attempt_id": "b-200",
                        "provider": "openai",
                        "http_status_code": 200,
                    }
                ],
            ),
        )

        results = _ordered_cascade(primary, backup).search_question(
            Question("Moat?", question_id="IQS_05")
        )

        assert results[0].score == 8
        assert results[0].metadata["provider"] == "openai"
        assert results[0].metadata["model_requested"] == "model-b"
        attempts = results[0].metadata["attempts"]
        assert [item["provider"] for item in attempts] == ["openai", "openai"]
        assert [item["provider_config_ref"] for item in attempts] == [
            "primary",
            "backup",
        ]
        assert [item["http_status_code"] for item in attempts] == [429, 200]
        assert all(item["policy_version"] == "policy-id@fixturehash" for item in attempts)
        assert [item["decision"] for item in attempts[-1]["route_trace"]] == [
            "provider_failure",
            "accepted_answer",
        ]
        primary.search_question.assert_called_once()
        backup.search_question.assert_called_once()

    @pytest.mark.parametrize(
        "answer",
        [
            _search_result("scored", 2),
            _search_result("unknown"),
            _search_result("insufficient_evidence"),
        ],
        ids=["low-score", "unknown", "insufficient-evidence"],
    )
    def test_answer_quality_or_missing_evidence_never_triggers_fallback(self, answer):
        primary = _provider("primary", "model-a", answer)
        backup = _provider("backup", "model-b", _search_result("scored", 9))

        result = _ordered_cascade(primary, backup).search_question(Question("Q?"))[0]

        assert result.status == answer.status
        assert result.score == answer.score
        assert result.metadata["execution"]["dispatch_outcome"]["state"] == "completed"
        primary.search_question.assert_called_once()
        backup.search_question.assert_not_called()

    def test_invalid_request_and_transport_ambiguity_do_not_fallback(self):
        invalid_request = _provider(
            "primary",
            "model-a",
            _search_result(
                "error",
                attempts=[{"attempt_id": "a-400", "http_status_code": 400}],
            ),
        )
        backup = _provider("backup", "model-b", _search_result("scored", 9))

        result = _ordered_cascade(invalid_request, backup).search_question(Question("Q?"))[0]

        assert result.status == "error"
        assert result.metadata["execution"]["dispatch_outcome"] == {
            "scope": "provider_dispatch",
            "state": "failed",
            "wait_reason": "manual_review",
            "resume_condition": "request_or_provider_configuration_corrected",
        }
        backup.search_question.assert_not_called()

        uncertain = _provider(
            "primary",
            "model-a",
            _search_result(
                "error",
                attempts=[{"attempt_id": "a-timeout", "failure_type": "Timeout"}],
            ),
        )
        uncertain_result = _ordered_cascade(uncertain, backup).search_question(Question("Q?"))[0]
        assert uncertain_result.metadata["execution"]["dispatch_outcome"] == {
            "scope": "provider_dispatch",
            "state": "uncertain",
            "wait_reason": "provider_response_unknown",
            "resume_condition": "reconcile_same_attempt_before_retry",
            "uncertain_attempt_id": "a-timeout",
        }
        backup.search_question.assert_not_called()

        rate_limited = _provider(
            "primary",
            "model-a",
            _search_result("error", attempts=[{"attempt_id": "a-429", "http_status_code": 429}]),
        )
        invalid_backup = _provider(
            "backup",
            "model-b",
            _search_result("error", attempts=[{"attempt_id": "b-400", "http_status_code": 400}]),
        )
        final_invalid = _ordered_cascade(rate_limited, invalid_backup).search_question(
            Question("Q?")
        )[0]
        assert final_invalid.metadata["execution"]["dispatch_outcome"]["state"] == "failed"
        assert final_invalid.metadata["execution"]["dispatch_outcome"]["wait_reason"] == (
            "manual_review"
        )

    def test_unknown_search_capability_is_skipped_and_eligible_backup_runs(self):
        primary = _provider("primary", "model-a", _search_result("scored", 1))
        primary.client.supports_web_search = None
        backup = _provider("backup", "model-b", _search_result("scored", 8))

        result = _ordered_cascade(primary, backup).search_question(Question("Q?"))[0]

        assert result.score == 8
        assert result.metadata["execution"]["route_trace"][0]["decision"] == (
            "skipped_unverified_web_search"
        )
        primary.search_question.assert_not_called()
        backup.search_question.assert_called_once()

    def test_no_verified_search_route_returns_setup_required_without_sending(self):
        primary = _provider("primary", "model-a", _search_result("scored", 1))
        primary.client.supports_web_search = None
        backup = _provider("backup", "model-b", _search_result("scored", 8), supports_search=False)

        result = _ordered_cascade(primary, backup).search_question(Question("Q?"))[0]

        assert result.status == "error"
        assert result.metadata["attempts"] == []
        assert result.metadata["execution"]["dispatch_outcome"] == {
            "scope": "provider_dispatch",
            "state": "setup_required",
            "wait_reason": "search_capability_unavailable",
            "resume_condition": "provider_configuration_changed",
        }
        primary.search_question.assert_not_called()
        backup.search_question.assert_not_called()

    def test_server_error_can_fallback_but_dispatch_limit_is_hard(self):
        primary = _provider(
            "primary",
            "model-a",
            _search_result(
                "error",
                attempts=[{"attempt_id": "a-503", "http_status_code": 503}],
            ),
        )
        backup = _provider("backup", "model-b", _search_result("scored", 8))

        cascaded = _ordered_cascade(primary, backup).search_question(Question("Q?"))[0]
        assert cascaded.score == 8
        backup.search_question.assert_called_once()

        limited_primary = _provider(
            "primary",
            "model-a",
            _search_result(
                "error",
                attempts=[{"attempt_id": "a-429", "http_status_code": 429}],
            ),
        )
        limited_backup = _provider("backup", "model-b", _search_result("scored", 8))
        limited = _ordered_cascade(limited_primary, limited_backup, max_attempts=1).search_question(
            Question("Q?")
        )[0]

        assert limited.status == "error"
        assert limited.metadata["execution"]["dispatch_outcome"] == {
            "scope": "provider_dispatch",
            "state": "retry_wait_recommended",
            "scheduled": False,
            "wait_reason": "provider_rate_limited",
            "resume_condition": "next_scheduled_dispatch_or_provider_recovery",
        }
        assert [item["decision"] for item in limited.metadata["execution"]["route_trace"]] == [
            "provider_failure",
            "skipped_attempt_budget",
        ]
        limited_primary.search_question.assert_called_once()
        limited_backup.search_question.assert_not_called()

    def test_quota_cooldown_remains_waitable_for_later_questions_in_same_run(self):
        primary = _provider(
            "primary",
            "model-a",
            _search_result(
                "error",
                attempts=[
                    {
                        "attempt_id": "a-quota",
                        "http_status_code": 429,
                        "provider_error_code": "insufficient_quota",
                    }
                ],
            ),
        )
        backup = _provider("backup", "model-b", _search_result("scored", 8))
        routes = [
            _route("primary", "primary", "model-a"),
            _route("backup", "backup", "model-b"),
        ]
        for route in routes:
            route["quota_group"] = "shared-five-hour-plan"
        cascade = OrderedSearchProviderCascade(
            providers=[primary, backup],
            routes=routes,
            policy_version="policy-id@fixturehash",
            max_attempts_per_dispatch_round=2,
        )

        first = cascade.search_question(Question("first"))[0]
        second = cascade.search_question(Question("second"))[0]

        assert first.metadata["execution"]["dispatch_outcome"]["state"] == (
            "retry_wait_recommended"
        )
        assert second.metadata["execution"]["dispatch_outcome"] == {
            "scope": "provider_dispatch",
            "state": "retry_wait_recommended",
            "scheduled": False,
            "wait_reason": "provider_quota_cooldown",
            "resume_condition": "quota_window_reset_or_policy_update",
        }
        assert backup.search_question.call_count == 0
        assert [item["decision"] for item in second.metadata["execution"]["route_trace"]] == [
            "skipped_quota_group_cooldown",
            "skipped_quota_group_cooldown",
        ]

    def test_runtime_search_unavailable_falls_back_to_verified_route(self):
        primary_result = _search_result("insufficient_evidence")
        primary_result.metadata["search_status"] = "unavailable"
        primary = _provider("primary", "model-a", primary_result)
        backup = _provider("backup", "model-b", _search_result("scored", 8))

        result = _ordered_cascade(primary, backup).search_question(Question("Q?"))[0]

        assert result.score == 8
        assert result.metadata["execution"]["route_trace"][0]["failure_category"] == (
            "runtime_search_unavailable"
        )
        primary.search_question.assert_called_once()
        backup.search_question.assert_called_once()

    def test_runtime_search_unavailable_then_backup_score_8_marks_dispatch_completed(
        self,
    ):
        primary_result = _search_result("insufficient_evidence")
        primary_result.metadata["search_status"] = "unavailable"
        primary = _provider("primary", "model-a", primary_result)
        backup = _provider("backup", "model-b", _search_result("scored", 8))

        result = _ordered_cascade(primary, backup).search_question(Question("Q?"))[0]

        assert result.score == 8
        assert result.metadata["execution"]["dispatch_outcome"] == {
            "scope": "provider_dispatch",
            "state": "completed",
            "wait_reason": None,
            "resume_condition": None,
        }
        primary.search_question.assert_called_once()
        backup.search_question.assert_called_once()

    def test_retry_after_cools_route_without_blocking_or_repeating_it(self, monkeypatch):
        primary = _provider(
            "primary",
            "model-a",
            _search_result(
                "error",
                attempts=[
                    {
                        "attempt_id": "a-long-retry-after",
                        "http_status_code": 429,
                        "retry_after_seconds": 86400,
                    }
                ],
            ),
        )
        backup = _provider("backup", "model-b", _search_result("scored", 8))
        monkeypatch.setattr(
            "src.utils.llm_integration.time.sleep",
            lambda *_args: pytest.fail("rate-limit recovery must not block the worker"),
        )
        cascade = _ordered_cascade(primary, backup)

        first = cascade.search_question(Question("first"))[0]
        second = cascade.search_question(Question("second"))[0]

        assert first.score == 8
        assert second.score == 8
        assert second.metadata["execution"]["route_trace"][0]["decision"] == (
            "skipped_rate_limit_cooldown"
        )
        assert second.metadata["execution"]["route_trace"][0]["retry_after_seconds"] > 0
        primary.search_question.assert_called_once()
        assert backup.search_question.call_count == 2

    def test_short_retry_after_waits_once_then_retries_preferred_route(self, monkeypatch):
        primary = _provider(
            "primary",
            "model-a",
            _search_result(
                "error",
                attempts=[
                    {
                        "attempt_id": "a-short-429",
                        "http_status_code": 429,
                        "retry_after_seconds": 0.5,
                    }
                ],
            ),
        )
        primary.search_question.side_effect = [
            [
                _search_result(
                    "error",
                    attempts=[
                        {
                            "attempt_id": "a-short-429",
                            "http_status_code": 429,
                            "retry_after_seconds": 0.5,
                        }
                    ],
                )
            ],
            [
                _search_result(
                    "scored",
                    9,
                    attempts=[{"attempt_id": "a-retry-200", "http_status_code": 200}],
                )
            ],
        ]
        backup = _provider("backup", "model-b", _search_result("scored", 8))
        sleeps = []
        monkeypatch.setattr("src.utils.llm_integration.time.sleep", sleeps.append)

        result = _ordered_cascade(primary, backup, max_attempts=3).search_question(
            Question("Retry after?")
        )[0]

        assert result.score == 9
        assert sleeps == [0.5]
        assert primary.search_question.call_count == 2
        backup.search_question.assert_not_called()
        assert [item["decision"] for item in result.metadata["execution"]["route_trace"]] == [
            "provider_rate_limit_retry_after",
            "accepted_answer",
        ]

    def test_each_new_question_skips_user_preferred_route_while_rate_limited(self):
        primary = _provider("primary", "model-a", _search_result("error"))
        primary.search_question.side_effect = [
            [
                _search_result(
                    "error",
                    attempts=[{"attempt_id": "a-429", "http_status_code": 429}],
                )
            ],
            [_search_result("scored", 2)],
        ]
        backup = _provider("backup", "model-b", _search_result("scored", 8))
        cascade = _ordered_cascade(primary, backup)

        cascade.search_question(Question("first"))
        second = cascade.search_question(Question("second"))[0]

        assert second.score == 8
        assert second.metadata["execution"]["route_trace"][0]["decision"] == (
            "skipped_rate_limit_cooldown"
        )
        assert primary.search_question.call_count == 1
        assert backup.search_question.call_count == 2

    def test_unsupported_search_route_is_skipped_before_dispatch(self):
        primary = _provider(
            "primary", "model-a", _search_result("scored", 1), supports_search=False
        )
        backup = _provider("backup", "model-b", _search_result("scored", 8))

        result = _ordered_cascade(primary, backup).search_question(Question("Q?"))[0]

        assert result.score == 8
        primary.search_question.assert_not_called()
        backup.search_question.assert_called_once()
        assert result.metadata["execution"]["route_trace"][0]["decision"] == (
            "skipped_missing_web_search"
        )

    def test_all_routes_without_search_capability_fail_without_any_dispatch(self):
        primary = _provider(
            "primary", "model-a", _search_result("scored", 1), supports_search=False
        )
        backup = _provider("backup", "model-b", _search_result("scored", 8), supports_search=False)

        result = _ordered_cascade(primary, backup).search_question(Question("Q?"))[0]

        assert result.status == "error"
        assert result.metadata["failure_type"] == "provider_unavailable"
        assert result.metadata["execution"]["dispatch_outcome"] == {
            "scope": "provider_dispatch",
            "state": "setup_required",
            "wait_reason": "search_capability_unavailable",
            "resume_condition": "provider_configuration_changed",
        }
        primary.search_question.assert_not_called()
        backup.search_question.assert_not_called()


def test_policy_revision_reloads_at_undispatched_question_boundaries(tmp_path):
    """A mid-run policy save applies to later questions at the next dispatch boundary."""
    from src.config.llm_config import LLMConfig

    config_path = tmp_path / "llm_apis.json"
    policy_a = _ordered_policy()
    fixture_providers = {
        "primary": {"enabled": True, "api_key": "fixture-a"},
        "backup": {"enabled": True, "api_key": "fixture-b"},
    }
    config_path.write_text(
        json.dumps({"providers": fixture_providers, "quick_scan_model_policy": policy_a}),
        encoding="utf-8",
    )
    config = LLMConfig(str(config_path))
    snapshot_a = config.get_quick_scan_model_policy()

    events = []

    def provider_factory(snapshot):
        providers = []
        for route in snapshot["routes"]:
            provider = Mock()
            provider.get_provider_name.return_value = "probe"
            provider.model = route["model"]
            provider.client.supports_web_search = True
            provider.search_question.side_effect = lambda question, route=route: [
                _search_result(
                    "scored",
                    8,
                    attempts=[
                        {
                            "attempt_id": "attempt-" + route["id"],
                            "http_status_code": 200,
                        }
                    ],
                )
            ]
            providers.append(provider)
        events.append((snapshot["policy_version"], snapshot["routes"][0]["id"]))
        return providers

    target = {"snapshot": snapshot_a}

    def current_policy():
        return target["snapshot"]

    cascade = OrderedSearchProviderCascade(
        providers=[],
        routes=[],
        policy_version=snapshot_a["policy_version"],
        max_attempts_per_dispatch_round=2,
        policy_source={
            "policy_provider": current_policy,
            "provider_factory": provider_factory,
            "effective_mode": "next_run",
        },
    )

    cascade.search_question(Question("q1"))
    saved = dict(policy_a)
    saved["models"] = [
        {
            "id": "backup-route",
            "enabled": True,
            "provider_config_ref": "backup",
            "model": "model-b2",
            "quota_group": "account-b",
            "max_in_flight": 1,
        },
        {
            "id": "primary-route",
            "enabled": True,
            "provider_config_ref": "primary",
            "model": "model-a2",
            "quota_group": "account-a",
            "max_in_flight": 1,
        },
    ]
    config.config["providers"] = {
        "backup": {"enabled": True, "api_key": "fixture-b"},
        "primary": {"enabled": True, "api_key": "fixture-a"},
    }
    snapshot_b = config.save_quick_scan_model_policy(saved)
    target["snapshot"] = snapshot_b

    cascade.search_question(Question("q2"))

    assert snapshot_b["policy_version"] != snapshot_a["policy_version"]
    # next_run keeps the startup revision for the whole run: one factory call, no swap.
    assert events == [(snapshot_a["policy_version"], "primary-route")]


def test_policy_immediate_mode_never_mutates_the_dispatched_revision(tmp_path):
    """Immediate profile switching records the boundary and preserves receipts."""
    from src.config.llm_config import LLMConfig

    config_path = tmp_path / "llm_apis.json"
    policy_a = _ordered_policy()
    fixture_providers = {
        "primary": {"enabled": True, "api_key": "fixture-a"},
        "backup": {"enabled": True, "api_key": "fixture-b"},
    }
    config_path.write_text(
        json.dumps({"providers": fixture_providers, "quick_scan_model_policy": policy_a}),
        encoding="utf-8",
    )
    config = LLMConfig(str(config_path))
    snapshot_a = config.get_quick_scan_model_policy()
    target = {"snapshot": snapshot_a}
    snapshots = []

    def current_policy():
        return target["snapshot"]

    def provider_factory(snapshot):
        providers = []
        for route in snapshot["routes"]:
            provider = Mock()
            provider.get_provider_name.return_value = "probe"
            provider.model = route["model"]
            provider.client.supports_web_search = True
            provider.search_question.return_value = [
                _search_result(
                    "scored",
                    8,
                    attempts=[
                        {
                            "attempt_id": "attempt-" + route["id"],
                            "http_status_code": 200,
                        }
                    ],
                )
            ]
            providers.append(provider)
        return providers

    cascade = OrderedSearchProviderCascade(
        providers=[],
        routes=[],
        policy_version=snapshot_a["policy_version"],
        max_attempts_per_dispatch_round=2,
        policy_source={
            "policy_provider": current_policy,
            "provider_factory": provider_factory,
            "effective_mode": "immediate",
        },
    )

    first = cascade.search_question(Question("q1"))[0]
    saved = dict(policy_a)
    saved["models"] = [
        {
            "id": "backup-route",
            "enabled": True,
            "provider_config_ref": "backup",
            "model": "model-b2",
            "quota_group": "account-b",
            "max_in_flight": 1,
        },
        {
            "id": "primary-route",
            "enabled": True,
            "provider_config_ref": "primary",
            "model": "model-a2",
            "quota_group": "account-a",
            "max_in_flight": 1,
        },
    ]
    config.config["providers"] = {
        "backup": {"enabled": True, "api_key": "fixture-b"},
        "primary": {"enabled": True, "api_key": "fixture-a"},
    }
    snapshot_b = config.save_quick_scan_model_policy(saved)
    target["snapshot"] = snapshot_b

    second = cascade.search_question(Question("q2"))[0]

    assert first.metadata["execution"]["policy_version"] == snapshot_a["policy_version"]
    assert second.metadata["execution"]["policy_version"] == snapshot_b["policy_version"]
    assert first.metadata["provider_config_ref"] == "primary"
    assert second.metadata["execution"].get("policy_transition", {}).get("at_boundary") is True

    third = cascade.search_question(Question("q3"))[0]
    assert third.metadata["execution"]["policy_version"] == snapshot_b["policy_version"]
    assert third.metadata["execution"]["dispatch_outcome"]["state"] == "completed"
    snapshots.append(snapshot_b)


def test_failing_policy_provider_on_first_boundary_reports_setup_without_dispatch():
    """Bootstrap failure degrades to setup_required with zero POSTs, no crash."""
    calls = []

    def provider_factory(snapshot):
        calls.append(snapshot)
        return []

    cascade = OrderedSearchProviderCascade(
        providers=[],
        routes=[],
        policy_version="policy-id@startup",
        max_attempts_per_dispatch_round=2,
        policy_source={
            "policy_provider": lambda: (_ for _ in ()).throw(RuntimeError("disk")),
            "provider_factory": provider_factory,
            "effective_mode": "immediate",
        },
    )
    result = cascade.search_question(Question("q1"))[0]

    assert result.status == "error"
    assert result.metadata["failure_type"] == "provider_unavailable"
    assert result.metadata["attempts"] == []
    assert calls == []
    assert result.metadata["execution"]["dispatch_outcome"]["state"] == ("setup_required")


def test_hot_snapshot_with_invalid_quota_group_is_rejected_whole(tmp_path):
    """An adopted snapshot must not half-apply a missing quota_group."""
    from src.config.llm_config import LLMConfig

    policy_a = _ordered_policy()
    fixture_providers = {
        "primary": {"enabled": True, "api_key": "fixture-a"},
        "backup": {"enabled": True, "api_key": "fixture-b"},
    }
    config_path = tmp_path / "llm_apis.json"
    config_path.write_text(
        json.dumps({"providers": fixture_providers, "quick_scan_model_policy": policy_a}),
        encoding="utf-8",
    )
    config = LLMConfig(str(config_path))
    snapshot_a = config.get_quick_scan_model_policy()
    target = {"snapshot": snapshot_a}

    def provider_factory(snapshot):
        providers = []
        for route in snapshot["routes"]:
            provider = Mock()
            provider.get_provider_name.return_value = "probe"
            provider.model = route["model"]
            provider.client.supports_web_search = True
            provider.search_question.return_value = [
                _search_result(
                    "scored",
                    8,
                    attempts=[
                        {
                            "attempt_id": "attempt-" + route["id"],
                            "http_status_code": 200,
                        }
                    ],
                )
            ]
            providers.append(provider)
        return providers

    cascade = OrderedSearchProviderCascade(
        providers=[],
        routes=[],
        policy_version=snapshot_a["policy_version"],
        max_attempts_per_dispatch_round=2,
        policy_source={
            "policy_provider": lambda: target["snapshot"],
            "provider_factory": provider_factory,
            "effective_mode": "immediate",
        },
        health_store=QuickScanProviderHealth(tmp_path / "health.sqlite"),
        quota_groups=snapshot_a["quota_groups"],
    )
    cascade.search_question(Question("q1"))

    snapshot_b = json.loads(json.dumps(snapshot_a))
    snapshot_b["policy_version"] = snapshot_a["policy_version"] + "-invalid"
    for route in snapshot_b["routes"]:
        route["quota_group"] = "does-not-exist"
    target["snapshot"] = snapshot_b

    old_routes = cascade.routes
    old_groups = dict(cascade.quota_groups)
    second = cascade.search_question(Question("q2"))[0]
    # The corrupt snapshot is rejected whole; the running revision still answers.
    assert cascade.policy_version == snapshot_a["policy_version"]
    assert cascade.routes == old_routes
    assert cascade.quota_groups == old_groups
    assert second.score == 8
    assert second.metadata["execution"]["policy_version"] == snapshot_a["policy_version"]
    assert cascade.providers[0].search_question.call_count == 2


def test_policy_update_failure_between_questions_keeps_running_snapshot(tmp_path):
    """A failed policy save must not half-apply between questions."""
    from src.config.llm_config import LLMConfig

    config_path = tmp_path / "llm_apis.json"
    policy_a = _ordered_policy()
    fixture_providers = {
        "primary": {"enabled": True, "api_key": "fixture-a"},
        "backup": {"enabled": True, "api_key": "fixture-b"},
    }
    config_path.write_text(
        json.dumps({"providers": fixture_providers, "quick_scan_model_policy": policy_a}),
        encoding="utf-8",
    )
    config = LLMConfig(str(config_path))
    snapshot_a = config.get_quick_scan_model_policy()
    target = {"snapshot": snapshot_a}

    def current_policy():
        return target["snapshot"]

    def provider_factory(snapshot):
        providers = []
        for route in snapshot["routes"]:
            provider = Mock()
            provider.get_provider_name.return_value = "probe"
            provider.model = route["model"]
            provider.client.supports_web_search = True
            provider.search_question.return_value = [
                _search_result(
                    "scored",
                    8,
                    attempts=[
                        {
                            "attempt_id": "attempt-" + route["id"],
                            "http_status_code": 200,
                        }
                    ],
                )
            ]
            providers.append(provider)
        return providers

    cascade = OrderedSearchProviderCascade(
        providers=[],
        routes=[],
        policy_version=snapshot_a["policy_version"],
        max_attempts_per_dispatch_round=2,
        policy_source={
            "policy_provider": current_policy,
            "provider_factory": provider_factory,
            "effective_mode": "next_run",
        },
    )

    cascade.search_question(Question("q1"))
    saved = dict(policy_a)
    saved["models"][0]["max_in_flight"] = 0
    with pytest.raises(ValueError):
        config.save_quick_scan_model_policy(saved)

    next_snapshot = current_policy()
    assert next_snapshot["policy_version"] == snapshot_a["policy_version"]

    cascade.search_question(Question("q2"))
    assert current_policy()["policy_version"] == snapshot_a["policy_version"]


def _ordered_policy():
    return {
        "schema_version": "2.0.0",
        "policy_id": "grand-audit-policy",
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
            "max_cost": 10,
            "max_requests": 20,
            "max_cost_per_attempt": 1,
            "reset_on_restart": False,
        },
        "cost_policy": {
            "pricing_basis": "verified_rate_card",
            "pricing_ref": "grand-audit-rate-card",
            "include_search_charges": True,
            "include_failed_attempts": True,
            "reserve_before_dispatch": True,
            "unknown_actual_cost_action": "retain_reservation_and_pause",
        },
        "quota_groups": [
            {
                "id": "account-a",
                "max_in_flight": 1,
                "window_seconds_hint": None,
                "unknown_reset_cooldown_seconds": 60,
                "half_open_probe_limit": 1,
            },
            {
                "id": "account-b",
                "max_in_flight": 1,
                "window_seconds_hint": None,
                "unknown_reset_cooldown_seconds": 60,
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
            "max_attempts_per_dispatch_round": 2,
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
                "model": "model-a",
                "quota_group": "account-a",
                "max_in_flight": 1,
            },
            {
                "id": "backup-route",
                "enabled": True,
                "provider_config_ref": "backup",
                "model": "model-b",
                "quota_group": "account-b",
                "max_in_flight": 1,
            },
        ],
    }


def _par11_routes(providers_configs):
    routes = []
    for index, (name, model, group) in enumerate(providers_configs):
        routes.append(
            {
                "id": name,
                "provider_config_ref": name,
                "model": model,
                "eligible": True,
                "quota_group": group,
            }
        )
    return routes


def _par11_policy(policy_id="par-11-unit-policy", route_wait=True):
    policy = {
        "configured": True,
        "policy_id": policy_id,
        "policy_version": policy_id + "@v1",
        "budget": {
            "currency": "USD",
            "max_cost": 5,
            "max_requests": 20,
            "max_cost_per_attempt": 1,
        },
        "cost_policy": {
            "pricing_basis": "verified_rate_card",
            "pricing_ref": "par-11-rate-card",
            "reserve_before_dispatch": True,
            "unknown_actual_cost_action": "retain_reservation_and_pause",
        },
        "dispatch": {"max_in_flight_total": 2},
        "quota_groups": [
            {"id": "group-a", "max_in_flight": 1},
            {"id": "group-b", "max_in_flight": 1},
        ],
        "routes": [
            {
                "id": "route-a",
                "provider_config_ref": "primary",
                "model": "model-a",
                "quota_group": "group-a",
                "max_in_flight": 1,
                "eligible": True,
                "unavailable_reason": None,
            },
            {
                "id": "route-b",
                "provider_config_ref": "backup",
                "model": "model-b",
                "quota_group": "group-b",
                "max_in_flight": 1,
                "eligible": True,
                "unavailable_reason": None,
            },
        ],
    }
    return policy


class TestPar11PreferredCapacityWait:
    """PAR-11: preferred-route capacity full must wait, never fallback to B."""

    def _run_two_questions(
        self, tmp_path, monkeypatch, *, hold_preferred, wait_seconds, release_after=0.4
    ):
        """Two sequential-on-same-slot questions dispatched by one cascade.

        Returns a dict with per-route POST counts, receipts, and the release
        hook so the test can control when the preferred slot frees.
        """
        from threading import Event, Thread

        from src.providers import llm_client as llm_client_module
        from src.providers.llm_client import LLMClient
        from src.utils.quick_scan_work_store import QuickScanWorkStore
        from src.utils.quick_scan_work_transport import (
            bind_quick_scan_budget,
            bind_quick_scan_route,
        )

        store = QuickScanWorkStore(tmp_path / "work.sqlite")
        policy = _par11_policy()
        posts = {"route-a": 0, "route-b": 0}
        route_seen = []
        first_post_entered = Event()
        release_slot = Event()
        session = Mock()
        session.headers = {}

        def post(_url, *, json, **_kwargs):
            route_id = "route-a" if json["model"] == "model-a" else "route-b"
            posts[route_id] = posts.get(route_id, 0) + 1
            route_seen.append(route_id)
            first_post_entered.set()
            if hold_preferred:
                release_slot.wait(timeout=20)
            return _http_ok_response(route_id)

        session.post.side_effect = post
        manager = Mock()
        manager.get_sync_session.return_value = session
        monkeypatch.setattr(llm_client_module, "http_client_manager", manager)

        class _TransportProvider:
            def __init__(self, route_id):
                self.route_id = route_id
                self.model = "model-a" if route_id == "route-a" else "model-b"
                self.client = Mock()
                self.client.supports_web_search = True
                self._llm = LLMClient(
                    api_key="fixture-key",
                    model=self.model,
                    base_url="https://api.openai.com/v1/chat/completions",
                    provider_name="openai" if route_id == "route-a" else "minimax",
                )

            def get_provider_name(self):
                return "primary" if self.route_id == "route-a" else "backup"

            def search_question(self, question):
                prompt = question.text
                with bind_quick_scan_route(
                    route_id=self.route_id,
                    provider=(self.route_id == "route-a" and "primary" or "backup"),
                    model_requested=self.model,
                    quota_group="group-a" if self.route_id == "route-a" else "group-b",
                ):
                    response = self._llm.send_search_request(prompt)
                return [_search_result("scored", 8, attempts=[_attempt_from(response)])]

        cascade = OrderedSearchProviderCascade(
            providers=[_TransportProvider("route-a"), _TransportProvider("route-b")],
            routes=policy["routes"],
            policy_version=policy["policy_version"],
            max_attempts_per_dispatch_round=2,
            require_search=False,
        )
        cascade.PREFERRED_CAPACITY_WAIT_SECONDS = wait_seconds
        timeline = []
        results = {}
        worker_done = Event()

        def run_question(index, label):
            with bind_quick_scan_budget(
                store,
                policy,
                cost_resolver=lambda _receipt: {
                    "actual_cost": 0.01,
                    "pricing_ref": "par-11-rate-card",
                    "source_ref": "par-11-unit-stub",
                },
            ):
                result = cascade.search_question(Question(label))
            results[label] = result[0]
            timeline.append((label, result[0].metadata["execution"]["route_trace"][0]["decision"]))
            worker_done.set()

        worker = Thread(target=run_question, args=(1, "q1"))
        worker.start()
        assert first_post_entered.wait(timeout=15)

        def run_second():
            run_question(2, "q2")

        worker2 = Thread(target=run_second)
        worker2.start()
        if hold_preferred:
            time.sleep(0.4)
            snapshot_during_wait = dict(posts)
            time.sleep(max(0.0, release_after - 0.4))
            release_slot.set()
        else:
            release_slot.set()
            snapshot_during_wait = dict(posts)
        worker.join(timeout=30)
        worker2.join(timeout=30)
        return {
            "posts": posts,
            "results": results,
            "timeline": timeline,
            "route_seen": route_seen,
            "snapshot_during_wait": snapshot_during_wait,
        }

    def test_wait_for_preferred_route_then_send_only_preferred(self, tmp_path, monkeypatch):
        out = self._run_two_questions(tmp_path, monkeypatch, hold_preferred=True, wait_seconds=30.0)
        posts = out["posts"]
        assert out["snapshot_during_wait"] == {"route-a": 1, "route-b": 0}
        assert posts == {"route-a": 2, "route-b": 0}
        q2_trace = out["results"]["q2"].metadata["execution"]["route_trace"]
        assert [item["decision"] for item in q2_trace if "decision" in item] == ["accepted_answer"]

    def test_preferred_route_never_release_yields_deferred_without_fallback(
        self, tmp_path, monkeypatch
    ):
        out = self._run_two_questions(
            tmp_path,
            monkeypatch,
            hold_preferred=True,
            wait_seconds=0.4,
            release_after=1.2,
        )
        assert out["snapshot_during_wait"] == {"route-a": 1, "route-b": 0}
        assert out["posts"] == {"route-a": 1, "route-b": 0}
        q2 = out["results"]["q2"]
        assert q2.status == "insufficient_evidence"
        assert q2.metadata["failure_type"] == "budget_deferred"
        decisions = {item["decision"] for item in q2.metadata["execution"]["route_trace"]}
        assert "budget_deferred" in decisions


def _http_ok_response(route_id):
    response = Mock()
    response.headers = {"x-request-id": f"req-{route_id}-ok"}
    response.status_code = 200
    response.json.return_value = {
        "id": f"resp-{route_id}",
        "status": "completed",
        "model": "actual-model",
        "output": [
            {
                "type": "web_search_call",
                "id": "ws_" + route_id,
                "status": "completed",
                "action": {
                    "type": "search",
                    "sources": [{"type": "url", "url": "https://example.com/entity"}],
                },
            },
            {
                "type": "message",
                "status": "completed",
                "content": [{"type": "output_text", "text": "fixture answer"}],
            },
        ],
    }
    return response


def _attempt_from(response):
    return {"attempt_id": "fixture-attempt", "http_status_code": 200}


class TestCachedLlmRequestDecorator:
    """测试 LLM 请求缓存装饰器。"""

    def test_decorator_caches_results(self):
        """测试装饰器缓存结果。"""
        call_count = [0]

        @cached_llm_request()
        def mock_llm_call(provider: str, prompt: str, system_prompt: str = "") -> str:
            call_count[0] += 1
            return f"Response from {provider}"

        # 第一次调用
        result1 = mock_llm_call("deepseek", "test prompt")
        assert result1 == "Response from deepseek"
        assert call_count[0] == 1

        # 第二次调用应该从缓存返回
        result2 = mock_llm_call("deepseek", "test prompt")
        assert result2 == "Response from deepseek"
        assert call_count[0] == 1  # 不应该增加

    def test_decorator_different_params(self):
        """测试不同参数不会命中缓存。"""
        call_count = [0]

        @cached_llm_request()
        def mock_llm_call(provider: str, prompt: str, system_prompt: str = "") -> str:
            call_count[0] += 1
            return f"Response to: {prompt}"

        mock_llm_call("deepseek", "prompt1")
        mock_llm_call("deepseek", "prompt2")
        assert call_count[0] == 2

    def test_decorator_with_custom_cache(self):
        """测试使用自定义缓存。"""
        custom_cache = RequestCache()
        call_count = [0]

        @cached_llm_request(cache=custom_cache)
        def mock_llm_call(provider: str, prompt: str, system_prompt: str = "") -> str:
            call_count[0] += 1
            return "result"

        mock_llm_call("deepseek", "prompt")
        mock_llm_call("deepseek", "prompt")
        assert call_count[0] == 1  # 应该只调用一次

        assert custom_cache.get_stats()["hits"] == 1


class TestGlobalInstances:
    """测试全局实例。"""

    def test_global_token_tracker(self):
        """测试全局 token 追踪器。"""
        tracker = get_global_token_tracker()
        assert isinstance(tracker, TokenTracker)

    def test_global_request_cache(self):
        """测试全局请求缓存。"""
        cache = get_global_request_cache()
        assert isinstance(cache, RequestCache)


def _durable_cascade(tmp_path, clock, primary, backup, *, shared_group=False):
    routes = [
        _route("primary", "primary", "model-a"),
        _route("backup", "backup", "model-b"),
    ]
    routes[0]["quota_group"] = "account-a"
    routes[1]["quota_group"] = "account-a" if shared_group else "account-b"
    return OrderedSearchProviderCascade(
        providers=[primary, backup],
        routes=routes,
        policy_version="policy-id@fixturehash",
        max_attempts_per_dispatch_round=2,
        health_store=QuickScanProviderHealth(tmp_path / "health.sqlite", clock=lambda: clock[0]),
        quota_groups=[
            {
                "id": "account-a",
                "unknown_reset_cooldown_seconds": 60,
                "half_open_probe_limit": 1,
            },
            {
                "id": "account-b",
                "unknown_reset_cooldown_seconds": 60,
                "half_open_probe_limit": 1,
            },
        ],
    )


def test_durable_quota_cooldown_survives_new_cascade_and_backup_does_not_clear_it(
    tmp_path,
):
    clock = [1_800_000_000.0]
    quota_failure = _search_result(
        "error",
        attempts=[
            {
                "attempt_id": "a-quota",
                "http_status_code": 429,
                "provider_error_code": "insufficient_quota",
            }
        ],
    )
    first_primary = _provider("primary", "model-a", quota_failure)
    first_backup = _provider("backup", "model-b", _search_result("scored", 8))
    first = _durable_cascade(tmp_path, clock, first_primary, first_backup)
    assert first.search_question(Question("q1"))[0].score == 8

    later_primary = _provider("primary", "model-a", _search_result("scored", 9))
    later_backup = _provider("backup", "model-b", _search_result("scored", 7))
    second = _durable_cascade(tmp_path, clock, later_primary, later_backup)
    answer = second.search_question(Question("q2"))[0]
    assert answer.score == 7
    assert answer.metadata["execution"]["route_trace"][0]["decision"] == (
        "skipped_quota_group_cooldown"
    )
    later_primary.search_question.assert_not_called()
    # A probe is eligible after the configured conservative interval, but only
    # its own success restores A. B's earlier answer has no effect on A.
    clock[0] += 60
    assert (
        _durable_cascade(tmp_path, clock, later_primary, later_backup)
        .search_question(Question("q3"))[0]
        .score
        == 9
    )
    later_primary.search_question.assert_called_once()


def test_durable_plain_429_cools_one_route_not_its_entire_group(tmp_path):
    clock = [1_800_000_000.0]
    limited = _provider(
        "primary",
        "model-a",
        _search_result(
            "error",
            attempts=[
                {
                    "attempt_id": "a-429",
                    "http_status_code": 429,
                    "retry_after_seconds": 600,
                }
            ],
        ),
    )
    alternate = _provider("backup", "model-b", _search_result("scored", 8))
    first = _durable_cascade(tmp_path, clock, limited, alternate, shared_group=True)
    assert first.search_question(Question("q1"))[0].score == 8
    next_primary = _provider("primary", "model-a", _search_result("scored", 9))
    next_backup = _provider("backup", "model-b", _search_result("scored", 7))
    again = _durable_cascade(tmp_path, clock, next_primary, next_backup, shared_group=True)
    assert again.search_question(Question("q2"))[0].score == 7
    next_primary.search_question.assert_not_called()
    next_backup.search_question.assert_called_once()


def test_durable_all_cooling_reports_persisted_eligibility_without_dispatch(tmp_path):
    clock = [1_800_000_000.0]
    ledger = QuickScanProviderHealth(tmp_path / "health.sqlite", clock=lambda: clock[0])
    ledger.quota_exhausted("account-a", unknown_reset_cooldown_seconds=60)
    ledger.quota_exhausted("account-b", unknown_reset_cooldown_seconds=60)
    primary = _provider("primary", "model-a", _search_result("scored", 9))
    backup = _provider("backup", "model-b", _search_result("scored", 8))
    result = _durable_cascade(tmp_path, clock, primary, backup).search_question(Question("q1"))[0]
    assert result.status == "error"
    outcome = result.metadata["execution"]["dispatch_outcome"]
    assert outcome["state"] == "retry_wait_recommended"
    assert outcome["scheduled"] is False
    assert outcome["retry_eligible_at"].endswith("Z")
    primary.search_question.assert_not_called()
    backup.search_question.assert_not_called()


def test_durable_cascade_rejects_bad_group_configuration_before_any_dispatch(tmp_path):
    clock = [1_800_000_000.0]
    provider = _provider("primary", "model-a", _search_result("scored", 8))
    with pytest.raises(ValueError, match="quota_group"):
        OrderedSearchProviderCascade(
            providers=[provider],
            routes=[_route("primary", "primary", "model-a")],
            policy_version="policy-id@fixturehash",
            max_attempts_per_dispatch_round=1,
            health_store=QuickScanProviderHealth(
                tmp_path / "health.sqlite", clock=lambda: clock[0]
            ),
            quota_groups=[],
        )
    provider.search_question.assert_not_called()


def test_zero_retry_after_on_confirmed_quota_uses_unknown_reset_probe(tmp_path):
    clock = [1_800_000_000.0]
    primary = _provider(
        "primary",
        "model-a",
        _search_result(
            "error",
            attempts=[
                {
                    "attempt_id": "a-quota-zero",
                    "http_status_code": 429,
                    "provider_error_code": "insufficient_quota",
                    "retry_after_seconds": 0,
                }
            ],
        ),
    )
    backup = _provider("backup", "model-b", _search_result("scored", 8))
    result = _durable_cascade(tmp_path, clock, primary, backup).search_question(Question("q1"))[0]
    assert result.score == 8
    assert result.metadata["execution"]["route_trace"][0]["reset_at_source"] == ("unknown")


def test_plain_429_without_header_exposes_local_cooldown_not_provider_retry_after(
    tmp_path,
):
    clock = [1_800_000_000.0]
    limited = _provider(
        "primary",
        "model-a",
        _search_result("error", attempts=[{"attempt_id": "a-429", "http_status_code": 429}]),
    )
    backup = _provider("backup", "model-b", _search_result("scored", 8))
    first = _durable_cascade(tmp_path, clock, limited, backup, shared_group=True)
    answer = first.search_question(Question("q1"))[0]
    decision = answer.metadata["execution"]["route_trace"][0]
    assert decision["reset_at_source"] == "unknown"
    assert decision["next_probe_at"].endswith("Z")
    assert "retry_after_seconds" not in decision
    assert answer.score == 8

    next_primary = _provider("primary", "model-a", _search_result("scored", 9))
    next_backup = _provider("backup", "model-b", _search_result("scored", 7))
    second = _durable_cascade(tmp_path, clock, next_primary, next_backup, shared_group=True)
    next_answer = second.search_question(Question("q2"))[0]
    skip = next_answer.metadata["execution"]["route_trace"][0]
    assert skip["reset_at_source"] == "unknown"
    assert skip["cooldown_remaining_seconds"] > 0
    assert "retry_after_seconds" not in skip
