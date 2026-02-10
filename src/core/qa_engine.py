"""Q&A 引擎核心编排器。

该模块是系统的核心，负责协调问题处理的整个流程。
"""

from typing import List, Dict, Any, Optional

from src.core.models import Question, Answer, QAResult, QABatchResult
from src.interfaces.search_provider import SearchProvider
from src.services.answer_generator import AnswerGenerator
from src.core.exceptions import ProcessingError, ValidationError
from src.config.settings import (
    DISPLAY_LINE_WIDTH,
    DISPLAY_QUESTION_TRUNCATE,
    DISPLAY_TITLE_TRUNCATE,
)
from src.utils.logger import get_logger

logger = get_logger(__name__)


class QAEngine:
    """Q&A 处理引擎。

    负责协调整个问题-答案处理流程：
    1. 接收问题列表
    2. 使用搜索服务获取信息
    3. 使用答案生成器生成答案
    4. 返回结果
    """

    def __init__(
        self, search_provider: SearchProvider, answer_generator: Optional[AnswerGenerator] = None
    ):
        """初始化 Q&A 引擎。

        Args:
            search_provider: 搜索提供者实例
            answer_generator: 答案生成器实例（可选，默认创建新实例）
        """
        self.search_provider = search_provider
        self.answer_generator = answer_generator or AnswerGenerator()
        logger.info(f"QAEngine 初始化完成（搜索提供者: {search_provider.get_provider_name()}）")

    def process_question(self, question_text: str) -> QAResult:
        """处理单个问题。

        Args:
            question_text: 问题文本

        Returns:
            问答结果对象

        Raises:
            ValidationError: 问题验证失败
            ProcessingError: 问题处理失败
        """
        logger.debug(f"处理单个问题: {question_text[:DISPLAY_QUESTION_TRUNCATE]}...")

        # 验证并创建问题对象
        try:
            question = Question(text=question_text)
        except ValueError as e:
            raise ValidationError(message=f"问题验证失败: {str(e)}", field="question")

        # 执行搜索
        try:
            search_results = self.search_provider.search(question.text)
            logger.debug(f"搜索完成，返回 {len(search_results)} 个结果")
        except ProcessingError:
            raise
        except (ValueError, RuntimeError) as e:
            logger.error(f"搜索失败: {e}")
            raise ProcessingError(message=f"搜索执行失败: {str(e)}", question=question.text)

        # 生成答案
        try:
            answer = self.answer_generator.generate_answer(question, search_results)
        except ProcessingError:
            raise
        except RuntimeError as e:
            logger.error(f"答案生成失败: {e}")
            raise ProcessingError(message=f"答案生成失败: {str(e)}", question=question.text)

        # 创建结果对象
        result = QAResult(question=question, answer=answer)
        logger.debug(f"问题处理完成: {question.text[:DISPLAY_TITLE_TRUNCATE]}...")

        return result

    def process_questions(self, question_texts: List[str]) -> QABatchResult:
        """批量处理问题。

        Args:
            question_texts: 问题文本列表

        Returns:
            批次结果对象

        Raises:
            ValidationError: 问题列表验证失败
        """
        if not question_texts:
            raise ValidationError(message="问题列表不能为空")

        batch_result = QABatchResult(total_questions=len(question_texts))
        logger.info(f"开始批量处理 {len(question_texts)} 个问题")

        # 添加控制台进度显示
        print(f"\n{'=' * DISPLAY_LINE_WIDTH}")
        print(f"开始处理 {len(question_texts)} 个问题...")
        print(f"{'=' * DISPLAY_LINE_WIDTH}\n")

        for i, question_text in enumerate(question_texts, 1):
            # 显示当前进度（控制台）
            progress_pct = (i - 1) / len(question_texts) * 100
            print(
                f"\r[{i}/{len(question_texts)}] 进度: {progress_pct:.1f}% - "
                f"正在处理: {question_text[:DISPLAY_QUESTION_TRUNCATE]}...",
                end="",
                flush=True,
            )

            try:
                result = self.process_question(question_text)
                batch_result.add_result(result)
                logger.info(f"[{i}/{len(question_texts)}] 处理成功")

                # 显示完成标记（控制台）
                print(
                    f"\r[{i}/{len(question_texts)}] 进度: {i / len(question_texts) * 100:.1f}% - "
                    f"[OK] 完成: {question_text[:DISPLAY_QUESTION_TRUNCATE]}...  ",
                    end="",
                    flush=True,
                )

            except (ValidationError, ProcessingError) as e:
                logger.error(f"[{i}/{len(question_texts)}] 处理失败: {e}")

                # 显示失败标记（控制台）
                print(
                    f"\r[{i}/{len(question_texts)}] 进度: {i / len(question_texts) * 100:.1f}% - "
                    f"[FAIL] 失败: {question_text[:DISPLAY_QUESTION_TRUNCATE]}...  ",
                    end="",
                    flush=True,
                )

                # 添加错误结果，继续处理下一个问题
                try:
                    error_question = Question(text=question_text)
                    error_answer = Answer(text=f"处理失败: {str(e)}", source="error")
                    error_result = QAResult(
                        question=error_question, answer=error_answer, metadata={"error": str(e)}
                    )
                    batch_result.add_result(error_result)
                except (ConnectionError, TimeoutError):
                    # 如果连错误结果都无法创建，跳过此问题
                    logger.error(f"[{i}/{len(question_texts)}] 无法创建错误结果，跳过")

        print(f"\n\n{'=' * DISPLAY_LINE_WIDTH}")
        print(f"所有问题处理完成！")
        print(f"{'=' * DISPLAY_LINE_WIDTH}\n")
        logger.info(f"批量处理完成，成功: {batch_result.processed_count}/{len(question_texts)}")
        return batch_result

    def output_results(
        self, batch_result: QABatchResult, output_file: Optional[str] = None
    ) -> None:
        """输出结果。

        Args:
            batch_result: 批次结果对象
            output_file: 输出文件路径（可选，默认输出到控制台）
        """
        import json

        result_dict = batch_result.to_dict()
        json_output = json.dumps(result_dict, ensure_ascii=False, indent=4)

        if output_file:
            from pathlib import Path

            output_path = Path(output_file)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(json_output)
            logger.info(f"[OK] 结果已保存到: {output_file}")
        else:
            print(json_output)
            logger.info("[OK] 结果已输出到控制台")

    def get_statistics(self, batch_result: QABatchResult) -> Dict[str, Any]:
        """获取统计信息。

        Args:
            batch_result: 批次结果对象

        Returns:
            包含统计信息的字典
        """
        error_count = sum(1 for r in batch_result.results if r.metadata.get("error"))

        return {
            "total_questions": batch_result.total_questions,
            "processed_count": batch_result.processed_count,
            "success_count": batch_result.processed_count - error_count,
            "error_count": error_count,
            "is_complete": batch_result.is_complete(),
            "created_at": batch_result.created_at.isoformat(),
        }
