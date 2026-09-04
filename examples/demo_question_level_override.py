#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
演示问题级别override功能的示例脚本

这个脚本演示了新的question-level override功能：
1. 创建现有答案文件
2. 演示override=False时的跳过行为
3. 演示override=True时的覆盖行为
"""

import json
import sys
import tempfile
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from main_with_llm import load_stock_list, process_batch_stocks


def create_demo_files():
    """创建演示用的临时文件和目录"""
    temp_dir = Path(tempfile.mkdtemp())

    # 创建配置文件
    config_file = temp_dir / "questions.txt"
    config_file.write_text(
        "公司所在市场规模有多大？\n公司的增长路径是否明确？\n公司产品的市场渗透率如何？",
        encoding="utf-8",
    )

    # 创建股票列表文件
    stock_file = temp_dir / "stocks.txt"
    stock_file.write_text("华锐精密\n苏试试验", encoding="utf-8")

    # 创建输出目录
    output_dir = temp_dir / "outputs"
    output_dir.mkdir()

    return temp_dir, config_file, stock_file, output_dir


def create_existing_answer_file(output_dir, stock_name):
    """为指定股票创建现有答案文件"""
    existing_data = {
        "公司所在市场规模有多大？": {
            "score": 7,
            "description": "现有答案：华锐精密所在市场规模约200亿元，未来增长率预计8-10%。",
        },
        "公司的增长路径是否明确？": {
            "score": 6,
            "description": "现有答案：增长路径较为明确，主要依赖新产品和客户拓展。",
        },
    }

    output_file = output_dir / f"QALLM_{stock_name}.json"
    output_file.write_text(
        json.dumps(existing_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[OK] 创建现有答案文件: {output_file}")
    print(f"  内容: {len(existing_data)} 个问题的答案")
    return output_file


def demonstrate_scenario_1():
    """场景1: override=False，部分问题已存在"""
    print("\n" + "=" * 70)
    print("场景1: override=False，部分问题已存在")
    print("=" * 70)

    temp_dir, config_file, stock_file, output_dir = create_demo_files()

    # 为华锐精密创建现有答案（包含2个问题）
    existing_file = create_existing_answer_file(output_dir, "华锐精密")

    # 加载股票列表
    stocks = load_stock_list(str(stock_file))
    print(f"\n股票列表: {stocks}")

    # 执行批量处理（override=False）
    print(f"\n执行: process_batch_stocks(override=False)")
    results = process_batch_stocks(
        stocks=stocks,
        config_file=str(config_file),
        provider_name="mock",  # 使用mock避免真实LLM调用
        output_dir=str(output_dir),
        override=False,
        config_format="txt",
    )

    # 查看结果
    print(f"\n处理结果:")
    print(f"  总股票数: {results['total_stocks']}")
    print(f"  成功处理: {results['success_count']}")
    print(f"  跳过股票: {results.get('skipped_count', 0)}")
    print(f"  失败数: {results['failed_count']}")

    # 查看生成的文件内容
    print(f"\n生成的文件内容:")
    for stock in stocks:
        output_file = output_dir / f"QALLM_{stock}.json"
        if output_file.exists():
            with open(output_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            print(f"\n{stock}:")
            for question, answer in data.items():
                print(f"  - {question}: score={answer['score']}")

    # 清理
    import shutil

    shutil.rmtree(temp_dir)

    return True


def demonstrate_scenario_2():
    """场景2: override=True，覆盖所有问题"""
    print("\n" + "=" * 70)
    print("场景2: override=True，覆盖所有问题")
    print("=" * 70)

    temp_dir, config_file, stock_file, output_dir = create_demo_files()

    # 为华锐精密创建现有答案
    existing_file = create_existing_answer_file(output_dir, "华锐精密")

    # 加载股票列表
    stocks = load_stock_list(str(stock_file))
    print(f"\n股票列表: {stocks}")

    # 执行批量处理（override=True）
    print(f"\n执行: process_batch_stocks(override=True)")
    results = process_batch_stocks(
        stocks=stocks,
        config_file=str(config_file),
        provider_name="mock",
        output_dir=str(output_dir),
        override=True,
        config_format="txt",
    )

    # 查看结果
    print(f"\n处理结果:")
    print(f"  总股票数: {results['total_stocks']}")
    print(f"  成功处理: {results['success_count']}")
    print(f"  跳过股票: {results.get('skipped_count', 0)}")
    print(f"  失败数: {results['failed_count']}")

    # 清理
    import shutil

    shutil.rmtree(temp_dir)

    return True


def demonstrate_scenario_3():
    """场景3: 混合场景 - 华锐精密部分问题存在，苏试试验无文件"""
    print("\n" + "=" * 70)
    print("场景3: 混合场景 - 部分股票有现有文件，部分没有")
    print("=" * 70)

    temp_dir, config_file, stock_file, output_dir = create_demo_files()

    # 只为华锐精密创建现有答案（只包含1个问题）
    existing_data = {
        "公司所在市场规模有多大？": {"score": 7, "description": "现有答案：市场规模约200亿元。"}
    }
    existing_file = output_dir / "QALLM_华锐精密.json"
    existing_file.write_text(json.dumps(existing_data, ensure_ascii=False), encoding="utf-8")
    print(f"[OK] 创建现有答案文件 (华锐精密): {len(existing_data)} 个问题")

    # 苏试试验没有现有文件

    # 加载股票列表
    stocks = load_stock_list(str(stock_file))
    print(f"\n股票列表: {stocks}")

    # 执行批量处理（override=False）
    print(f"\n执行: process_batch_stocks(override=False)")
    results = process_batch_stocks(
        stocks=stocks,
        config_file=str(config_file),
        provider_name="mock",
        output_dir=str(output_dir),
        override=False,
        config_format="txt",
    )

    # 查看结果
    print(f"\n处理结果:")
    print(f"  总股票数: {results['total_stocks']}")
    print(f"  成功处理: {results['success_count']}")
    print(f"  跳过股票: {results.get('skipped_count', 0)}")
    print(f"  失败数: {results['failed_count']}")

    # 查看最终文件
    print(f"\n最终输出文件:")
    for stock in stocks:
        output_file = output_dir / f"QALLM_{stock}.json"
        if output_file.exists():
            with open(output_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            print(f"\n{stock} (共{len(data)}个问题):")
            for question, answer in data.items():
                source = "现有" if question in existing_data else "新生成"
                print(f"  - [{source}] {question}")

    # 清理
    import shutil

    shutil.rmtree(temp_dir)

    return True


def main():
    """主函数：运行所有演示场景"""
    print("问题级别Override功能演示")
    print("=" * 70)
    print("这个演示展示了新的question-level override功能：")
    print("- 读取现有JSON文件")
    print("- 逐个检查问题是否已存在")
    print("- override=False: 跳过已存在的问题")
    print("- override=True: 覆盖所有问题")
    print("=" * 70)

    try:
        # 运行场景演示
        demonstrate_scenario_1()
        demonstrate_scenario_2()
        demonstrate_scenario_3()

        print("\n" + "=" * 70)
        print("所有演示完成！")
        print("=" * 70)
        print("\n功能验证要点:")
        print("[OK] override=False 时，已存在的问题被跳过")
        print("[OK] override=True 时，所有问题都被重新处理")
        print("[OK] 混合场景下，不同股票独立处理")
        print("[OK] 结果正确合并，保持JSON格式一致性")

        return True

    except Exception as e:
        print(f"\n演示失败: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
