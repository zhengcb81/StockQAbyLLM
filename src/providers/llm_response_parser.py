#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 响应解析器。

负责解析和提取 LLM 返回的 JSON 内容。
"""

import json
import re
from typing import Optional, Tuple, Any, Dict
from src.utils.logger import get_logger
from src.config.settings import DEFAULT_SCORE, SCORE_MIN, SCORE_MAX

logger = get_logger(__name__)


class LLMResponseParser:
    """LLM 响应解析器类。"""

    def __init__(self):
        """初始化解析器。"""
        # 查找JSON代码块的正则表达式
        self.json_patterns = [
            r'\{[^{}]*"score"[^{}]*"description"[^{}]*\}',  # 简单模式
            (
                r'\{(?:[^{}]|\{[^{}]*\})*"score"(?:[^{}]|\{[^{}]*\})*'
                r'"description"(?:[^{}]|\{[^{}]*\})*\}'  # 支持一层嵌套
            ),
            r"```json\s*(\{.*?\})\s*```",  # Markdown 代码块
            r'(\{[^{}]*"score".*?"description"[^{}]*\})',  # 更宽松的匹配
        ]

    def parse_response(self, content: str) -> Optional[Tuple[int, str]]:
        """解析 LLM 响应内容。

        Args:
            content: LLM 响应的原始字符串内容

        Returns:
            (评分, 描述) 元组，如果解析失败则返回 None
        """
        # 尝试直接解析 JSON
        result = self._try_parse_json_direct(content)
        if result:
            return result

        # 如果直接解析失败，尝试从文本中提取 JSON
        logger.warning("无法直接解析JSON，尝试从文本中提取")
        result = self._try_extract_json_with_regex(content)
        return result

    def _try_parse_json_direct(self, content: str) -> Optional[Tuple[int, str]]:
        """尝试直接解析 JSON。"""
        try:
            llm_response = json.loads(content)
            return self._validate_and_extract(llm_response, content, "直接JSON")
        except json.JSONDecodeError:
            return None

    def _try_extract_json_with_regex(self, content: str) -> Optional[Tuple[int, str]]:
        """使用正则表达式提取并解析 JSON。"""
        for pattern in self.json_patterns:
            json_match = re.search(pattern, content, re.DOTALL)
            if json_match:
                try:
                    json_str = json_match.group(1) if "```json" in pattern else json_match.group(0)
                    llm_response = json.loads(json_str)
                    result = self._validate_and_extract(llm_response, content, "正则提取")
                    if result:
                        return result
                except (json.JSONDecodeError, KeyError, ValueError) as e:
                    logger.debug("使用模式解析失败: %s", e)
                    continue
        return None

    def _validate_and_extract(
        self, data: Any, raw_content: str, method: str
    ) -> Optional[Tuple[int, str]]:
        """验证 JSON 数据并提取评分和描述。"""
        if not isinstance(data, dict):
            return None

        if "score" not in data or "description" not in data:
            logger.warning("LLM返回的JSON格式不符合要求: 缺失 score 或 description")
            return None

        try:
            score = int(data.get("score", DEFAULT_SCORE))
            description = str(data.get("description", raw_content))
            # 确保评分在有效范围内
            score = max(SCORE_MIN, min(SCORE_MAX, score))
            logger.info("成功解析LLM响应（%s），评分: %d/10", method, score)
            return (score, description)
        except (ValueError, TypeError) as e:
            logger.warning("解析字段值失败: %s", e)
            return None
