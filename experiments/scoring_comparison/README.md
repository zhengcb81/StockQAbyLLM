# Jev vs LLM 打分对比实验

对比 **Jev（TypeSafe System One）** 与项目原有 LLM 在**同一批「公司 × 维度问题」**上的打分差异。
按需求，**只比较分数，不关心描述文本**。

## 实验设计

### 测试集
- **股票（3 只）**：海康威视、中微公司、东鹏饮料
- **问题（6 个，覆盖 6 个不同维度）**：市场规模、技术护城河、定价能力、管理层诚信、现金流质量、竞争地位
- 共 **18 条样本记录**

### ★ 题目来源：全部取自 config.json 原文

**本实验不发明、不复制、不改写任何题目。**

`test_set.json` 只声明"用哪些股票"和"用哪些题"，题目用 `{category, index}` 引用：

```json
{
  "stocks": ["海康威视", "中微公司", "东鹏饮料"],
  "questions": [
    {"category": "市场与增长潜力", "index": 0},
    {"category": "财务健康与资本结构", "index": 0}
  ]
}
```

运行时由 `config_loader.py` 从 `config.json` 读取题目**原文**（含题目自带的
"评分提示：1 = ...；10 = ..."），原样送给 Jev 和 LLM 两侧。

`index` 是该 category 下 `questions` 数组的下标（从 0 开始）。
用 `--list-questions` 可以查看全部 39 题的 key：

```bash
python experiments/scoring_comparison/run_comparison.py --list-questions
```

### 分数可比性保证（关键）

| 维度 | Jev 侧 | LLM 侧 |
|------|--------|--------|
| 输入 state | 公司名 | 公司名 |
| **提问内容** | **config.json 原题原文** | **config.json 原题原文** |
| 评分锚点 | 由原题 low/high 锚点生成 10 档描述性 criteria | 原题自带的评分提示（1 = ...；10 = ...） |
| 原始尺度 | 0–9 连续值（概率加权） | 1–10 整数 |
| **归一化** | **raw / 9** | **(raw − 1) / 9** |

归一化后两侧都是 **0–1**，数值越高越好。

### Jev 侧的适配说明

Jev 不支持自由文本输出，只有三种原语。本实验使用 **Score 原语**：

- criteria 为 **10 档描述性量表**（官方要求 2–10 档，且警告纯数字等级会失败）
- 量表从"明显偏差，几乎不具备该优势"到"卓越，行业标杆级别"，两端分别绑定原题的 low/high 锚点
- 返回 `score`（概率加权均值）、`probabilities`、`confidence`
- 归一化到 0–1 后与 LLM 对比

## 运行方式

### 1. 配置 API key

**Jev（TypeSafe）**——参考 https://console.typesafe.ai/keys

```bash
# Windows (bash)
export TYPESAFE_API_KEY="你的key"

# Windows (cmd)
set TYPESAFE_API_KEY=你的key
```

**其他 LLM**——编辑项目根目录 `llm_apis.json`，填入对应提供商的 `api_key`
（deepseek / minimax / glm / mimo 已预置，`enabled` 均为 true）。

### 2. 运行实验

```bash
# 完整实验（3 股票 × 6 问题 × 所有模型）
python experiments/scoring_comparison/run_comparison.py

# Jev key 还没到位时，先只跑 LLM 侧
python experiments/scoring_comparison/run_comparison.py --skip-jev

# 只跑 Jev 侧
python experiments/scoring_comparison/run_comparison.py --only-jev

# 自定义子集（适合快速验证，省 API 成本）
# 不加 --questions 就跑 test_set.json 里声明的全部题目
python experiments/scoring_comparison/run_comparison.py --stocks 海康威视 --questions 市场与增长潜力_0

# 查看 config.json 全部题目的 key
python experiments/scoring_comparison/run_comparison.py --list-questions

# 调整 Jev 量表档位数（2-10）
python experiments/scoring_comparison/run_comparison.py --jev-levels 5
```

### 3. 生成可视化报告

```bash
python experiments/scoring_comparison/render_report.py
# 输出: results/jev_vs_llm_report.html
```

## 文件说明

| 文件 | 职责 |
|------|------|
| `config_loader.py` | 从 config.json 解析原题，分离题干与锚点 |
| `test_set.json` | 只声明股票列表 + 题目引用（不含题目文本） |
| `jev_client.py` | Jev API 客户端（Score 原语） |
| `llm_scorer.py` | LLM 侧调用 + score 解析 |
| `run_comparison.py` | 主程序 |
| `render_report.py` | HTML 报告渲染 |

## 输出物

| 文件 | 内容 |
|------|------|
| `results/jev_vs_llm_raw.json` | 原始逐题分数 + 统计汇总 |
| `results/jev_vs_llm_report.html` | 可视化对比报告（含柱状对比图） |

## 统计指标说明

- **均值 / 标准差**：各模型在 18 条样本上的归一化分数分布。标准差反映模型跨题打分的波动性。
- **两两分差**：`mean_abs_diff` 是关键指标——它衡量两个模型在同一题上分歧的平均幅度，比均值差更能反映"打分是否一致"。
- **分歧 Top 10**：极差最大的题目，用于发现哪些维度上模型判断差异最明显。
- **Jev 的 confidence**：与标准差含义不同。confidence 是**单题内部**各档位概率分布的集中度（越集中越高），不是跨题的分值离散度。

## 已知限制

1. **Jev 是新品**：官方未公开模型权重与技术论文，底层架构细节未知。建议先小规模验证再用于生产。
2. **Jev 无 state 数据源**：本项目目前只靠模型自身世界知识回答，未接入财报/公告等外部数据。Jev 的 `state` 字段目前只放了公司名和问题背景，信息量与 LLM 侧一致，但对 Jev 而言信息量偏少，可能影响其打分质量。
3. **非自回归无推理链**：Jev 不做延伸推理，对复合型问题（跨多个维度）表现可能弱于 LLM。本测试集已刻意选取**单一维度**的问题以降低这一影响。
4. **单次采样**：LLM 侧 temperature=0.7，存在随机性。若需更稳健结论，建议多次重复取均值（可后续扩展 `--repeat` 参数）。
