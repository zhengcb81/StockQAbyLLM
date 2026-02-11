#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""异步 LLM 提供者。

基于 httpx 的异步 LLM API 提供者，支持并发请求。
"""

import asyncio
import httpx
from typing import List, Dict, Any, Optional, Tuple, cast

from src.config.settings import (
    DEFAULT_SCORE,
    DISPLAY_QUERY_TRUNCATE,
    DISPLAY_TITLE_TRUNCATE,
)
from src.utils.logger import get_logger
from .base_llm_provider import BaseLLMProvider
from .llm_client import AsyncLLMClient

logger = get_logger(__name__)


class AsyncLLMProvider(BaseLLMProvider):
    """异步 LLM 提供者类。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client = AsyncLLMClient(
            api_key=self.api_key,
            model=self.model,
            base_url=self.base_url,
            timeout=self.timeout
        )

    def search(self, query: str) -> List[Dict[str, Any]]:  # type: ignore[override]
        """同步搜索方法（兼容接口）。"""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        return loop.run_until_complete(self.search_async(query))

    async def search_async(self, query: str) -> List[Dict[str, Any]]:
        """异步搜索方法。"""
        logger.info("异步 LLM 处理问题: %s...", query[:DISPLAY_QUERY_TRUNCATE])

        prompt = self._build_prompt(query)
        score, description = await self._call_llm_api_async(prompt)

        result = {
            "title": f"关于 '{query[:DISPLAY_TITLE_TRUNCATE]}...' 的评估",
            "url": "",
            "snippet": description,
            "score": score,
            "source": "llm_api_async",
        }

        logger.info("异步 LLM 答案生成完成 (评分: %d/10)", score)
        return [result]

    async def _call_llm_api_async(self, prompt: str) -> Tuple[int, str]:
        """异步调用 LLM API，带重试。"""
        if not self.api_key:
            logger.warning("LLM API未配置，返回占位符结果")
            return (DEFAULT_SCORE, "LLM API未配置。请配置 API密钥。")

        for retry in range(self.max_retries):
            try:
                logger.info("正在异步调用 %s API... (尝试 %d/%d)", self.provider_name, retry + 1, self.max_retries)
                
                content = await self.client.send_request_async(prompt)
                result = self.parser.parse_response(content)
                
                if result:
                    return result

                if retry < self.max_retries - 1:
                    wait_time = self.retry_strategy.get_wait_time(retry)
                    logger.warning("解析失败，%.1f 秒后重试...", wait_time)
                    await asyncio.sleep(wait_time)
                else:
                    logger.error("经过 %d 次尝试后仍无法解析JSON，返回默认评分", self.max_retries)
                    return (DEFAULT_SCORE, content)

            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                if retry < self.max_retries - 1:
                    wait_time = self.retry_strategy.get_wait_time(retry)
                    logger.warning("异步 API请求失败: %s，%.1f 秒后重试...", e, wait_time)
                    await asyncio.sleep(wait_time)
                else:
                    logger.error("%s 异步 API请求失败，已重试 %d 次: %s", self.provider_name, self.max_retries, e)
                    raise Exception(f"异步 API请求失败，已重试 {self.max_retries} 次: {str(e)}") from e
            
            except Exception as e:
                if retry < self.max_retries - 1:
                    wait_time = self.retry_strategy.get_wait_time(retry)
                    logger.warning("异步发生未知错误: %s，%.1f 秒后重试...", e, wait_time)
                    await asyncio.sleep(wait_time)
                else:
                    logger.error("经过 %d 次尝试后仍发生错误: %s", self.max_retries, e)
                    return (DEFAULT_SCORE, f"处理失败: {str(e)}")

        raise Exception("异步 API调用失败：未知错误")

    async def batch_search_async(self, queries: List[str]) -> List[List[Dict[str, Any]]]:
        """批量异步搜索。"""
        tasks = [self.search_async(query) for query in queries]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        processed_results: List[List[Dict[str, Any]]] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error("查询 %s... 处理失败: %s", queries[i][:50], result)
                processed_results.append([])
            else:
                processed_results.append(cast(List[Dict[str, Any]], result))

        return processed_results