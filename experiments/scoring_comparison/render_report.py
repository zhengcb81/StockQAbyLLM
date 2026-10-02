#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把对比实验结果渲染成 HTML 可视化报告。

读取 run_comparison.py 产出的 JSON，生成一个自带 Chart.js 的静态页面，
包含：各模型分值分布（箱线散点）、每题分差热力对比、两两差异汇总。

用法：
    python experiments/scoring_comparison/render_report.py
    python experiments/scoring_comparison/render_report.py --input results/jev_vs_llm_raw.json
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

EXPERIMENT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = EXPERIMENT_DIR / "results" / "jev_vs_llm_raw.json"
DEFAULT_OUTPUT = EXPERIMENT_DIR / "results" / "jev_vs_llm_report.html"

MODEL_COLORS = [
    "#534AB7",
    "#1D9E75",
    "#D85A30",
    "#378ADD",
    "#BA7517",
    "#D4537E",
    "#639922",
    "#888780",
]


def build_chart_data(report: Dict[str, Any]) -> Dict[str, Any]:
    """构造图表所需的数据结构。"""
    records = report["records"]
    stats = report["statistics"]
    models = list(stats.keys())

    labels = [f"{r['stock']}·{r.get('question_label', r.get('question_key', ''))}" for r in records]

    datasets = []
    for i, m in enumerate(models):
        data = [r["scores"].get(m, {}).get("normalized") for r in records]
        datasets.append(
            {
                "label": m,
                "data": data,
                "backgroundColor": MODEL_COLORS[i % len(MODEL_COLORS)],
                "borderColor": MODEL_COLORS[i % len(MODEL_COLORS)],
            }
        )

    return {"labels": labels, "datasets": datasets, "models": models}


def render_html(report: Dict[str, Any]) -> str:
    """生成 HTML 报告字符串。"""
    chart_data = build_chart_data(report)
    stats = report["statistics"]
    meta = report["meta"]

    stat_rows = ""
    for m, s in stats.items():
        stat_rows += (
            f"<tr><td class='mono'>{m}</td><td>{s['n']}</td>"
            f"<td>{s['mean']:.3f}</td><td>{s['std']:.3f}</td>"
            f"<td>{s['min']:.3f}</td><td>{s['max']:.3f}</td></tr>\n"
        )

    pair_rows = ""
    for key, v in report.get("pairwise", {}).items():
        pair_rows += (
            f"<tr><td>{key}</td><td>{v['n']}</td>"
            f"<td>{v['mean_diff']:+.3f}</td><td>{v['mean_abs_diff']:.3f}</td>"
            f"<td>{v['max_abs_diff']:.3f}</td></tr>\n"
        )
    if not pair_rows:
        pair_rows = "<tr><td colspan='5' class='muted'>无有效样本对</td></tr>"

    div_rows = ""
    for d in report.get("divergences", [])[:10]:
        cells = ""
        for m in stats:
            v = d["scores"].get(m)
            cells += f"<td>{f'{v:.2f}' if v is not None else '-'}</td>"
        div_rows += (
            f"<tr><td>{d['stock']}</td>"
            f"<td class='mono'>{d.get('question_label', d.get('question_key', ''))}</td>"
            f"<td>{d['spread']:.3f}</td>{cells}</tr>\n"
        )
    if not div_rows:
        div_rows = "<tr><td colspan='3' class='muted'>无有效样本</td></tr>"

    div_header = "".join(f"<th>{m}</th>" for m in stats)
    chart_json = json.dumps(chart_data, ensure_ascii=False)
    colors_json = json.dumps(MODEL_COLORS[: len(stats)], ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Jev vs LLM 打分校准对比</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 32px 40px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                 "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    background: #fafaf8; color: #1a1a18;
    line-height: 1.6;
  }}
  h1 {{ font-size: 22px; font-weight: 500; margin: 0 0 6px; }}
  h2 {{ font-size: 16px; font-weight: 500; margin: 32px 0 12px;
        padding-bottom: 8px; border-bottom: 1px solid #e5e3dd; }}
  .sub {{ color: #6b6a64; font-size: 13px; margin-bottom: 24px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
            gap: 12px; margin-bottom: 8px; }}
  .card {{ background: #fff; border-radius: 10px; padding: 14px 16px;
           border: 1px solid #e5e3dd; }}
  .card .lbl {{ font-size: 12px; color: #6b6a64; margin-bottom: 4px; }}
  .card .val {{ font-size: 22px; font-weight: 500; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff;
           border-radius: 10px; overflow: hidden; border: 1px solid #e5e3dd;
           font-size: 13px; }}
  th {{ background: #f4f3ef; text-align: left; padding: 10px 12px;
        font-weight: 500; font-size: 12px; color: #444; }}
  td {{ padding: 9px 12px; border-top: 1px solid #f0efea; }}
  .mono {{ font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; }}
  .muted {{ color: #999; font-style: italic; }}
  .chart-wrap {{ background: #fff; border-radius: 10px; padding: 16px;
                 border: 1px solid #e5e3dd; margin-bottom: 8px; }}
  .note {{ font-size: 12px; color: #6b6a64; background: #f4f3ef;
           border-radius: 8px; padding: 12px 14px; margin-top: 8px; }}
  .legend {{ display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px;
             color: #6b6a64; margin-bottom: 10px; }}
  .legend span {{ display: flex; align-items: center; gap: 5px; }}
  .dot {{ width: 10px; height: 10px; border-radius: 2px; display: inline-block; }}
</style>
</head>
<body>
  <h1>Jev vs LLM 打分校准对比</h1>
  <div class="sub">
    测试集：{", ".join(meta.get("stocks", []))} ·
    {len(meta.get("question_keys", []))} 个维度问题 ·
    题目原文来自 config.json ·
    分数全部归一化到 0-1（数值越高越好）
  </div>

  <div class="cards">
    <div class="card"><div class="lbl">对比模型数</div><div class="val">{len(stats)}</div></div>
    <div class="card"><div class="lbl">样本记录数</div><div class="val">{len(report["records"])}</div></div>
    <div class="card"><div class="lbl">Jev 档位数</div><div class="val">{meta.get("jev_levels", "-")}</div></div>
    <div class="card"><div class="lbl">Jev 可用</div><div class="val">{"是" if meta.get("jev_available") else "否"}</div></div>
  </div>

  <h2>各模型每题打分对比</h2>
  <div class="chart-wrap">
    <div class="legend" id="legend"></div>
    <div style="position: relative; width: 100%; height: 380px;">
      <canvas id="scoreChart" role="img"
        aria-label="各模型在每道题上的归一化打分对比">各模型打分对比图</canvas>
    </div>
  </div>
  <div class="note">
    每根柱子代表一个模型在某道题上的归一化分数。柱子高度差异越大，说明模型间分歧越大。
  </div>

  <h2>模型汇总统计</h2>
  <table>
    <thead><tr><th>模型</th><th>样本数</th><th>均值</th><th>标准差</th><th>最小值</th><th>最大值</th></tr></thead>
    <tbody>{stat_rows}</tbody>
  </table>
  <div class="note">
    标准差反映该模型在跨题打分上的波动性。Jev 的置信度（confidence）与标准差不同：
    前者是单题内部标签分布的一致性，后者是跨题的分值离散度。
  </div>

  <h2>两两分差</h2>
  <table>
    <thead><tr><th>对比</th><th>样本数</th><th>平均差</th><th>平均绝对差</th><th>最大绝对差</th></tr></thead>
    <tbody>{pair_rows}</tbody>
  </table>

  <h2>分歧最大的题目 Top 10</h2>
  <table>
    <thead><tr><th>股票</th><th>问题</th><th>极差</th>{div_header}</tr></thead>
    <tbody>{div_rows}</tbody>
  </table>

<script>
const chartData = {chart_json};
const modelColors = {colors_json};

const legendEl = document.getElementById("legend");
chartData.models.forEach((m, i) => {{
  const s = document.createElement("span");
  s.innerHTML = '<span class="dot" style="background:' + modelColors[i] + '"></span>' + m;
  legendEl.appendChild(s);
}});

new Chart(document.getElementById("scoreChart"), {{
  type: "bar",
  data: {{
    labels: chartData.labels,
    datasets: chartData.datasets.map(d => ({{
      label: d.label,
      data: d.data,
      backgroundColor: d.backgroundColor,
      borderRadius: 3
    }}))
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      y: {{ min: 0, max: 1, title: {{ display: true, text: "归一化分数 (0-1)" }} }},
      x: {{ ticks: {{ autoSkip: false, maxRotation: 45, minRotation: 30, font: {{ size: 10 }} }} }}
    }}
  }}
}});
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="渲染对比实验 HTML 报告")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="原始结果 JSON 路径")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="HTML 输出路径")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"[错误] 输入文件不存在: {input_path}")
        print("请先运行 run_comparison.py 生成结果。")
        return 1

    with open(input_path, "r", encoding="utf-8") as f:
        report = json.load(f)

    html = render_html(report)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"报告已生成: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
