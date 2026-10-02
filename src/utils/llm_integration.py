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
from dataclasses import dataclass
from functools import wraps
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Sequence, TypeVar, cast

from src.core.models import Question, SearchResult
from src.interfaces.search_provider import SearchProvider
from src.utils.logger import get_logger
from src.utils.quick_scan_provider_health import QuickScanProviderHealth
from src.utils.quick_scan_work_transport import (
    QuickScanBudgetDeferredError,
    bind_quick_scan_route,
)

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

    def __init__(self) -> None:
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


class OrderedSearchProviderCascade(SearchProvider):
    """Execute one question against an ordered, per-question provider snapshot.

    Unlike the legacy stateful ProviderCascade, this class never keeps a
    global current-provider index. A successful answer (including a low score,
    unknown answer, or insufficient evidence) ends this question's dispatch.
    Only an explicit, classified HTTP provider failure permits the next route.

    Q04 runtime policy semantics: the cascade can optionally bind an external
    policy source. Each not-yet-dispatched question consults the source at the
    dispatch boundary; ``next_run`` keeps the startup snapshot for the whole
    run, ``immediate`` switches to the newest valid snapshot at the next
    undispatched question boundary. A question that has already started keeps
    its own revision, receipts and route trace untouched.
    """

    MAX_INLINE_RETRY_AFTER_SECONDS = 2.0
    DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 60.0
    PREFERRED_CAPACITY_WAIT_SECONDS = 30.0
    PREFERRED_CAPACITY_POLL_SECONDS = 0.05

    def __init__(
        self,
        providers: Sequence[Optional[SearchProvider]],
        routes: Sequence[Dict[str, Any]],
        *,
        policy_version: str,
        max_attempts_per_dispatch_round: int,
        require_search: bool = True,
        health_store: Optional[QuickScanProviderHealth] = None,
        quota_groups: Optional[Sequence[Dict[str, Any]]] = None,
        policy_source: Optional[Dict[str, Any]] = None,
    ) -> None:
        if policy_source is not None:
            self._validate_policy_source(policy_source)
            self._policy_source: Optional[Dict[str, Any]] = dict(policy_source)
            self.providers: tuple = ()
            self.routes: tuple = ()
        else:
            self._policy_source = None
            self._validate_static_route_tables(providers, routes)
            self._validate_static_policy_fields(policy_version, max_attempts_per_dispatch_round)
            self.providers = tuple(providers)
            self.routes = tuple(dict(route) for route in routes)
        self.policy_version = policy_version
        self.max_attempts = max_attempts_per_dispatch_round
        self.require_search = require_search
        self.health_store = health_store
        self.quota_groups = {group["id"]: dict(group) for group in (quota_groups or [])}
        if health_store is not None:
            for route in self.routes or ():
                group = self.quota_groups.get(route.get("quota_group"))
                if (
                    group is None
                    or type(group.get("unknown_reset_cooldown_seconds")) is not int
                    or not 1 <= group["unknown_reset_cooldown_seconds"] <= 86400
                    or type(group.get("half_open_probe_limit")) is not int
                    or group["half_open_probe_limit"] != 1
                ):
                    raise ValueError("持久健康状态要求所有route有有效的quota_group配置")
        self.disabled_routes: set[str] = set()
        self.cooled_quota_groups: set[str] = set()
        self.rate_limited_routes: Dict[str, Optional[float]] = {}
        self._revision_lock = Lock()
        self._route_inflight: Dict[str, int] = {}
        first = next((item for item in self.providers if item is not None), None)
        self.provider_name = (
            first.get_provider_name()
            if first is not None
            else (self.routes[0]["provider_config_ref"] if self.routes else None)
        )
        self.model = getattr(first, "model", None) if first is not None else None

    @staticmethod
    def _validate_policy_source(policy_source: Dict[str, Any]) -> None:
        if not isinstance(policy_source, dict):
            raise ValueError("policy_source必须是对象")
        unknown = set(policy_source) - {
            "policy_provider",
            "provider_factory",
            "effective_mode",
        }
        if unknown:
            raise ValueError(f"policy_source包含未知字段: {sorted(unknown)}")
        if not callable(policy_source.get("policy_provider")):
            raise ValueError("policy_source.policy_provider必须可调用")
        if not callable(policy_source.get("provider_factory")):
            raise ValueError("policy_source.provider_factory必须可调用")
        mode = policy_source.get("effective_mode")
        if isinstance(mode, str) and mode in {"next_run", "immediate"}:
            return
        if callable(mode):
            return
        raise ValueError("policy_source.effective_mode必须是next_run或immediate")

    @staticmethod
    def _validate_static_route_tables(
        providers: Sequence[Optional[SearchProvider]],
        routes: Sequence[Dict[str, Any]],
    ) -> None:
        if len(providers) != len(routes):
            raise ValueError("provider实例数量必须与模型策略route数量一致")
        if not routes:
            raise ValueError("模型策略route列表不能为空")

    def _validate_static_policy_fields(
        self,
        policy_version: str,
        max_attempts_per_dispatch_round: int,
    ) -> None:
        if not policy_version:
            raise ValueError("模型策略必须包含policy_version")
        if type(max_attempts_per_dispatch_round) is not int or max_attempts_per_dispatch_round < 1:
            raise ValueError("max_attempts_per_dispatch_round必须是正整数")
        if max_attempts_per_dispatch_round > 32:
            raise ValueError("单轮模型尝试数不得超过32")

    @classmethod
    def _validate_static_quota_groups(
        cls,
        routes: Sequence[Dict[str, Any]],
        health_store: Optional[QuickScanProviderHealth],
        quota_groups: Optional[Sequence[Dict[str, Any]]],
    ) -> Dict[str, Dict[str, Any]]:
        groups = {group["id"]: dict(group) for group in (quota_groups or [])}
        if health_store is not None:
            for route in routes:
                group = groups.get(route.get("quota_group"))
                if (
                    group is None
                    or type(group.get("unknown_reset_cooldown_seconds")) is not int
                    or not 1 <= group["unknown_reset_cooldown_seconds"] <= 86400
                    or type(group.get("half_open_probe_limit")) is not int
                    or group["half_open_probe_limit"] != 1
                ):
                    raise ValueError("持久健康状态要求所有route有有效的quota_group配置")
        return groups

    def bind_policy_source(
        self,
        policy_provider: Callable[[], Dict[str, Any]],
        provider_factory: Callable[[Dict[str, Any]], Sequence[SearchProvider]],
        *,
        effective_mode: str = "next_run",
    ) -> None:
        """Bind a live policy provider; static routes stay until the first boundary.

        Parameters mirror the constructor's ``policy_source`` fields plus the
        factory that materializes provider objects from each policy snapshot.
        """
        self._validate_policy_source(
            {
                "policy_provider": policy_provider,
                "provider_factory": provider_factory,
                "effective_mode": effective_mode,
            }
        )
        with self._revision_lock:
            self._policy_source = {
                "policy_provider": policy_provider,
                "provider_factory": provider_factory,
                "effective_mode": effective_mode,
            }

    def _effective_mode(self) -> str:
        """Resolve the mode once per boundary; callables re-read config."""
        source = self._policy_source
        if source is None:
            return "next_run"
        mode = source.get("effective_mode", "next_run")
        if callable(mode):
            try:
                mode = mode()
            except Exception:
                return "next_run"
        return mode if isinstance(mode, str) and mode in ("next_run", "immediate") else "next_run"

    def _safe_policy_snapshot(self) -> Optional[Dict[str, Any]]:
        """Read a whole, valid policy snapshot or nothing at all."""
        source = self._policy_source
        if source is None:
            return None
        try:
            policy_provider = source["policy_provider"]
            snapshot = policy_provider()
        except Exception:
            return None
        if not isinstance(snapshot, dict):
            return None
        if not snapshot.get("configured"):
            return None
        routes = snapshot.get("routes")
        version = snapshot.get("policy_version")
        if (
            not isinstance(routes, list)
            or not routes
            or not isinstance(version, str)
            or not version
        ):
            return None
        return snapshot

    def get_provider_name(self) -> str:
        """Return the first configured provider for legacy report headers."""
        # ``provider_name`` stays None only until a policy-bound cascade
        # bootstraps its routes; keep the legacy value instead of inventing a name.
        return cast(str, self.provider_name)

    def _refresh_policy_revision(self) -> bool:
        """Swap in a new provider table at an undispatched question boundary.

        ``next_run`` keeps the startup revision for the whole run; ``immediate``
        adopts the newest whole, valid snapshot. A cascade created without
        static routes bootstraps from the bound snapshot once. Returns True
        when the active revision changed.
        """
        if self._policy_source is None:
            return False
        effective_mode = self._effective_mode()
        bootstrap = not self.routes
        if effective_mode != "immediate" and not bootstrap:
            return False
        snapshot = self._safe_policy_snapshot()
        if snapshot is None:
            return False
        new_version = snapshot["policy_version"]
        with self._revision_lock:
            if self.policy_version == new_version and self.routes:
                return False
            try:
                provider_factory = self._policy_source["provider_factory"]
                new_providers = provider_factory(snapshot)
            except Exception:
                return False
            new_routes = snapshot.get("routes", [])
            if (
                not isinstance(new_routes, list)
                or not new_routes
                or any(
                    not isinstance(route, dict)
                    or not isinstance(route.get("provider_config_ref"), str)
                    or not route["provider_config_ref"]
                    for route in new_routes
                )
                or not isinstance(new_providers, (list, tuple))
                or len(new_providers) != len(new_routes)
            ):
                return False
            try:
                max_attempts = int(snapshot.get("max_attempts_per_dispatch_round", 1) or 1)
                self._validate_static_policy_fields(new_version, max_attempts)
                groups = snapshot.get("quota_groups")
                if not isinstance(groups, list):
                    return False
                candidate_groups = self._validate_static_quota_groups(
                    new_routes, self.health_store, groups
                )
                candidate_routes = tuple(dict(route) for route in new_routes)
                candidate_providers = tuple(new_providers)
                candidate_name = next(
                    (item.get_provider_name() for item in candidate_providers if item is not None),
                    candidate_routes[0]["provider_config_ref"],
                )
                candidate_model = next(
                    (
                        getattr(item, "model", None)
                        for item in candidate_providers
                        if item is not None
                    ),
                    None,
                )
            except (TypeError, ValueError, KeyError, AttributeError):
                return False
            self.providers = candidate_providers
            self.routes = candidate_routes
            self.policy_version = new_version
            self.max_attempts = max_attempts
            self.quota_groups = candidate_groups
            self.disabled_routes.clear()
            self.rate_limited_routes.clear()
            self._last_policy_transition = True if effective_mode == "immediate" else None
            self.provider_name = candidate_name
            self.model = candidate_model
            return True

    def _record_transition(self, result: SearchResult, executed: Dict[str, Any]) -> None:
        if self._policy_source is None or not getattr(self, "_last_policy_transition", None):
            return
        executed["policy_transition"] = {
            "at_boundary": True,
            "new_policy_version": self.policy_version,
        }
        attempts = result.metadata.get("attempts")
        if isinstance(attempts, list) and attempts and isinstance(attempts[0], dict):
            attempts[0]["policy_transition"] = {"at_boundary": True}
        self._last_policy_transition = None

    def _wait_for_preferred_capacity(self, route_id: str) -> bool:
        """Wait a bounded time for only the preferred route's own in-flight slot.

        Never bypasses the wait by falling back to another route. Capacity is
        mirrored by the durable budget ledger when one is bound; a plain local
        mirror counts parallel calls made through this cascade instance.

        The ledger is configured once per run with the startup policy (Q08/Q11
        own persistent budget lineage), so the mirrored limit follows that
        revision; a hot-swapped route absent from the ledger falls through to
        the transport gate, which defers it with zero POSTs rather than
        dispatching untracked capacity.
        """
        deadline = time.monotonic() + self.PREFERRED_CAPACITY_WAIT_SECONDS
        while self._preferred_route_busy(route_id):
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.PREFERRED_CAPACITY_POLL_SECONDS)
        return True

    def _preferred_route_busy(self, route_id: str) -> bool:
        from src.utils.quick_scan_work_transport import _BUDGET_BINDING

        budget = _BUDGET_BINDING.get()
        if budget is None:
            return False
        store = budget.store
        policy = budget.policy
        route_limit = None
        routes = policy.get("routes") or []
        for route in routes:
            if route.get("id") == route_id:
                route_limit = route.get("max_in_flight")
                break
        if route_limit is None:
            return False
        try:
            store.get_quick_scan_budget_status(policy["policy_id"])
        except Exception:
            return False
        try:
            import sqlite3

            connection = sqlite3.connect(store.path)
            try:
                row = connection.execute(
                    "SELECT COUNT(*) FROM quick_scan_budget_attempt "
                    "WHERE policy_id=? AND route_id=? AND in_flight=1",
                    (policy["policy_id"], route_id),
                ).fetchone()
                in_flight = row[0] if row else 0
            finally:
                connection.close()
        except Exception:
            in_flight = 0
        return bool(in_flight >= route_limit)

    def search(self, query: str) -> List[SearchResult]:
        """Compatibility search path; quick-scan uses search_question instead."""
        return self.search_question(Question(text=query))

    def search_question(self, question: Question) -> List[SearchResult]:
        """Try eligible ordered providers for one question, stopping on an answer."""
        self._refresh_policy_revision()
        route_trace: List[Dict[str, Any]] = []
        sent_attempts: List[Dict[str, Any]] = []
        request_count = 0
        last_failure: Optional[List[SearchResult]] = None
        last_failure_route: Optional[Dict[str, Any]] = None
        last_failure_provider: Optional[SearchProvider] = None
        if self._policy_source is not None and not self.routes:
            return self._annotate(
                [
                    SearchResult(
                        title="Quick-scan policy unavailable",
                        snippet="运行中模型策略不可用，本题未发送网络请求。",
                        source="error",
                        status="error",
                        metadata={
                            "failure_type": "provider_unavailable",
                            "search_status": "unavailable",
                            "execution": {"search_status": "unavailable"},
                            "attempts": [],
                        },
                    )
                ],
                {"id": None, "provider_config_ref": None, "model": None},
                None,
                [{"decision": "skipped_policy_unavailable"}],
                [],
            )
        if self.routes:
            preferred = self.routes[0]
            preferred_id = preferred.get("id")
            if not self._wait_for_preferred_capacity(preferred_id):
                route_trace.append(
                    {
                        "decision": "budget_deferred",
                        "budget_reason": "dispatch_route_capacity_full",
                        "route_id": preferred.get("id"),
                        "provider_config_ref": preferred.get("provider_config_ref"),
                        "requested_model": preferred.get("model"),
                    }
                )
                return self._annotate(
                    [
                        SearchResult(
                            title="Quick-scan dispatch deferred",
                            snippet="首选路由并发槽持续占用，本题未发送网络请求。",
                            source="quick_scan_budget",
                            status="insufficient_evidence",
                            metadata={
                                "failure_type": "budget_deferred",
                                "search_status": "unavailable",
                                "capacity_route": preferred_id,
                            },
                        )
                    ],
                    preferred,
                    None,
                    route_trace,
                    sent_attempts,
                )

        for index, (route, provider) in enumerate(zip(self.routes, self.providers)):
            route_context = {
                "ordinal": index + 1,
                "route_id": route.get("id"),
                "provider_config_ref": route.get("provider_config_ref"),
                "requested_model": route.get("model"),
            }
            quota_group = route.get("quota_group")
            route_id = route.get("id")
            health_route_id = self._health_route_id(route)
            probe_token: Optional[str] = None
            if route_id in self.disabled_routes:
                route_trace.append({**route_context, "decision": "skipped_auth_disabled"})
                continue
            if self.health_store is None and quota_group in self.cooled_quota_groups:
                route_trace.append({**route_context, "decision": "skipped_quota_group_cooldown"})
                continue
            rate_limit_remaining = self._rate_limit_remaining(route_id)
            if self.health_store is None and route_id in self.rate_limited_routes:
                decision = {
                    **route_context,
                    "decision": "skipped_rate_limit_cooldown",
                }
                if rate_limit_remaining is not None:
                    decision["retry_after_seconds"] = rate_limit_remaining
                route_trace.append(decision)
                continue
            if not route.get("eligible", True) or provider is None:
                route_trace.append({**route_context, "decision": "skipped_unavailable"})
                continue

            client = getattr(provider, "client", None)
            search_capability = getattr(client, "supports_web_search", None)
            if self.require_search and search_capability is not True:
                skip_reason = (
                    "skipped_missing_web_search"
                    if search_capability is False
                    else "skipped_unverified_web_search"
                )
                route_trace.append({**route_context, "decision": skip_reason})
                continue

            provider_search = getattr(provider, "search_question", None)
            if request_count >= self.max_attempts:
                route_trace.append({**route_context, "decision": "skipped_attempt_budget"})
                continue
            if self.health_store is not None:
                group_config = self.quota_groups[quota_group]
                admission = self.health_store.admit(
                    route_id=health_route_id,
                    group_id=quota_group,
                    unknown_reset_cooldown_seconds=group_config["unknown_reset_cooldown_seconds"],
                )
                if not admission.allowed:
                    decision = {
                        **route_context,
                        "decision": (
                            "skipped_rate_limit_cooldown"
                            if admission.reason == "route_rate_limited"
                            else "skipped_quota_group_cooldown"
                        ),
                    }
                    if admission.wait_seconds is not None:
                        decision["cooldown_remaining_seconds"] = admission.wait_seconds
                        if admission.reset_at_source == "retry_after":
                            decision["retry_after_seconds"] = admission.wait_seconds
                    if admission.next_probe_at is not None:
                        decision["next_probe_at"] = admission.next_probe_at
                    if admission.reset_at_source is not None:
                        decision["reset_at_source"] = admission.reset_at_source
                    route_trace.append(decision)
                    continue
                probe_token = admission.probe_token
            while request_count < self.max_attempts:
                route_trace.append({**route_context, "decision": "dispatched"})
                request_count += 1
                attempt_start = len(sent_attempts)
                try:
                    with bind_quick_scan_route(
                        route_id=route_id,
                        provider=route_context["provider_config_ref"],
                        model_requested=route_context["requested_model"]
                        or getattr(provider, "model", None),
                        quota_group=quota_group,
                    ):
                        if callable(provider_search):
                            results = provider_search(question)
                        else:
                            results = provider.search(question.text)
                except QuickScanBudgetDeferredError as error:
                    route_trace[-1]["decision"] = "budget_deferred"
                    route_trace[-1]["budget_reason"] = error.reason
                    return self._annotate(
                        [
                            SearchResult(
                                title="Quick-scan budget deferred",
                                snippet="本次请求未发送：预算或并发账本要求等待容量释放或费用核对。",
                                source="quick_scan_budget",
                                status="insufficient_evidence",
                                metadata={"failure_type": "budget_deferred"},
                            )
                        ],
                        route,
                        provider,
                        route_trace,
                        sent_attempts,
                    )
                except Exception:
                    if self.health_store is not None and probe_token is not None:
                        self.health_store.probe_failed(
                            quota_group,
                            probe_token,
                            unknown_reset_cooldown_seconds=group_config[
                                "unknown_reset_cooldown_seconds"
                            ],
                        )
                    raise

                if not results:
                    route_trace[-1]["decision"] = "answered_without_results"
                    if self.health_store is not None:
                        self.health_store.probe_succeeded(quota_group, probe_token)
                    return self._annotate(
                        [
                            SearchResult(
                                title="No model result",
                                snippet="当前提供商没有返回结果。",
                                source="no_results",
                                status="insufficient_evidence",
                            )
                        ],
                        route,
                        provider,
                        route_trace,
                        sent_attempts,
                    )

                first = results[0]
                transport_attempts = first.metadata.get("attempts", [])
                if isinstance(transport_attempts, list):
                    request_count += max(0, len(transport_attempts) - 1)
                    for attempt in transport_attempts:
                        if isinstance(attempt, dict):
                            sent_attempts.append(
                                {
                                    **attempt,
                                    "provider_config_ref": route_context["provider_config_ref"],
                                    "requested_model": route_context["requested_model"],
                                    "route_id": route_context["route_id"],
                                    "route_ordinal": route_context["ordinal"],
                                }
                            )
                    route_trace[-1]["transport_attempt_count"] = len(transport_attempts) or 1

                failure_category = self._fallback_failure_category(first)
                if (
                    failure_category is None
                    and self.require_search
                    and first.status != "error"
                    and self._search_status(first) != "executed"
                ):
                    failure_category = "runtime_search_unavailable"
                if failure_category is None:
                    route_trace[-1]["decision"] = (
                        "stopped_non_fallback_error"
                        if first.status == "error"
                        else "accepted_answer"
                    )
                    if self.health_store is not None:
                        if first.status == "error":
                            self.health_store.probe_failed(
                                quota_group,
                                probe_token,
                                unknown_reset_cooldown_seconds=group_config[
                                    "unknown_reset_cooldown_seconds"
                                ],
                            )
                        else:
                            self.health_store.probe_succeeded(quota_group, probe_token)
                    return self._annotate(results, route, provider, route_trace, sent_attempts)

                route_trace[-1]["decision"] = "provider_failure"
                route_trace[-1]["failure_category"] = failure_category
                last_failure = results
                last_failure_route = route
                last_failure_provider = provider
                if len(sent_attempts) > attempt_start:
                    sent_attempts[-1]["failure_category"] = failure_category

                if failure_category == "quota_exhausted" and isinstance(quota_group, str):
                    if self.health_store is None:
                        self.cooled_quota_groups.add(quota_group)
                    else:
                        retry_after_value: Any = (
                            sent_attempts[-1].get("retry_after_seconds") if sent_attempts else None
                        )
                        if type(retry_after_value) in (int, float) and retry_after_value > 0:
                            retry_after = retry_after_value
                        else:
                            retry_after = None
                        route_trace[-1]["next_probe_at"] = self.health_store.quota_exhausted(
                            quota_group,
                            unknown_reset_cooldown_seconds=group_config[
                                "unknown_reset_cooldown_seconds"
                            ],
                            retry_after_seconds=retry_after,
                        )
                        route_trace[-1]["reset_at_source"] = (
                            "retry_after" if retry_after is not None else "unknown"
                        )
                elif failure_category in {
                    "authentication_rejected",
                    "model_or_endpoint_unavailable",
                    "runtime_search_unavailable",
                }:
                    self.disabled_routes.add(route_id)
                elif failure_category == "rate_limited":
                    retry_after_value = (
                        sent_attempts[-1].get("retry_after_seconds") if sent_attempts else None
                    )
                    known_retry_after = (
                        type(retry_after_value) in (int, float) and retry_after_value > 0
                    )
                    if known_retry_after:
                        retry_after = float(retry_after_value)
                        route_trace[-1]["retry_after_seconds"] = retry_after
                        already_waited_for_route = any(
                            item.get("route_id") == route_id
                            and item.get("decision") == "provider_rate_limit_retry_after"
                            for item in route_trace[:-1]
                        )
                        if (
                            retry_after <= self.MAX_INLINE_RETRY_AFTER_SECONDS
                            and not already_waited_for_route
                            and request_count < self.max_attempts
                        ):
                            route_trace[-1]["decision"] = "provider_rate_limit_retry_after"
                            time.sleep(retry_after)
                            continue
                        if self.health_store is None:
                            self.rate_limited_routes[route_id] = time.monotonic() + retry_after
                    else:
                        retry_after = self.DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
                        if self.health_store is None:
                            self.rate_limited_routes[route_id] = None
                    if self.health_store is not None:
                        route_trace[-1]["next_probe_at"] = self.health_store.rate_limited(
                            health_route_id,
                            cooldown_seconds=retry_after,
                            source="retry_after" if known_retry_after else "unknown",
                        )
                        route_trace[-1]["reset_at_source"] = (
                            "retry_after" if known_retry_after else "unknown"
                        )
                        self.health_store.probe_failed(
                            quota_group,
                            probe_token,
                            unknown_reset_cooldown_seconds=group_config[
                                "unknown_reset_cooldown_seconds"
                            ],
                        )
                elif failure_category == "provider_server_error":
                    remaining = self._count_eligible_routes(
                        index + 1, self.disabled_routes, self.cooled_quota_groups
                    )
                    if request_count + 1 + remaining <= self.max_attempts:
                        route_trace[-1]["decision"] = "provider_server_retry"
                        continue
                if (
                    self.health_store is not None
                    and probe_token is not None
                    and failure_category not in {"quota_exhausted", "rate_limited"}
                ):
                    self.health_store.probe_failed(
                        quota_group,
                        probe_token,
                        unknown_reset_cooldown_seconds=group_config[
                            "unknown_reset_cooldown_seconds"
                        ],
                    )
                break

        if last_failure is not None and last_failure_route is not None:
            return self._annotate(
                last_failure,
                last_failure_route,
                last_failure_provider,
                route_trace,
                sent_attempts,
            )

        route = next((item for item in self.routes if item.get("eligible", True)), self.routes[0])
        return self._annotate(
            [
                SearchResult(
                    title="No eligible quick-scan provider",
                    snippet="没有已配置且支持联网搜索的模型；本题未发送网络请求。",
                    source="error",
                    status="error",
                    metadata={
                        "failure_type": "provider_unavailable",
                        "search_status": "unavailable",
                        "execution": {"search_status": "unavailable"},
                        "attempts": [],
                    },
                )
            ],
            route,
            None,
            route_trace,
            sent_attempts,
        )

    @staticmethod
    def _health_route_id(route: Dict[str, Any]) -> str:
        """Use actual provider configuration/model, not a reusable display ordinal."""
        identity = json.dumps(
            [route.get("provider_config_ref"), route.get("model")],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @staticmethod
    def _fallback_failure_category(result: SearchResult) -> Optional[str]:
        """Classify only explicit final HTTP failures; never infer from answer quality."""
        if result.status != "error":
            return None
        if result.metadata.get("failure_type") == "outcome_uncertain":
            return None
        attempts = result.metadata.get("attempts", [])
        if not isinstance(attempts, list) or not attempts:
            return None
        status_code = (
            attempts[-1].get("http_status_code") if isinstance(attempts[-1], dict) else None
        )
        if status_code == 429:
            latest_attempt = attempts[-1] if isinstance(attempts[-1], dict) else {}
            if latest_attempt.get("provider_error_code") in {
                "insufficient_quota",
                "quota_exceeded",
                "billing_hard_limit_reached",
                "account_quota_exceeded",
                "insufficient_funds",
            }:
                return "quota_exhausted"
            return "rate_limited"
        if status_code in (401, 403):
            return "authentication_rejected"
        if status_code == 404:
            return "model_or_endpoint_unavailable"
        if type(status_code) is int and 500 <= status_code <= 599:
            return "provider_server_error"
        # Invalid-request 4xx, timeout/connection ambiguity, malformed output,
        # and local configuration failures do not trigger duplicate dispatch.
        return None

    @staticmethod
    def _search_status(result: SearchResult) -> Optional[str]:
        metadata = result.metadata
        execution = metadata.get("execution", {})
        if isinstance(metadata.get("search_status"), str):
            return str(metadata["search_status"])
        if isinstance(execution, dict) and isinstance(execution.get("search_status"), str):
            return str(execution["search_status"])
        return None

    def _rate_limit_remaining(self, route_id: str) -> Optional[float]:
        """Return remaining cooldown seconds; None means no known expiry or no entry."""
        if route_id not in self.rate_limited_routes:
            return None
        cooldown_until = self.rate_limited_routes[route_id]
        if cooldown_until is None:
            return None
        remaining = cooldown_until - time.monotonic()
        if remaining <= 0:
            del self.rate_limited_routes[route_id]
            return None
        return remaining

    def _count_eligible_routes(
        self,
        start_index: int,
        disabled_routes: set[str],
        cooled_quota_groups: set[str],
    ) -> int:
        count = 0
        for route, provider in zip(self.routes[start_index:], self.providers[start_index:]):
            if provider is None or not route.get("eligible", True):
                continue
            if (
                route.get("id") in disabled_routes
                or route.get("quota_group") in cooled_quota_groups
            ):
                continue
            if self._rate_limit_remaining(str(route.get("id") or "")) is not None:
                continue
            if str(route.get("id") or "") in self.rate_limited_routes:
                continue
            client = getattr(provider, "client", None)
            if self.require_search and getattr(client, "supports_web_search", None) is not True:
                continue
            count += 1
        return count

    def _annotate(
        self,
        results: List[SearchResult],
        route: Dict[str, Any],
        provider: Optional[SearchProvider],
        route_trace: List[Dict[str, Any]],
        sent_attempts: List[Dict[str, Any]],
    ) -> List[SearchResult]:
        provider_config_ref = route.get("provider_config_ref")
        requested_model = (
            route.get("model") or getattr(provider, "model", None) if provider is not None else None
        )
        for result in results:
            result.metadata = dict(result.metadata)
            execution = result.metadata.get("execution", {})
            execution = dict(execution) if isinstance(execution, dict) else {}
            candidates = [execution, result.metadata]
            current_attempts = result.metadata.get("attempts")
            if isinstance(current_attempts, list):
                candidates.extend(reversed(current_attempts))
            actual_provider = next(
                (
                    candidate["provider"]
                    for candidate in candidates
                    if isinstance(candidate, dict)
                    and candidate.get("provider") in {"openai", "minimax", "mimo"}
                    and (
                        candidate.get("response_id")
                        or type(candidate.get("http_status_code")) is int
                    )
                ),
                None,
            )
            result.metadata["provider"] = actual_provider
            result.metadata["provider_config_ref"] = provider_config_ref
            result.metadata["model_requested"] = requested_model
            result.metadata["execution"] = execution
            execution["provider"] = actual_provider
            execution["provider_config_ref"] = provider_config_ref
            execution["route_id"] = route.get("id")
            result.metadata["execution"]["policy_version"] = self.policy_version
            result.metadata["execution"]["route_trace"] = [dict(item) for item in route_trace]
            result.metadata["execution"]["dispatch_outcome"] = self._dispatch_outcome(
                result, route_trace, sent_attempts
            )
            if result.status == "error" and sent_attempts:
                final_receipt = sent_attempts[-1]
                for field_name in (
                    "response_status",
                    "http_status_code",
                    "failure_type",
                    "started_at",
                    "completed_at",
                    "attempt_id",
                    "prompt_sha256",
                    "search_receipt_id",
                    "provider_error_code",
                    "retry_after_seconds",
                ):
                    if final_receipt.get(field_name) is not None:
                        result.metadata["execution"][field_name] = final_receipt[field_name]
            result.metadata["attempts"] = [
                {
                    **attempt,
                    "policy_version": self.policy_version,
                    "route_trace": [dict(item) for item in route_trace],
                }
                for attempt in sent_attempts
            ]
            self._record_transition(result, result.metadata["execution"])
        return results

    @staticmethod
    def _dispatch_outcome(
        result: SearchResult,
        route_trace: List[Dict[str, Any]],
        sent_attempts: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Describe whether this dispatch completed, needs setup, or can be retried."""
        retry_eligible_at = min(
            (
                item["next_probe_at"]
                for item in route_trace
                if isinstance(item.get("next_probe_at"), str)
            ),
            default=None,
        )
        if (
            result.status != "error"
            and route_trace
            and route_trace[-1].get("decision") in {"accepted_answer", "answered_without_results"}
        ):
            return {
                "scope": "provider_dispatch",
                "state": "completed",
                "wait_reason": None,
                "resume_condition": None,
            }
        if route_trace and route_trace[-1].get("decision") == "budget_deferred":
            return {
                "scope": "provider_dispatch",
                "state": "budget_deferred",
                "wait_reason": route_trace[-1].get("budget_reason"),
                "resume_condition": "budget_reconciled_or_dispatch_capacity_available",
            }
        last_failure = next(
            (item for item in reversed(route_trace) if item.get("failure_category")),
            None,
        )
        category = last_failure.get("failure_category") if last_failure else None
        if result.metadata.get("failure_type") == "outcome_uncertain":
            final_attempt = sent_attempts[-1] if sent_attempts else {}
            outcome: Dict[str, Any] = {
                "scope": "provider_dispatch",
                "state": "uncertain",
                "wait_reason": "provider_response_unknown",
                "resume_condition": "reconcile_same_attempt_before_retry",
            }
            if final_attempt.get("attempt_id"):
                outcome["uncertain_attempt_id"] = final_attempt["attempt_id"]
            return outcome
        if result.status == "error" and sent_attempts:
            final_attempt = sent_attempts[-1]
            if (
                final_attempt.get("attempt_id")
                and final_attempt.get("http_status_code") is None
                and final_attempt.get("failure_type")
            ):
                return {
                    "scope": "provider_dispatch",
                    "state": "uncertain",
                    "wait_reason": "provider_response_unknown",
                    "resume_condition": "reconcile_same_attempt_before_retry",
                    "uncertain_attempt_id": final_attempt["attempt_id"],
                }

        if route_trace and route_trace[-1].get("decision") == "stopped_non_fallback_error":
            return {
                "scope": "provider_dispatch",
                "state": "failed",
                "wait_reason": "manual_review",
                "resume_condition": "request_or_provider_configuration_corrected",
            }

        if category == "runtime_search_unavailable":
            return {
                "scope": "provider_dispatch",
                "state": "setup_required",
                "wait_reason": "runtime_search_capability_unavailable",
                "resume_condition": "provider_capability_or_configuration_changed",
            }

        decisions = {item.get("decision") for item in route_trace}
        if result.metadata.get("failure_type") == "provider_unavailable" and not sent_attempts:
            if "skipped_quota_group_cooldown" in decisions:
                outcome = {
                    "scope": "provider_dispatch",
                    "state": "retry_wait_recommended",
                    "scheduled": False,
                    "wait_reason": "provider_quota_cooldown",
                    "resume_condition": "quota_window_reset_or_policy_update",
                }
                if retry_eligible_at is not None:
                    outcome["retry_eligible_at"] = retry_eligible_at
                return outcome
            if "skipped_rate_limit_cooldown" in decisions:
                remaining = [
                    item["retry_after_seconds"]
                    for item in route_trace
                    if item.get("decision") == "skipped_rate_limit_cooldown"
                    and type(item.get("retry_after_seconds")) in (int, float)
                ]
                rate_limit_outcome: Dict[str, Any] = {
                    "scope": "provider_dispatch",
                    "state": "retry_wait_recommended",
                    "scheduled": False,
                    "wait_reason": "provider_rate_limited",
                    "resume_condition": (
                        "retry_after_elapsed"
                        if remaining
                        else "next_dispatch_round_or_provider_recovery"
                    ),
                }
                if remaining:
                    rate_limit_outcome["retry_after_seconds"] = min(remaining)
                if retry_eligible_at is not None:
                    rate_limit_outcome["retry_eligible_at"] = retry_eligible_at
                return rate_limit_outcome
            wait_reason = (
                "search_capability_unavailable"
                if decisions.intersection(
                    {"skipped_missing_web_search", "skipped_unverified_web_search"}
                )
                else "provider_not_configured"
            )
            return {
                "scope": "provider_dispatch",
                "state": "setup_required",
                "wait_reason": wait_reason,
                "resume_condition": "provider_configuration_changed",
            }

        if last_failure is not None and category in {
            "rate_limited",
            "quota_exhausted",
            "provider_server_error",
        }:
            wait_reason = {
                "rate_limited": "provider_rate_limited",
                "quota_exhausted": "provider_quota_cooldown",
                "provider_server_error": "provider_transient_error",
            }[category]
            category_outcome: Dict[str, Any] = {
                "scope": "provider_dispatch",
                "state": "retry_wait_recommended",
                "scheduled": False,
                "wait_reason": wait_reason,
                "resume_condition": (
                    "retry_after_elapsed"
                    if last_failure.get("retry_after_seconds") is not None
                    else "next_scheduled_dispatch_or_provider_recovery"
                ),
            }
            if last_failure.get("retry_after_seconds") is not None:
                category_outcome["retry_after_seconds"] = last_failure["retry_after_seconds"]
            if retry_eligible_at is not None:
                category_outcome["retry_eligible_at"] = retry_eligible_at
            return category_outcome

        if result.status != "error":
            return {
                "scope": "provider_dispatch",
                "state": "completed",
                "wait_reason": None,
                "resume_condition": None,
            }

        return {
            "scope": "provider_dispatch",
            "state": "failed",
            "wait_reason": "manual_review",
            "resume_condition": "request_or_provider_configuration_corrected",
        }


# ============================================================================
# 装饰器：缓存 LLM 请求
# ============================================================================


def cached_llm_request(cache: Optional[RequestCache] = None) -> Callable[..., Any]:
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
        def wrapper(
            provider: str,
            prompt: str,
            system_prompt: str = "",
            *args: Any,
            **kwargs: Any,
        ) -> T:
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
