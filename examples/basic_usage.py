#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基础使用示例：处理单个问题。

这是一个最简单的示例，展示如何使用 StockQAbyLLM 处理单个问题。
"""

import os
import sys

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config.config_provider import ConfigProvider
from src.core.models import Answer, QAResult, Question
from src.runners.basic_runner import BasicRunner


def main():
    """主函数：演示基础用法。"""
    print("=" * 60)
    print("StockQAbyLLM - 基础使用示例")
    print("=" * 60)
    print()

    # 1. 创建配置
    # 注意：实际使用时需要配置真实的 API 密钥
    config = ConfigProvider.get_config(
        api_key="your-api-key-here",  # 替换为实际的 API 密钥
        model="gpt-4o-mini",
        base_url="https://api.openai.com/v1/chat/completions",
    )

    # 2. 创建问题
    question = Question(text="什么是市盈率？它是如何计算的？", category="财务指标")

    print(f"问题: {question.text}")
    print()

    # 3. 创建运行器并处理问题
    # 注意：当前版本为占位符实现，不会实际调用 LLM
    runner = BasicRunner(config)
    result = runner.process_single(question)

    # 4. 显示结果
    print("=" * 60)
    print("处理结果:")
    print("=" * 60)

    if result.success:
        print(f"答案: {result.answer.text}")
        print()
        print(f"使用的模型: {result.metadata.get('model', 'N/A')}")
        print(f"处理时间: {result.metadata.get('processing_time', 'N/A')}秒")
    else:
        print(f"处理失败: {result.error}")

    print()
    print("=" * 60)
    print("注意: 当前版本为占位符实现，答案为模拟数据。")
    print("      实际使用需要配置真实的 API 密钥。")
    print("=" * 60)


if __name__ == "__main__":
    main()
