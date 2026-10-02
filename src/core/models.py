"""数据模型定义。

该模块定义了 StockQAbyLLM 系统中使用的所有数据模型。
使用 dataclasses 以提供类型安全和默认值。
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_isoformat(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


@dataclass
class SearchResult:
    """表示一个搜索结果。

    统一的搜索结果类型，用于替换 Dict[str, Any]。

    Attributes:
        title: 结果标题
        snippet: 结果摘要
        source: 结果来源（例如：'web_search', 'llm'）
        url: 结果 URL（可选）
        rank: 结果排名（可选）
        score: 结果评分（可选，1-10分，主要用于LLM结果）
        created_at: 结果创建时间
    """

    title: str
    snippet: str
    source: str = "unknown"
    url: Optional[str] = None
    rank: int = 0
    score: Optional[int] = None
    created_at: datetime = field(default_factory=datetime.now)
    status: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """验证搜索结果。"""
        if not self.title or not self.title.strip():
            raise ValueError("搜索结果标题不能为空")
        if not self.snippet or not self.snippet.strip():
            raise ValueError("搜索结果摘要不能为空")
        if self.score is not None and (
            type(self.score) is not int or self.score < 1 or self.score > 10
        ):
            raise ValueError("评分必须是1-10之间的整数")
        if self.status is not None and self.status not in {
            "scored",
            "unknown",
            "insufficient_evidence",
            "not_applicable",
            "error",
        }:
            raise ValueError("答案状态无效")
        if self.status is not None and ((self.status == "scored") != (self.score is not None)):
            raise ValueError("只有scored状态可以包含1-10分，其他状态必须为空分")

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式。

        Returns:
            包含所有字段的字典
        """
        result: Dict[str, Any] = {
            "title": self.title,
            "snippet": self.snippet,
            "source": self.source,
            "url": self.url,
            "rank": self.rank,
        }
        if self.score is not None:
            result["score"] = self.score
        if self.status is not None:
            result["status"] = self.status
        if self.metadata:
            result["metadata"] = self.metadata
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SearchResult":
        """从字典创建 SearchResult 实例。

        Args:
            data: 包含搜索结果的字典

        Returns:
            SearchResult 实例
        """
        return cls(
            title=data.get("title", ""),
            snippet=data.get("snippet", ""),
            source=data.get("source", "unknown"),
            url=data.get("url"),
            rank=data.get("rank", 0),
            score=data.get("score"),
            status=data.get("status"),
            metadata=data.get("metadata", {}),
        )

    def __str__(self) -> str:
        """返回搜索结果的字符串表示。"""
        return f"[{self.source}] {self.title}"


@dataclass
class Question:
    """表示一个问题。

    Attributes:
        text: 问题的文本内容
        created_at: 问题创建的时间戳
    """

    text: str
    created_at: datetime = field(default_factory=datetime.now)
    question_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """验证问题文本。"""
        if not self.text or not self.text.strip():
            raise ValueError("问题文本不能为空")
        # 去除首尾空格
        self.text = self.text.strip()
        if self.question_id is not None and (
            not isinstance(self.question_id, str)
            or not re.fullmatch(r"[A-Z0-9_]+", self.question_id)
        ):
            raise ValueError("问题ID必须仅包含大写字母、数字和下划线")

    def __str__(self) -> str:
        """返回问题的字符串表示。"""
        return self.text


@dataclass
class Answer:
    """表示一个答案。

    Attributes:
        text: 答案的文本内容（描述性回答）
        score: 答案的评分（1-10分）
        source: 答案来源（例如：'web_search', 'llm'）
        created_at: 答案生成的时间戳
    """

    text: str
    score: Optional[int] = None
    source: str = "web_search"
    created_at: datetime = field(default_factory=datetime.now)
    status: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """验证答案文本和评分。"""
        if not self.text or not self.text.strip():
            raise ValueError("答案文本不能为空")
        if self.score is not None and (
            type(self.score) is not int or self.score < 1 or self.score > 10
        ):
            raise ValueError("评分必须是1-10之间的整数")
        if self.status is None:
            self.status = (
                "scored"
                if self.score is not None
                else ("error" if self.source == "error" else "unknown")
            )
        if self.status not in {
            "scored",
            "unknown",
            "insufficient_evidence",
            "not_applicable",
            "error",
        }:
            raise ValueError("答案状态无效")
        if (self.status == "scored") != (self.score is not None):
            raise ValueError("只有scored状态可以包含1-10分，其他状态必须为空分")

    def __str__(self) -> str:
        """返回答案的字符串表示。"""
        return self.text


@dataclass
class QAResult:
    """表示问答结果。

    Attributes:
        question: 问题对象
        answer: 答案对象
        metadata: 额外的元数据信息
    """

    question: Question
    answer: Answer
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式，用于 JSON 序列化。

        Returns:
            包含问题和答案（评分+描述）的字典
        """
        return {
            str(self.question): {
                "score": self.answer.score,
                "description": self.answer.text,
            }
        }

    def __str__(self) -> str:
        """返回结果的字符串表示。"""
        return f"Q: {self.question}\nA: {self.answer}"


# Q05（LLM-16）有限答案序列化边界：仅白名单字段可进入交换包。
QUICK_SCAN_ANSWER_FIELDS = (
    "question_id",
    "status",
    "score",
    "description",
    "source_urls",
    "published_date",
    "information_as_of",
    "check_level",
    "check_level_receipt_id",
)
QUICK_SCAN_ANSWER_DESCRIPTION_MAX_CHARS = 5000


def serialize_answer_for_exchange(question_id: str, answer: Mapping[str, Any]) -> Dict[str, Any]:
    """有限答案序列化边界（Q05 / LLM-16）。

    仅保留 ``QUICK_SCAN_ANSWER_FIELDS`` 白名单字段：模型输出夹带的权威声称
    字段（source_manifest、formal_profile、"审核通过" 等）在此被丢弃，不会
    成为可信证据或触发跨仓写入；description 限长；答案文本只作为惰性字符串
    透传，不参与任何执行策略或工具决策（LLM-08）。

    Args:
        question_id: 问题标识，作为输出的唯一权威来源
        answer: 原始答案映射（可能夹带伪造字段）

    Returns:
        仅含白名单字段的交换用答案字典
    """
    description = answer.get("description")
    if not isinstance(description, str):
        description = "" if description is None else str(description)
    if len(description) > QUICK_SCAN_ANSWER_DESCRIPTION_MAX_CHARS:
        description = description[:QUICK_SCAN_ANSWER_DESCRIPTION_MAX_CHARS]
    source_urls = answer.get("source_urls")
    if not isinstance(source_urls, list):
        source_urls = []
    source_urls = [url for url in source_urls if isinstance(url, str)]
    check_level = answer.get("check_level")
    if not isinstance(check_level, str) or not check_level:
        check_level = "unverified_model_output"
    return {
        "question_id": question_id,
        "status": answer.get("status"),
        "score": answer.get("score"),
        "description": description,
        "source_urls": source_urls,
        "published_date": answer.get("published_date"),
        "information_as_of": answer.get("information_as_of"),
        "check_level": check_level,
        "check_level_receipt_id": answer.get("check_level_receipt_id"),
    }


@dataclass
class QABatchResult:
    """表示批量问答结果。

    Attributes:
        results: 单个问答结果列表
        total_questions: 总问题数
        processed_count: 已处理的问题数
        created_at: 批次创建时间
    """

    results: list[QAResult] = field(default_factory=list)
    total_questions: int = 0
    processed_count: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def add_result(self, result: QAResult) -> None:
        """添加一个问答结果。

        Args:
            result: 要添加的问答结果
        """
        self.results.append(result)
        self.processed_count += 1

    def to_dict(self) -> Dict[str, str]:
        """转换为字典格式，用于 JSON 序列化。

        Returns:
            包含所有问题-答案对的字典
        """
        combined = {}
        for result in self.results:
            combined.update(result.to_dict())
        return combined

    def to_quick_scan_dict(
        self,
        *,
        entity_id: str,
        company_name: str,
        provider_name: Optional[str],
        requested_model: Optional[str],
    ) -> Dict[str, Any]:
        """Serialize a stable, per-question quick-scan envelope with a separate execution receipt."""
        answers: Dict[str, Dict[str, Any]] = {}
        execution_receipts: Dict[str, Dict[str, Any]] = {}
        transport_providers: set[str] = set()
        transport_models: set[str] = set()
        for result in self.results:
            question_id = result.question.question_id
            if not question_id:
                raise ValueError("quick-scan结果必须带有显式question_id，不能按位置补ID")
            if question_id in answers:
                raise ValueError(f"quick-scan结果包含重复question_id: {question_id}")

            metadata = result.answer.metadata
            source_urls = metadata.get("source_urls", [])
            if not isinstance(source_urls, list):
                source_urls = []
            source_urls = [url for url in source_urls if isinstance(url, str)]
            answers[question_id] = serialize_answer_for_exchange(
                question_id,
                {
                    "status": result.answer.status,
                    "score": result.answer.score,
                    "description": result.answer.text,
                    "source_urls": source_urls,
                    "published_date": None,
                    "information_as_of": None,
                    "check_level": "unverified_model_output",
                    "check_level_receipt_id": None,
                },
            )
            answer_sha256 = hashlib.sha256(
                json.dumps(
                    answers[question_id],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            execution = metadata.get("execution", {})
            execution = execution if isinstance(execution, dict) else {}
            attempts = metadata.get("attempts", [])
            attempts = attempts if isinstance(attempts, list) else []
            final_model = metadata.get("model_requested", requested_model)
            provider_candidates = [execution, metadata, *reversed(attempts)]
            final_provider = next(
                (
                    candidate["provider"]
                    for candidate in provider_candidates
                    if isinstance(candidate, dict)
                    and candidate.get("provider") in {"openai", "minimax", "mimo"}
                    and (
                        candidate.get("response_id")
                        or type(candidate.get("http_status_code")) is int
                    )
                ),
                None,
            )
            credible_attempts = [
                attempt
                for attempt in attempts
                if isinstance(attempt, dict)
                and attempt.get("provider") in {"openai", "minimax", "mimo"}
                and (attempt.get("response_id") or type(attempt.get("http_status_code")) is int)
            ]
            if credible_attempts:
                for attempt in credible_attempts:
                    transport_providers.add(attempt["provider"])
                    attempt_model = attempt.get("requested_model")
                    if not isinstance(attempt_model, str) and attempt["provider"] == final_provider:
                        attempt_model = final_model
                    if isinstance(attempt_model, str) and attempt_model:
                        transport_models.add(attempt_model)
            elif final_provider is not None:
                transport_providers.add(final_provider)
                if isinstance(final_model, str) and final_model:
                    transport_models.add(final_model)
            execution_receipts[question_id] = {
                "answer_sha256": answer_sha256,
                "answered_at": _utc_isoformat(result.answer.created_at),
                "input_question_sha256": hashlib.sha256(
                    result.question.text.encode("utf-8")
                ).hexdigest(),
                "provider": final_provider,
                "requested_model": final_model,
                "actual_model": metadata.get("actual_model"),
                "request_id": metadata.get("request_id"),
                "response_id": metadata.get("response_id"),
                "response_status": execution.get("response_status"),
                "http_status_code": execution.get("http_status_code"),
                "failure_type": metadata.get("failure_type") or execution.get("failure_type"),
                "started_at": execution.get("started_at"),
                "completed_at": execution.get("completed_at"),
                "attempt_id": execution.get("attempt_id"),
                "prompt_sha256": execution.get("prompt_sha256"),
                "search_receipt_id": execution.get("search_receipt_id"),
                "search_status": metadata.get("search_status") or "not_attempted",
                "source_urls": source_urls,
                "web_search_calls": execution.get("web_search_calls", []),
                "attempts": attempts,
                "format_repair": metadata.get("format_repair"),
                "dispatch_outcome": execution.get("dispatch_outcome"),
            }

        completed_at = max(
            _as_utc(value)
            for value in [
                self.created_at,
                *(result.answer.created_at for result in self.results),
            ]
        )
        return {
            "schema_version": "stockqa.quick_scan_result/1.0.0",
            "entity": {"entity_id": entity_id, "name": company_name},
            "observed_at": _utc_isoformat(completed_at),
            "provider": {
                "name": next(iter(transport_providers)) if len(transport_providers) == 1 else None,
                "requested_model": (
                    next(iter(transport_models)) if len(transport_models) == 1 else None
                ),
            },
            "answers": answers,
            "execution_receipts": execution_receipts,
        }

    def is_complete(self) -> bool:
        """检查是否所有问题都已处理。

        Returns:
            如果处理完成返回 True，否则返回 False
        """
        return self.processed_count >= self.total_questions

    def __str__(self) -> str:
        """返回批次结果的字符串表示。"""
        return f"BatchResult: {self.processed_count}/{self.total_questions} processed"
