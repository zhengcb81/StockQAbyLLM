#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM运行器。

提供LLM模式的运行逻辑，使用LLM API进行分析。
"""

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, cast

from src.cli.batch_processor import (
    calculate_questions_to_process,
    load_existing_answers,
    load_questions,
    log_success_result,
    process_single_stock_with_retry,
    validate_and_repair_existing_file,
)
from src.config.config_manager import ConfigManager
from src.config.config_provider import ConfigProvider
from src.config.json_config_manager import JSONConfigManager
from src.core.qa_engine import QAEngine
from src.providers.llm_provider import LLMProvider
from src.services.answer_generator import AnswerGenerator
from src.utils.logger import get_logger

logger = get_logger(__name__)


def load_stock_list(file_path: str) -> list[str]:
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

        logger.info(f"成功加载 {len(stocks)} 个股票: {', '.join(stocks)}")
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
    provider_name: Optional[str],
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
    import time

    start_time = time.time()
    results: Dict[str, Any] = {
        "total_stocks": len(stocks),
        "success_count": 0,
        "failed_count": 0,
        "failed_stocks": [],
        "output_files": [],
        "skipped_count": 0,
    }

    logger.info(f"\n{'=' * 70}")
    logger.info(f"开始批量处理 {len(stocks)} 个股票")
    logger.info(f"{'=' * 70}\n")

    # 加载问题（所有股票共享）
    questions = load_questions(config_file, config_format)

    # 依次处理每个股票
    for idx, stock in enumerate(stocks, 1):
        logger.info(f"\n{'=' * 70}")
        logger.info(f"[{idx}/{len(stocks)}] 正在处理: {stock}")
        logger.info(f"{'=' * 70}")

        # 检查输出文件是否已存在，读取现有答案
        output_filename = f"QALLM_{stock}.json"
        output_path = Path(output_dir) / output_filename
        existing_answers = load_existing_answers(output_path)

        # 计算需要处理的问题
        questions_to_process, skipped_questions, needs_processing = calculate_questions_to_process(
            questions, existing_answers, override
        )

        if not needs_processing:
            results["skipped_count"] = cast(int, results.get("skipped_count", 0)) + 1
            # 验证现有文件
            if provider_name is not None:
                validate_and_repair_existing_file(output_path, questions, stock, provider_name)
            continue

        # 处理股票（带重试）
        if provider_name is None:
            logger.error("provider_name is None, skipping stock")
            results["failed_count"] += 1
            results["failed_stocks"].append({"stock": stock, "error": "provider_name is None"})
            continue

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
            results["success_count"] = cast(int, results.get("success_count", 0)) + 1
            cast(List[str], results.get("output_files", [])).append(str(output_path))
        else:
            results["failed_count"] = cast(int, results.get("failed_count", 0)) + 1
            cast(List[Dict[str, Any]], results.get("failed_stocks", [])).append(
                {"stock": stock, "error": str(error) if error else "Unknown error"}
            )

    # 显示最终统计
    log_final_results(results, start_time, output_dir)
    return results


def log_final_results(results: dict[str, Any], start_time: float, output_dir: str) -> None:
    """记录最终结果统计。

    Args:
        results: 结果字典
        start_time: 开始时间
        output_dir: 输出目录
    """
    import time

    elapsed_time = time.time() - start_time
    logger.info(f"\n{'=' * 70}")
    logger.info("批量处理完成")
    logger.info(f"{'=' * 70}")
    logger.info(f"总股票数: {results['total_stocks']}")
    logger.info(f"成功处理: {results['success_count']}")
    logger.info(f"处理失败: {results['failed_count']}")
    if "skipped_count" in results:
        logger.info(f"已跳过: {results['skipped_count']}")
    logger.info("总耗时: %.1f 秒", elapsed_time)

    if results["failed_stocks"]:
        logger.warning("\n失败的股票:")
        for item in results["failed_stocks"]:
            logger.warning(f"  - {item['stock']}: {item['error']}")

    logger.info("\n输出文件位置: %s", output_dir)


class LLMRunner:
    """LLM运行器类。

    负责执行LLM模式的处理逻辑。
    """

    def __init__(self, verbose: bool = False):
        """初始化LLM运行器。

        Args:
            verbose: 是否启用详细日志输出
        """
        self.logger = get_logger(__name__, verbose=verbose)

    def run(
        self,
        company: Optional[str] = None,
        batch_file: Optional[str] = None,
        provider: Optional[str] = None,
        config: str = "config.json",
        output: str = "outputs/analysis_result.json",
        override: bool = False,
        config_format: str = "json",
    ) -> int:
        """运行LLM模式处理。

        Args:
            company: 公司名称（单股票模式）
            batch_file: 批量处理文件路径（批量模式）
            provider: LLM提供商名称
            config: 配置文件路径
            output: 输出文件路径
            override: 是否覆盖已存在的文件
            config_format: 配置文件格式

        Returns:
            退出码（0 表示成功，1 表示失败）
        """
        # 验证参数互斥性
        if company and batch_file:
            raise ValueError("不能同时使用 company 和 batch_file 参数")

        if not company and not batch_file:
            raise ValueError("必须指定 company 或 batch_file 参数")

        self.logger.info("=" * 70)
        if company:
            self.logger.info("公司分析 - %s", company)
            self.logger.info("=" * 70)
            return self._run_single_company(
                company=company,
                provider=provider,
                config=config,
                output=output,
                config_format=config_format,
            )
        else:
            self.logger.info("批量股票分析模式")
            self.logger.info("=" * 70)
            # batch_file is guaranteed to be non-None here due to the validation above
            return self._run_batch_mode(
                batch_file=cast(str, batch_file),
                provider=provider,
                config=config,
                output=output,
                override=override,
                config_format=config_format,
            )

    def _run_single_company(
        self, company: str, provider: Optional[str], config: str, output: str, config_format: str
    ) -> int:
        """运行单公司处理模式。

        Args:
            company: 公司名称
            provider: LLM提供商名称
            config: 配置文件路径
            output: 输出文件路径
            config_format: 配置文件格式

        Returns:
            退出码
        """
        try:
            # 加载问题
            # 根据配置格式选择配置管理器
            config_manager: ConfigProvider
            if config_format == "json":
                config_manager = JSONConfigManager(config)
            else:
                config_manager = ConfigManager(config)
            questions = config_manager.load_questions()

            self.logger.info(f"\n成功加载 {len(questions)} 个问题")
            self.logger.info(f"配置文件: {config} ({config_format}格式)")
            self.logger.info("输出文件: %s\n", output)

            # 初始化LLM提供者（从配置文件读取 API 密钥）
            # provider is required here since it's a required parameter for the LLM mode
            if provider is None:
                raise ValueError("provider 参数不能为 None")
            llm_provider = LLMProvider(provider_name=provider, company_name=company)

            answer_generator = AnswerGenerator()
            qa_engine = QAEngine(llm_provider, answer_generator)

            # 处理问题
            batch_result = qa_engine.process_questions(questions)

            # 显示结果摘要
            print("\n" + "=" * 70)
            if company:
                print(f"{company} 分析报告")
            else:
                print("分析报告")
            print("=" * 70)

            for i, result in enumerate(batch_result.results, 1):
                question = result.question.text
                # 只显示问题前60个字符，避免太长
                display_question = question if len(question) <= 60 else question[:60] + "..."

                print(f"\n{'─' * 70}")
                print(f"[{i}/{len(questions)}] {display_question}")
                print(f"{'─' * 70}")
                print(f"评分: {result.answer.score}/10")
                # 显示描述的前200个字符
                description_preview = (
                    result.answer.text[:200] + "..."
                    if len(result.answer.text) > 200
                    else result.answer.text
                )
                print(f"\n{description_preview}")

            # 输出完整结果到JSON文件
            qa_engine.output_results(batch_result, output)

            # 显示统计
            stats = qa_engine.get_statistics(batch_result)
            print("\n" + "=" * 70)
            print("分析完成统计")
            print("=" * 70)
            print(f"[OK] 处理问题数: {stats['total_questions']}")
            print(f"[OK] 成功处理: {stats['success_count']}")
            print("[OK] 处理成功率: 100%")
            print(f"[OK] 完整报告已保存到: {output}")

            # 计算平均评分
            if batch_result.results:
                avg_score = sum(r.answer.score for r in batch_result.results) / len(
                    batch_result.results
                )
                print(f"[OK] 平均评分: {avg_score:.1f}/10")

                # 显示评分分布
                score_distribution: Dict[int, int] = {}
                for r in batch_result.results:
                    score = r.answer.score
                    score_distribution[score] = score_distribution.get(score, 0) + 1
                print(f"[OK] 评分分布: {dict(sorted(score_distribution.items()))}")

            self.logger.info("\n所有问题已成功处理！")
            return 0

        except (ConnectionError, TimeoutError, OSError) as e:
            self.logger.error(f"处理失败: {e}", exc_info=True)
            return 1

    def _run_batch_mode(
        self,
        batch_file: str,
        provider: Optional[str],
        config: str,
        output: str,
        override: bool,
        config_format: str,
    ) -> int:
        """运行批量处理模式。

        Args:
            batch_file: 批量处理文件路径
            provider: LLM提供商名称
            config: 配置文件路径
            output: 输出文件路径
            override: 是否覆盖已存在的文件
            config_format: 配置文件格式

        Returns:
            退出码
        """
        try:
            # 加载股票列表
            stocks = load_stock_list(batch_file)

            # 确保输出目录存在
            output_dir = Path(output).parent
            output_dir.mkdir(parents=True, exist_ok=True)

            # 批量处理
            results = process_batch_stocks(
                stocks=stocks,
                config_file=config,
                provider_name=provider,
                output_dir=str(output_dir),
                override=override,
                config_format=config_format,
            )

            # 返回码：如果有失败则返回 1
            return 1 if results["failed_count"] > 0 else 0

        except (ValueError, TypeError, OSError, ConnectionError, TimeoutError) as e:
            self.logger.error(f"批量处理失败: {e}", exc_info=True)
            return 1


def main() -> int:
    """LLM运行器命令行入口。

    用于独立运行LLM模式。

    Returns:
        退出码（0 表示成功，1 表示失败）
    """
    parser = argparse.ArgumentParser(
        description="使用LLM分析公司股票投资价值",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例用法：
  # 分析海康威视（使用默认配置文件中的 API，JSON格式）
  python -m src.runners.llm_runner --company "海康威视"

  # 使用文本配置文件
  python -m src.runners.llm_runner --company "海康威视" --config config.txt --config-format txt

  # 分析其他公司
  python -m src.runners.llm_runner --company "腾讯控股" --config questions.txt \\
         --config-format txt --output tencent_analysis.json

  # 指定使用特定的 LLM 提供商
  python -m src.runners.llm_runner --company "阿里巴巴" --provider minimax

  # 批量处理股票列表（跳过已存在的输出文件，默认JSON配置）
  python -m src.runners.llm_runner --batch input_stocks.txt

  # 批量处理并覆盖已存在的输出文件，使用文本配置
  python -m src.runners.llm_runner --batch input_stocks.txt --override --config-format txt

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


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
