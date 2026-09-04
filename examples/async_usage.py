#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""异步处理示例：使用异步运行器处理问题。

演示如何使用异步方式提高处理效率。
"""

import asyncio
import os
import sys

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path

from src.config.config_provider import ConfigProvider
from src.core.models import Question
from src.runners.llm_runner import LLMRunner


async def process_single_async():
    """异步处理单个问题。"""
    print("=" * 60)
    print("StockQAbyLLM - 异步处理示例（单个问题）")
    print("=" * 60)
    print()

    # 1. 创建配置
    config = ConfigProvider.get_config(
        api_key="your-api-key-here",  # 替换为实际的 API 密钥
        model="gpt-4o-mini",
        base_url="https://api.openai.com/v1/chat/completions",
    )

    # 2. 创建问题
    question = Question("什么是异步编程？它的优势是什么？")

    print(f"问题: {question.text}")
    print()

    # 3. 创建异步运行器并处理
    runner = LLMRunner(config)
    result = await runner.process_async(question)

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


async def process_batch_async():
    """异步批量处理多个问题。"""
    print("=" * 60)
    print("StockQAbyLLM - 异步批量处理示例")
    print("=" * 60)
    print()

    # 1. 创建配置
    config = ConfigProvider.get_config(
        api_key="your-api-key-here",  # 替换为实际的 API 密钥
        model="gpt-4o-mini",
        base_url="https://api.openai.com/v1/chat/completions",
    )

    # 2. 创建多个问题
    questions = [
        Question("什么是 Python？"),
        Question("什么是 JavaScript？"),
        Question("什么是 Rust？"),
    ]

    print(f"准备处理 {len(questions)} 个问题...")
    print()

    # 3. 创建异步运行器
    runner = LLMRunner(config)

    # 4. 并发处理所有问题
    import time

    start_time = time.time()

    # 使用 asyncio.gather 并发执行
    tasks = [runner.process_async(q) for q in questions]
    results = await asyncio.gather(*tasks)

    end_time = time.time()
    total_time = end_time - start_time

    # 5. 显示结果
    print("=" * 60)
    print("处理完成!")
    print("=" * 60)
    print()

    for i, (question, result) in enumerate(zip(questions, results), 1):
        print(f"[{i}] {question.text}")
        if result.success:
            print(f"    答案: {result.answer.text[:50]}...")
        else:
            print(f"    失败: {result.error}")
        print()

    print(f"总处理时间: {total_time:.2f}秒")
    print(f"平均每个问题: {total_time/len(questions):.2f}秒")

    print()
    print("=" * 60)
    print("注意: 当前版本为占位符实现，答案为模拟数据。")
    print("      实际使用需要配置真实的 API 密钥。")
    print("=" * 60)


async def main():
    """主函数：演示异步处理。"""
    # 可以选择运行单个或批量示例
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "batch":
        await process_batch_async()
    else:
        await process_single_async()


if __name__ == "__main__":
    asyncio.run(main())
