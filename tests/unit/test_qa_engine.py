"""测试 QAEngine。

该模块测试 QAEngine 类的功能。
"""

from unittest.mock import MagicMock, Mock

import pytest

from src.core.exceptions import ProcessingError, ValidationError
from src.core.models import Answer, Question, SearchResult
from src.core.qa_engine import QAEngine
from src.interfaces.search_provider import SearchProvider
from src.services.answer_generator import AnswerGenerator
from tests.helpers.mocks import (
    FailingSearchProvider,
    MockSearchProvider,
    PartialFailingSearchProvider,
)


class TestQAEngine:
    """测试 QAEngine 类。"""

    def test_init(self):
        """测试初始化。"""
        search_provider = MockSearchProvider()
        answer_generator = AnswerGenerator()
        engine = QAEngine(search_provider, answer_generator)

        assert engine.search_provider == search_provider
        assert engine.answer_generator == answer_generator

    def test_init_with_default_answer_generator(self):
        """测试使用默认答案生成器初始化。"""
        search_provider = MockSearchProvider()
        engine = QAEngine(search_provider)

        assert engine.answer_generator is not None
        assert isinstance(engine.answer_generator, AnswerGenerator)

    def test_process_single_question(self):
        """测试处理单个问题。"""
        search_provider = MockSearchProvider()
        answer_generator = AnswerGenerator()
        engine = QAEngine(search_provider, answer_generator)

        result = engine.process_question("测试问题")

        assert result.question.text == "测试问题"
        assert result.answer.text is not None
        assert result.answer.source == "mock"

    def test_process_empty_question_raises_error(self):
        """测试处理空问题抛出异常。"""
        search_provider = MockSearchProvider()
        answer_generator = AnswerGenerator()
        engine = QAEngine(search_provider, answer_generator)

        with pytest.raises(ValidationError):
            engine.process_question("")

    def test_process_whitespace_only_question_raises_error(self):
        """测试处理纯空格问题抛出异常。"""
        search_provider = MockSearchProvider()
        answer_generator = AnswerGenerator()
        engine = QAEngine(search_provider, answer_generator)

        with pytest.raises(ValidationError):
            engine.process_question("   ")

    def test_process_questions_batch(self):
        """测试批量处理问题。"""
        search_provider = MockSearchProvider()
        answer_generator = AnswerGenerator()
        engine = QAEngine(search_provider, answer_generator)

        questions = ["问题1", "问题2", "问题3"]
        batch_result = engine.process_questions(questions)

        assert batch_result.total_questions == 3
        assert batch_result.processed_count == 3
        assert batch_result.is_complete()
        assert len(batch_result.results) == 3

    def test_process_empty_questions_list_raises_error(self):
        """测试处理空问题列表抛出异常。"""
        search_provider = MockSearchProvider()
        answer_generator = AnswerGenerator()
        engine = QAEngine(search_provider, answer_generator)

        with pytest.raises(ValidationError):
            engine.process_questions([])

    def test_process_questions_with_search_failure(self):
        """测试搜索失败时的处理。"""
        search_provider = FailingSearchProvider()
        answer_generator = AnswerGenerator()
        engine = QAEngine(search_provider, answer_generator)

        questions = ["问题1", "问题2"]
        batch_result = engine.process_questions(questions)

        # 应该继续处理，即使某些问题失败
        assert batch_result.processed_count == 2
        # 至少有一个应该有错误标记
        error_count = sum(1 for r in batch_result.results if r.metadata.get("error"))
        assert error_count > 0

    def test_get_statistics(self):
        """测试获取统计信息。"""
        search_provider = MockSearchProvider()
        answer_generator = AnswerGenerator()
        engine = QAEngine(search_provider, answer_generator)

        questions = ["问题1", "问题2", "问题3"]
        batch_result = engine.process_questions(questions)

        stats = engine.get_statistics(batch_result)

        assert stats["total_questions"] == 3
        assert stats["processed_count"] == 3
        assert stats["success_count"] == 3
        assert stats["error_count"] == 0
        assert stats["is_complete"] is True
        assert "created_at" in stats

    def test_get_statistics_with_errors(self):
        """测试包含错误的统计信息。"""
        search_provider = PartialFailingSearchProvider(failing_queries=["问题2"])
        answer_generator = AnswerGenerator()
        engine = QAEngine(search_provider, answer_generator)

        questions = ["问题1", "问题2", "问题3"]
        batch_result = engine.process_questions(questions)

        stats = engine.get_statistics(batch_result)

        assert stats["total_questions"] == 3
        assert stats["processed_count"] == 3
        assert stats["error_count"] == 1
        assert stats["success_count"] == 2
