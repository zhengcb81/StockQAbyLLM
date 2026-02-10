"""测试服务模块。

该模块测试 SearchService 和 AnswerGenerator 类。
"""

import pytest

from src.services.search_service import SearchService
from src.services.answer_generator import AnswerGenerator
from src.core.models import Question, Answer, SearchResult
from src.core.exceptions import ProcessingError


class TestSearchService:
    """测试 SearchService 类。"""

    def test_init(self):
        """测试初始化。"""
        service = SearchService()
        assert service.provider_name == "web_search"

    def test_init_with_custom_provider_name(self):
        """测试使用自定义提供者名称初始化。"""
        service = SearchService(provider_name="custom_search")
        assert service.provider_name == "custom_search"

    def test_search_with_valid_query(self):
        """测试有效搜索查询。"""
        service = SearchService()
        results = service.search("Python编程")

        assert isinstance(results, list)
        assert len(results) > 0
        assert isinstance(results[0], SearchResult)
        assert results[0].title is not None
        assert results[0].snippet is not None
        assert results[0].source is not None

    def test_search_with_empty_query_raises_error(self):
        """测试空查询抛出异常。"""
        service = SearchService()

        with pytest.raises(ProcessingError):
            service.search("")

    def test_search_with_whitespace_only_query_raises_error(self):
        """测试纯空格查询抛出异常。"""
        service = SearchService()

        with pytest.raises(ProcessingError):
            service.search("   ")

    def test_get_provider_name(self):
        """测试获取提供者名称。"""
        service = SearchService(provider_name="test_provider")
        assert service.get_provider_name() == "test_provider"


class TestAnswerGenerator:
    """测试 AnswerGenerator 类。"""

    def test_init(self):
        """测试初始化。"""
        generator = AnswerGenerator()
        assert generator is not None

    def test_generate_answer_with_valid_results(self):
        """测试使用有效搜索结果生成答案。"""
        generator = AnswerGenerator()
        question = Question(text="如何学习Python？")

        search_results = [
            SearchResult(
                title="Python学习指南",
                url="https://example.com",
                snippet="Python学习指南摘要",
                source="web_search",
            )
        ]

        answer = generator.generate_answer(question, search_results)

        assert answer.text is not None
        assert answer.source == "web_search"

    def test_generate_answer_with_empty_results(self):
        """测试使用空搜索结果生成答案。"""
        generator = AnswerGenerator()
        question = Question(text="如何学习Python？")

        answer = generator.generate_answer(question, [])

        assert answer.text is not None
        assert "没有找到" in answer.text
        assert answer.source == "no_results"

    def test_generate_batch_answers(self):
        """测试批量生成答案。"""
        generator = AnswerGenerator()

        questions = [Question(text="问题1"), Question(text="问题2"), Question(text="问题3")]

        all_search_results = [
            [SearchResult(title="结果1", snippet="摘要1", source="web")],
            [SearchResult(title="结果2", snippet="摘要2", source="web")],
            [SearchResult(title="结果3", snippet="摘要3", source="web")],
        ]

        answers = generator.generate_batch_answers(questions, all_search_results)

        assert len(answers) == 3
        assert all(isinstance(a, Answer) for a in answers)

    def test_generate_batch_answers_with_mismatched_counts_raises_error(self):
        """测试问题数和结果数不匹配时抛出异常。"""
        generator = AnswerGenerator()

        questions = [Question(text="问题1"), Question(text="问题2")]
        all_search_results = [[SearchResult(title="结果1", snippet="摘要1", source="web")]]

        with pytest.raises(ProcessingError):
            generator.generate_batch_answers(questions, all_search_results)

    def test_generate_batch_answers_continues_on_error(self):
        """测试批量生成时遇到错误继续处理。"""
        generator = AnswerGenerator()

        questions = [Question(text="问题1"), Question(text="问题2"), Question(text="问题3")]

        # 第二个问题的搜索结果标记为 no_results
        all_search_results = [
            [SearchResult(title="结果1", snippet="摘要1", source="web")],
            [SearchResult(title="结果2", snippet="摘要2", source="no_results")],
            [SearchResult(title="结果3", snippet="摘要3", source="web")],
        ]

        answers = generator.generate_batch_answers(questions, all_search_results)

        # 应该返回所有答案，即使某些有问题
        assert len(answers) == 3
