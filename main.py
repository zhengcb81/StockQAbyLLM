#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""StockQAbyLLM - 股票问答系统

基于大语言模型的问答系统，支持从配置文件批量处理问题并生成答案。
提供两种运行模式：基础模式 (basic) 和 LLM模式 (llm)。
"""

import sys
import argparse
from pathlib import Path

# 添加 src 到 Python 路径
sys.path.insert(0, str(Path(__file__).parent / "src"))

# pylint: disable=wrong-import-position
from src.config.settings import DEFAULT_CONFIG_FILE
from src.utils.logger import get_logger


def main() -> int:
    """主入口函数。

    Returns:
        退出码（0 表示成功，1 表示失败）
    """
    # 解析命令行参数
    parser = argparse.ArgumentParser(
        description="StockQAbyLLM - 股票问答系统", formatter_class=argparse.RawDescriptionHelpFormatter
    )

    # 模式选择
    parser.add_argument(
        "--mode",
        choices=["basic", "llm"],
        default="basic",
        help="运行模式：basic（基础模式，不依赖LLM API）或 llm（LLM模式，使用LLM API分析）（默认: basic）",
    )

    # 通用参数
    parser.add_argument("--config", default=DEFAULT_CONFIG_FILE, help=f"配置文件路径（默认: {DEFAULT_CONFIG_FILE}）")
    parser.add_argument("--output", help="输出文件路径（可选，默认输出到控制台）")
    parser.add_argument("--verbose", "-v", action="store_true", help="启用详细日志输出")

    # LLM模式特定参数
    llm_group = parser.add_argument_group("LLM模式参数")
    llm_group.add_argument("--company", "-c", type=str, help="要分析的公司名称（如：海康威视、腾讯控股等），仅LLM模式有效")
    llm_group.add_argument(
        "--provider",
        "-p",
        type=str,
        default=None,
        help="指定使用的 LLM 提供商（如：deepseek, minimax, glm），默认使用配置文件中的默认提供商，仅LLM模式有效",
    )
    llm_group.add_argument(
        "--batch", "-b", type=str, metavar="FILE", help="批量处理模式：从文件读取股票列表（每行一个股票名称），仅LLM模式有效"
    )
    llm_group.add_argument("--override", action="store_true", help="覆盖已存在的输出文件（默认：跳过已存在的文件），仅LLM模式有效")
    llm_group.add_argument(
        "--config-format",
        type=str,
        choices=["json", "txt"],
        default="json",
        help="指定配置文件格式（json 或 txt，默认：json），仅LLM模式有效",
    )

    args = parser.parse_args()

    # 初始化日志系统
    logger = get_logger(__name__, verbose=args.verbose)
    logger.info("=" * 60)
    logger.info("StockQAbyLLM 系统启动 - 模式: %s", args.mode)
    logger.info("=" * 60)

    try:
        if args.mode == "basic":
            # 基础模式
            from src.runners.basic_runner import BasicRunner

            basic_runner = BasicRunner(verbose=args.verbose)
            return basic_runner.run(config_path=args.config, output_path=args.output)

        if args.mode == "llm":
            # LLM模式
            from src.runners.llm_runner import LLMRunner

            llm_runner = LLMRunner(verbose=args.verbose)

            # 验证LLM模式特定参数
            if args.company and args.batch:
                logger.error("不能同时使用 --company 和 --batch 参数")
                print("\n错误: 不能同时使用 --company 和 --batch 参数", file=sys.stderr)
                return 1

            if not args.company and not args.batch:
                logger.error("LLM模式必须指定 --company 或 --batch 参数")
                print("\n错误: LLM模式必须指定 --company 或 --batch 参数", file=sys.stderr)
                print("\n使用示例:")
                print('  python main.py --mode llm --company "海康威视"')
                print("  python main.py --mode llm --batch stocks.txt")
                return 1

            return llm_runner.run(
                company=args.company,
                batch_file=args.batch,
                provider=args.provider,
                config=args.config,
                output=args.output,
                override=args.override,
                config_format=args.config_format,
            )

        logger.error("未知模式: %s", args.mode)
        return 1

    except KeyboardInterrupt:
        logger.info("用户中断执行")
        print("\n\n操作已取消", file=sys.stderr)
        return 130  # 标准的 Unix 退出码 128 + SIGINT(2)

    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("未预期的错误: %s", e, exc_info=True)
        print(f"\n未预期的错误: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
