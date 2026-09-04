"""测试 Mock 对象。

该模块提供用于测试的 Mock 对象和辅助函数。
"""

from typing import Any, Dict, List, Optional
from unittest.mock import Mock

from src.core.models import Answer, QABatchResult, QAResult, Question, SearchResult
from src.interfaces.search_provider import SearchProvider


class MockLLMProvider:
    """模拟 LLM 提供者。"""

    @staticmethod
    def create(
        api_key: str = "test_key",
        model: str = "test_model",
        provider_name: str = "test_provider",
        company_name: str = "test_company",
    ) -> Mock:
        """创建一个模拟的 LLMProvider 对象。

        Args:
            api_key: API 密钥
            model: 模型名称
            provider_name: 提供者名称
            company_name: 公司名称

        Returns:
            Mock 对象
        """
        mock = Mock()
        mock.api_key = api_key
        mock.model = model
        mock.provider_name = provider_name
        mock.company_name = company_name
        mock.get_provider_name = Mock(return_value=provider_name)
        return mock

    @staticmethod
    def create_with_search_results(query: str, results: List[Dict[str, Any]]) -> Mock:
        """创建一个带有搜索结果的模拟 LLMProvider。

        Args:
            query: 搜索查询
            results: 搜索结果列表

        Returns:
            Mock 对象
        """
        mock = MockLLMProvider.create()
        mock.search = Mock(return_value=results)
        return mock


class MockQAEngine:
    """模拟 QA 引擎。"""

    @staticmethod
    def create(
        results: Optional[List[QAResult]] = None,
        statistics: Optional[Dict[str, Any]] = None,
    ) -> Mock:
        """创建一个模拟的 QAEngine 对象。

        Args:
            results: 处理结果列表
            statistics: 统计信息

        Returns:
            Mock 对象
        """
        mock = Mock()

        # 默认统计信息
        default_stats = {
            "total_questions": len(results) if results else 0,
            "success_count": len(results) if results else 0,
            "error_count": 0,
            "processed_count": len(results) if results else 0,
            "is_complete": True,
            "created_at": "2026-01-01T00:00:00",
        }

        mock.get_statistics = Mock(return_value=statistics or default_stats)

        if results:
            batch = QABatchResult(total_questions=len(results))
            for result in results:
                batch.add_result(result)
            mock.process_questions = Mock(return_value=batch)
        else:
            mock.process_questions = Mock(return_value=QABatchResult(total_questions=0))

        return mock


class MockSearchProvider(SearchProvider):
    """模拟搜索提供者，实现SearchProvider接口。

    用于测试QAEngine和其他依赖SearchProvider的组件。
    """

    def __init__(
        self,
        mock_results: Optional[List[SearchResult]] = None,
        provider_name: str = "mock_search",
    ):
        """初始化模拟搜索提供者。

        Args:
            mock_results: 要返回的模拟结果列表
            provider_name: 提供者名称
        """
        self.mock_results = mock_results or [
            SearchResult(
                title="测试结果",
                url="https://test.com",
                snippet="测试摘要",
                source="mock",
            )
        ]
        self._provider_name = provider_name

    def search(self, query: str) -> List[SearchResult]:
        """返回模拟搜索结果。"""
        return self.mock_results

    def get_provider_name(self) -> str:
        """返回提供者名称。"""
        return self._provider_name


class FailingSearchProvider(SearchProvider):
    """失败的搜索提供者，用于测试错误处理。"""

    def __init__(self, error_message: str = "搜索失败"):
        """初始化失败的搜索提供者。

        Args:
            error_message: 错误消息
        """
        self.error_message = error_message

    def search(self, query: str) -> List[SearchResult]:
        """抛出ProcessingError。"""
        from src.core.exceptions import ProcessingError

        raise ProcessingError(self.error_message)

    def get_provider_name(self) -> str:
        """返回提供者名称。"""
        return "failing_search"


class PartialFailingSearchProvider(SearchProvider):
    """部分失败的搜索提供者，用于测试部分错误处理。

    只对特定查询失败，其他查询返回正常结果。
    """

    def __init__(
        self,
        failing_queries: Optional[List[str]] = None,
        error_message: str = "搜索失败",
    ):
        """初始化部分失败的搜索提供者。

        Args:
            failing_queries: 应该失败的查询列表
            error_message: 错误消息
        """
        self.failing_queries = failing_queries or []
        self.error_message = error_message

    def search(self, query: str) -> List[SearchResult]:
        """对特定查询抛出ProcessingError，其他返回正常结果。"""
        from src.core.exceptions import ProcessingError

        if query in self.failing_queries:
            raise ProcessingError(self.error_message)

        return [SearchResult(title="测试结果", snippet="测试摘要", source="mock")]

    def get_provider_name(self) -> str:
        """返回提供者名称。"""
        return "partial_failing_search"


def create_mock_config_content(
    single_category: bool = True, with_extra_fields: bool = False
) -> Dict[str, Any]:
    """创建模拟的配置文件内容。

    Args:
        single_category: 是否使用单个 category 格式
        with_extra_fields: 是否包含额外字段

    Returns:
        配置字典
    """
    if single_category:
        config = {
            "category": "投资逻辑",
            "questions": ["公司的核心竞争优势是什么？", "公司的财务状况如何？"],
        }
    else:
        config = {
            "categories": [
                {"category": "投资逻辑", "questions": ["公司的核心竞争优势是什么？"]},
                {"category": "财务状况", "questions": ["公司的财务状况如何？"]},
            ]
        }

    if with_extra_fields:
        config["version"] = "1.0"
        config["created_at"] = "2026-01-01"

    return config


def create_mock_llm_response(score: int = 7, description: str = "测试描述") -> Dict[str, Any]:
    """创建模拟的 LLM API 响应。

    Args:
        score: 评分
        description: 描述

    Returns:
        响应字典
    """
    return {"score": score, "description": description}


def create_mock_search_results(
    titles: List[str],
    urls: Optional[List[str]] = None,
    snippets: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """创建模拟的搜索结果。

    Args:
        titles: 标题列表
        urls: URL列表（可选）
        snippets: 摘要列表（可选）

    Returns:
        搜索结果列表
    """
    results = []
    for i, title in enumerate(titles):
        result = {
            "title": title,
            "url": urls[i] if urls else f"https://example.com/{i}",
            "snippet": snippets[i] if snippets else f"摘要{i}",
        }
        results.append(result)
    return results
