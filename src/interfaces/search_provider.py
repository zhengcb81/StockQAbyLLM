"""搜索提供者接口。

该模块定义了搜索服务的抽象接口，支持策略模式。
不同的搜索实现可以通过实现此接口来替换。
"""

from abc import ABC, abstractmethod
from typing import List

from src.core.models import SearchResult


class SearchProvider(ABC):
    """搜索提供者抽象基类。

    所有搜索服务实现都应该继承此类并实现 search 方法。
    这允许轻松替换不同的搜索策略（例如：网络搜索、本地搜索、LLM 生成等）。
    """

    @abstractmethod
    def search(self, query: str) -> List[SearchResult]:
        """执行搜索并返回结果。

        Args:
            query: 搜索查询字符串

        Returns:
            搜索结果列表，每个结果是一个 SearchResult 对象

        Raises:
            ProcessingError: 搜索失败时抛出

        Example:
            >>> provider = WebSearchProvider()
            >>> results = provider.search("如何学习Python")
            >>> print(results[0].title)
            "Python 学习指南"
        """
        pass

    @abstractmethod
    def get_provider_name(self) -> str:
        """获取搜索提供者的名称。

        Returns:
            提供者名称（例如：'web_search', 'llm', 'local'）
        """
        pass
