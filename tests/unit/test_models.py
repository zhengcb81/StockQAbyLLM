"""测试数据模型。

该模块测试 Question、Answer、QAResult、QABatchResult 和 SearchResult 类。
"""

import hashlib
import json
from datetime import datetime

import pytest

from src.core.exceptions import ValidationError
from src.core.models import Answer, QABatchResult, QAResult, Question, SearchResult


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

    @pytest.mark.parametrize("invalid", [True, False, 0, 11, 1.5, "8"])
    def test_answer_rejects_non_integer_or_out_of_range_scores(self, invalid):
        with pytest.raises(ValueError, match="评分必须是1-10之间的整数"):
            Answer(text="答案", score=invalid)

    def test_answer_null_score_has_non_scored_status(self):
        answer = Answer(text="缺少可核验依据", score=None, status="insufficient_evidence")
        assert answer.score is None
        assert answer.status == "insufficient_evidence"

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

    def test_quick_scan_envelope_keeps_entity_time_score_and_execution_separate(self):
        batch = QABatchResult(total_questions=2)
        batch.add_result(
            QAResult(
                question=Question(text="优势题", question_id="IQS_05"),
                answer=Answer(
                    text="有公开来源",
                    score=8,
                    source="llm_api",
                    metadata={
                        "provider": "openai",
                        "model_requested": "requested-model",
                        "actual_model": "actual-model",
                        "request_id": "req_1",
                        "response_id": "resp_1",
                        "search_status": "executed",
                        "source_urls": ["https://example.com/source"],
                        "execution": {"web_search_calls": [{"id": "ws_1", "status": "completed"}]},
                    },
                ),
            )
        )
        batch.add_result(
            QAResult(
                question=Question(text="缺据题", question_id="IQS_06"),
                answer=Answer(
                    text="缺少可核验依据",
                    score=None,
                    status="insufficient_evidence",
                    source="llm_api",
                ),
            )
        )

        output = batch.to_quick_scan_dict(
            entity_id="issuer:US0378331005",
            company_name="Example Inc.",
            provider_name="openai",
            requested_model="requested-model",
        )

        assert output["schema_version"] == "stockqa.quick_scan_result/1.0.0"
        assert output["entity"]["entity_id"] == "issuer:US0378331005"
        assert output["observed_at"].endswith("Z")
        assert output["answers"]["IQS_05"]["score"] == 8
        assert "response_kind" not in output["answers"]["IQS_05"]
        assert output["execution_receipts"]["IQS_05"]["answered_at"].endswith("Z")
        assert output["answers"]["IQS_05"]["check_level"] == "unverified_model_output"
        assert output["answers"]["IQS_05"]["source_urls"] == ["https://example.com/source"]
        assert output["answers"]["IQS_06"]["score"] is None
        assert output["answers"]["IQS_06"]["status"] == "insufficient_evidence"
        assert output["execution_receipts"]["IQS_05"]["request_id"] == "req_1"
        assert output["execution_receipts"]["IQS_05"]["actual_model"] == "actual-model"
        assert (
            output["execution_receipts"]["IQS_05"]["input_question_sha256"]
            == hashlib.sha256("优势题".encode("utf-8")).hexdigest()
        )
        expected_answer_sha256 = hashlib.sha256(
            json.dumps(
                output["answers"]["IQS_05"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        assert output["execution_receipts"]["IQS_05"]["answer_sha256"] == expected_answer_sha256

    def test_quick_scan_envelope_preserves_dispatch_outcome(self):
        dispatch_outcome = {
            "scope": "provider_dispatch",
            "state": "completed",
            "wait_reason": None,
            "resume_condition": None,
        }
        batch = QABatchResult(total_questions=1)
        batch.add_result(
            QAResult(
                question=Question(text="优势题", question_id="IQS_05"),
                answer=Answer(
                    text="有公开来源",
                    score=8,
                    metadata={"execution": {"dispatch_outcome": dispatch_outcome}},
                ),
            )
        )

        output = batch.to_quick_scan_dict(
            entity_id="issuer:test",
            company_name="Example Inc.",
            provider_name="backup",
            requested_model="model-b",
        )

        assert output["execution_receipts"]["IQS_05"]["dispatch_outcome"] == dispatch_outcome

    def test_quick_scan_envelope_does_not_publish_a_route_alias_as_provider(self):
        batch = QABatchResult(total_questions=2)
        for question_id, transport_provider, model in (
            ("IQS_05", "openai", "gpt-4.1-mini"),
            ("IQS_06", "minimax", "MiniMax-M3"),
        ):
            batch.add_result(
                QAResult(
                    question=Question(text=question_id, question_id=question_id),
                    answer=Answer(
                        text="有来源",
                        score=8,
                        metadata={
                            "provider": transport_provider,
                            "model_requested": model,
                            "response_id": f"resp-{question_id}",
                            "search_status": "executed",
                            "execution": {"provider": transport_provider},
                            "attempts": [
                                {
                                    "provider": transport_provider,
                                    "provider_config_ref": f"alias-{question_id}",
                                    "route_id": f"route-{question_id}",
                                    "requested_model": model,
                                    "http_status_code": 200,
                                }
                            ],
                        },
                    ),
                )
            )

        output = batch.to_quick_scan_dict(
            entity_id="issuer:test",
            company_name="Example Inc.",
            provider_name="primary-alias",
            requested_model="preferred-model",
        )
        assert output["provider"] == {"name": None, "requested_model": None}
        assert output["execution_receipts"]["IQS_05"]["provider"] == "openai"
        assert output["execution_receipts"]["IQS_06"]["provider"] == "minimax"

    def test_quick_scan_envelope_unsent_result_has_no_actual_provider(self):
        batch = QABatchResult(total_questions=1)
        batch.add_result(
            QAResult(
                question=Question(text="问题", question_id="IQS_05"),
                answer=Answer(text="未发送", score=None, status="insufficient_evidence"),
            )
        )
        output = batch.to_quick_scan_dict(
            entity_id="issuer:test",
            company_name="Example Inc.",
            provider_name="primary-alias",
            requested_model="preferred-model",
        )
        assert output["provider"] == {"name": None, "requested_model": None}
        assert output["execution_receipts"]["IQS_05"]["provider"] is None

    @pytest.mark.parametrize("questions", [["no-id"], ["one", "two"]])
    def test_quick_scan_envelope_never_invents_missing_or_duplicate_ids(self, questions):
        batch = QABatchResult(total_questions=len(questions))
        ids = ["IQS_05", "IQS_05"] if len(questions) == 2 else [None]
        for text, question_id in zip(questions, ids):
            batch.add_result(
                QAResult(
                    question=Question(text=text, question_id=question_id),
                    answer=Answer(text="answer", score=8),
                )
            )
        with pytest.raises(ValueError, match="必须带有显式question_id|重复question_id"):
            batch.to_quick_scan_dict(
                entity_id="issuer:test",
                company_name="Example Inc.",
                provider_name="openai",
                requested_model="fixture",
            )


class TestSearchResult:
    """测试 SearchResult 类。"""

    def test_create_valid_search_result(self):
        """测试创建有效搜索结果。"""
        result = SearchResult(title="标题", snippet="摘要")
        assert result.title == "标题"
        assert result.source == "unknown"

    def test_empty_title_raises_error(self):
        """测试空标题抛出异常。"""
        with pytest.raises(ValueError, match="搜索结果标题不能为空"):
            SearchResult(title="", snippet="摘要")

    def test_empty_snippet_raises_error(self):
        """测试空摘要抛出异常。"""
        with pytest.raises(ValueError, match="搜索结果摘要不能为空"):
            SearchResult(title="标题", snippet="")

    def test_to_dict_without_score(self):
        """测试无分数时转换为字典。"""
        result = SearchResult(title="标题", snippet="摘要", url="https://example.com")
        d = result.to_dict()
        assert d["title"] == "标题"
        assert d["url"] == "https://example.com"
        assert "score" not in d

    def test_to_dict_with_score(self):
        """测试有分数时转换为字典。"""
        result = SearchResult(title="标题", snippet="摘要", score=8)
        d = result.to_dict()
        assert d["score"] == 8

    def test_from_dict_roundtrip(self):
        """测试从字典创建实例。"""
        d = {"title": "标题", "snippet": "摘要", "source": "llm", "url": None, "rank": 2}
        result = SearchResult.from_dict(d)
        assert result.title == "标题"
        assert result.source == "llm"
        assert result.rank == 2
