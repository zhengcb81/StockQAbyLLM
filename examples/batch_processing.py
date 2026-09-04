#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量处理示例：从文件处理多个问题。

演示如何从配置文件批量加载和处理问题。
"""

import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path
from src.runners.basic_runner import BasicRunner
from src.config.config_provider import ConfigProvider
from src.config.config_manager import ConfigManager
from src.utils.console_reporter import ConsoleReporter


def main():
    """主函数：演示批量处理。"""
    print("=" * 60)
    print("StockQAbyLLM - 批量处理示例")
    print("=" * 60)
    print()

    # 1. 创建配置
    config = ConfigProvider.get_config(
        api_key="your-api-key-here",  # 替换为实际的 API 密钥
        model="gpt-4o-mini",
        base_url="https://api.openai.com/v1/chat/completions"
    )

    # 2. 从配置文件加载问题
    # 如果 config_examples/batch_questions.txt 不存在，使用默认问题
    config_file = Path(__file__).parent / "config_examples" / "batch_questions.txt"

    if config_file.exists():
        questions = ConfigManager.load_from_file(str(config_file))
        print(f"从配置文件加载了 {len(questions)} 个问题")
    else:
        # 使用默认问题列表
        from src.core.models import Question
        questions = [
            Question("什么是市盈率？"),
            Question("什么是市净率？"),
            Question("如何分析股票的财务报表？"),
        ]
        print(f"使用默认问题列表 ({len(questions)} 个问题)")

    print()

    # 3. 创建运行器
    runner = BasicRunner(config)

    # 4. 批量处理问题
    print("开始批量处理...")
    print("=" * 60)
    print()

    # 使用进度报告器显示处理进度
    reporter = ConsoleReporter()
    results = []

    for i, question in enumerate(questions, 1):
        print(f"[{i}/{len(questions)}] 处理: {question.text}")
        result = runner.process_single(question)
        results.append(result)

        # 显示结果摘要
        if result.success:
            print(f"  ✓ 成功 - 答案长度: {len(result.answer.text)} 字符")
        else:
            print(f"  ✗ 失败 - {result.error}")
        print()

    # 5. 显示统计信息
    print("=" * 60)
    print("处理完成!")
    print("=" * 60)
    print()

    success_count = sum(1 for r in results if r.success)
    fail_count = len(results) - success_count

    print(f"总问题数: {len(results)}")
    print(f"成功: {success_count}")
    print(f"失败: {fail_count}")
    print()

    # 6. 保存结果到文件
    output_file = Path(__file__).parent / "batch_results.json"
    runner.save_results(results, str(output_file))
    print(f"结果已保存到: {output_file}")

    print()
    print("=" * 60)
    print("注意: 当前版本为占位符实现，答案为模拟数据。")
    print("      实际使用需要配置真实的 API 密钥。")
    print("=" * 60)


if __name__ == "__main__":
    main()
