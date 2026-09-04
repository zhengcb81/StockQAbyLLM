"""测试网络搜索服务。

该模块测试 SearchService 类的异常处理。
"""

from unittest.mock import patch

import pytest

from src.core.exceptions import ProcessingError
from src.services.search_service import SearchService


class TestSearchService:
    """测试 SearchService 类。"""

    def test_search_returns_placeholder_result(self):
        """测试搜索返回占位符结果。"""
        service = SearchService()
        results = service.search("测试查询")

        assert len(results) == 1
        assert results[0].source == "web_search"

    @patch("src.services.search_service.SearchResult")
    def test_search_connection_error(self, mock_result):
        """测试连接错误被包装为ProcessingError。"""
        mock_result.side_effect = ConnectionError("network down")
        service = SearchService()

        with pytest.raises(ProcessingError, match="搜索执行失败"):
            service.search("测试查询")

    @patch("src.services.search_service.SearchResult")
    def test_search_processing_error_reraised(self, mock_result):
        """测试ProcessingError原样传播。"""
        mock_result.side_effect = ProcessingError(message="处理失败", question="测试查询")
        service = SearchService()

        with pytest.raises(ProcessingError, match="处理失败"):
            service.search("测试查询")
