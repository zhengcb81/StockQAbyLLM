#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Jev vs LLM 打分对比实验主程序。

设计目标：
  同一批「公司 × 维度问题」，分别让 Jev（TypeSafe System One）和
  项目原有的多家 LLM 打分，**只比较分数**，不关心描述文本。

核心可比性保证：
  1. 输入 state 相同（同一公司、同一问题）
  2. 评分锚点相同（都来自 config.json 原题的 1 分/10 分描述）
  3. 分数尺度统一归一化到 0-1：
     - Jev：10 档 criteria，归一化 = raw_score / 9
     - LLM：1-10 分，归一化 = (raw_score - 1) / 9

用法：
    # 完整实验（需要 TYPESAFE_API_KEY 和 llm_apis.json 中的 key）
    python experiments/scoring_comparison/run_comparison.py

    # 只跑 LLM 侧（Jev key 未就绪时）
    python experiments/scoring_comparison/run_comparison.py --skip-jev

    # 只跑 Jev 侧
    python experiments/scoring_comparison/run_comparison.py --only-jev

    # 指定问题/股票子集
    python experiments/scoring_comparison/run_comparison.py --questions market_size,cash_flow
"""

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

EXPERIMENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = EXPERIMENT_DIR.parents[1]

sys.path.insert(0, str(EXPERIMENT_DIR))

from config_loader import resolve_questions  # noqa: E402
from jev_client import JevClient, JevError  # noqa: E402
from llm_scorer import call_llm_provider  # noqa: E402

DEFAULT_TEST_SET = EXPERIMENT_DIR / "test_set.json"
LLM_CONFIG_FILE = PROJECT_ROOT / "llm_apis.json"

JEV_KEY = "jev"


def load_test_set(path: Path) -> Dict[str, Any]:
    """加载实验测试集配置（股票列表 + 题目引用）。"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_llm_providers() -> Dict[str, Dict[str, Any]]:
    """加载 llm_apis.json 中已启用的提供商。"""
    if not LLM_CONFIG_FILE.exists():
        return {}
    with open(LLM_CONFIG_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)
    providers = config.get("providers", {})
    return {name: cfg for name, cfg in providers.items() if cfg.get("enabled", False)}


def run_experiment(
    test_set: Dict[str, Any],
    providers: Dict[str, Dict[str, Any]],
    question_keys: Optional[List[str]] = None,
    stocks: Optional[List[str]] = None,
    skip_jev: bool = False,
    only_jev: bool = False,
    jev_levels: int = 10,
    config_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """执行完整对比实验。

    题目全部来自 config.json 原文，不做任何改写。

    Returns:
        {
            "meta": {...},
            "records": [ {stock, question_key, category, question, scores: {model: {...}}} ],
        }
    """
    stocks = stocks or test_set["stocks"]

    # 从 config.json 解析原始题目
    all_questions = resolve_questions(test_set["questions"], config_path=config_path)
    if question_keys:
        all_questions = [q for q in all_questions if q.key in question_keys]

    jev = JevClient()
    jev_available = not skip_jev and jev.is_configured()
    if not skip_jev and not jev_available:
        print("[警告] TYPESAFE_API_KEY 未配置，将跳过 Jev 侧。")
        print("       申请地址: https://console.typesafe.ai/keys")
        print("       设置方式: set TYPESAFE_API_KEY=你的key  (Windows)")
        print()

    records: List[Dict[str, Any]] = []
    total = len(stocks) * len(all_questions)
    idx = 0

    for stock in stocks:
        for q in all_questions:
            idx += 1
            print(f"[{idx}/{total}] {stock} · {q.category} #{q.index + 1}")

            scores: Dict[str, Dict[str, Any]] = {}

            # ---- Jev 侧（用原题文本）----
            if not skip_jev:
                if jev_available:
                    state = f"公司名称：{stock}"
                    try:
                        r = jev.score(
                            state=state,
                            question=q.raw_text,
                            low_anchor=q.low_anchor,
                            high_anchor=q.high_anchor,
                            levels=jev_levels,
                        )
                        scores[JEV_KEY] = {
                            "raw_score": r["raw_score"],
                            "normalized": r["normalized"],
                            "confidence": r["confidence"],
                            "error": None,
                        }
                        print(
                            f"        Jev: 原始={r['raw_score']:.2f}/{jev_levels - 1}  "
                            f"归一化={r['normalized']:.3f}  "
                            f"置信度={r['confidence']}"
                        )
                    except (JevError, Exception) as e:  # noqa: BLE001
                        scores[JEV_KEY] = {
                            "raw_score": None,
                            "normalized": None,
                            "confidence": None,
                            "error": str(e),
                        }
                        print(f"        Jev: 失败 - {e}")
                else:
                    scores[JEV_KEY] = {
                        "raw_score": None,
                        "normalized": None,
                        "confidence": None,
                        "error": "API key 未配置",
                    }

            # ---- LLM 侧（用原题文本）----
            if not only_jev:
                for pname, pcfg in providers.items():
                    r = call_llm_provider(
                        provider_name=pname,
                        provider_config=pcfg,
                        company=stock,
                        question_raw=q.raw_text,
                    )
                    scores[pname] = {
                        "raw_score": r["raw_score"],
                        "normalized": r["normalized"],
                        "confidence": None,
                        "elapsed": r["elapsed"],
                        "error": r["error"],
                    }
                    if r["error"]:
                        print(f"        {pname}: 失败 - {r['error']}")
                    else:
                        print(
                            f"        {pname}: {r['raw_score']}/10  "
                            f"归一化={r['normalized']:.3f}  ({r['elapsed']}s)"
                        )

            records.append(
                {
                    "stock": stock,
                    "question_key": q.key,
                    "question_label": q.label,
                    "category": q.category,
                    "weight": q.weight,
                    "question": q.raw_text,
                    "scores": scores,
                }
            )
            print()

    return {
        "meta": {
            "stocks": stocks,
            "question_keys": [q.key for q in all_questions],
            "question_labels": [q.label for q in all_questions],
            "jev_levels": jev_levels,
            "jev_available": jev_available,
            "llm_providers": list(providers.keys()) if not only_jev else [],
            "config_source": str(config_path or (PROJECT_ROOT / "config.json")),
        },
        "records": records,
    }


def compute_statistics(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按模型汇总统计。

    Returns:
        {
            model: {
                "n": 有效样本数,
                "mean": 均值,
                "std": 标准差,
                "min": 最小值,
                "max": 最大值,
                "values": [...],
            }
        }
    """
    by_model: Dict[str, List[float]] = {}

    for rec in records:
        for model, s in rec["scores"].items():
            val = s.get("normalized")
            if val is not None:
                by_model.setdefault(model, []).append(val)

    stats: Dict[str, Any] = {}
    for model, values in by_model.items():
        stats[model] = {
            "n": len(values),
            "mean": round(statistics.mean(values), 4),
            "std": round(statistics.pstdev(values), 4) if len(values) > 1 else 0.0,
            "min": round(min(values), 4),
            "max": round(max(values), 4),
            "values": [round(v, 4) for v in values],
        }
    return stats


def compute_pairwise(records: List[Dict[str, Any]], models: List[str]) -> Dict[str, Any]:
    """计算模型两两之间的分差统计（仅在两者都成功的样本上）。"""
    pairwise: Dict[str, Any] = {}

    for i, m1 in enumerate(models):
        for m2 in models[i + 1 :]:
            diffs: List[float] = []
            for rec in records:
                a = rec["scores"].get(m1, {}).get("normalized")
                b = rec["scores"].get(m2, {}).get("normalized")
                if a is not None and b is not None:
                    diffs.append(a - b)
            if diffs:
                key = f"{m1} vs {m2}"
                pairwise[key] = {
                    "n": len(diffs),
                    "mean_diff": round(statistics.mean(diffs), 4),
                    "mean_abs_diff": round(statistics.mean(abs(d) for d in diffs), 4),
                    "max_abs_diff": round(max(abs(d) for d in diffs), 4),
                }
    return pairwise


def build_report(experiment: Dict[str, Any]) -> Dict[str, Any]:
    """构建完整对比报告。"""
    records = experiment["records"]
    models: List[str] = []
    for rec in records:
        for m in rec["scores"]:
            if m not in models:
                models.append(m)

    stats = compute_statistics(records)
    pairwise = compute_pairwise(records, [m for m in models if m in stats])

    # 记录每题的最大分歧（用于发现哪些维度模型间差异最大）
    divergences = []
    for rec in records:
        vals = {
            m: s["normalized"] for m, s in rec["scores"].items() if s.get("normalized") is not None
        }
        if len(vals) > 1:
            divergences.append(
                {
                    "stock": rec["stock"],
                    "question_key": rec.get("question_key", ""),
                    "question_label": rec.get("question_label", ""),
                    "spread": round(max(vals.values()) - min(vals.values()), 4),
                    "scores": vals,
                }
            )
    divergences.sort(key=lambda x: x["spread"], reverse=True)

    return {
        "meta": experiment["meta"],
        "statistics": stats,
        "pairwise": pairwise,
        "divergences": divergences,
        "records": records,
    }


def print_summary(report: Dict[str, Any]) -> None:
    """在控制台打印对比摘要。"""
    stats = report["statistics"]
    models = list(stats.keys())

    print("=" * 72)
    print("打分校准对比结果（归一化到 0-1，数值越高越好）")
    print("=" * 72)

    if not models:
        print("没有任何有效样本。请检查 API key 配置。")
        return

    header = f"{'模型':<12}{'样本':>6}{'均值':>10}{'标准差':>10}{'最小':>10}{'最大':>10}"
    print(header)
    print("-" * 72)
    for m in models:
        s = stats[m]
        print(
            f"{m:<12}{s['n']:>6}{s['mean']:>10.3f}{s['std']:>10.3f}"
            f"{s['min']:>10.3f}{s['max']:>10.3f}"
        )

    if report["pairwise"]:
        print()
        print("两两分差（归一化尺度）")
        print("-" * 72)
        print(f"{'对比':<24}{'样本':>6}{'平均差':>10}{'平均绝对差':>12}{'最大绝对差':>12}")
        for key, v in report["pairwise"].items():
            print(
                f"{key:<24}{v['n']:>6}{v['mean_diff']:>10.3f}"
                f"{v['mean_abs_diff']:>12.3f}{v['max_abs_diff']:>12.3f}"
            )

    if report["divergences"]:
        print()
        print("分歧最大的题目 Top 5")
        print("-" * 72)
        for d in report["divergences"][:5]:
            parts = "  ".join(f"{k}={v:.2f}" for k, v in d["scores"].items())
            print(
                f"{d['stock']} · {d.get('question_label', d.get('question_key'))}  极差={d['spread']:.3f}"
            )
            print(f"    {parts}")

    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Jev vs LLM 打分对比实验",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--test-set",
        default=str(DEFAULT_TEST_SET),
        help="测试集配置文件路径（只含股票列表和题目引用，题目原文从 config.json 读取）",
    )
    parser.add_argument("--config", help="config.json 路径（默认项目根目录下的 config.json）")
    parser.add_argument(
        "--questions",
        help="只跑指定题目 key，逗号分隔（如 市场与增长潜力_0,财务健康与资本结构_0）",
    )
    parser.add_argument("--stocks", help="只跑指定股票，逗号分隔")
    parser.add_argument("--skip-jev", action="store_true", help="跳过 Jev 侧")
    parser.add_argument("--only-jev", action="store_true", help="只跑 Jev 侧")
    parser.add_argument("--jev-levels", type=int, default=10, help="Jev 量表档位数（2-10）")
    parser.add_argument(
        "--output",
        default=str(EXPERIMENT_DIR / "results" / "jev_vs_llm_raw.json"),
        help="原始结果输出路径",
    )
    parser.add_argument(
        "--list-questions", action="store_true", help="列出 config.json 中全部题目及其 key"
    )
    args = parser.parse_args()

    config_path = Path(args.config) if args.config else None

    if args.list_questions:
        from config_loader import load_config_questions

        print("config.json 中的全部题目：")
        print("-" * 72)
        for q in load_config_questions(config_path):
            print(f"  {q.key:<32} {q.category} #{q.index + 1}")
        return 0

    test_set = load_test_set(Path(args.test_set))
    providers = {} if args.only_jev else load_llm_providers()

    question_keys = args.questions.split(",") if args.questions else None
    stocks = args.stocks.split(",") if args.stocks else None

    print(f"测试集配置: {args.test_set}")
    print(f"题目来源: {config_path or (PROJECT_ROOT / 'config.json')}")
    print(f"股票: {stocks or test_set['stocks']}")
    print(f"LLM 提供商: {list(providers.keys()) or '(跳过)'}")
    print(f"Jev 档位数: {args.jev_levels}")
    print()

    experiment = run_experiment(
        test_set=test_set,
        providers=providers,
        question_keys=question_keys,
        stocks=stocks,
        skip_jev=args.skip_jev,
        only_jev=args.only_jev,
        jev_levels=args.jev_levels,
        config_path=config_path,
    )

    report = build_report(experiment)
    print_summary(report)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"原始结果已保存: {output_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
