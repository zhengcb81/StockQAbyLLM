#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM API 配置管理器。

从配置文件加载多个 LLM API 配置，支持自动切换和备用。
"""

import copy
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

from src.config.settings import DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT
from src.utils.logger import get_logger

logger = get_logger(__name__)


class LLMConfig:
    """LLM API 配置管理器。"""

    def __init__(self, config_file: str = "llm_apis.json"):
        """初始化配置管理器。

        Args:
            config_file: 配置文件路径
        """
        self.config_file = Path(config_file)
        self.config: Dict[str, Any] = {}
        self._load_config()

    def _load_config(self) -> None:
        """从文件加载配置。"""
        if not self.config_file.exists():
            logger.warning("配置文件不存在: %s", self.config_file)
            self.config = {"default_provider": "deepseek", "providers": {}}
            return

        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                self.config = json.load(f)
            logger.info("成功加载 LLM 配置文件: %s", self.config_file)
        except json.JSONDecodeError as e:
            logger.error("配置文件 JSON 格式错误: %s", e)
            self.config = {"default_provider": "deepseek", "providers": {}}
        except (OSError, ValueError) as e:
            logger.error("加载配置文件失败: %s", e)
            self.config = {"default_provider": "deepseek", "providers": {}}

    def get_default_provider(self) -> str:
        """获取默认的 LLM 提供者名称。"""
        return cast(str, self.config.get("default_provider", "deepseek"))

    def get_provider_config(self, provider_name: str) -> Optional[Dict[str, Any]]:
        """获取指定 LLM 提供者的配置。

        Args:
            provider_name: 提供者名称（如：deepseek, minimax, glm）

        Returns:
            提供者配置字典，如果不存在返回 None
        """
        providers: Dict[str, Any] = cast(Dict[str, Any], self.config.get("providers", {}))
        return cast(Optional[Dict[str, Any]], providers.get(provider_name))

    def get_enabled_providers(self) -> Dict[str, Dict[str, Any]]:
        """获取所有已启用的 LLM 提供者。

        Returns:
            {provider_name: provider_config} 字典
        """
        providers = self.config.get("providers", {})
        enabled = {
            name: config for name, config in providers.items() if config.get("enabled", False)
        }
        logger.info(f"已启用的 LLM 提供者: {list(enabled.keys())}")
        return enabled

    def get_api_key(self, provider_name: str) -> Optional[str]:
        """获取指定提供者的 API 密钥。

        Args:
            provider_name: 提供者名称

        Returns:
            API 密钥字符串，如果不存在或未启用返回 None
        """
        config = self.get_provider_config(provider_name)
        if not config:
            logger.warning("提供者配置不存在: %s", provider_name)
            return None

        if not config.get("enabled", False):
            logger.warning("提供者未启用: %s", provider_name)
            return None

        api_key = cast(Optional[str], config.get("api_key", ""))
        if not api_key:
            logger.warning("提供者 API 密钥为空: %s", provider_name)
            return None

        return api_key

    def get_base_url(self, provider_name: str) -> str:
        """获取指定提供者的 API base URL。

        Args:
            provider_name: 提供者名称

        Returns:
            Base URL 字符串
        """
        config = self.get_provider_config(provider_name)
        if not config:
            return ""
        return cast(str, config.get("base_url", ""))

    def get_model(self, provider_name: str) -> str:
        """获取指定提供者的默认模型名称。

        Args:
            provider_name: 提供者名称

        Returns:
            模型名称字符串
        """
        config = self.get_provider_config(provider_name)
        if not config:
            return ""
        return cast(str, config.get("model", ""))

    def get_quick_scan_model_policy(self, default_provider: Optional[str] = None) -> Dict[str, Any]:
        """Return one immutable-at-call-time ordered quick-scan route snapshot.

        The optional policy lives in this StockQA-owned config file under
        ``quick_scan_model_policy``. An absent or unconfigured policy preserves
        the legacy single-provider behavior. Active routes follow the order in
        the saved policy; the returned fingerprint changes even if a user edits
        the route order but reuses the same policy_id.
        """
        policy = self.config.get("quick_scan_model_policy")
        if policy is None or (isinstance(policy, dict) and policy.get("configured") is False):
            provider = default_provider or self.get_default_provider()
            provider_config = self.get_provider_config(provider) or {}
            model = provider_config.get("model", "")
            has_key = self._has_provider_credential(provider, provider_config)
            route = {
                "id": "legacy-default",
                "provider_config_ref": provider,
                "model": model,
                "enabled": True,
                "eligible": has_key,
                "unavailable_reason": None if has_key else "provider_credential_missing",
            }
            version_input = json.dumps(
                {"mode": "legacy-single-provider", "provider": provider, "model": model},
                sort_keys=True,
                separators=(",", ":"),
            )
            return {
                "configured": False,
                "policy_id": "legacy-single-provider",
                "policy_version": "legacy-single-provider@"
                + hashlib.sha256(version_input.encode("utf-8")).hexdigest()[:12],
                "routes": [route],
                "required_capabilities": ["web_search", "structured_output"],
                "max_attempts_per_dispatch_round": 1,
            }

        if not isinstance(policy, dict):
            raise ValueError("quick_scan_model_policy必须是对象")
        self._validate_quick_scan_policy(policy)

        routes: List[Dict[str, Any]] = []
        providers = self.config.get("providers", {})
        for entry in policy["models"]:
            if not entry["enabled"]:
                continue
            provider_ref = entry["provider_config_ref"]
            provider_config = providers.get(provider_ref)
            if not isinstance(provider_config, dict):
                raise ValueError(f"模型策略引用了不存在的StockQA提供商配置: {provider_ref}")
            enabled = provider_config.get("enabled") is True
            has_key = self._has_provider_credential(provider_ref, provider_config)
            reason = None
            if not enabled:
                reason = "provider_config_disabled"
            elif not has_key:
                reason = "provider_credential_missing"
            routes.append(
                {
                    "id": entry["id"],
                    "provider_config_ref": provider_ref,
                    "model": entry["model"],
                    "quota_group": entry["quota_group"],
                    "max_in_flight": entry["max_in_flight"],
                    "enabled": True,
                    "eligible": enabled and has_key,
                    "unavailable_reason": reason,
                }
            )

        canonical = json.dumps(policy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        return {
            "configured": True,
            "policy_id": policy["policy_id"],
            "policy_version": f"{policy['policy_id']}@{digest}",
            "routes": routes,
            "quota_groups": [
                {
                    "id": group["id"],
                    "max_in_flight": group["max_in_flight"],
                    "unknown_reset_cooldown_seconds": group["unknown_reset_cooldown_seconds"],
                    "half_open_probe_limit": group["half_open_probe_limit"],
                }
                for group in policy["quota_groups"]
            ],
            "dispatch": {
                "max_in_flight_total": policy["dispatch"]["max_in_flight_total"],
            },
            "budget": copy.deepcopy(policy["budget"]),
            "cost_policy": copy.deepcopy(policy["cost_policy"]),
            "required_capabilities": list(policy["dispatch"]["required_capabilities"]),
            "max_attempts_per_dispatch_round": policy["fallback"][
                "max_attempts_per_dispatch_round"
            ],
        }

    def save_quick_scan_model_policy(self, policy: Dict[str, Any]) -> Dict[str, Any]:
        """Atomically save a validated policy for the next run.

        A running LLMRunner retains the snapshot it loaded at run start. This
        method never stores provider credentials inside the policy object.
        """
        self._validate_quick_scan_policy(policy)
        # Validate provider references and model selection before touching disk.
        snapshot_policy = copy.deepcopy(self.config)
        snapshot_policy["quick_scan_model_policy"] = copy.deepcopy(policy)
        previous = self.config
        self.config = snapshot_policy
        try:
            self.get_quick_scan_model_policy()
        except Exception:
            self.config = previous
            raise

        self.config = previous
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=self.config_file.parent,
                prefix=f".{self.config_file.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_name = temp_file.name
                json.dump(snapshot_policy, temp_file, ensure_ascii=False, indent=2)
                temp_file.write("\n")
                temp_file.flush()
                os.fsync(temp_file.fileno())
            Path(temp_name).replace(self.config_file)
        except Exception:
            if temp_name and Path(temp_name).exists():
                Path(temp_name).unlink()
            raise

        self.config = snapshot_policy
        return self.get_quick_scan_model_policy()

    @staticmethod
    def _validate_quick_scan_policy(policy: Dict[str, Any]) -> None:
        """Validate the complete active v2 contract without a runtime dependency."""
        if not isinstance(policy, dict):
            raise ValueError("quick_scan_model_policy必须是对象")
        LLMConfig._reject_secret_fields(policy)

        def obj(value: Any, path: str, keys: set[str]) -> Dict[str, Any]:
            if not isinstance(value, dict):
                raise ValueError(f"{path}必须是对象")
            missing, extra = keys - set(value), set(value) - keys
            if missing or extra:
                raise ValueError(
                    f"{path}字段缺失或未知 (missing={sorted(missing)}, extra={sorted(extra)})"
                )
            return value

        def text(value: Any, path: str, max_length: Optional[int] = None) -> str:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{path}必须是非空字符串")
            if max_length is not None and len(value) > max_length:
                raise ValueError(f"{path}长度不能超过{max_length}字符")
            return value

        def boolean(value: Any, path: str) -> None:
            if type(value) is not bool:
                raise ValueError(f"{path}必须是布尔值")

        def integer(value: Any, path: str, low: int, high: Optional[int] = None) -> None:
            if type(value) is not int or value < low or (high is not None and value > high):
                raise ValueError(f"{path}整数范围不符合v2策略约束")

        def number(value: Any, path: str, low: float, exclusive: bool = False) -> None:
            try:
                finite = type(value) in (int, float) and math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite or (value <= low if exclusive else value < low):
                raise ValueError(f"{path}数值范围不符合v2策略约束")

        def const(value: Any, expected: Any, path: str) -> None:
            if type(value) is not type(expected) or value != expected:
                raise ValueError(f"{path}必须为{expected!r}")

        root = obj(
            policy,
            "quick_scan_model_policy",
            {
                "schema_version",
                "policy_id",
                "execution_owner",
                "configured",
                "dispatch",
                "budget",
                "cost_policy",
                "quota_groups",
                "models",
                "fallback",
                "comparison",
                "resume",
            },
        )
        const(root["schema_version"], "2.0.0", "schema_version")
        text(root["policy_id"], "policy_id", max_length=200)
        const(root["execution_owner"], "StockQAbyLLM", "execution_owner")
        const(root["configured"], True, "configured")

        dispatch = obj(
            root["dispatch"],
            "dispatch",
            {
                "max_in_flight_total",
                "max_questions_per_pack",
                "one_entity_per_pack",
                "separate_score_and_fact_packs",
                "required_capabilities",
                "on_preferred_capacity_full",
                "speculative_racing",
            },
        )
        integer(dispatch["max_in_flight_total"], "dispatch.max_in_flight_total", 1, 32)
        integer(dispatch["max_questions_per_pack"], "dispatch.max_questions_per_pack", 1, 32)
        const(dispatch["one_entity_per_pack"], True, "dispatch.one_entity_per_pack")
        const(
            dispatch["separate_score_and_fact_packs"],
            True,
            "dispatch.separate_score_and_fact_packs",
        )
        caps = dispatch["required_capabilities"]
        if (
            not isinstance(caps, list)
            or any(
                not isinstance(cap, str) or cap not in {"web_search", "structured_output"}
                for cap in caps
            )
            or len(set(caps)) != len(caps)
            or not {"web_search", "structured_output"}.issubset(set(caps))
        ):
            raise ValueError(
                "dispatch.required_capabilities必须包含唯一的web_search和structured_output"
            )
        const(dispatch["on_preferred_capacity_full"], "wait", "dispatch.on_preferred_capacity_full")
        const(dispatch["speculative_racing"], False, "dispatch.speculative_racing")

        budget = obj(
            root["budget"],
            "budget",
            {"currency", "max_cost", "max_requests", "max_cost_per_attempt", "reset_on_restart"},
        )
        currency = text(budget["currency"], "budget.currency")
        if re.fullmatch(r"[A-Z]{3}", currency) is None:
            raise ValueError("budget.currency必须是三个大写ASCII字母")
        number(budget["max_cost"], "budget.max_cost", 0, exclusive=True)
        integer(budget["max_requests"], "budget.max_requests", 1)
        number(budget["max_cost_per_attempt"], "budget.max_cost_per_attempt", 0, exclusive=True)
        const(budget["reset_on_restart"], False, "budget.reset_on_restart")

        cost = obj(
            root["cost_policy"],
            "cost_policy",
            {
                "pricing_basis",
                "pricing_ref",
                "include_search_charges",
                "include_failed_attempts",
                "reserve_before_dispatch",
                "unknown_actual_cost_action",
            },
        )
        if not isinstance(cost["pricing_basis"], str) or cost["pricing_basis"] not in {
            "verified_rate_card",
            "user_cap",
        }:
            raise ValueError("cost_policy.pricing_basis不受支持")
        pricing_ref = cost["pricing_ref"]
        if pricing_ref is not None:
            text(pricing_ref, "cost_policy.pricing_ref")
        elif cost["pricing_basis"] == "verified_rate_card":
            raise ValueError("cost_policy.pricing_ref在verified_rate_card时必须为非空字符串")
        const(cost["include_search_charges"], True, "cost_policy.include_search_charges")
        const(cost["include_failed_attempts"], True, "cost_policy.include_failed_attempts")
        const(cost["reserve_before_dispatch"], True, "cost_policy.reserve_before_dispatch")
        const(
            cost["unknown_actual_cost_action"],
            "retain_reservation_and_pause",
            "cost_policy.unknown_actual_cost_action",
        )

        groups = root["quota_groups"]
        if not isinstance(groups, list) or not groups:
            raise ValueError("quota_groups必须是非空数组")
        group_ids: set[str] = set()
        for index, value in enumerate(groups):
            path = f"quota_groups[{index}]"
            group = obj(
                value,
                path,
                {
                    "id",
                    "max_in_flight",
                    "window_seconds_hint",
                    "unknown_reset_cooldown_seconds",
                    "half_open_probe_limit",
                },
            )
            group_id = text(group["id"], f"{path}.id", max_length=200)
            if group_id in group_ids:
                raise ValueError(f"quota_groups.id重复: {group_id}")
            group_ids.add(group_id)
            integer(group["max_in_flight"], f"{path}.max_in_flight", 1, 32)
            if group["window_seconds_hint"] is not None:
                integer(group["window_seconds_hint"], f"{path}.window_seconds_hint", 1)
            integer(
                group["unknown_reset_cooldown_seconds"],
                f"{path}.unknown_reset_cooldown_seconds",
                1,
                86400,
            )
            const(group["half_open_probe_limit"], 1, f"{path}.half_open_probe_limit")

        models = root["models"]
        if not isinstance(models, list) or not models:
            raise ValueError("models必须是非空数组")
        route_ids: set[str] = set()
        active_count = 0
        for index, value in enumerate(models):
            path = f"models[{index}]"
            model = obj(
                value,
                path,
                {"id", "enabled", "provider_config_ref", "model", "quota_group", "max_in_flight"},
            )
            route_id = text(model["id"], f"{path}.id", max_length=200)
            if route_id in route_ids:
                raise ValueError(f"models.id重复: {route_id}")
            route_ids.add(route_id)
            boolean(model["enabled"], f"{path}.enabled")
            text(model["provider_config_ref"], f"{path}.provider_config_ref", max_length=200)
            text(model["model"], f"{path}.model", max_length=200)
            group_id = text(model["quota_group"], f"{path}.quota_group", max_length=200)
            if group_id not in group_ids:
                raise ValueError(f"{path}.quota_group引用不存在的组: {group_id}")
            integer(model["max_in_flight"], f"{path}.max_in_flight", 1, 32)
            active_count += int(model["enabled"])
        if active_count == 0:
            raise ValueError("模型策略至少需要一个enabled模型")

        fallback = obj(
            root["fallback"],
            "fallback",
            {
                "on_quota_exhausted",
                "on_rate_limit",
                "on_server_error",
                "on_auth_error",
                "on_invalid_request",
                "on_malformed_response",
                "on_missing_capability",
                "on_timeout",
                "on_low_score",
                "on_unknown_answer",
                "on_fallback_success",
                "max_attempts_per_dispatch_round",
            },
        )
        for key, expected in {
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
        }.items():
            const(fallback[key], expected, f"fallback.{key}")
        integer(
            fallback["max_attempts_per_dispatch_round"],
            "fallback.max_attempts_per_dispatch_round",
            1,
            32,
        )

        comparison = obj(
            root["comparison"],
            "comparison",
            {
                "enabled",
                "max_models_per_question",
                "max_cost",
                "max_requests",
                "same_input_and_cutoff_required",
                "separate_from_primary_fallback",
            },
        )
        boolean(comparison["enabled"], "comparison.enabled")
        integer(comparison["max_models_per_question"], "comparison.max_models_per_question", 2, 8)
        comparison_enabled = comparison["enabled"]
        number(
            comparison["max_cost"],
            "comparison.max_cost",
            0,
            exclusive=comparison_enabled,
        )
        integer(
            comparison["max_requests"],
            "comparison.max_requests",
            1 if comparison_enabled else 0,
        )
        const(
            comparison["same_input_and_cutoff_required"],
            True,
            "comparison.same_input_and_cutoff_required",
        )
        const(
            comparison["separate_from_primary_fallback"],
            True,
            "comparison.separate_from_primary_fallback",
        )

        resume = obj(
            root["resume"],
            "resume",
            {
                "success_checkpoint",
                "replay_outbox_before_dispatch",
                "preserve_uncertain_requests",
                "persist_cooldowns_and_budget",
            },
        )
        const(resume["success_checkpoint"], "per_question", "resume.success_checkpoint")
        const(resume["replay_outbox_before_dispatch"], True, "resume.replay_outbox_before_dispatch")
        const(resume["preserve_uncertain_requests"], True, "resume.preserve_uncertain_requests")
        const(resume["persist_cooldowns_and_budget"], True, "resume.persist_cooldowns_and_budget")

    @staticmethod
    def _reject_secret_fields(value: Any) -> None:
        forbidden = {"api_key", "secret", "access_token", "refresh_token", "password"}
        if isinstance(value, dict):
            for key, child in value.items():
                if isinstance(key, str) and key.lower() in forbidden:
                    raise ValueError("模型策略不得包含API密钥或其他凭据字段")
                LLMConfig._reject_secret_fields(child)
        elif isinstance(value, list):
            for child in value:
                LLMConfig._reject_secret_fields(child)

    @staticmethod
    def _has_provider_credential(provider_name: str, provider_config: Dict[str, Any]) -> bool:
        if isinstance(provider_config.get("api_key"), str) and provider_config["api_key"].strip():
            return True
        return any(
            bool(os.getenv(name))
            for name in (
                f"{provider_name.upper()}_API_KEY",
                f"{provider_name.upper()}_KEY",
                "API_KEY",
                "LLM_API_KEY",
            )
        )

    def get_timeout(self, provider_name: str) -> int:
        """获取指定提供者的超时时间。

        Args:
            provider_name: 提供者名称

        Returns:
            超时时间（秒）
        """
        config = self.get_provider_config(provider_name)
        if not config:
            return DEFAULT_TIMEOUT
        return cast(int, config.get("timeout", DEFAULT_TIMEOUT))

    def get_max_retries(self, provider_name: str) -> int:
        """获取指定提供者的最大重试次数。

        Args:
            provider_name: 提供者名称

        Returns:
            最大重试次数
        """
        config = self.get_provider_config(provider_name)
        if not config:
            return DEFAULT_MAX_RETRIES
        return cast(int, config.get("max_retries", DEFAULT_MAX_RETRIES))

    def is_provider_enabled(self, provider_name: str) -> bool:
        """检查提供者是否已启用。

        Args:
            provider_name: 提供者名称

        Returns:
            True 如果已启用，否则 False
        """
        config = self.get_provider_config(provider_name)
        if not config:
            return False
        return cast(bool, config.get("enabled", False))

    def reload(self) -> None:
        """重新加载配置文件。"""
        logger.info("重新加载 LLM 配置...")
        self._load_config()
