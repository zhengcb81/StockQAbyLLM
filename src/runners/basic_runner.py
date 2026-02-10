#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基础运行器。

提供基础模式的运行逻辑，不依赖LLM API。
"""

import sys
from pathlib import Path
from typing import Optional

from src.core.qa_engine import QAEngine
from src.config.config_manager import ConfigManager
from src.services.search_service import SearchService
from src.services.answer_generator import AnswerGenerator
from src.core.exceptions import StockQAError
from src.utils.logger import get_logger


class BasicRunner:
    """基础运行器类。

    负责执行基础模式的处理逻辑。
    """

    def __init__(self, verbose: bool = False):
        """初始化基础运行器。

        Args:
            verbose: 是否启用详细日志输出
        """
        self.logger = get_logger(__name__, verbose=verbose)

    def run(self, config_path: str, output_path: Optional[str] = None) -> int:
        """运行基础模式处理。

        Args:
            config_path: 配置文件路径
            output_path: 输出文件路径（可选）

        Returns:
            退出码（0 表示成功，1 表示失败）
        """
        self.logger.info("=" * 60)
        self.logger.info("StockQAbyLLM 基础模式启动")
        self.logger.info("=" * 60)

        try:
            # 加载配置
            self.logger.info(f"正在从配置文件加载问题: {config_path}")
            config_manager = ConfigManager(config_path)
            questions = config_manager.load_questions()

            # 验证问题
            config_manager.validate_questions(questions)

            # 初始化 QAEngine
            search_service = SearchService()
            answer_generator = AnswerGenerator()
            qa_engine = QAEngine(search_provider=search_service, answer_generator=answer_generator)

            # 处理问题
            batch_result = qa_engine.process_questions(questions)

            # 输出结果
            qa_engine.output_results(batch_result, output_path)

            # 显示统计信息
            stats = qa_engine.get_statistics(batch_result)
            self.logger.info(f"处理统计: {stats}")

            self.logger.info("=" * 60)
            self.logger.info("基础模式运行成功完成")
            self.logger.info("=" * 60)

            return 0

        except StockQAError as e:
            self.logger.error(f"系统错误: {e}")
            print(f"\n错误: {e}", file=sys.stderr)
            return 1

        except KeyboardInterrupt:
            self.logger.info("用户中断执行")
            print("\n\n操作已取消", file=sys.stderr)
            return 130  # 标准的 Unix 退出码 128 + SIGINT(2)

        except RuntimeError as e:
            self.logger.error(f"未预期的错误: {e}", exc_info=True)
            print(f"\n未预期的错误: {e}", file=sys.stderr)
            return 1


def main() -> int:
    """基础运行器命令行入口。

    用于独立运行基础模式。

    Returns:
        退出码（0 表示成功，1 表示失败）
    """
    import argparse
    from src.config.settings import DEFAULT_CONFIG_FILE

    parser = argparse.ArgumentParser(
        description="StockQAbyLLM - 基础模式", formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG_FILE, help=f"配置文件路径（默认: {DEFAULT_CONFIG_FILE}）"
    )
    parser.add_argument("--output", help="输出文件路径（可选，默认输出到控制台）")
    parser.add_argument("--verbose", "-v", action="store_true", help="启用详细日志输出")

    args = parser.parse_args()

    runner = BasicRunner(verbose=args.verbose)
    return runner.run(args.config, args.output)


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
