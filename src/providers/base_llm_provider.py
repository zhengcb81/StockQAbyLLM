#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 提供者基类。

提供同步和异步 LLM 提供者的共享逻辑。
"""

import os
from typing import List, Dict, Any, Optional, Tuple
from abc import abstractmethod

from src.interfaces.search_provider import SearchProvider
from src.config.llm_config import LLMConfig
from src.config.settings import (
    DEFAULT_TIMEOUT,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_DELAY,
    MAX_TOKENS,
    TEMPERATURE,
)
from src.utils.logger import get_logger
from .llm_response_parser import LLMResponseParser
from .llm_retry_strategy import LLMRetryStrategy

logger = get_logger(__name__)


def mask_api_key(api_key: str) -> str:
    """脱敏API密钥。"""
    if not api_key:
        return "***"
    if len(api_key) <= 8:
        return "***"
    return f"{api_key[:4]}...{api_key[-4:]}"


class BaseLLMProvider(SearchProvider):
    """LLM 提供者基类。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        company_name: Optional[str] = None,
        provider_name: Optional[str] = None,
        config_file: str = "llm_apis.json",
    ):
        """初始化。"""
        self.config_manager = LLMConfig(config_file)
        self.company_name = company_name
        self.provider_name = provider_name or self.config_manager.get_default_provider()
        self.api_key = api_key
        self.model = model

        # 初始化默认值
        self.base_url = "https://api.deepseek.com/v1/chat/completions"
        self.timeout = DEFAULT_TIMEOUT
        self.max_retries = DEFAULT_MAX_RETRIES

        # 初始化辅助组件
        self.parser = LLMResponseParser()
        self.retry_strategy = LLMRetryStrategy(max_retries=DEFAULT_MAX_RETRIES)

        self._load_config()

    def _load_config(self):
        """从配置文件或环境变量加载配置。"""
        provider_config = self.config_manager.get_provider_config(self.provider_name)
        
        if provider_config:
            # 基础配置
            self.base_url = provider_config.get("base_url", self.base_url)
            self.timeout = provider_config.get("timeout", self.timeout)
            self.max_retries = provider_config.get("max_retries", self.max_retries)
            self.retry_strategy.max_retries = self.max_retries
            
            if not self.model:
                self.model = provider_config.get("model", "")

            if not self.api_key:
                # 尝试从环境变量获取
                env_key_names = [
                    f"{self.provider_name.upper()}_API_KEY",
                    f"{self.provider_name.upper()}_KEY",
                    "API_KEY",
                    "LLM_API_KEY",
                ]
                for env_key in env_key_names:
                    env_api_key = os.getenv(env_key)
                    if env_api_key:
                        self.api_key = env_api_key
                        logger.info("从环境变量 %s 读取 API 密钥", env_key)
                        break

                if not self.api_key:
                    self.api_key = provider_config.get("api_key", "")
                    if self.api_key:
                        logger.warning("从配置文件读取 API 密钥（建议使用环境变量）")

        masked_key = mask_api_key(self.api_key) if self.api_key else "N/A"
        logger.info(
            "%s initialized (provider: %s, model: %s, api_key: %s)",
            self.__class__.__name__,
            self.provider_name,
            self.model,
            masked_key,
        )

    def _build_prompt(self, question: str) -> str:
        """构建通用的LLM提示词。"""
        company_context = f"关于公司：{self.company_name}\n" if self.company_name else ""

        return f"""你是一位专业的投资分析师。请对以下问题进行深入分析并给出评分。

{company_context}**问题**：
{question}

**要求**：
1. 基于该公司的实际情况进行分析
2. 给出一个1-10分的评分（1=最差，10=最好）
3. 提供详细的评分理由和分析，包括：
   - 评估维度的具体表现
   - 支撑评分的数据或事实
   - 相关风险因素
   - 与行业同行的对比（如适用）
4. **重要**：请严格按以下JSON格式返回：
{{
  "score": <评分数字，1-10之间的整数>,
  "description": "<详细分析描述，包含评估维度、具体分析、数据支撑、风险因素等>"
}}

请返回JSON格式的回答："""

    def get_provider_name(self) -> str:
        """获取提供者名称。"""
        return self.provider_name or "llm_api"

    @abstractmethod
    def search(self, query: str) -> List[Dict[str, Any]]:
        """执行搜索。"""
        pass
