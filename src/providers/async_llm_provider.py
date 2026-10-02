#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""异步 LLM 提供者。

基于 httpx 的异步 LLM API 提供者，支持并发请求。
"""

import asyncio
from typing import Any, Dict, List, Optional, Tuple, cast

import httpx

from src.config.settings import DISPLAY_QUERY_TRUNCATE, DISPLAY_TITLE_TRUNCATE
from src.core.models import Question, SearchResult
from src.utils.logger import get_logger

from .base_llm_provider import BaseLLMProvider
from .llm_client import (
    AsyncLLMClient,
    LLMTransportAttemptError,
    SearchCapabilityUnavailable,
)
from .llm_response_parser import ParsedLLMAnswer

logger = get_logger(__name__)


class AsyncLLMProvider(BaseLLMProvider):
    """异步 LLM 提供者类。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # 确保api_key和model不为None
        api_key = self.api_key or ""
        model = self.model or ""
        self.client = AsyncLLMClient(
            api_key=api_key,
            model=model,
            base_url=self.base_url,
            timeout=self.timeout,
            provider_name=self.provider_name,
        )

    @staticmethod
    def _evidenced_search_provider(execution_metadata: Dict[str, Any]) -> Optional[str]:
        """Name a transport only after a provider response or HTTP status exists."""
        candidates = [execution_metadata]
        attempts = execution_metadata.get("attempts")
        if isinstance(attempts, list):
            candidates.extend(reversed(attempts))
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            provider = candidate.get("provider")
            if not isinstance(provider, str) or provider not in {"openai", "minimax", "mimo"}:
                continue
            if candidate.get("response_id") or type(candidate.get("http_status_code")) is int:
                return provider
        return None

    def search(self, query: str) -> List[SearchResult]:
        """同步搜索方法（兼容接口）。"""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        return loop.run_until_complete(self.search_async(query))

    async def search_async(self, query: str) -> List[SearchResult]:
        """异步搜索方法。"""
        return await self._search_async(query, question_id=None)

    async def search_question_async(self, question: Question) -> List[SearchResult]:
        """Async execution for an identified quick-scan question."""
        return await self._search_async(question.text, question_id=question.question_id)

    def search_question(self, question: Question) -> List[SearchResult]:
        """Synchronous compatibility wrapper for identified questions."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(self.search_question_async(question))

    async def _search_async(self, query: str, question_id: Optional[str]) -> List[SearchResult]:
        logger.info("异步 LLM 处理问题: %s...", query[:DISPLAY_QUERY_TRUNCATE])

        prompt = self._build_prompt(query, question_id=question_id)
        parsed, execution_metadata = await self._call_llm_api_async(
            prompt,
            expected_question_id=question_id,
            expected_entity_id=self.entity_id if self.require_search else None,
            expected_company_name=self.company_name if self.require_search else None,
        )

        if self.require_search:
            execution_metadata = dict(execution_metadata)
            execution_metadata["provider_config_ref"] = self.provider_name
            attempts = execution_metadata.get("attempts")
            if isinstance(attempts, list):
                execution_metadata["attempts"] = [
                    (
                        {**attempt, "provider_config_ref": self.provider_name}
                        if isinstance(attempt, dict)
                        else attempt
                    )
                    for attempt in attempts
                ]

        result = SearchResult(
            title=f"关于 '{query[:DISPLAY_TITLE_TRUNCATE]}...' 的评估",
            snippet=parsed.description,
            source="llm_api_async",
            url="",
            rank=0,
            score=parsed.score,
            status=parsed.status,
            metadata={
                "question_id": parsed.question_id,
                "entity_id": parsed.entity_id,
                "company_name": parsed.company_name,
                "parsed_score": parsed.score,
                "provider": (
                    self._evidenced_search_provider(execution_metadata)
                    if self.require_search
                    else self.provider_name
                ),
                "provider_config_ref": self.provider_name,
                "model_requested": self.model,
                "request_id": execution_metadata.get("request_id"),
                "response_id": execution_metadata.get("response_id"),
                "actual_model": execution_metadata.get("actual_model"),
                "search_status": execution_metadata.get("search_status"),
                "source_urls": execution_metadata.get("source_urls", []),
                "execution": execution_metadata,
                "attempts": execution_metadata.get("attempts", []),
                "format_repair": execution_metadata.get("format_repair"),
            },
        )

        logger.info("异步 LLM 答案生成完成 (状态: %s)", parsed.status)
        return [result]

    async def _repair_invalid_answer_async(
        self,
        original_prompt: str,
        malformed_response: str,
        expected_question_id: Optional[str],
        expected_entity_id: Optional[str],
        expected_company_name: Optional[str],
        initial_metadata: Dict[str, Any],
    ) -> Tuple[ParsedLLMAnswer, Dict[str, Any]]:
        """Perform at most one explicitly budgeted async format repair."""
        repair_prompt = self._build_format_repair_prompt(
            original_prompt,
            malformed_response,
            expected_question_id,
            expected_entity_id,
            expected_company_name,
        )
        repair_metadata: Dict[str, Any] = {}
        try:
            if self.require_search:
                response = await self.client.send_search_request_async(repair_prompt)
                repair_metadata = response.execution_metadata
                if not response.search_verified:
                    metadata = self._merge_format_repair_metadata(
                        initial_metadata, repair_metadata, status="unverified"
                    )
                    return (
                        ParsedLLMAnswer(None, "格式修复响应未通过联网搜索执行验证。", "unknown"),
                        metadata,
                    )
                repaired_content = response.content
            else:
                repaired_content = await self.client.send_request_async(repair_prompt)

            repaired = self.parser.parse_structured_response(
                repaired_content,
                expected_question_id=expected_question_id,
                expected_entity_id=expected_entity_id,
                expected_company_name=expected_company_name,
                strict_json_only=self.require_search,
            )
            metadata = self._merge_format_repair_metadata(
                initial_metadata,
                repair_metadata,
                status="repaired" if repaired is not None else "failed",
            )
            if repaired is None:
                return (
                    ParsedLLMAnswer(None, "一次格式修复后仍无法验证结构化回答。", "unknown"),
                    metadata,
                )
            return repaired, metadata
        except Exception as error:
            logger.warning("异步格式修复失败（%s）", type(error).__name__)
            if isinstance(error, LLMTransportAttemptError):
                repair_metadata = {
                    **error.attempt_receipt,
                    "attempts": [error.attempt_receipt],
                    "search_status": "unverified",
                }
            metadata = self._merge_format_repair_metadata(
                initial_metadata,
                repair_metadata,
                status="failed",
                failure_type=type(error).__name__,
            )
            if self.require_search and not metadata.get("search_status"):
                metadata["search_status"] = "unavailable"
            return ParsedLLMAnswer(None, "格式修复请求失败。", "unknown"), metadata

    async def _call_llm_api_async(
        self,
        prompt: str,
        expected_question_id: Optional[str] = None,
        expected_entity_id: Optional[str] = None,
        expected_company_name: Optional[str] = None,
    ) -> Tuple[ParsedLLMAnswer, Dict[str, Any]]:
        """异步调用 LLM API，带重试。"""
        if not self.api_key:
            logger.warning("LLM API未配置，返回占位符结果")
            return ParsedLLMAnswer(None, "LLM API未配置。请配置 API密钥。", "error"), {}

        if self.require_search and not self.client.supports_web_search:
            return (
                ParsedLLMAnswer(
                    None, "当前提供商或端点不支持可验证的联网搜索。", "insufficient_evidence"
                ),
                {"provider": self.provider_name, "search_status": "unavailable"},
            )

        transport_attempts: List[Dict[str, Any]] = []
        for retry in range(self.max_retries):
            try:
                logger.info(
                    "正在异步调用 %s API... (尝试 %d/%d)",
                    self.provider_name,
                    retry + 1,
                    self.max_retries,
                )

                execution_metadata: Dict[str, Any] = {}
                if self.require_search:
                    search_response = await self.client.send_search_request_async(prompt)
                    execution_metadata = search_response.execution_metadata
                    if transport_attempts:
                        execution_metadata = self._prepend_attempt_history(
                            execution_metadata, transport_attempts
                        )
                    if not search_response.search_verified:
                        return (
                            ParsedLLMAnswer(
                                None,
                                "无法验证本次请求实际完成了联网搜索及来源返回。",
                                "insufficient_evidence",
                            ),
                            execution_metadata,
                        )
                    content = search_response.content
                else:
                    content = await self.client.send_request_async(prompt)
                result = self.parser.parse_structured_response(
                    content,
                    expected_question_id=expected_question_id,
                    expected_entity_id=expected_entity_id,
                    expected_company_name=expected_company_name,
                    strict_json_only=self.require_search,
                )

                if result is not None:
                    return result, execution_metadata
                if self.require_search:
                    if self.format_repair_budget:
                        return await self._repair_invalid_answer_async(
                            prompt,
                            content,
                            expected_question_id,
                            expected_entity_id,
                            expected_company_name,
                            execution_metadata,
                        )
                    execution_metadata = dict(execution_metadata)
                    execution_metadata["format_repair"] = {
                        "attempted": False,
                        "status": "not_enabled",
                        "failure_type": "invalid_answer",
                    }
                    return (
                        ParsedLLMAnswer(None, "无法验证结构化评分回答及目标公司身份。", "unknown"),
                        execution_metadata,
                    )
                if self.format_repair_budget:
                    return await self._repair_invalid_answer_async(
                        prompt,
                        content,
                        expected_question_id,
                        expected_entity_id,
                        expected_company_name,
                        execution_metadata,
                    )

                if retry < self.max_retries - 1:
                    wait_time = self.retry_strategy.get_wait_time(retry)
                    logger.warning("解析失败，%.1f 秒后重试...", wait_time)
                    await asyncio.sleep(wait_time)
                else:
                    logger.error("重试 %d 次后仍然无法解析JSON", self.max_retries)
                    return (
                        ParsedLLMAnswer(None, "无法解析结构化评分回答。", "error"),
                        execution_metadata,
                    )

            except LLMTransportAttemptError as error:
                transport_attempts.append(error.attempt_receipt)
                if retry < self.max_retries - 1:
                    wait_time = self.retry_strategy.get_wait_time(retry)
                    logger.warning(
                        "异步API请求失败（%s），%.1f 秒后重试...",
                        error.failure_type,
                        wait_time,
                    )
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(
                        "%s 异步API请求失败，已尝试 %d 次（%s）",
                        self.provider_name,
                        len(transport_attempts),
                        error.failure_type,
                    )
                    return (
                        ParsedLLMAnswer(None, f"联网请求失败（{error.failure_type}）。", "error"),
                        {
                            "provider": self.provider_name,
                            "search_status": "unverified",
                            "attempts": transport_attempts,
                            "failure_type": error.failure_type,
                        },
                    )

            except SearchCapabilityUnavailable as e:
                return (
                    ParsedLLMAnswer(None, str(e), "insufficient_evidence"),
                    {"provider": self.provider_name, "search_status": "unavailable"},
                )
            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                if retry < self.max_retries - 1:
                    wait_time = self.retry_strategy.get_wait_time(retry)
                    logger.warning(
                        "异步 API请求失败（%s），%.1f 秒后重试...", type(e).__name__, wait_time
                    )
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(
                        "%s 异步 API请求失败，已重试 %d 次（%s）",
                        self.provider_name,
                        self.max_retries,
                        type(e).__name__,
                    )
                    raise Exception(
                        f"异步 API请求失败，已重试 {self.max_retries} 次（{type(e).__name__}）"
                    ) from e

            except Exception as e:
                if retry < self.max_retries - 1:
                    wait_time = self.retry_strategy.get_wait_time(retry)
                    logger.warning(
                        "异步发生未知错误（%s），%.1f 秒后重试...", type(e).__name__, wait_time
                    )
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(
                        "经过 %d 次尝试后仍发生错误（%s）",
                        self.max_retries,
                        type(e).__name__,
                    )
                    return ParsedLLMAnswer(None, f"处理失败（{type(e).__name__}）", "error"), {}

        raise Exception("异步 API调用失败：未知错误")

    async def batch_search_async(self, queries: List[str]) -> List[List[SearchResult]]:
        """批量异步搜索。"""
        tasks = [self.search_async(query) for query in queries]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        processed_results: List[List[SearchResult]] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error("查询 %s... 处理失败: %s", queries[i][:50], result)
                processed_results.append([])
            else:
                processed_results.append(cast(List[SearchResult], result))

        return processed_results
