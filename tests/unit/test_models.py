"""测试数据模型。

该模块测试 Question、Answer、QAResult 和 QABatchResult 类。
"""

import pytest
from datetime import datetime

from src.core.models import Question, Answer, QAResult, QABatchResult
from src.core.exceptions import ValidationError


class TestQuestion:
    """测试 Question 类。"""

    def test_create_valid_question(self):
        """测试创建有效问题。"""
        question = Question(text="如何学习Python？")
        assert question.text == "如何学习Python？"
        assert isinstance(question.created_at, datetime)

    def test_question_strips_whitespace(self):
        """测试去除首尾空格。"""
        question = Question(text="  如何学习Python？  ")
        assert question.text == "如何学习Python？"

    def test_question_empty_string_raises_error(self):
        """测试空字符串抛出异常。"""
        with pytest.raises(ValueError, match="问题文本不能为空"):
            Question(text="")

    def test_question_whitespace_only_raises_error(self):
        """测试仅包含空格抛出异常。"""
        with pytest.raises(ValueError, match="问题文本不能为空"):
            Question(text="   ")

    def test_question_str_representation(self):
        """测试字符串表示。"""
        question = Question(text="测试问题")
        assert str(question) == "测试问题"


class TestAnswer:
    """测试 Answer 类。"""

    def test_create_valid_answer(self):
        """测试创建有效答案。"""
        answer = Answer(text="这是答案")
        assert answer.text == "这是答案"
        assert answer.source == "web_search"
        assert isinstance(answer.created_at, datetime)

    def test_answer_with_custom_source(self):
        """测试自定义来源。"""
        answer = Answer(text="这是答案", source="llm")
        assert answer.source == "llm"

    def test_answer_empty_string_raises_error(self):
        """测试空字符串抛出异常。"""
        with pytest.raises(ValueError, match="答案文本不能为空"):
            Answer(text="")

    def test_answer_str_representation(self):
        """测试字符串表示。"""
        answer = Answer(text="测试答案")
        assert str(answer) == "测试答案"


class TestQAResult:
    """测试 QAResult 类。"""

    def test_create_qa_result(self):
        """测试创建问答结果。"""
        question = Question(text="问题")
        answer = Answer(text="答案")
        result = QAResult(question=question, answer=answer)

        assert result.question == question
        assert result.answer == answer
        assert result.metadata == {}

    def test_qa_result_with_metadata(self):
        """测试带元数据的问答结果。"""
        question = Question(text="问题")
        answer = Answer(text="答案")
        metadata = {"confidence": 0.95, "source": "web"}
        result = QAResult(question=question, answer=answer, metadata=metadata)

        assert result.metadata == metadata

    def test_qa_result_to_dict(self):
        """测试转换为字典。"""
        question = Question(text="问题")
        answer = Answer(text="答案", score=8)
        result = QAResult(question=question, answer=answer)

        result_dict = result.to_dict()
        # 实际输出格式: {"问题": {"score": 8, "description": "答案"}}
        assert "问题" in result_dict
        assert result_dict["问题"]["score"] == 8
        assert result_dict["问题"]["description"] == "答案"

    def test_qa_result_str_representation(self):
        """测试字符串表示。"""
        question = Question(text="问题")
        answer = Answer(text="答案")
        result = QAResult(question=question, answer=answer)

        result_str = str(result)
        assert "Q: 问题" in result_str
        assert "A: 答案" in result_str


class TestQABatchResult:
    """测试 QABatchResult 类。"""

    def test_create_empty_batch_result(self):
        """测试创建空批次结果。"""
        batch = QABatchResult()
        assert batch.results == []
        assert batch.total_questions == 0
        assert batch.processed_count == 0
        assert batch.is_complete()

    def test_batch_result_add_result(self):
        """测试添加结果。"""
        batch = QABatchResult(total_questions=2)
        question = Question(text="问题")
        answer = Answer(text="答案")
        result = QAResult(question=question, answer=answer)

        batch.add_result(result)

        assert len(batch.results) == 1
        assert batch.processed_count == 1
        assert not batch.is_complete()

    def test_batch_result_completion(self):
        """测试批次完成。"""
        batch = QABatchResult(total_questions=2)

        for i in range(2):
            question = Question(text=f"问题{i}")
            answer = Answer(text=f"答案{i}")
            result = QAResult(question=question, answer=answer)
            batch.add_result(result)

        assert batch.processed_count == 2
        assert batch.is_complete()

    def test_batch_result_to_dict(self):
        """测试转换为字典。"""
        batch = QABatchResult(total_questions=2)

        for i in range(2):
            question = Question(text=f"问题{i}")
            answer = Answer(text=f"答案{i}", score=5 + i)
            result = QAResult(question=question, answer=answer)
            batch.add_result(result)

        result_dict = batch.to_dict()
        # 实际输出格式: {"问题0": {"score": 5, "description": "答案0"},
        #                 "问题1": {"score": 6, "description": "答案1"}}
        assert "问题0" in result_dict
        assert "问题1" in result_dict
        assert result_dict["问题0"]["score"] == 5
        assert result_dict["问题0"]["description"] == "答案0"
        assert result_dict["问题1"]["score"] == 6
        assert result_dict["问题1"]["description"] == "答案1"

    def test_batch_result_str_representation(self):
        """测试字符串表示。"""
        batch = QABatchResult(total_questions=5)
        question = Question(text="问题")
        answer = Answer(text="答案")
        result = QAResult(question=question, answer=answer)
        batch.add_result(result)

        result_str = str(batch)
        assert "1/5" in result_str
