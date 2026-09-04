#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM API 配置管理器。

从配置文件加载多个 LLM API 配置，支持自动切换和备用。
"""

import json
from pathlib import Path
from typing import Any, Dict, Optional, cast

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
