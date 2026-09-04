#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试 LLM 集成增强模块。"""

import time

import pytest

from src.utils.llm_integration import (
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
        assert result == {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}


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
