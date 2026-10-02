#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Jev (TypeSafe System One) API 客户端。

Jev 不走 OpenAI 兼容的 /chat/completions 接口，而是走 /v1/systemone：
  - 输入：state（被评估的上下文）+ questions（类型化的决策问题）
  - 输出：answers[id] = {score, probabilities, confidence, legend}

本模块使用 Score 原语对"公司投资维度"打分，并归一化到 0-1，
以便和 LLM 给出的 1-10 分直接比较。

参考文档：
  - https://docs.typesafe.ai/introduction/quickstart
  - https://docs.typesafe.ai/primitives/score
"""

import os
import time
from typing import Any, Dict, List, Optional

import requests

TYPESAFE_API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"


class JevError(Exception):
    """Jev API 调用异常。"""


class JevClient:
    """TypeSafe Jev 客户端。

    仅实现本实验需要的 Score 原语调用。
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        timeout: int = 120,
        max_retries: int = 3,
    ):
        """初始化 Jev 客户端。

        Args:
            api_key: TypeSafe API key，缺省从环境变量 TYPESAFE_API_KEY 读取
            model: 模型 ID，jev-latest（稳定版）或 jev-preview
            timeout: 请求超时秒数
            max_retries: 最大重试次数
        """
        self.api_key = api_key or os.getenv("TYPESAFE_API_KEY", "")
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries

    def is_configured(self) -> bool:
        """检查 API key 是否已配置。"""
        return bool(self.api_key)

    def build_score_question(
        self,
        question: str,
        low_anchor: str,
        high_anchor: str,
        levels: int = 10,
    ) -> Dict[str, Any]:
        """构造一个 Score 类型的 Jev 问题。

        Jev 的 Score 要求 criteria 是"有序的描述性等级"（2-10 档），
        且官方明确警告：纯数字等级会失败；必须用描述性文字。

        这里把原题的"1 = 低锚点；10 = 高锚点"展开成 levels 档描述性量表，
        使 Jev 的 10 档与原 LLM 的 1-10 分一一对应。

        ★ 注意：instructions 使用**原题文本原样**（含原题的评分提示），
          不做任何改写；criteria 的两端锚点也直接取自原题。

        Args:
            question: 原题完整文本（来自 config.json，含评分提示）
            low_anchor: 原题的 1 分锚点描述
            high_anchor: 原题的 10 分锚点描述
            levels: 档位数量（2-10，默认 10）

        Returns:
            Jev questions 字典中的一个条目
        """
        levels = max(2, min(10, levels))
        # 用区分度明确的程度词构建量表，避免相邻档位措辞雷同。
        # 官方文档警告：等级必须能清晰互相区分，否则 Jev 会落在相邻档位之间。
        descriptors = self._level_descriptors(levels)

        criteria: List[str] = []
        for i, desc in enumerate(descriptors):
            if i == 0:
                criteria.append(f"{desc}。具体表现为：{low_anchor}")
            elif i == levels - 1:
                criteria.append(f"{desc}。具体表现为：{high_anchor}")
            else:
                criteria.append(
                    f"{desc}。处于「{low_anchor}」与「{high_anchor}」之间的"
                    f"{self._band_label(i, levels)}水平"
                )

        return {
            "type": "score",
            "instructions": question,
            "criteria": criteria,
        }

    @staticmethod
    def _level_descriptors(levels: int) -> List[str]:
        """返回从差到好的程度词序列（长度为 levels）。

        分档逻辑：底部 2 档用"极差/较差"，顶部 2 档用"优秀/极优"，
        中间按段位给出可区分的程度描述。
        """
        if levels == 2:
            return ["明显偏差", "明显优秀"]

        full = [
            "明显偏差，几乎不具备该优势",
            "较差，显著弱于同行业平均水平",
            "偏弱，低于同行业平均水平",
            "略低于平均水平，存在明显短板",
            "中等偏下，勉强达到行业平均水平",
            "中等偏上，略优于行业平均水平",
            "良好，明显优于行业平均水平",
            "优秀，处于行业领先梯队",
            "非常优秀，接近行业顶尖水平",
            "卓越，行业标杆级别",
        ]
        if levels == 10:
            return full

        # 非 10 档时按比例采样，保证首尾固定、中间均匀
        indices = [round(i * 9 / (levels - 1)) for i in range(levels)]
        return [full[min(i, 9)] for i in indices]

    @staticmethod
    def _band_label(index: int, levels: int) -> str:
        """给出中间档位的区间标签。"""
        ratio = index / (levels - 1)
        if ratio < 0.35:
            return "中等偏下"
        if ratio < 0.65:
            return "中等"
        return "中等偏上"

    def score(
        self,
        state: str,
        question: str,
        low_anchor: str,
        high_anchor: str,
        levels: int = 10,
    ) -> Dict[str, Any]:
        """用 Jev 的 Score 原语对给定 state 打分。

        Args:
            state: 被评估的上下文（这里是公司名称 + 提问背景）
            question: 问题文本
            low_anchor: 最低分锚点
            high_anchor: 最高分锚点
            levels: 档位数量

        Returns:
            {
                "raw_score": float,        # Jev 原始分（0 ~ levels-1）
                "normalized": float,       # 归一化到 0-1
                "confidence": float,       # 0-1
                "probabilities": dict,     # 各档位概率
                "levels": int,
            }

        Raises:
            JevError: API 未配置或调用失败
        """
        if not self.is_configured():
            raise JevError(
                "TypeSafe API key 未配置。请设置环境变量 TYPESAFE_API_KEY，"
                "或在调用时传入 api_key 参数。申请地址: https://console.typesafe.ai/keys"
            )

        payload = {
            "state": state,
            "model": self.model,
            "questions": {
                "target": self.build_score_question(question, low_anchor, high_anchor, levels)
            },
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                response = requests.post(
                    TYPESAFE_API_URL,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data = response.json()
                return self._parse_score_response(data, levels)

            except requests.RequestException as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    wait = 2**attempt
                    time.sleep(wait)

        raise JevError(f"Jev API 请求失败（已重试 {self.max_retries} 次）: {last_error}")

    def _parse_score_response(self, data: Dict[str, Any], levels: int) -> Dict[str, Any]:
        """解析 Jev 的 Score 响应。"""
        answers = data.get("answers", {})
        answer = answers.get("target")

        if not answer:
            raise JevError(f"Jev 响应中缺少 target 答案: {data}")

        raw_score = answer.get("score")
        if raw_score is None:
            raise JevError(f"Jev 响应中缺少 score 字段: {answer}")

        max_level = levels - 1
        normalized = float(raw_score) / max_level if max_level > 0 else 0.0

        return {
            "raw_score": float(raw_score),
            "normalized": round(normalized, 4),
            "confidence": answer.get("confidence"),
            "probabilities": answer.get("probabilities", {}),
            "levels": levels,
            "usage": data.get("usage", {}),
        }
