"""答案生成器。

该模块负责基于搜索结果生成最终答案。
"""

from typing import List

from src.core.exceptions import ProcessingError
from src.core.models import Answer, Question, SearchResult
from src.utils.logger import get_logger

logger = get_logger(__name__)


class AnswerGenerator:
    """答案生成器。

    基于搜索结果生成自然语言答案。
    当前实现为简单模板，未来可升级为 LLM 驱动。
    """

    def __init__(self) -> None:
        """初始化答案生成器。"""
        logger.debug("初始化答案生成器")

    def generate_answer(self, question: Question, search_results: List[SearchResult]) -> Answer:
        """基于搜索结果生成答案。

        Args:
            question: 问题对象
            search_results: 搜索结果列表

        Returns:
            答案对象

        Raises:
            ProcessingError: 答案生成失败时抛出
        """
        logger.debug(f"为问题生成答案: {question.text[:50]}...")

        if not search_results:
            logger.warning("没有搜索结果，返回默认答案")
            return Answer(
                text=f"抱歉，没有找到关于 '{question.text}' 的相关信息。",
                score=None,
                status="insufficient_evidence",
                source="no_results",
            )

        try:
            # 从搜索结果中提取答案
            first_result = search_results[0]

            # 如果没有找到结果，返回提示信息
            if first_result.source == "no_results":
                answer_text = first_result.snippet
                answer_score = None
                answer_status = "insufficient_evidence"
            else:
                # 使用搜索结果中的实际答案内容
                # 对于 LLM 知识库或其他搜索提供者，答案在 snippet 字段中
                answer_text = first_result.snippet
                answer_score = first_result.score
                answer_status = first_result.status or (
                    "scored" if answer_score is not None else "unknown"
                )

            answer_source = first_result.source
            response_question_id = first_result.metadata.get("question_id")
            should_check_question_id = question.question_id is not None and (
                response_question_id is not None or answer_status == "scored"
            )
            if should_check_question_id and response_question_id != question.question_id:
                answer_text = "拒绝接收：响应的问题ID与当前待办不一致。"
                answer_score = None
                answer_status = "error"
                answer_source = "error"

            parsed_score = first_result.metadata.get("parsed_score")
            if (
                answer_status != "error"
                and parsed_score is not None
                and parsed_score != answer_score
            ):
                answer_text = "拒绝接收：外层评分与结构化回复中的评分不一致。"
                answer_score = None
                answer_status = "error"
                answer_source = "error"

            answer = Answer(
                text=answer_text,
                score=answer_score,
                status=answer_status,
                source=answer_source,
                metadata=first_result.metadata,
            )

            logger.debug(f"答案生成完成: {answer.text[:50]}...")
            return answer

        except (IndexError, KeyError, ValueError, TypeError, RuntimeError) as e:
            logger.error("答案生成失败（%s）", type(e).__name__)
            raise ProcessingError(
                message=f"答案生成失败（{type(e).__name__}）", question=question.text
            ) from e

    def generate_batch_answers(
        self, questions: List[Question], all_search_results: List[List[SearchResult]]
    ) -> List[Answer]:
        """批量生成答案。

        Args:
            questions: 问题列表
            all_search_results: 对应的搜索结果列表

        Returns:
            答案列表

        Raises:
            ProcessingError: 答案生成失败时抛出
        """
        if len(questions) != len(all_search_results):
            raise ProcessingError(
                message="问题和搜索结果数量不匹配",
                details={
                    "questions_count": len(questions),
                    "results_count": len(all_search_results),
                },
            )

        answers = []
        for question, search_results in zip(questions, all_search_results):
            try:
                answer = self.generate_answer(question, search_results)
                answers.append(answer)
            except ProcessingError:
                # 继续处理其他问题
                answers.append(
                    Answer(text="处理问题时发生错误", score=None, status="error", source="error")
                )

        return answers
