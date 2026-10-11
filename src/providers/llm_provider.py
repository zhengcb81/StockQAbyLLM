#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同步 LLM 提供者。

使用 LLM API 分析公司并返回评分和描述。
"""

import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, cast

import requests

from src.config.settings import DISPLAY_QUERY_TRUNCATE, DISPLAY_TITLE_TRUNCATE
from src.core.models import Question, SearchResult
from src.utils.logger import get_logger
from src.utils.quick_scan_work_transport import (
    QuickScanBudgetDeferredError,
    QuickScanWorkPersistenceError,
    QuickScanWorkUncertainError,
    bind_quick_scan_format_repair,
    external_answer_search_mode,
)

from .base_llm_provider import BaseLLMProvider
from .llm_client import (
    LLMClient,
    LLMTransportAttemptError,
    SearchCapabilityUnavailable,
    _validate_search_generation_controls,
)
from .llm_response_parser import ParsedLLMAnswer

logger = get_logger(__name__)


@dataclass(frozen=True)
class QuestionPackSearchResult:
    """One physical response and its independently parsed logical answers."""

    results: Tuple[SearchResult, ...]
    missing_question_ids: Tuple[str, ...]
    invalid_question_ids: Tuple[str, ...]
    execution_metadata: Dict[str, Any]


class LLMProvider(BaseLLMProvider):
    """同步 LLM 提供者类。"""

    def __init__(
        self,
        *args: Any,
        thinking_enabled: Optional[bool] = None,
        max_output_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> None:
        _validate_search_generation_controls(thinking_enabled, max_output_tokens)
        super().__init__(*args, **kwargs)
        if not self.require_search and (
            thinking_enabled is not None or max_output_tokens is not None
        ):
            raise ValueError("explicit generation controls require the verified search client")
        self.thinking_enabled = thinking_enabled
        self.max_output_tokens = max_output_tokens
        # 确保api_key和model不为None
        api_key = self.api_key or ""
        model = self.model or ""
        self.client = LLMClient(
            api_key=api_key,
            model=model,
            base_url=self.base_url,
            timeout=self.timeout,
            provider_name=self.provider_name,
            model_resolution=self.model_resolution,
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
            if not isinstance(provider, str) or provider not in {
                "openai",
                "minimax",
                "mimo",
                "deepseek",
            }:
                continue
            if candidate.get("response_id") or type(candidate.get("http_status_code")) is int:
                return provider
        return None

    def search(self, query: str) -> List[SearchResult]:
        """执行同步搜索。"""
        return self._search(query, question_id=None)

    def search_question(self, question: Question) -> List[SearchResult]:
        """Execute one identified quick-scan question without inferring its ID."""
        return self._search(question.text, question_id=question.question_id)

    def _search(self, query: str, question_id: Optional[str]) -> List[SearchResult]:
        logger.info("LLM处理问题: %s...", query[:DISPLAY_QUERY_TRUNCATE])

        prompt = self._build_prompt(query, question_id=question_id)
        parsed, execution_metadata = self._call_llm_api(
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

        result = self._result_from_parsed(query, parsed, execution_metadata)

        logger.info("LLM答案生成完成 (状态: %s)", parsed.status)
        return [result]

    def _result_from_parsed(
        self, query: str, parsed: ParsedLLMAnswer, execution_metadata: Dict[str, Any]
    ) -> SearchResult:
        from src.utils.quick_scan_work_transport import project_quick_scan_search

        search_projection = project_quick_scan_search(execution_metadata)

        return SearchResult(
            title=f"关于 '{query[:DISPLAY_TITLE_TRUNCATE]}...' 的评估",
            snippet=parsed.description,
            source="llm_api",
            url="",
            rank=0,
            score=parsed.score,
            status=parsed.status,
            metadata={
                "question_id": parsed.question_id,
                "entity_id": parsed.entity_id,
                "company_name": parsed.company_name,
                "parsed_score": parsed.score,
                "information_as_of": getattr(parsed, "information_as_of", None),
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
                "search_status": search_projection.get("search_status"),
                "search_receipt_id": search_projection.get("search_receipt_id"),
                "source_urls": search_projection.get("source_urls", []),
                "failure_type": execution_metadata.get("failure_type"),
                "execution": execution_metadata,
                "attempts": execution_metadata.get("attempts", []),
                "format_repair": execution_metadata.get("format_repair"),
            },
        )

    def _search_generation_options(self) -> Dict[str, Any]:
        options: Dict[str, Any] = {}
        if self.thinking_enabled is not None:
            options["thinking_enabled"] = self.thinking_enabled
        if self.max_output_tokens is not None:
            options["max_output_tokens"] = self.max_output_tokens
        return options

    def search_question_pack(
        self,
        questions: List[Question],
        *,
        thinking_enabled: bool = True,
        max_output_tokens: int = 8192,
    ) -> QuestionPackSearchResult:
        """Send one registered native-search pack, with no retries or format repair.

        This parser/provider step does not attest financial correctness or an
        import ACK. Each returned member retains the one physical usage receipt;
        missing and invalid members are diagnostics, never synthetic answers.
        """
        if not self.require_search:
            raise ValueError("question packs require verified search")
        if type(thinking_enabled) is not bool or type(max_output_tokens) is not int:
            raise ValueError("question pack generation controls must be explicit bool/int")
        _validate_search_generation_controls(thinking_enabled, max_output_tokens)
        if (
            not isinstance(self.entity_id, str)
            or not self.entity_id.strip()
            or self.entity_id != self.entity_id.strip()
            or not isinstance(self.company_name, str)
            or not self.company_name.strip()
            or self.company_name != self.company_name.strip()
        ):
            raise ValueError("question pack requires exact entity and company identity")
        if not isinstance(questions, list) or not 1 <= len(questions) <= 30:
            raise ValueError("questions must be a list of 1..30 identified questions")
        if any(
            not isinstance(question, Question)
            or not isinstance(question.text, str)
            or not question.text.strip()
            or not isinstance(question.question_id, str)
            or not 1 <= len(question.question_id) <= 160
            or re.fullmatch(r"[A-Z0-9_]+", question.question_id) is None
            for question in questions
        ):
            raise ValueError("question pack members need stable uppercase question IDs")
        question_ids = tuple(cast(str, question.question_id) for question in questions)
        if len(set(question_ids)) != len(question_ids):
            raise ValueError("question pack IDs must be unique")
        if external_answer_search_mode() is not None:
            raise QuickScanWorkPersistenceError(
                "question packs currently require native search; external evidence needs independent member proofs"
            )
        from src.utils.quick_scan_work_transport import (
            assert_question_pack_binding,
            project_question_pack_transport,
        )

        assert_question_pack_binding(question_ids, self.entity_id)
        prompt = self._build_prompt("", question_pack=questions)

        def reply(
            metadata: Dict[str, Any],
            status: str,
            results: Tuple[SearchResult, ...] = (),
            missing: Tuple[str, ...] = (),
            invalid: Tuple[str, ...] = (),
        ) -> QuestionPackSearchResult:
            returned = dict(metadata)
            returned["question_pack"] = {
                "status": status,
                "answer_status": "unknown" if not results else "independent_member_statuses",
                "question_ids": list(question_ids),
                "missing_question_ids": list(missing),
                "invalid_question_ids": list(invalid),
                "format_repair_attempted": False,
            }
            return QuestionPackSearchResult(results, missing, invalid, returned)

        if not self.api_key:
            return reply(
                {"search_status": "unavailable", "failure_type": "missing_api_key"},
                "unavailable",
                missing=question_ids,
            )
        if not self.client.supports_web_search:
            return reply(
                {"search_status": "unavailable", "failure_type": "unsupported_search"},
                "unavailable",
                missing=question_ids,
            )
        try:
            response = self.client.send_search_request(
                prompt,
                thinking_enabled=thinking_enabled,
                max_output_tokens=max_output_tokens,
            )
        except SearchCapabilityUnavailable as error:
            return reply(
                {"search_status": "unavailable", "failure_type": type(error).__name__},
                "unavailable",
                missing=question_ids,
            )
        except LLMTransportAttemptError as error:
            return reply(
                {
                    **error.attempt_receipt,
                    "attempts": [error.attempt_receipt],
                    "search_status": "unverified",
                    "failure_type": error.failure_type,
                },
                "transport_failed",
                missing=question_ids,
            )

        # Never accept the model/provider response's member map as authority.
        # The projector re-reads the active immutable group and final owner receipt.
        trusted = project_question_pack_transport(response.execution_metadata)
        members = trusted.get("members")
        if not isinstance(members, list) or [
            member.get("question_id") for member in members
        ] != list(question_ids):
            raise QuickScanWorkPersistenceError(
                "question pack owner membership differs from supplied order"
            )
        metadata = dict(response.execution_metadata)
        metadata["provider_config_ref"] = self.provider_name
        if not response.search_verified:
            return reply(metadata, "search_unverified", missing=question_ids)
        parsed = self.parser.parse_question_pack_response(
            response.content,
            question_ids,
            expected_entity_id=self.entity_id,
            expected_company_name=self.company_name,
        )
        if parsed is None:
            return reply(metadata, "rejected", invalid=question_ids)
        by_id = {member["question_id"]: member for member in members}
        questions_by_id = {question.question_id: question for question in questions}
        physical = metadata.get("work_transport")
        if not isinstance(physical, dict):
            raise QuickScanWorkPersistenceError("question pack has no actual physical transport")
        results = []
        for answer in parsed.answers:
            member = by_id[answer.question_id]
            execution = dict(metadata)
            execution["work_transport"] = {
                **physical,
                "work_attempt_id": member["work_attempt_id"],
                "work_item_id": member["work_item_id"],
                "physical_budget_attempt_id": trusted["budget_attempt_id"],
                "question_pack_id": trusted["pack_id"],
            }
            execution["question_pack_member"] = {
                "pack_id": trusted["pack_id"],
                "question_id": answer.question_id,
                "work_item_id": member["work_item_id"],
                "work_attempt_id": member["work_attempt_id"],
                "execution_attempt_id": trusted["execution_attempt_id"],
                "usage_basis": "shared_physical_request_not_apportioned",
            }
            results.append(
                self._result_from_parsed(
                    questions_by_id[answer.question_id].text, answer, execution
                )
            )
        status = (
            "partial" if parsed.missing_question_ids or parsed.invalid_question_ids else "complete"
        )
        return reply(
            metadata,
            status,
            tuple(results),
            parsed.missing_question_ids,
            parsed.invalid_question_ids,
        )

    def _repair_invalid_answer(
        self,
        original_prompt: str,
        malformed_response: str,
        expected_question_id: Optional[str],
        expected_entity_id: Optional[str],
        expected_company_name: Optional[str],
        initial_metadata: Dict[str, Any],
    ) -> Tuple[ParsedLLMAnswer, Dict[str, Any]]:
        """Perform at most one explicitly budgeted format repair."""
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
                with bind_quick_scan_format_repair():
                    response = self.client.send_search_request(
                        repair_prompt, **self._search_generation_options()
                    )
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
                repaired_content = self.client.send_request(repair_prompt)

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
            if isinstance(
                error,
                (
                    QuickScanBudgetDeferredError,
                    QuickScanWorkPersistenceError,
                    QuickScanWorkUncertainError,
                ),
            ):
                raise
            logger.warning("格式修复失败（%s）", type(error).__name__)
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

    def _call_llm_api(
        self,
        prompt: str,
        max_retries: Optional[int] = None,
        expected_question_id: Optional[str] = None,
        expected_entity_id: Optional[str] = None,
        expected_company_name: Optional[str] = None,
    ) -> Tuple[ParsedLLMAnswer, Dict[str, Any]]:
        """调用 LLM API，带重试。"""
        if not self.api_key:
            logger.warning("LLM API未配置，返回占位符结果")
            return ParsedLLMAnswer(None, "LLM API未配置。请配置 API密钥。", "error"), {}

        if (
            self.require_search
            and not self.client.supports_web_search
            and external_answer_search_mode() != "external_context_only"
        ):
            return (
                ParsedLLMAnswer(
                    None, "当前提供商或端点不支持可验证的联网搜索。", "insufficient_evidence"
                ),
                {"provider": None, "search_status": "unavailable"},
            )

        # 使用提供的 max_retries 或默认值
        num_retries = max_retries if max_retries is not None else self.max_retries
        transport_attempts: List[Dict[str, Any]] = []

        for retry in range(num_retries):
            try:
                logger.info(
                    "正在调用 %s API... (尝试 %d/%d)",
                    self.provider_name,
                    retry + 1,
                    num_retries,
                )

                execution_metadata: Dict[str, Any] = {}
                if self.require_search:
                    search_response = self.client.send_search_request(
                        prompt, **self._search_generation_options()
                    )
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
                    content = self.client.send_request(prompt)
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
                        return self._repair_invalid_answer(
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
                    return self._repair_invalid_answer(
                        prompt,
                        content,
                        expected_question_id,
                        expected_entity_id,
                        expected_company_name,
                        execution_metadata,
                    )

                if retry < num_retries - 1:
                    wait_time = self.retry_strategy.get_wait_time(retry)
                    logger.warning("解析失败，%.1f 秒后重试...", wait_time)
                    time.sleep(wait_time)
                else:
                    logger.error("重试 %d 次后仍然无法解析JSON", num_retries)
                    return (
                        ParsedLLMAnswer(None, "无法解析结构化评分回答。", "error"),
                        execution_metadata,
                    )

            except (
                QuickScanBudgetDeferredError,
                QuickScanWorkPersistenceError,
                QuickScanWorkUncertainError,
            ):
                raise
            except LLMTransportAttemptError as error:
                transport_attempts.append(error.attempt_receipt)
                if retry < num_retries - 1:
                    wait_time = self.retry_strategy.get_wait_time(retry)
                    logger.warning(
                        "API请求失败（%s），%.1f 秒后重试...", error.failure_type, wait_time
                    )
                    time.sleep(wait_time)
                else:
                    logger.error(
                        "%s API请求失败，已尝试 %d 次（%s）",
                        self.provider_name,
                        len(transport_attempts),
                        error.failure_type,
                    )
                    return (
                        ParsedLLMAnswer(None, f"联网请求失败（{error.failure_type}）。", "error"),
                        {
                            "provider": None,
                            "search_status": "unverified",
                            "attempts": transport_attempts,
                            "failure_type": error.failure_type,
                        },
                    )

            except SearchCapabilityUnavailable as e:
                return (
                    ParsedLLMAnswer(None, str(e), "insufficient_evidence"),
                    {"provider": None, "search_status": "unavailable"},
                )
            except requests.RequestException as e:
                if retry < num_retries - 1:
                    wait_time = self.retry_strategy.get_wait_time(retry)
                    logger.warning(
                        "API请求失败（%s），%.1f 秒后重试...", type(e).__name__, wait_time
                    )
                    time.sleep(wait_time)
                else:
                    logger.error(
                        "%s API请求失败，已重试 %d 次（%s）",
                        self.provider_name,
                        num_retries,
                        type(e).__name__,
                    )
                    raise Exception(
                        f"API请求失败，已重试 {num_retries} 次（{type(e).__name__}）"
                    ) from e

            except Exception as e:
                if retry < num_retries - 1:
                    wait_time = self.retry_strategy.get_wait_time(retry)
                    logger.warning(
                        "发生未知错误（%s），%.1f 秒后重试...", type(e).__name__, wait_time
                    )
                    time.sleep(wait_time)
                else:
                    logger.error("经过 %d 次尝试后仍发生错误（%s）", num_retries, type(e).__name__)
                    return ParsedLLMAnswer(None, f"处理失败（{type(e).__name__}）", "error"), {}

        raise Exception("API调用失败：未知错误")
