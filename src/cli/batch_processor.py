#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量处理模块。

该模块包含批量处理股票的逻辑，将原来的复杂函数拆分为多个小函数。
"""

import json
import time
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Union, cast

from src.core.qa_engine import QAEngine
from src.config.config_provider import ConfigProvider
from src.config.config_manager import ConfigManager
from src.config.json_config_manager import JSONConfigManager
from src.services.answer_generator import AnswerGenerator
from src.providers.llm_provider import LLMProvider
from src.utils.logger import get_logger
from src.utils.output_validator import validate_and_repair_output, RepairResult
from src.config.settings import DEFAULT_MAX_RETRIES, DEFAULT_RETRY_DELAY, DEFAULT_SCORE

logger = get_logger(__name__)


def load_questions(config_file: str, config_format: str) -> List[str]:
    """加载问题列表。

    Args:
        config_file: 配置文件路径
        config_format: 配置文件格式 (json 或 txt)

    Returns:
        问题列表
    """
    config_manager: ConfigProvider
    if config_format == "json":
        config_manager = JSONConfigManager(config_file)
    else:
        config_manager = ConfigManager(config_file)
    questions = config_manager.load_questions()
    logger.info("配置文件: %s (%s格式, %s 个问题)", config_file, config_format, len(questions))
    return questions


def load_existing_answers(output_path: Path) -> Optional[Dict[str, Any]]:
    """加载现有的答案文件。

    Args:
        output_path: 输出文件路径

    Returns:
        现有答案字典，如果文件不存在或加载失败则返回 None
    """
    if not output_path.exists():
        return None

    try:
        with open(output_path, "r", encoding="utf-8") as f:
            existing_answers = cast(Dict[str, Any], json.load(f))
        logger.info(f"  发现现有文件，已加载 {len(existing_answers)} 个已有答案")
        return existing_answers
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("  读取现有文件失败: %s，将作为新文件处理", e)
        return None


def calculate_questions_to_process(
    questions: List[str], existing_answers: Optional[Dict[str, Any]], override: bool
) -> Tuple[List[str], List[str], bool]:
    """计算需要处理的问题列表。

    Args:
        questions: 配置文件中的所有问题
        existing_answers: 现有答案字典
        override: 是否覆盖模式

    Returns:
        (需要处理的问题列表, 跳过的问题列表, 是否需要处理)
    """
    if existing_answers and not override:
        # override=False: 只处理不存在的问题
        new_questions = [q for q in questions if q not in existing_answers]
        skipped_questions = [q for q in questions if q in existing_answers]

        if skipped_questions:
            logger.info(f"  跳过 {len(skipped_questions)} 个已有问题")

        if not new_questions:
            logger.info(" 所有问题均已存在，无需处理")
            return [], skipped_questions, False

        logger.info(f"  需要处理 {len(new_questions)} 个新问题")
        return new_questions, skipped_questions, True
    else:
        # override=True 或无现有文件: 处理所有问题
        questions_to_process = questions
        if existing_answers:
            logger.info(f"  覆盖模式：将重新处理所有 {len(questions)} 个问题")
        else:
            logger.info(f"  新文件模式：处理所有 {len(questions)} 个问题")
        return questions_to_process, [], True


def validate_and_repair_existing_file(
    output_path: Path, questions: List[str], stock: str, provider_name: str
) -> bool:
    """验证并修复现有文件（当所有问题都已存在时）。

    Args:
        output_path: 输出文件路径
        questions: 配置文件中的所有问题
        stock: 股票名称
        provider_name: LLM 提供商名称

    Returns:
        验证是否成功
    """
    try:
        llm_provider = LLMProvider(provider_name=provider_name, company_name=stock)
        answer_generator = AnswerGenerator()
        qa_engine = QAEngine(llm_provider, answer_generator)

        repair_result = validate_and_repair_output(
            output_path=output_path,
            config_questions=questions,
            qa_engine=qa_engine,
            max_retries=DEFAULT_MAX_RETRIES,
            create_backup=True,
        )

        log_repair_result(repair_result)
        return True

    except RuntimeError as e:
        logger.error(f"  [验证] 验证/修复过程出错: {e}")
        logger.warning(f"  [验证] 输出文件可能存在不一致，请手动检查: {output_path}")
        return False


def log_repair_result(repair_result: RepairResult) -> None:
    """记录验证和修复结果。

    Args:
        repair_result: 验证和修复结果
    """
    if repair_result.validation_result.total_issues > 0:
        logger.info(f"  [验证] 发现问题: {repair_result.validation_result.total_issues} 个")

        if repair_result.validation_result.missing_questions:
            logger.info(f"    - 缺失: {len(repair_result.validation_result.missing_questions)} 个")
            logger.info(f"    - 已修复: {len(repair_result.repaired_questions)} 个")
            if repair_result.failed_questions:
                logger.warning(f"    - 修复失败: {len(repair_result.failed_questions)} 个")

        if repair_result.validation_result.extra_questions:
            logger.info(
                f"    - 多余: {len(repair_result.validation_result.extra_questions)} 个 (已移除)"
            )
            if repair_result.backup_created:
                logger.info("    - 备份: %s", repair_result.backup_path)

        if repair_result.validation_result.incomplete_answers:
            logger.info(
                f"    - 不完整答案: {len(repair_result.validation_result.incomplete_answers)} 个"
            )
    else:
        logger.info("  [验证] [OK] 验证通过，所有问题完整匹配")


def process_single_stock_with_retry(
    stock: str,
    questions_to_process: List[str],
    provider_name: str,
    output_path: Path,
    existing_answers: Optional[Dict[str, Any]],
    override: bool,
    max_retries: int,
    all_questions: List[str],
) -> Tuple[bool, Optional[Exception]]:
    """处理单个股票（带重试机制）。

    Args:
        stock: 股票名称
        questions_to_process: 需要处理的问题列表
        provider_name: LLM 提供商名称
        output_path: 输出文件路径
        existing_answers: 现有答案字典
        override: 是否覆盖模式
        max_retries: 最大重试次数
        all_questions: 所有问题的列表（用于验证）

    Returns:
        (是否成功, 最后的错误)
    """
    for retry in range(max_retries):
        try:
            # 初始化 LLM 提供者和 QA 引擎
            llm_provider = LLMProvider(provider_name=provider_name, company_name=stock)

            answer_generator = AnswerGenerator()
            qa_engine = QAEngine(llm_provider, answer_generator)

            # 处理问题
            batch_result = qa_engine.process_questions(questions_to_process)

            # 如果存在现有答案且不是覆盖模式，需要合并结果
            if existing_answers and not override:
                batch_result = merge_existing_answers(batch_result, existing_answers, qa_engine)

            # 输出结果
            qa_engine.output_results(batch_result, str(output_path))

            # 验证并修复输出文件
            validate_and_repair_output_file(output_path, all_questions, qa_engine)

            # 记录成功
            log_success_result(stock, output_path, batch_result, questions_to_process)
            return True, None

        except (ValueError, KeyError) as e:
            if retry < max_retries - 1:
                wait_time = (retry + 1) * DEFAULT_RETRY_DELAY
                logger.warning(f"  处理失败 (尝试 {retry + 1}/{max_retries}): {e}")
                logger.info("  等待 %s 秒后重试...", wait_time)
                time.sleep(wait_time)
            else:
                logger.error(f"  [ERROR] {stock} 处理失败，已重试 {max_retries} 次")
                return False, e

    return False, None


def merge_existing_answers(
    batch_result: Any, existing_answers: Dict[str, Any], qa_engine: QAEngine
) -> Any:
    """合并现有答案到批处理结果。

    Args:
        batch_result: 批处理结果对象
        existing_answers: 现有答案字典
        qa_engine: QA 引擎

    Returns:
        合并后的批处理结果
    """
    from src.core.models import Question, Answer, QAResult

    for question_text, answer_data in existing_answers.items():
        question = Question(text=question_text)
        answer = Answer(
            text=answer_data.get("description", ""), score=answer_data.get("score", DEFAULT_SCORE)
        )
        result = QAResult(question=question, answer=answer, metadata={"source": "existing"})
        batch_result.add_result(result)

    logger.info(f"  合并了 {len(existing_answers)} 个现有答案")
    return batch_result


def validate_and_repair_output_file(
    output_path: Path, questions: List[str], qa_engine: QAEngine
) -> bool:
    """验证并修复输出文件。

    Args:
        output_path: 输出文件路径
        questions: 配置文件中的所有问题
        qa_engine: QA 引擎

    Returns:
        验证是否成功
    """
    try:
        repair_result = validate_and_repair_output(
            output_path=output_path,
            config_questions=questions,
            qa_engine=qa_engine,
            max_retries=DEFAULT_MAX_RETRIES,
            create_backup=True,
        )

        log_repair_result(repair_result)
        return True

    except RuntimeError as e:
        logger.error(f"  [验证] 验证/修复过程出错: {e}")
        logger.warning(f"  [验证] 输出文件可能存在不一致，请手动检查: {output_path}")
        return False


def log_success_result(
    stock: str, output_path: Path, batch_result: Any, questions_to_process: List[str]
) -> None:
    """记录成功处理的结果。

    Args:
        stock: 股票名称
        output_path: 输出文件路径
        batch_result: 批处理结果
        questions_to_process: 处理的问题列表
    """
    logger.info(f"\n[OK] {stock} 处理成功！")
    logger.info("  输出文件: %s", output_path)

    # 显示简要统计
    stats = {
        "total_questions": len(batch_result.results),
        "success_count": len(batch_result.results),
        "error_count": 0,
    }
    avg_score = sum(r.answer.score for r in batch_result.results) / len(batch_result.results)
    logger.info("  平均评分: %.1f/10", avg_score)
    logger.info(f"  处理问题数: {stats['total_questions']} (新处理: {len(questions_to_process)})")
