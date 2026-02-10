"""数据模型定义。

该模块定义了 StockQAbyLLM 系统中使用的所有数据模型。
使用 dataclasses 以提供类型安全和默认值。
"""

from dataclasses import dataclass, field
from typing import Dict, Any, Optional
from datetime import datetime


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
        created_at: 结果创建时间
    """

    title: str
    snippet: str
    source: str = "unknown"
    url: Optional[str] = None
    rank: int = 0
    created_at: datetime = field(default_factory=datetime.now)

    def __post_init__(self) -> None:
        """验证搜索结果。"""
        if not self.title or not self.title.strip():
            raise ValueError("搜索结果标题不能为空")
        if not self.snippet or not self.snippet.strip():
            raise ValueError("搜索结果摘要不能为空")

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式。

        Returns:
            包含所有字段的字典
        """
        return {
            "title": self.title,
            "snippet": self.snippet,
            "source": self.source,
            "url": self.url,
            "rank": self.rank,
        }

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

    def __post_init__(self) -> None:
        """验证问题文本。"""
        if not self.text or not self.text.strip():
            raise ValueError("问题文本不能为空")
        # 去除首尾空格
        self.text = self.text.strip()

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
    score: int = 5  # 默认评分
    source: str = "web_search"
    created_at: datetime = field(default_factory=datetime.now)

    def __post_init__(self) -> None:
        """验证答案文本和评分。"""
        if not self.text or not self.text.strip():
            raise ValueError("答案文本不能为空")
        if not isinstance(self.score, int) or self.score < 1 or self.score > 10:
            raise ValueError("评分必须是1-10之间的整数")

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
        return {str(self.question): {"score": self.answer.score, "description": self.answer.text}}

    def __str__(self) -> str:
        """返回结果的字符串表示。"""
        return f"Q: {self.question}\nA: {self.answer}"


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
    created_at: datetime = field(default_factory=datetime.now)

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

    def is_complete(self) -> bool:
        """检查是否所有问题都已处理。

        Returns:
            如果处理完成返回 True，否则返回 False
        """
        return self.processed_count >= self.total_questions

    def __str__(self) -> str:
        """返回批次结果的字符串表示。"""
        return f"BatchResult: {self.processed_count}/{self.total_questions} processed"
