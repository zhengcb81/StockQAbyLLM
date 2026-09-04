#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 集成增强工具。

提供 Provider 降级、Token 追踪、请求缓存等 LLM 集成功能。
"""

import hashlib
import json
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from functools import wraps
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, TypeVar, cast

from src.utils.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


# ============================================================================
# Token 使用追踪
# ============================================================================


@dataclass
class TokenUsage:
    """Token 使用统计。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """总 token 数（计算属性）。"""
        return self.prompt_tokens + self.completion_tokens

    def add(self, other: "TokenUsage") -> None:
        """累加另一个 TokenUsage。"""
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens

    def to_dict(self) -> Dict[str, int]:
        """转换为字典。"""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class RequestCost:
    """请求成本统计。

    注意：这里使用估算的 token 价格，实际价格可能因提供商而异。
    """

    input_cost: float = 0.0  # 输入 token 成本（美元）
    output_cost: float = 0.0  # 输出 token 成本（美元）

    @property
    def total_cost(self) -> float:
        """总成本（计算属性）。"""
        return self.input_cost + self.output_cost

    # 价格配置（每百万 token 的美元价格）
    INPUT_PRICE_PER_M: float = 0.5  # 默认输入价格
    OUTPUT_PRICE_PER_M: float = 1.5  # 默认输出价格

    def calculate_from_tokens(self, prompt_tokens: int, completion_tokens: int) -> None:
        """根据 token 数量计算成本。"""
        self.input_cost = (prompt_tokens / 1_000_000) * self.INPUT_PRICE_PER_M
        self.output_cost = (completion_tokens / 1_000_000) * self.OUTPUT_PRICE_PER_M

    def add(self, other: "RequestCost") -> None:
        """累加另一个 RequestCost。"""
        self.input_cost += other.input_cost
        self.output_cost += other.output_cost

    def to_dict(self) -> Dict[str, float]:
        """转换为字典。"""
        return {
            "input_cost": round(self.input_cost, 6),
            "output_cost": round(self.output_cost, 6),
            "total_cost": round(self.total_cost, 6),
        }


class TokenTracker:
    """Token 使用和成本追踪器。"""

    def __init__(self):
        """初始化追踪器。"""
        self._usage_by_provider: Dict[str, TokenUsage] = defaultdict(TokenUsage)
        self._cost_by_provider: Dict[str, RequestCost] = defaultdict(RequestCost)
        self._request_count: int = 0
        self._lock = Lock()

    def record_request(
        self,
        provider: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        """记录一次请求的 token 使用。

        Args:
            provider: LLM 提供商名称
            prompt_tokens: 输入 token 数
            completion_tokens: 输出 token 数
        """
        with self._lock:
            self._request_count += 1

            # 记录 token 使用
            usage = TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
            self._usage_by_provider[provider].add(usage)

            # 计算成本
            cost = RequestCost()
            cost.calculate_from_tokens(prompt_tokens, completion_tokens)
            self._cost_by_provider[provider].add(cost)

            logger.debug(
                "记录 token 使用: provider=%s, prompt=%d, completion=%d, total=%d, cost=$%.6f",
                provider,
                prompt_tokens,
                completion_tokens,
                usage.total_tokens,
                cost.total_cost,
            )

    def get_total_usage(self) -> TokenUsage:
        """获取总 token 使用量。"""
        total = TokenUsage()
        with self._lock:
            for usage in self._usage_by_provider.values():
                total.add(usage)
        return total

    def get_total_cost(self) -> RequestCost:
        """获取总成本。"""
        total = RequestCost()
        with self._lock:
            for cost in self._cost_by_provider.values():
                total.add(cost)
        return total

    def get_usage_by_provider(self) -> Dict[str, TokenUsage]:
        """获取各提供商的 token 使用量。"""
        with self._lock:
            return dict(self._usage_by_provider)

    def get_cost_by_provider(self) -> Dict[str, RequestCost]:
        """获取各提供商的成本。"""
        with self._lock:
            return dict(self._cost_by_provider)

    def get_request_count(self) -> int:
        """获取总请求数。"""
        return self._request_count

    def reset(self) -> None:
        """重置所有统计数据。"""
        with self._lock:
            self._usage_by_provider.clear()
            self._cost_by_provider.clear()
            self._request_count = 0
        logger.debug("Token 追踪器已重置")

    def log_summary(self) -> None:
        """记录摘要信息。"""
        total_usage = self.get_total_usage()
        total_cost = self.get_total_cost()

        logger.info("=" * 60)
        logger.info("Token 使用统计摘要")
        logger.info("=" * 60)
        logger.info("总请求数: %d", self._request_count)
        logger.info(
            "总 Token 数: %d (输入: %d, 输出: %d)",
            total_usage.total_tokens,
            total_usage.prompt_tokens,
            total_usage.completion_tokens,
        )
        logger.info("总成本: $%.6f", total_cost.total_cost)

        if self._usage_by_provider:
            logger.info("")
            logger.info("各提供商详情:")
            for provider, usage in self._usage_by_provider.items():
                cost = self._cost_by_provider[provider]
                logger.info(
                    "  %s: %d tokens, $%.6f",
                    provider,
                    usage.total_tokens,
                    cost.total_cost,
                )
        logger.info("=" * 60)


# 全局 token 追踪器
_global_token_tracker = TokenTracker()


def get_global_token_tracker() -> TokenTracker:
    """获取全局 token 追踪器。"""
    return _global_token_tracker


# ============================================================================
# 请求缓存
# ============================================================================


class RequestCache:
    """LLM 请求缓存。

    缓存相同输入的请求结果，避免重复调用 API。
    使用 LRU 策略限制缓存大小。
    """

    def __init__(self, max_size: int = 1000, ttl_seconds: int = 3600):
        """初始化缓存。

        Args:
            max_size: 最大缓存条目数
            ttl_seconds: 缓存过期时间（秒）
        """
        self._cache: Dict[str, Any] = {}
        self._timestamps: Dict[str, float] = {}
        self._access_count: Dict[str, int] = defaultdict(int)
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._lock = Lock()
        self._hits = 0
        self._misses = 0

    def _make_key(self, provider: str, prompt: str, system_prompt: str) -> str:
        """生成缓存键。"""
        key_data = f"{provider}:{system_prompt}:{prompt}"
        return hashlib.sha256(key_data.encode()).hexdigest()

    def get(self, provider: str, prompt: str, system_prompt: str) -> Optional[Any]:
        """获取缓存结果。

        Args:
            provider: LLM 提供商名称
            prompt: 用户提示词
            system_prompt: 系统提示词

        Returns:
            缓存的结果，如果不存在或已过期则返回 None
        """
        key = self._make_key(provider, prompt, system_prompt)

        with self._lock:
            if key not in self._cache:
                self._misses += 1
                return None

            # 检查是否过期
            timestamp = self._timestamps[key]
            if time.time() - timestamp > self._ttl:
                # 过期，删除
                del self._cache[key]
                del self._timestamps[key]
                del self._access_count[key]
                self._misses += 1
                logger.debug("缓存条目已过期: %s", key[:16])
                return None

            self._hits += 1
            self._access_count[key] += 1
            logger.debug("缓存命中: %s (访问次数: %d)", key[:16], self._access_count[key])
            return self._cache[key]

    def set(self, provider: str, prompt: str, system_prompt: str, result: Any) -> None:
        """设置缓存结果。

        Args:
            provider: LLM 提供商名称
            prompt: 用户提示词
            system_prompt: 系统提示词
            result: 要缓存的结果
        """
        key = self._make_key(provider, prompt, system_prompt)

        with self._lock:
            # 如果缓存已满，移除最少使用的条目
            if len(self._cache) >= self._max_size and key not in self._cache:
                # 找到访问次数最少的条目
                lru_key = min(self._access_count.keys(), key=lambda k: self._access_count[k])
                del self._cache[lru_key]
                del self._timestamps[lru_key]
                del self._access_count[lru_key]
                logger.debug("缓存已满，移除 LRU 条目: %s", lru_key[:16])

            self._cache[key] = result
            self._timestamps[key] = time.time()
            self._access_count[key] = 1
            logger.debug("缓存已设置: %s", key[:16])

    def clear(self) -> None:
        """清空缓存。"""
        with self._lock:
            self._cache.clear()
            self._timestamps.clear()
            self._access_count.clear()
        logger.debug("请求缓存已清空")

    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计信息。"""
        with self._lock:
            total = self._hits + self._misses
            hit_rate = self._hits / total if total > 0 else 0

            return {
                "size": len(self._cache),
                "max_size": self._max_size,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": hit_rate,
            }

    def log_stats(self) -> None:
        """记录缓存统计信息。"""
        stats = self.get_stats()
        logger.info(
            "请求缓存统计: 大小=%d/%d, 命中=%d, 未命中=%d, 命中率=%.2f%%",
            stats["size"],
            stats["max_size"],
            stats["hits"],
            stats["misses"],
            stats["hit_rate"] * 100,
        )


# 全局请求缓存
_global_request_cache = RequestCache()


def get_global_request_cache() -> RequestCache:
    """获取全局请求缓存。"""
    return _global_request_cache


# ============================================================================
# 请求关联 ID 追踪
# ============================================================================


class RequestContext:
    """请求上下文。

    用于追踪一个完整请求的生命周期。
    """

    def __init__(self, request_id: Optional[str] = None):
        """初始化请求上下文。

        Args:
            request_id: 请求 ID，如果不提供则自动生成
        """
        self.request_id = request_id or str(uuid.uuid4())
        self.start_time = time.time()
        self.metadata: Dict[str, Any] = {}

    def get_elapsed_time(self) -> float:
        """获取已用时间（秒）。"""
        return time.time() - self.start_time

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典。"""
        return {
            "request_id": self.request_id,
            "start_time": self.start_time,
            "elapsed_time": self.get_elapsed_time(),
            "metadata": self.metadata,
        }


_current_context: Optional[RequestContext] = None
_context_lock = Lock()


def get_request_context() -> Optional[RequestContext]:
    """获取当前请求上下文。"""
    with _context_lock:
        return _current_context


def set_request_context(context: Optional[RequestContext]) -> None:
    """设置当前请求上下文。"""
    with _context_lock:
        global _current_context
        _current_context = context


def create_request_context() -> RequestContext:
    """创建新的请求上下文并设置为当前上下文。"""
    context = RequestContext()
    set_request_context(context)
    return context


def generate_request_id() -> str:
    """生成新的请求 ID。"""
    return str(uuid.uuid4())


# ============================================================================
# Provider 降级
# ============================================================================


class HealthCheckProtocol:
    """Provider 健康检查协议。

    用于检查 Provider 是否处于健康状态。
    """

    def check_health(self, provider: str) -> bool:
        """检查 Provider 是否健康。

        Args:
            provider: Provider 名称

        Returns:
            True 表示健康，False 表示不健康
        """
        raise NotImplementedError


class ProviderCascade:
    """Provider 降级器。

    当主 Provider 失败时，自动降级到备用 Provider。
    支持健康检查和渐进式恢复。
    """

    def __init__(
        self,
        providers: List[str],
        health_check: Optional[Callable[[str], bool]] = None,
        failure_threshold: int = 1,
        recovery_threshold: int = 1,
    ):
        """初始化降级器。

        Args:
            providers: Provider 名称列表，按优先级排序
            health_check: 可选的健康检查回调，接收provider名称返回是否健康
            failure_threshold: 连续失败次数阈值，达到后降级（默认1次即降级）
            recovery_threshold: 连续成功次数阈值，达到后恢复主Provider（默认1次即恢复）
        """
        if not providers:
            raise ValueError("Provider 列表不能为空")

        self._providers = providers
        self._current_index = 0
        self._failure_counts: Dict[str, int] = defaultdict(int)
        self._success_counts: Dict[str, int] = defaultdict(int)
        self._health_check = health_check
        self._failure_threshold = failure_threshold
        self._recovery_threshold = recovery_threshold
        self._lock = Lock()
        self._recovery_pending = False  # 标记是否正在尝试恢复
        logger.info(
            "Provider 降级器已初始化: %s (失败阈值: %d, 恢复阈值: %d)",
            providers,
            failure_threshold,
            recovery_threshold,
        )

    @property
    def health_check(self) -> Optional[Callable[[str], bool]]:
        """获取健康检查回调。"""
        return self._health_check

    @health_check.setter
    def health_check(self, checker: Optional[Callable[[str], bool]]) -> None:
        """设置健康检查回调。"""
        self._health_check = checker
        logger.debug("健康检查回调已更新")

    def get_primary(self) -> str:
        """获取主 Provider。"""
        return self._providers[0]

    def get_current(self) -> str:
        """获取当前使用的 Provider。"""
        with self._lock:
            return self._providers[self._current_index]

    def mark_failure(self, provider: str) -> None:
        """标记 Provider 失败。

        Args:
            provider: 失败的 Provider 名称
        """
        with self._lock:
            self._failure_counts[provider] += 1
            self._success_counts[provider] = 0  # 重置成功计数
            logger.warning(
                "Provider 失败: %s (失败次数: %d/%d)",
                provider,
                self._failure_counts[provider],
                self._failure_threshold,
            )

            # 检查是否达到失败阈值
            if self._failure_counts[provider] >= self._failure_threshold:
                self._try_fallback(provider)

    def _try_fallback(self, failed_provider: str) -> None:
        """尝试降级到备用 Provider。"""
        current_provider = self._providers[self._current_index]
        if failed_provider == current_provider:
            next_index = self._current_index + 1
            if next_index < len(self._providers):
                self._current_index = next_index
                new_provider = self._providers[next_index]
                logger.info("Provider 降级: %s -> %s", current_provider, new_provider)
            else:
                logger.error("所有 Provider 都已失败，降级失败")

    def mark_success(self, provider: str) -> None:
        """标记 Provider 成功。

        Args:
            provider: 成功的 Provider 名称
        """
        with self._lock:
            # 重置失败计数
            self._failure_counts[provider] = 0
            self._success_counts[provider] += 1

            # 如果不是主 Provider，考虑切换回主 Provider
            if provider != self._providers[0]:
                success_count = self._success_counts[provider]
                logger.debug(
                    "Provider 成功: %s (成功次数: %d/%d)",
                    provider,
                    success_count,
                    self._recovery_threshold,
                )

                # 检查是否执行健康检查
                if self._health_check is not None:
                    is_healthy = self._health_check(self._providers[0])
                    if not is_healthy:
                        logger.info("主 Provider 不健康，延迟恢复")
                        self._recovery_pending = True
                        return

                # 检查是否达到恢复阈值
                if success_count >= self._recovery_threshold:
                    self._current_index = 0
                    self._recovery_pending = False
                    logger.info("Provider 恢复: %s -> %s", provider, self._providers[0])

    def get_failure_counts(self) -> Dict[str, int]:
        """获取各 Provider 的失败次数。"""
        with self._lock:
            return dict(self._failure_counts)

    def get_success_counts(self) -> Dict[str, int]:
        """获取各 Provider 的成功次数。"""
        with self._lock:
            return dict(self._success_counts)

    def is_recovering(self) -> bool:
        """检查是否正在恢复。"""
        return self._recovery_pending

    def reset(self) -> None:
        """重置降级器状态。"""
        with self._lock:
            self._current_index = 0
            self._failure_counts.clear()
            self._success_counts.clear()
            self._recovery_pending = False
        logger.debug("Provider 降级器已重置")


# ============================================================================
# 装饰器：缓存 LLM 请求
# ============================================================================


def cached_llm_request(cache: Optional[RequestCache] = None):
    """缓存 LLM 请求的装饰器。

    Args:
        cache: 使用的缓存实例，如果为 None 则使用全局缓存

    Returns:
        装饰器函数
    """
    if cache is None:
        cache = _global_request_cache

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(provider: str, prompt: str, system_prompt: str = "", *args, **kwargs) -> T:
            # 尝试从缓存获取
            cached_result = cache.get(provider, prompt, system_prompt)
            if cached_result is not None:
                return cast(T, cached_result)

            # 调用原函数
            result = func(provider, prompt, system_prompt, *args, **kwargs)

            # 缓存结果
            cache.set(provider, prompt, system_prompt, result)

            return result

        return wrapper

    return decorator
