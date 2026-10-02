#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 侧打分调用。

复用项目现有的 src.providers.llm_client.LLMClient，用同一个问题
向各家 LLM 提问，只提取 score 并归一化到 0-1。

与 Jev 侧保持一致：state 相同、问题相同、1-10 分尺度。
"""

import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

# 把项目根目录加入路径，以便复用 src 下的 LLMClient
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.providers.llm_client import LLMClient  # noqa: E402


class LLMScoreError(Exception):
    """LLM 打分异常。"""


def build_llm_prompt(company: str, question_raw: str) -> str:
    """构造与 Jev 语义完全等价的 LLM 提示词。

    ★ 问题部分使用**原题文本原样**（来自 config.json，含原题的
      "评分提示：1 = ...；10 = ..."），不做任何改写或补写锚点，
      以保证与 Jev 侧的提问内容一致。

    Args:
        company: 公司名称
        question_raw: 原题完整文本（含评分提示）

    Returns:
        完整提示词
    """
    return f"""你是一位专业的投资分析师。请对以下问题进行深入分析并给出评分。

关于公司：{company}

**问题**：
{question_raw}

**要求**：
1. 基于该公司的实际情况进行分析
2. 按照上述评分提示给出一个 1-10 分的评分（1=最差，10=最好）
3. **重要**：请严格按以下JSON格式返回：
{{
  "score": <评分数字，1-10之间的整数>,
  "description": "<简要分析>"
}}

请返回JSON格式的回答："""


def parse_score_from_text(text: str) -> Optional[int]:
    """从 LLM 返回文本中解析 score。

    容错处理：优先解析 JSON，失败则用正则兜底。

    Args:
        text: LLM 原始返回文本

    Returns:
        1-10 的整数分值，解析失败返回 None
    """
    if not text:
        return None

    # 尝试直接解析 JSON
    try:
        data = json.loads(text.strip())
        if isinstance(data, dict) and "score" in data:
            return _coerce_score(data["score"])
    except (json.JSONDecodeError, ValueError):
        pass

    # 尝试从 Markdown 代码块中提取 JSON
    json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(1))
            if isinstance(data, dict) and "score" in data:
                return _coerce_score(data["score"])
        except (json.JSONDecodeError, ValueError):
            pass

    # 尝试提取最外层的 {...}
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        try:
            data = json.loads(brace_match.group(0))
            if isinstance(data, dict) and "score" in data:
                return _coerce_score(data["score"])
        except (json.JSONDecodeError, ValueError):
            pass

    # 兜底：找形如 "score": 8 的模式
    score_match = re.search(r'"?score"?\s*[:：]\s*(\d+(?:\.\d+)?)', text, re.IGNORECASE)
    if score_match:
        return _coerce_score(score_match.group(1))

    return None


def _coerce_score(value: Any) -> Optional[int]:
    """把解析出的分值规整到 1-10 整数区间。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return max(1, min(10, int(round(num))))


def normalize_llm_score(score: int) -> float:
    """把 LLM 的 1-10 分归一化到 0-1。

    与 Jev 侧对齐：Jev 的 10 档 criteria 中，档位 0 对应最差、档位 9 对应最好，
    归一化公式为 raw/9。这里 LLM 的 1 分对应 0，10 分对应 1，
    归一化公式为 (score-1)/9。

    Args:
        score: 1-10 的整数分

    Returns:
        0-1 的归一化分数
    """
    return round((score - 1) / 9, 4)


def call_llm_provider(
    provider_name: str,
    provider_config: Dict[str, Any],
    company: str,
    question_raw: str,
) -> Dict[str, Any]:
    """调用单个 LLM 提供商打分。

    Args:
        provider_name: 提供商名称（deepseek/minimax/glm/mimo）
        provider_config: llm_apis.json 中的该提供商配置
        company: 公司名称
        question_raw: 原题完整文本（含评分提示）

    Returns:
        {
            "provider": str,
            "raw_score": int or None,
            "normalized": float or None,
            "error": str or None,
            "elapsed": float,
        }
    """
    result: Dict[str, Any] = {
        "provider": provider_name,
        "raw_score": None,
        "normalized": None,
        "error": None,
        "elapsed": 0.0,
    }

    api_key = provider_config.get("api_key", "")
    base_url = provider_config.get("base_url", "")
    model = provider_config.get("model", "")
    timeout = provider_config.get("timeout", 60)

    if not api_key:
        result["error"] = "API key 未配置"
        return result

    try:
        client = LLMClient(
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout=timeout,
        )
        prompt = build_llm_prompt(company, question_raw)

        start = time.time()
        content = client.send_request(prompt)
        result["elapsed"] = round(time.time() - start, 2)

        score = parse_score_from_text(content)
        if score is None:
            result["error"] = "返回中未能解析出 score"
            return result

        result["raw_score"] = score
        result["normalized"] = normalize_llm_score(score)

    except Exception as e:  # noqa: BLE001 - 实验脚本需容错，单点失败不应中断整体
        result["error"] = f"{type(e).__name__}: {e}"

    return result
