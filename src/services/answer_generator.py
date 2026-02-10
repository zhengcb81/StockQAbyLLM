"""答案生成器。

该模块负责基于搜索结果生成最终答案。
"""

from typing import List

from src.core.models import Question, Answer, SearchResult
from src.core.exceptions import ProcessingError
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
                text=f"抱歉，没有找到关于 '{question.text}' 的相关信息。", source="no_results"
            )

        try:
            # 从搜索结果中提取答案
            first_result = search_results[0]

            # 如果没有找到结果，返回提示信息
            if first_result.source == "no_results":
                answer_text = first_result.snippet
                answer_score = 1
            else:
                # 使用搜索结果中的实际答案内容
                # 对于 LLM 知识库或其他搜索提供者，答案在 snippet 字段中
                answer_text = first_result.snippet
                # 使用默认评分
                answer_score = 5

            answer = Answer(
                text=answer_text,
                score=answer_score,
                source=first_result.source,
            )

            logger.debug(f"答案生成完成: {answer.text[:50]}...")
            return answer

        except (IndexError, KeyError, ValueError, TypeError, RuntimeError) as e:
            logger.error(f"答案生成失败: {e}")
            raise ProcessingError(message=f"答案生成失败: {str(e)}", question=question.text)

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
                answers.append(Answer(text="处理问题时发生错误", source="error"))

        return answers
