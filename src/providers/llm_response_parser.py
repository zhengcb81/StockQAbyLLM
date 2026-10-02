#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 响应解析器。

负责解析和提取 LLM 返回的 JSON 内容。
"""

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from src.utils.logger import get_logger

logger = get_logger(__name__)


class _DuplicateJSONKeyError(ValueError):
    """Raised when a response object contains an ambiguous repeated key."""


def _reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKeyError("重复JSON字段")
        result[key] = value
    return result


def _nested_answer_conflicts_with_outer(
    description: str, outer: Dict[str, Any], expected_question_id: Optional[str]
) -> bool:
    """Reject conflicting answer layers while preserving consistent S02 payloads."""
    candidate_text = description.strip()
    if not (candidate_text.startswith("{") and candidate_text.endswith("}")):
        return False
    try:
        candidate = json.loads(candidate_text, object_pairs_hook=_reject_duplicate_json_keys)
    except _DuplicateJSONKeyError:
        return True
    except json.JSONDecodeError:
        return False
    if not isinstance(candidate, dict):
        return False

    has_identity = bool({"id", "question_id"}.intersection(candidate))
    if not has_identity or not ({"status", "score"}.intersection(candidate)):
        return False

    if "status" in candidate and candidate["status"] != outer.get("status", "scored"):
        return True
    if "score" in candidate:
        outer_score = outer.get("score")
        if type(candidate["score"]) is not type(outer_score) or candidate["score"] != outer_score:
            return True

    nested_ids = [candidate[key] for key in ("id", "question_id") if key in candidate]
    if any(not isinstance(value, str) or not value.strip() for value in nested_ids):
        return True
    if len(set(nested_ids)) > 1:
        return True
    outer_question_id = outer.get("question_id") or expected_question_id
    return bool(nested_ids and outer_question_id is not None and nested_ids[0] != outer_question_id)


def _iter_top_level_json_objects(content: str):
    """Yield balanced outer JSON objects without treating nested metadata as answers."""
    start = None
    depth = 0
    in_string = False
    escaped = False

    for index, character in enumerate(content):
        if start is None:
            if character == "{":
                start = index
                depth = 1
                in_string = False
                escaped = False
            continue

        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue

        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                yield content[start : index + 1]
                start = None


@dataclass(frozen=True)
class ParsedLLMAnswer:
    """经过严格校验的模型回答；非打分状态的score始终为空。"""

    score: Optional[int]
    description: str
    status: str
    question_id: Optional[str] = None
    entity_id: Optional[str] = None
    company_name: Optional[str] = None


class LLMResponseParser:
    """LLM 响应解析器类。"""

    def parse_response(self, content: str) -> Optional[Tuple[Optional[int], str]]:
        """解析 LLM 响应内容。

        Args:
            content: LLM 响应的原始字符串内容

        Returns:
            (评分, 描述) 元组，如果解析失败则返回 None
        """
        result = self.parse_structured_response(content)
        if result is None:
            return None
        return result.score, result.description

    def parse_structured_response(
        self,
        content: str,
        expected_question_id: Optional[str] = None,
        expected_entity_id: Optional[str] = None,
        expected_company_name: Optional[str] = None,
        strict_json_only: bool = False,
    ) -> Optional[ParsedLLMAnswer]:
        """解析结构化答案；quick-scan要求完整外层JSON与精确实体绑定。

        strict_json_only 语义（Q02/Q03 联合批次）：真实厂商（如 MiniMax 搜索流）
        会在 JSON 前输出前导分析文本。严格模式因此要求内容**恰好包含一个**完整
        顶层 JSON 对象，且必须通过全部身份/评分绑定校验；零个或多个对象一律
        fail-closed 拒绝。Q03 的硬化性质全部保留：重复键拒绝、嵌套/外层冲突
        拒绝、题义/实体/公司精确绑定。非严格路径保持既有首个有效对象兼容提取，
        并同样传递实体/公司绑定。
        """
        try:
            result = self._try_parse_json_direct(
                content,
                expected_question_id,
                expected_entity_id,
                expected_company_name,
            )
        except _DuplicateJSONKeyError:
            logger.warning("LLM返回的JSON包含重复字段；停止兼容提取")
            return None
        if result is not None:
            return result

        candidates = list(_iter_top_level_json_objects(content))
        if strict_json_only:
            if len(candidates) != 1:
                logger.warning(
                    "严格模式要求内容恰好包含一个完整外层JSON对象（发现 %d 个）",
                    len(candidates),
                )
                return None
            try:
                llm_response = json.loads(
                    candidates[0], object_pairs_hook=_reject_duplicate_json_keys
                )
            except _DuplicateJSONKeyError:
                logger.warning("严格提取候选包含重复JSON字段；拒绝回答")
                return None
            except json.JSONDecodeError:
                return None
            return self._validate_and_extract(
                llm_response,
                content,
                "严格提取",
                expected_question_id,
                expected_entity_id,
                expected_company_name,
            )

        logger.warning("无法直接解析JSON，尝试从文本中提取")
        return self._try_extract_json_with_regex(
            content, expected_question_id, expected_entity_id, expected_company_name
        )

    def _try_parse_json_direct(
        self,
        content: str,
        expected_question_id: Optional[str] = None,
        expected_entity_id: Optional[str] = None,
        expected_company_name: Optional[str] = None,
    ) -> Optional[ParsedLLMAnswer]:
        """尝试直接解析 JSON。"""
        try:
            llm_response = json.loads(content, object_pairs_hook=_reject_duplicate_json_keys)
            return self._validate_and_extract(
                llm_response,
                content,
                "直接JSON",
                expected_question_id,
                expected_entity_id,
                expected_company_name,
            )
        except json.JSONDecodeError:
            return None

    def _try_extract_json_with_regex(
        self,
        content: str,
        expected_question_id: Optional[str] = None,
        expected_entity_id: Optional[str] = None,
        expected_company_name: Optional[str] = None,
    ) -> Optional[ParsedLLMAnswer]:
        """从普通文本提取完整外层 JSON 对象以保持旧CLI兼容。"""
        for json_str in _iter_top_level_json_objects(content):
            try:
                llm_response = json.loads(json_str, object_pairs_hook=_reject_duplicate_json_keys)
            except _DuplicateJSONKeyError:
                logger.warning("提取候选包含重复JSON字段；停止兼容提取")
                return None
            except json.JSONDecodeError as e:
                logger.debug("跳过无效JSON候选: %s", e)
                continue

            result = self._validate_and_extract(
                llm_response,
                content,
                "兼容提取",
                expected_question_id,
                expected_entity_id,
                expected_company_name,
            )
            if result is not None:
                return result
        return None

    def _validate_and_extract(
        self,
        data: Any,
        raw_content: str,
        method: str,
        expected_question_id: Optional[str] = None,
        expected_entity_id: Optional[str] = None,
        expected_company_name: Optional[str] = None,
    ) -> Optional[ParsedLLMAnswer]:
        """验证 JSON 数据并提取评分和描述。"""
        if not isinstance(data, dict):
            return None

        if "score" not in data or "description" not in data:
            logger.warning("LLM返回的JSON格式不符合要求: 缺失 score 或 description")
            return None

        description = data.get("description")
        if not isinstance(description, str) or not description.strip():
            logger.warning("LLM返回的description必须是非空字符串")
            return None
        if _nested_answer_conflicts_with_outer(description, data, expected_question_id):
            logger.warning("LLM返回的description与外层答案状态、评分或题目ID冲突")
            return None

        status = data.get("status", "scored")
        score = data.get("score")
        if not isinstance(status, str) or status not in {
            "scored",
            "unknown",
            "insufficient_evidence",
            "not_applicable",
        }:
            logger.warning("LLM返回了无效的答案状态")
            return None
        if status == "scored":
            if type(score) is not int or not 1 <= score <= 10:
                logger.warning("LLM返回的评分必须是1-10之间的严格整数")
                return None
        elif score is not None:
            logger.warning("非打分状态的score必须为null")
            return None

        question_id = data.get("question_id")
        if expected_question_id is not None:
            if not isinstance(question_id, str) or question_id != expected_question_id:
                logger.warning("LLM返回的问题ID与当前待办ID不一致")
                return None
        elif question_id is not None and not isinstance(question_id, str):
            logger.warning("LLM返回的问题ID格式无效")
            return None

        entity_id = data.get("entity_id")
        if expected_entity_id is not None:
            if not isinstance(entity_id, str) or entity_id != expected_entity_id:
                logger.warning("LLM返回的实体ID与当前目标公司不一致")
                return None
        elif entity_id is not None and not isinstance(entity_id, str):
            logger.warning("LLM返回的实体ID格式无效")
            return None

        company_name = data.get("company_name")
        if expected_company_name is not None:
            if not isinstance(company_name, str) or company_name.strip() != expected_company_name:
                logger.warning("LLM返回的公司名称与当前目标公司不一致")
                return None
        elif company_name is not None and not isinstance(company_name, str):
            logger.warning("LLM返回的公司名称格式无效")
            return None

        if status == "scored":
            logger.info("成功解析LLM响应（%s），评分: %d/10", method, score)
        return ParsedLLMAnswer(
            score=score,
            description=description.strip(),
            status=status,
            question_id=question_id,
            entity_id=entity_id,
            company_name=company_name,
        )
