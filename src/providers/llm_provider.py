#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同步 LLM 提供者。

使用 LLM API 分析公司并返回评分和描述。
"""

import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from src.config.settings import (
    DEFAULT_SCORE,
    DISPLAY_QUERY_TRUNCATE,
    DISPLAY_TITLE_TRUNCATE,
)
from src.core.models import SearchResult
from src.utils.logger import get_logger

from .base_llm_provider import BaseLLMProvider
from .llm_client import LLMClient

logger = get_logger(__name__)


class LLMProvider(BaseLLMProvider):
    """同步 LLM 提供者类。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 确保api_key和model不为None
        api_key = self.api_key or ""
        model = self.model or ""
        self.client = LLMClient(
            api_key=api_key, model=model, base_url=self.base_url, timeout=self.timeout
        )

    def search(self, query: str) -> List[SearchResult]:
        """执行同步搜索。"""
        logger.info("LLM处理问题: %s...", query[:DISPLAY_QUERY_TRUNCATE])

        prompt = self._build_prompt(query)
        score, description = self._call_llm_api(prompt)

        result = SearchResult(
            title=f"关于 '{query[:DISPLAY_TITLE_TRUNCATE]}...' 的评估",
            snippet=description,
            source="llm_api",
            url="",
            rank=0,
            score=score,
        )

        logger.info("LLM答案生成完成 (评分: %d/10)", score)
        return [result]

    def _call_llm_api(self, prompt: str, max_retries: Optional[int] = None) -> Tuple[int, str]:
        """调用 LLM API，带重试。"""
        if not self.api_key:
            logger.warning("LLM API未配置，返回占位符结果")
            return (DEFAULT_SCORE, "LLM API未配置。请配置 API密钥。")

        # 使用提供的 max_retries 或默认值
        num_retries = max_retries if max_retries is not None else self.max_retries

        for retry in range(num_retries):
            try:
                logger.info(
                    "正在调用 %s API... (尝试 %d/%d)",
                    self.provider_name,
                    retry + 1,
                    num_retries,
                )

                content = self.client.send_request(prompt)
                result = self.parser.parse_response(content)

                if result:
                    return result

                # 解析失败，执行退避
                if retry < num_retries - 1:
                    wait_time = self.retry_strategy.get_wait_time(retry)
                    logger.warning("解析失败，%.1f 秒后重试...", wait_time)
                    time.sleep(wait_time)
                else:
                    logger.error("经过 %d 次尝试后仍无法解析JSON，返回默认评分", num_retries)
                    return (DEFAULT_SCORE, content)

            except requests.RequestException as e:
                if retry < num_retries - 1:
                    wait_time = self.retry_strategy.get_wait_time(retry)
                    logger.warning("API请求失败: %s，%.1f 秒后重试...", e, wait_time)
                    time.sleep(wait_time)
                else:
                    logger.error(
                        "%s API请求失败，已重试 %d 次: %s",
                        self.provider_name,
                        num_retries,
                        e,
                    )
                    raise Exception(f"API请求失败，已重试 {num_retries} 次: {str(e)}") from e

            except Exception as e:
                if retry < num_retries - 1:
                    wait_time = self.retry_strategy.get_wait_time(retry)
                    logger.warning("发生未知错误: %s，%.1f 秒后重试...", e, wait_time)
                    time.sleep(wait_time)
                else:
                    logger.error("经过 %d 次尝试后仍发生错误: %s", num_retries, e)
                    return (DEFAULT_SCORE, f"处理失败: {str(e)}")

        raise Exception("API调用失败：未知错误")
