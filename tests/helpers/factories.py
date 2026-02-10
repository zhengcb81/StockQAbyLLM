"""测试数据工厂。

该模块提供用于创建测试数据的工厂函数。
"""

from typing import Dict, Any, List, Optional
from src.core.models import Question, Answer, QAResult, QABatchResult


class QuestionFactory:
    """Question 对象工厂。"""

    @staticmethod
    def create(text: str = "测试问题") -> Question:
        """创建一个 Question 对象。

        Args:
            text: 问题文本

        Returns:
            Question 对象
        """
        return Question(text=text)

    @staticmethod
    def create_multiple(count: int, prefix: str = "问题") -> List[Question]:
        """创建多个 Question 对象。

        Args:
            count: 创建数量
            prefix: 问题前缀

        Returns:
            Question 对象列表
        """
        return [Question(text=f"{prefix}{i}") for i in range(1, count + 1)]

    @staticmethod
    def create_empty() -> Question:
        """创建一个空文本的 Question（用于测试异常）。"""
        return Question(text="")

    @staticmethod
    def create_whitespace_only() -> Question:
        """创建一个只包含空白字符的 Question（用于测试异常）。"""
        return Question(text="   ")


class AnswerFactory:
    """Answer 对象工厂。"""

    @staticmethod
    def create(score: int = 7, text: str = "这是测试答案", source: str = "test") -> Answer:
        """创建一个 Answer 对象。

        Args:
            score: 评分 (1-10)
            text: 答案文本
            source: 来源

        Returns:
            Answer 对象
        """
        return Answer(score=score, text=text, source=source)

    @staticmethod
    def create_multiple(count: int, start_score: int = 1) -> List[Answer]:
        """创建多个 Answer 对象。

        Args:
            count: 创建数量
            start_score: 起始评分

        Returns:
            Answer 对象列表
        """
        answers = []
        for i in range(count):
            score = min(start_score + i, 10)  # 最高10分
            answers.append(Answer(score=score, text=f"答案{i+1}", source="test"))
        return answers

    @staticmethod
    def create_empty() -> Answer:
        """创建一个空文本的 Answer（用于测试异常）。"""
        return Answer(score=0, text="", source="test")

    @staticmethod
    def create_invalid_score() -> Answer:
        """创建一个评分无效的 Answer（用于测试）。"""
        return Answer(score=11, text="无效评分", source="test")


class QAResultFactory:
    """QAResult 对象工厂。"""

    @staticmethod
    def create(
        question_text: str = "测试问题",
        score: int = 7,
        answer_text: str = "这是测试答案",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> QAResult:
        """创建一个 QAResult 对象。

        Args:
            question_text: 问题文本
            score: 评分 (1-10)
            answer_text: 答案文本
            metadata: 元数据

        Returns:
            QAResult 对象
        """
        question = Question(text=question_text)
        answer = Answer(score=score, text=answer_text, source="test")
        return QAResult(question=question, answer=answer, metadata=metadata or {})

    @staticmethod
    def create_multiple(count: int) -> QAResult:
        """创建多个 QAResult 对象。

        Args:
            count: 创建数量

        Returns:
            包含多个 QAResult 的 QAResult 对象
        """
        batch = QABatchResult(total_questions=count)
        for i in range(count):
            result = QAResultFactory.create(
                question_text=f"问题{i+1}", score=min(7 + i, 10), answer_text=f"这是问题{i+1}的答案"
            )
            batch.add_result(result)
        return batch


class QABatchResultFactory:
    """QABatchResult 对象工厂。"""

    @staticmethod
    def create_empty() -> QABatchResult:
        """创建一个空的 QABatchResult 对象。"""
        return QABatchResult(total_questions=0)

    @staticmethod
    def create_with_results(count: int) -> QABatchResult:
        """创建一个包含指定数量结果的 QABatchResult 对象。

        Args:
            count: 结果数量

        Returns:
            QABatchResult 对象
        """
        return QAResultFactory.create_multiple(count)

    @staticmethod
    def create_partial_success(total: int, success: int) -> QABatchResult:
        """创建一个部分成功的 QABatchResult 对象。

        Args:
            total: 总问题数
            success: 成功处理数

        Returns:
            QABatchResult 对象
        """
        batch = QABatchResult(total_questions=total)
        for i in range(success):
            batch.add_result(
                QAResultFactory.create(
                    question_text=f"问题{i+1}", score=7, answer_text=f"答案{i+1}"
                )
            )
        return batch
