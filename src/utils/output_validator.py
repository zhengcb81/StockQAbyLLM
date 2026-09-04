"""输出文件验证和修复模块。

该模块提供自动验证和修复输出 JSON 文件的功能，确保输出文件中的问题与配置文件中的问题完全一致。
"""

import json
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

from src.utils.logger import get_logger


@dataclass
class ValidationResult:
    """验证结果数据类。

    Attributes:
        is_valid: 输出文件是否有效（无缺失、无多余、无不完整）
        missing_questions: 在配置中但不在输出中的问题列表
        extra_questions: 在输出中但不在配置中的问题列表
        incomplete_answers: 缺少 score 或 description 的答案列表
        output_path: 输出文件路径
    """

    is_valid: bool
    missing_questions: List[str] = field(default_factory=list)
    extra_questions: List[str] = field(default_factory=list)
    incomplete_answers: List[str] = field(default_factory=list)
    output_path: Optional[Path] = None

    @property
    def total_issues(self) -> int:
        """总问题数。"""
        return (
            len(self.missing_questions) + len(self.extra_questions) + len(self.incomplete_answers)
        )


@dataclass
class RepairResult:
    """修复结果数据类。

    Attributes:
        validation_result: 验证结果对象
        repaired_questions: 成功修复的问题列表
        failed_questions: 修复失败的问题列表
        backup_created: 是否创建了备份文件
        backup_path: 备份文件路径
        retries_performed: 执行的重试次数
    """

    validation_result: ValidationResult
    repaired_questions: List[str] = field(default_factory=list)
    failed_questions: List[str] = field(default_factory=list)
    backup_created: bool = False
    backup_path: Optional[Path] = None
    retries_performed: int = 0

    @property
    def success_rate(self) -> float:
        """修复成功率。"""
        if not self.validation_result.missing_questions:
            return 1.0
        return len(self.repaired_questions) / len(self.validation_result.missing_questions)


class OutputValidator:
    """输出文件验证器。

    负责验证输出文件是否与配置文件匹配，并修复发现的问题。
    """

    def __init__(self, logger: Optional[Any] = None) -> None:
        """初始化验证器。

        Args:
            logger: 日志记录器对象（可选）
        """
        self.logger = logger or get_logger(__name__)

    def validate(self, output_path: Path, config_questions: List[str]) -> ValidationResult:
        """验证输出文件。

        比较输出文件中的问题与配置文件中的问题，检测：
        1. 缺失的问题（在配置中但不在输出中）
        2. 多余的问题（在输出中但不在配置中）
        3. 不完整的答案（缺少 score 或 description）

        Args:
            output_path: 输出文件路径
            config_questions: 配置文件中的问题列表

        Returns:
            ValidationResult 对象

        Raises:
            FileNotFoundError: 输出文件不存在
            ValidationError: JSON 格式错误
        """
        self.logger.info(f"[验证] 开始验证输出文件: {output_path}")

        # 加载输出文件
        output_data = self._load_output_file(output_path)

        # 比较问题集合
        output_questions = set(output_data.keys())
        config_questions_set = set(config_questions)

        # 检测缺失和多余的问题
        missing_questions = list(config_questions_set - output_questions)
        extra_questions = list(output_questions - config_questions_set)

        # 检查答案完整性
        incomplete_answers = []
        for question, answer_data in output_data.items():
            if not isinstance(answer_data, dict):
                incomplete_answers.append(question)
            elif "score" not in answer_data or "description" not in answer_data:
                incomplete_answers.append(question)

        # 判断是否有效
        is_valid = (
            len(missing_questions) == 0
            and len(extra_questions) == 0
            and len(incomplete_answers) == 0
        )

        result = ValidationResult(
            is_valid=is_valid,
            missing_questions=missing_questions,
            extra_questions=extra_questions,
            incomplete_answers=incomplete_answers,
            output_path=output_path,
        )

        # 记录验证结果
        if is_valid:
            self.logger.info(f"[验证] [OK] 验证通过，所有问题完整匹配")
        else:
            self.logger.info(f"[验证] 发现 {result.total_issues} 个问题:")
            if missing_questions:
                self.logger.info(f"  - 缺失问题: {len(missing_questions)} 个")
            if extra_questions:
                self.logger.info(f"  - 多余问题: {len(extra_questions)} 个")
            if incomplete_answers:
                self.logger.info(f"  - 不完整答案: {len(incomplete_answers)} 个")

        return result

    def _load_output_file(self, output_path: Path) -> Dict[str, Any]:
        """加载输出 JSON 文件。

        Args:
            output_path: 输出文件路径

        Returns:
            解析后的 JSON 数据

        Raises:
            FileNotFoundError: 文件不存在
            ValidationError: JSON 格式错误
        """
        if not output_path.exists():
            raise FileNotFoundError(f"输出文件不存在: {output_path}")

        try:
            with open(output_path, "r", encoding="utf-8") as f:
                return cast(Dict[str, Any], json.load(f))
        except json.JSONDecodeError as e:
            from src.core.exceptions import ValidationError

            raise ValidationError(message=f"JSON 格式错误: {str(e)}", field="output_file")

    def repair(
        self, validation_result: ValidationResult, qa_engine: Any, max_retries: int = 3
    ) -> RepairResult:
        """修复输出文件。

        根据验证结果修复发现的问题：
        1. 创建备份文件
        2. 移除多余的问题
        3. 修复缺失的问题（重新调用 LLM API）

        Args:
            validation_result: 验证结果对象
            qa_engine: QA 引擎实例
            max_retries: 每个问题的最大重试次数

        Returns:
            RepairResult 对象
        """
        self.logger.info(f"[修复] 开始修复...")

        repaired_questions: List[str] = []
        failed_questions: List[str] = []
        backup_created = False
        backup_path: Optional[Path] = None
        retries_performed = 0

        # 验证 output_path 存在
        if validation_result.output_path is None:
            self.logger.warning(f"[修复] 输出文件路径为空，跳过修复")
            return RepairResult(
                validation_result=validation_result,
                repaired_questions=repaired_questions,
                failed_questions=failed_questions,
                backup_created=backup_created,
                backup_path=backup_path,
                retries_performed=retries_performed,
            )

        output_path = validation_result.output_path

        # 如果有问题，创建备份
        if validation_result.total_issues > 0:
            backup_path = self._create_backup(output_path)
            backup_created = backup_path is not None

        # 移除多余的问题
        if validation_result.extra_questions:
            self.logger.info(f"[修复] 移除多余问题...")
            success = self._remove_extra_questions(output_path, validation_result.extra_questions)
            if not success:
                self.logger.warning(f"[修复] 移除多余问题时遇到错误")

        # 修复缺失的问题
        if validation_result.missing_questions:
            self.logger.info(f"[修复] 修复缺失问题...")
            repaired, failed = self._repair_missing_questions(
                output_path, validation_result.missing_questions, qa_engine, max_retries
            )
            repaired_questions = repaired
            failed_questions = failed
            retries_performed = len(validation_result.missing_questions) * max_retries

        self.logger.info(f"[修复] 修复完成")

        return RepairResult(
            validation_result=validation_result,
            repaired_questions=repaired_questions,
            failed_questions=failed_questions,
            backup_created=backup_created,
            backup_path=backup_path,
            retries_performed=retries_performed,
        )

    def _create_backup(self, output_path: Path) -> Optional[Path]:
        """创建带时间戳的备份文件。

        Args:
            output_path: 原始输出文件路径

        Returns:
            备份文件路径，如果失败则返回 None
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = output_path.with_suffix(f".json.{timestamp}.bak")

        try:
            shutil.copy2(output_path, backup_path)
            self.logger.info("  备份文件已创建: %s", backup_path)
            return backup_path
        except OSError as e:
            self.logger.error("  创建备份失败: %s", e)
            return None

    def _remove_extra_questions(self, output_path: Path, extra_questions: List[str]) -> bool:
        """从输出文件中移除多余的问题。

        Args:
            output_path: 输出文件路径
            extra_questions: 要移除的问题列表

        Returns:
            是否成功移除
        """
        try:
            # 读取当前文件
            with open(output_path, "r", encoding="utf-8") as f:
                output_data = json.load(f)

            # 移除多余的问题
            removed_count = 0
            for question in extra_questions:
                if question in output_data:
                    output_data.pop(question)
                    removed_count += 1

            # 写回文件
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(output_data, f, ensure_ascii=False, indent=4)

            self.logger.info("  已移除 %d 个多余问题", removed_count)
            return True

        except (OSError, ValueError, TypeError) as e:
            self.logger.error("  移除多余问题失败: %s", e)
            return False

    def _repair_missing_questions(
        self, output_path: Path, missing_questions: List[str], qa_engine: Any, max_retries: int
    ) -> tuple[List[str], List[str]]:
        """修复缺失的问题。

        对每个缺失的问题重新调用 LLM API 生成答案。

        Args:
            output_path: 输出文件路径
            missing_questions: 缺失的问题列表
            qa_engine: QA 引擎实例
            max_retries: 每个问题的最大重试次数

        Returns:
            (成功修复的问题列表, 失败的问题列表)
        """
        repaired: List[str] = []
        failed: List[str] = []

        if not missing_questions:
            return repaired, failed

        self.logger.info(f"  尝试修复 {len(missing_questions)} 个缺失问题...")

        for question in missing_questions:
            success = False

            # 重试机制
            for attempt in range(max_retries):
                try:
                    # 调用 QA 引擎处理问题
                    result = qa_engine.process_question(question)

                    # 读取当前输出文件
                    with open(output_path, "r", encoding="utf-8") as f:
                        output_data = json.load(f)

                    # 更新输出数据
                    output_data.update(result.to_dict())

                    # 写回文件
                    with open(output_path, "w", encoding="utf-8") as f:
                        json.dump(output_data, f, ensure_ascii=False, indent=4)

                    repaired.append(question)
                    success = True

                    # 截断问题文本用于显示
                    short_question = question[:50] + "..." if len(question) > 50 else question
                    self.logger.info(f"    [OK] 修复成功: {short_question}")
                    break

                except (ValueError, AttributeError, RuntimeError) as e:
                    short_question = question[:50] + "..." if len(question) > 50 else question
                    self.logger.warning(
                        f"    [FAIL] 修复失败 (尝试 {attempt + 1}/{max_retries}): "
                        f"{short_question} - {e}"
                    )

                    # 等待后重试
                    if attempt < max_retries - 1:
                        time.sleep(2)

            # 如果所有重试都失败
            if not success:
                failed.append(question)
                short_question = question[:50] + "..." if len(question) > 50 else question
                self.logger.error(f"    [FAIL] 最终修复失败: {short_question}")

        return repaired, failed


def validate_and_repair_output(
    output_path: Path,
    config_questions: List[str],
    qa_engine: Any,
    max_retries: int = 3,
    create_backup: bool = True,
) -> RepairResult:
    """验证并修复输出文件（一站式函数）。

    这是主要的入口函数，从 main_with_llm.py 调用。

    Args:
        output_path: 输出 JSON 文件路径
        config_questions: 配置文件中的问题列表
        qa_engine: QA 引擎实例
        max_retries: 每个问题的最大重试次数（默认：3）
        create_backup: 是否创建备份文件（默认：True）

    Returns:
        RepairResult 对象，包含修复详情

    Example:
        >>> repair_result = validate_and_repair_output(
        ...     output_path=Path("outputs/QALLM_华锐精密.json"),
        ...     config_questions=questions,
        ...     qa_engine=qa_engine,
        ...     max_retries=3
        ... )
        >>> if repair_result.validation_result.total_issues > 0:
        ...     print(f"发现并修复了 {repair_result.validation_result.total_issues} 个问题")
    """
    logger = get_logger(__name__)
    validator = OutputValidator(logger)

    # 验证
    validation_result = validator.validate(output_path, config_questions)

    # 如果有效，直接返回
    if validation_result.is_valid:
        return RepairResult(
            validation_result=validation_result,
            repaired_questions=[],
            failed_questions=[],
            backup_created=False,
            backup_path=None,
            retries_performed=0,
        )

    # 需要修复
    repair_result = validator.repair(validation_result, qa_engine, max_retries)

    # 记录修复结果
    if repair_result.repaired_questions:
        logger.info(f"  修复成功: {len(repair_result.repaired_questions)} 个")
    if repair_result.failed_questions:
        logger.warning(f"  修复失败: {len(repair_result.failed_questions)} 个")

    return repair_result
