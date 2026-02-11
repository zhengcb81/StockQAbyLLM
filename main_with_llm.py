#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主程序：使用LLM API分析公司。

该程序使用LLM API来分析任意公司，支持命令行参数配置。
完全通用，不硬编码任何公司特定信息。
"""

import sys
import argparse
import time
from pathlib import Path
from typing import List, Dict, Any

# 添加 src 到 Python 路径
sys.path.insert(0, str(Path(__file__).parent / "src"))

# pylint: disable=wrong-import-position
from src.utils.logger import get_logger
from src.cli.batch_processor import (
    load_questions,
    load_existing_answers,
    calculate_questions_to_process,
    validate_and_repair_existing_file,
    process_single_stock_with_retry,
)

logger = get_logger(__name__)


def load_stock_list(file_path: str) -> List[str]:
    """从文件加载股票列表。

    Args:
        file_path: 股票列表文件路径

    Returns:
        股票名称列表

    Raises:
        FileNotFoundError: 文件不存在
        ValueError: 文件为空或格式错误
    """
    logger.info("正在加载股票列表: %s", file_path)

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            stocks = [line.strip() for line in f if line.strip()]

        if not stocks:
            raise ValueError(f"股票列表文件为空: {file_path}")

        logger.info("成功加载 %d 个股票: %s", len(stocks), ", ".join(stocks))
        return stocks

    except FileNotFoundError:
        logger.error("股票列表文件不存在: %s", file_path)
        raise
    except OSError as e:
        logger.error("加载股票列表失败: %s", e)
        raise


def process_batch_stocks(
    stocks: List[str],
    config_file: str,
    provider_name: str,
    output_dir: str,
    max_retries: int = 3,
    override: bool = False,
    config_format: str = "json",
) -> Dict[str, Any]:
    """批量处理多个股票（问题级别override）。

    Args:
        stocks: 股票名称列表
        config_file: 问题配置文件路径
        provider_name: LLM 提供商名称
        output_dir: 输出目录路径
        max_retries: 每个股票的最大重试次数
        override: 是否覆盖已存在的问题答案（默认：False，跳过已存在的问题）
        config_format: 配置文件格式（json 或 txt，默认：json）

    Returns:
        处理结果统计字典
    """
    start_time = time.time()
    results: Dict[str, Any] = {
        "total_stocks": len(stocks),
        "success_count": 0,
        "failed_count": 0,
        "failed_stocks": [],
        "output_files": [],
    }

    logger.info("\n%s", "=" * 70)
    logger.info("开始批量处理 %d 个股票", len(stocks))
    logger.info("%s\n", "=" * 70)

    # 加载问题（所有股票共享）
    questions = load_questions(config_file, config_format)

    # 依次处理每个股票
    for idx, stock in enumerate(stocks, 1):
        logger.info("\n%s", "=" * 70)
        logger.info("[%d/%d] 正在处理: %s", idx, len(stocks), stock)
        logger.info("%s", "=" * 70)

        # 检查输出文件是否已存在，读取现有答案
        output_filename = f"QALLM_{stock}.json"
        output_path = Path(output_dir) / output_filename
        existing_answers = load_existing_answers(output_path)

        # 计算需要处理的问题
        questions_to_process, _, needs_processing = calculate_questions_to_process(
            questions, existing_answers, override
        )

        if not needs_processing:
            results["skipped_count"] = results.get("skipped_count", 0) + 1
            # 验证现有文件
            validate_and_repair_existing_file(output_path, questions, stock, provider_name)
            continue

        # 处理股票（带重试）
        success, error = process_single_stock_with_retry(
            stock=stock,
            questions_to_process=questions_to_process,
            provider_name=provider_name,
            output_path=output_path,
            existing_answers=existing_answers,
            override=override,
            max_retries=max_retries,
            all_questions=questions,
        )

        if success:
            results["success_count"] += 1
            results["output_files"].append(str(output_path))
        else:
            results["failed_count"] += 1
            results["failed_stocks"].append(
                {"stock": stock, "error": str(error) if error else "Unknown error"}
            )

    # 显示最终统计
    log_final_results(results, start_time, output_dir)
    return results


def log_final_results(results: Dict[str, Any], start_time: float, output_dir: str) -> None:
    """记录最终结果统计。

    Args:
        results: 结果字典
        start_time: 开始时间
        output_dir: 输出目录
    """
    elapsed_time = time.time() - start_time
    logger.info("\n%s", "=" * 70)
    logger.info("批量处理完成")
    logger.info("%s", "=" * 70)
    logger.info("总股票数: %d", results["total_stocks"])
    logger.info("成功处理: %d", results["success_count"])
    logger.info("处理失败: %d", results["failed_count"])
    if "skipped_count" in results:
        logger.info("已跳过: %d", results["skipped_count"])
    logger.info("总耗时: %.1f 秒", elapsed_time)

    if results["failed_stocks"]:
        logger.warning("\n失败的股票:")
        for item in results["failed_stocks"]:
            logger.warning("  - %s: %s", item["stock"], item["error"])

    logger.info("\n输出文件位置: %s", output_dir)


def main():
    """主函数（兼容性包装器）。"""
    # 解析命令行参数
    parser = argparse.ArgumentParser(
        description="使用LLM分析公司股票投资价值",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例用法：
  # 分析海康威视（使用默认配置文件中的 API，JSON格式）
  python main_with_llm.py --company "海康威视"

  # 使用文本配置文件
  python main_with_llm.py --company "海康威视" --config config.txt --config-format txt

  # 分析其他公司
  python main_with_llm.py --company "腾讯控股" --config questions.txt --config-format txt --output tencent_analysis.json

  # 指定使用特定的 LLM 提供商
  python main_with_llm.py --company "阿里巴巴" --provider minimax

  # 批量处理股票列表（跳过已存在的输出文件，默认JSON配置）
  python main_with_llm.py --batch input_stocks.txt

  # 批量处理并覆盖已存在的输出文件，使用文本配置
  python main_with_llm.py --batch input_stocks.txt --override --config-format txt

配置文件：
  API 密钥请在 llm_apis.json 中配置
        """,
    )

    parser.add_argument(
        "--company", "-c", type=str, help="要分析的公司名称（如：海康威视、腾讯控股等）"
    )

    parser.add_argument(
        "--provider",
        "-p",
        type=str,
        default=None,
        help="指定使用的 LLM 提供商（如：deepseek, minimax, glm），默认使用配置文件中的默认提供商",
    )

    parser.add_argument(
        "--config",
        "-f",
        type=str,
        default="config.json",
        help="问题配置文件路径（默认：config.json）",
    )

    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default="outputs/analysis_result.json",
        help="输出JSON文件路径（默认：outputs/analysis_result.json）",
    )

    parser.add_argument(
        "--batch",
        "-b",
        type=str,
        metavar="FILE",
        help="批量处理模式：从文件读取股票列表（每行一个股票名称）",
    )

    parser.add_argument(
        "--override", action="store_true", help="覆盖已存在的输出文件（默认：跳过已存在的文件）"
    )

    parser.add_argument(
        "--config-format",
        type=str,
        choices=["json", "txt"],
        default="json",
        help="指定配置文件格式（json 或 txt，默认：json）",
    )

    parser.add_argument("--verbose", "-v", action="store_true", help="启用详细日志输出")

    args = parser.parse_args()

    # 验证参数互斥性
    if args.company and args.batch:
        parser.error("不能同时使用 --company 和 --batch 参数")

    if not args.company and not args.batch:
        parser.error("必须指定 --company 或 --batch 参数")

    # 使用新的 LLMRunner
    try:
        from src.runners.llm_runner import LLMRunner

        runner = LLMRunner(verbose=args.verbose)
        return runner.run(
            company=args.company,
            batch_file=args.batch,
            provider=args.provider,
            config=args.config,
            output=args.output,
            override=args.override,
            config_format=args.config_format,
        )
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("运行失败: %s", e, exc_info=True)
        return 1


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
