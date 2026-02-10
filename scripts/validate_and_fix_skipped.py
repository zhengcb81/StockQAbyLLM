#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证和修复所有被跳过的股票输出文件。

该脚本专门用于处理那些所有问题都已存在，但可能有多余问题的输出文件。
"""

import sys
from pathlib import Path

# 添加 src 到 Python 路径
sys.path.insert(0, str(Path(__file__).parent / "src"))

from src.config.json_config_manager import JSONConfigManager
from src.utils.output_validator import validate_and_repair_output
from src.providers.llm_provider import LLMProvider
from src.services.answer_generator import AnswerGenerator
from src.core.qa_engine import QAEngine
from src.utils.logger import get_logger

logger = get_logger(__name__)


def validate_skipped_stocks():
    """验证和修复被跳过的股票文件。"""
    # 加载配置文件
    config_manager = JSONConfigManager("config.json")
    questions = config_manager.load_questions()
    logger.info(f"配置文件包含 {len(questions)} 个问题\n")

    # 需要验证的股票列表（这些股票被跳过了）
    skipped_stocks = [
        "东富龙",
        "苏试试验",
        "中微公司",
        "中密控股",
        "海康威视",
        "新华保险",
        "小米集团",
        "阿里巴巴",
        "特海国际",
        "药明生物",
        "药明康德",
    ]

    logger.info(f"开始验证 {len(skipped_stocks)} 个被跳过的股票...\n")

    for idx, stock in enumerate(skipped_stocks, 1):
        output_path = Path("outputs") / f"QALLM_{stock}.json"

        if not output_path.exists():
            logger.warning(f"[{idx}/{len(skipped_stocks)}] {stock} - 文件不存在，跳过")
            continue

        logger.info(f"[{idx}/{len(skipped_stocks)}] 验证: {stock}")
        logger.info(f"  文件: {output_path}")

        try:
            # 初始化 QA 引擎（用于验证）
            llm_provider = LLMProvider(provider_name=None, company_name=stock)
            answer_generator = AnswerGenerator()
            qa_engine = QAEngine(llm_provider, answer_generator)

            # 验证并修复
            repair_result = validate_and_repair_output(
                output_path=output_path,
                config_questions=questions,
                qa_engine=qa_engine,
                max_retries=3,
                create_backup=True
            )

            # 显示结果
            if repair_result.validation_result.total_issues > 0:
                logger.info(f"  发现问题: {repair_result.validation_result.total_issues} 个")

                if repair_result.validation_result.missing_questions:
                    logger.info(f"    - 缺失: {len(repair_result.validation_result.missing_questions)} 个")
                    if repair_result.repaired_questions:
                        logger.info(f"    - 已修复: {len(repair_result.repaired_questions)} 个")
                    if repair_result.failed_questions:
                        logger.warning(f"    - 修复失败: {len(repair_result.failed_questions)} 个")

                if repair_result.validation_result.extra_questions:
                    logger.info(f"    - 多余: {len(repair_result.validation_result.extra_questions)} 个 (已移除)")
                    if repair_result.backup_created:
                        logger.info(f"    - 备份: {repair_result.backup_path}")

                if repair_result.validation_result.incomplete_answers:
                    logger.info(f"    - 不完整答案: {len(repair_result.validation_result.incomplete_answers)} 个")

                logger.info(f"  修复完成!")
            else:
                logger.info(f"  [OK] 验证通过，所有问题完整匹配")

        except Exception as e:
            logger.error(f"  验证失败: {e}", exc_info=True)

        logger.info("")

    logger.info("=" * 70)
    logger.info("验证完成！")
    logger.info("=" * 70)


if __name__ == "__main__":
    validate_skipped_stocks()
