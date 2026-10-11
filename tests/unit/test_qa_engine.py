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

    def test_process_identified_question_uses_provider_question_id_path(self):
        provider = Mock()
        provider.get_provider_name.return_value = "fixture"
        provider.search_question.return_value = [
            SearchResult(
                title="fixture",
                snippet="evidence",
                source="llm_api",
                score=8,
                status="scored",
                metadata={"question_id": "IQS_05", "parsed_score": 8},
            )
        ]
        provider.search.side_effect = AssertionError("identified question used legacy path")
        question = Question("竞争优势如何？", question_id="IQS_05")
        engine = QAEngine(provider)

        result = engine.process_question(question)

        provider.search_question.assert_called_once_with(question)
        assert result.question.question_id == "IQS_05"
        assert result.answer.score == 8

    def test_process_question_sanitizes_provider_exception_details(self):
        provider = Mock()
        provider.get_provider_name.return_value = "fixture"
        provider.search_question.side_effect = ValueError("private credential response body")
        engine = QAEngine(provider)

        with pytest.raises(ProcessingError) as error:
            engine.process_question("Issuer quality")

        assert "private credential response body" not in str(error.value)
        assert "ValueError" in str(error.value)

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


@pytest.mark.parametrize("status", [400, 429, 503])
def test_error_result_keeps_typed_uncertain_receipt_without_checkpoint_or_retry(status):
    from src.utils.quick_scan_work_transport import QuickScanWorkUncertainError

    receipt = {
        "provider": "openai",
        "requested_model": "fixture-model",
        "http_status_code": status,
        "request_id": "fixture-request",
        "failure_type": "http_error",
        "search_status": "unavailable",
    }
    error = QuickScanWorkUncertainError("fixture-attempt", receipt)
    error.attempt_receipt["raw_body"] = "must-not-export-private-body"
    provider = Mock()
    provider.search_question.side_effect = error
    lifecycle = Mock()
    lifecycle.before_question.return_value = {"claimed": True, "work_item_id": "fixture-work"}
    lifecycle.reference_question.return_value = None
    lifecycle.hydrate_question.return_value = None
    from contextlib import nullcontext

    lifecycle.dispatch_context.return_value = nullcontext()
    generator = Mock()
    engine = QAEngine(provider, generator, work_item_lifecycle=lifecycle)
    result = engine.process_questions([Question("fixture quality", question_id="IQS_05")])
    answer = result.results[0].answer
    assert answer.status == "error" and answer.score is None
    generator.generate_answer.assert_not_called()
    lifecycle.after_question.assert_not_called()
    lifecycle.after_question_failed.assert_called_once()
    provider.search_question.assert_called_once()
    public = result.to_quick_scan_dict(
        entity_id="issuer:fixture",
        company_name="Fixture",
        provider_name="openai",
        requested_model="fixture-model",
    )
    execution = public["execution_receipts"]["IQS_05"]
    assert execution["http_status_code"] == status
    assert execution["attempts"] == [{**receipt, "attempt_id": "fixture-attempt"}]
    assert execution["dispatch_outcome"]["state"] == "uncertain"
    assert (
        execution["dispatch_outcome"]["resume_condition"] == "reconcile_same_attempt_before_retry"
    )
    assert "raw_body" not in str(public) and "must-not-export" not in str(public)


@pytest.mark.parametrize("kind", ["lookalike", "cycle", "deep"])
def test_uncertain_projection_rejects_untyped_or_unbounded_exception_chains(kind):
    from src.core.qa_engine import _uncertain_transport_metadata
    from src.utils.quick_scan_work_transport import QuickScanWorkUncertainError

    class LookalikeError(ValueError):
        attempt_id = "fixture-attempt"
        attempt_receipt = {"http_status_code": 503}

    error: BaseException = ValueError("fixture")
    if kind == "lookalike":
        error = LookalikeError("fixture")
    elif kind == "cycle":
        error.__cause__ = error
    else:
        cursor = error
        for _ in range(16):
            child = ValueError("fixture")
            cursor.__cause__ = child
            cursor = child
        cursor.__cause__ = QuickScanWorkUncertainError("fixture-attempt", {})
    assert _uncertain_transport_metadata(error) == {}
