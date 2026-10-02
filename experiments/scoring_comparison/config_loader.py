#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 config.json 加载原始题目。

关键原则：**题目一律从 config.json 读取，不复制、不改写**。

config.json 的题目格式为：
    "<题干>评分提示：1 = <锚点A>；10 = <锚点B>。"
    "<题干>（<补充说明>）（评分提示：1 = <锚点A>；10 = <锚点B>。）"

本模块负责：
  1. 按 category + index 定位原题
  2. 分离出「题干」与「1分/10分锚点」
  3. 把原题文本原样提供给两侧（Jev 和 LLM），保证提问内容一致
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_JSON = PROJECT_ROOT / "config.json"

# 匹配 "评分提示：1 = ...；10 = ..." 或 "（评分提示：1 = ...；10 = ...。）"
_ANCHOR_PATTERN = re.compile(
    r"[（(]?\s*评分提示\s*[:：]\s*"
    r"1\s*=\s*(?P<low>.+?)\s*[；;]\s*"
    r"10\s*=\s*(?P<high>.+?)\s*[。.）)]?\s*$"
)


class ConfigQuestion:
    """config.json 中的一道原始题目。"""

    def __init__(self, category: str, index: int, raw_text: str, weight: str = ""):
        self.category = category
        self.index = index
        self.raw_text = raw_text.strip()
        self.weight = weight

        match = _ANCHOR_PATTERN.search(self.raw_text)
        if match:
            self.low_anchor = match.group("low").strip()
            self.high_anchor = match.group("high").strip()
            # 题干 = 去掉评分提示部分后的剩余文本
            self.stem = self.raw_text[: match.start()].strip()
            # 清理题干尾部可能残留的括号或标点
            self.stem = re.sub(r"[（(]\s*$", "", self.stem).strip()
            self.has_anchors = True
        else:
            self.low_anchor = ""
            self.high_anchor = ""
            self.stem = self.raw_text
            self.has_anchors = False

    @property
    def key(self) -> str:
        """稳定标识，用于结果索引。"""
        slug = re.sub(r"[^\w]+", "_", self.category).strip("_")
        return f"{slug}_{self.index}"

    @property
    def label(self) -> str:
        """简短标签，用于图表。"""
        return f"{self.category}#{self.index + 1}"

    def __repr__(self) -> str:
        return f"ConfigQuestion(category={self.category!r}, index={self.index})"


def load_config_questions(config_path: Optional[Path] = None) -> List[ConfigQuestion]:
    """加载 config.json 的全部题目。

    Args:
        config_path: config.json 路径

    Returns:
        ConfigQuestion 列表，顺序与文件一致
    """
    path = Path(config_path) if config_path else DEFAULT_CONFIG_JSON
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    questions: List[ConfigQuestion] = []
    for category_block in data:
        category = category_block.get("category", "")
        weight = category_block.get("weight", "")
        for i, q_text in enumerate(category_block.get("questions", [])):
            questions.append(ConfigQuestion(category, i, q_text, weight))
    return questions


def resolve_questions(
    refs: List[Dict[str, Any]],
    all_questions: Optional[List[ConfigQuestion]] = None,
    config_path: Optional[Path] = None,
) -> List[ConfigQuestion]:
    """按 {category, index} 引用解析出题目。

    Args:
        refs: 引用列表，如 [{"category": "市场与增长潜力", "index": 0}]
        all_questions: 已加载的题目池，缺省则从 config.json 加载
        config_path: config.json 路径

    Returns:
        解析后的题目列表

    Raises:
        ValueError: 引用无法匹配到题目
    """
    pool = all_questions if all_questions is not None else load_config_questions(config_path)

    resolved: List[ConfigQuestion] = []
    for ref in refs:
        category = ref.get("category")
        index = ref.get("index")
        if category is None or index is None:
            raise ValueError(f"题目引用缺少 category 或 index: {ref}")

        matched = [q for q in pool if q.category == category and q.index == index]
        if not matched:
            available = sorted({q.category for q in pool})
            raise ValueError(
                f"无法在 config.json 中找到题目: category={category!r}, index={index}。"
                f"可用的 category: {available}"
            )
        resolved.append(matched[0])

    return resolved
