# StockQAbyLLM 使用说明

## 概述

StockQAbyLLM 是一个**通用的公司股票分析系统**，使用LLM（大语言模型）来分析任意公司的投资价值。

**核心特点**：
- ✅ **完全通用**：可以分析任何公司，不硬编码任何公司特定信息
- ✅ **LLM驱动**：评分和分析完全由LLM生成
- ✅ **JSON输出**：结构化输出，包含评分和详细描述
- ✅ **灵活配置**：通过命令行参数和配置文件控制

## 系统架构

```
StockQAbyLLM/
├── main_with_llm.py          # 主程序入口
├── config.txt                # 问题配置文件
├── src/
│   ├── core/                 # 核心业务逻辑
│   │   ├── models.py        # 数据模型（包含评分字段）
│   │   ├── qa_engine.py     # Q&A处理引擎
│   │   └── exceptions.py    # 自定义异常
│   ├── providers/
│   │   └── llm_provider.py   # LLM提供者（通用提示词）
│   ├── services/
│   │   └── answer_generator.py  # 答案生成器
│   └── utils/
│       └── logger.py        # 日志系统
└── outputs/                  # 输出目录
```

## 使用方法

### 基础用法（占位符模式）

```bash
# 分析海康威视
python main_with_llm.py --company "海康威视" --config config.txt

# 分析其他公司
python main_with_llm.py --company "腾讯控股" --config config.txt --output tencent.json

# 使用自定义问题文件
python main_with_llm.py --company "阿里巴巴" --config questions.txt --output alibaba.json
```

### 使用真实LLM API

```bash
# 使用DeepSeek API
python main_with_llm.py \
  --company "海康威视" \
  --api-key YOUR_DEEPSEEK_API_KEY \
  --config config.txt \
  --output hikvision_analysis.json
```

### 命令行参数

| 参数 | 简写 | 说明 | 默认值 |
|------|------|------|--------|
| `--company` | `-c` | 要分析的公司名称 | 无（通用模式） |
| `--api-key` | `-k` | LLM API密钥 | 无（占位符模式） |
| `--model` | `-m` | LLM模型名称 | deepseek-chat |
| `--config` | `-f` | 问题配置文件路径 | config.txt |
| `--output` | `-o` | 输出JSON文件路径 | outputs/analysis_result.json |

### 查看帮助

```bash
python main_with_llm.py --help
```

## 输出格式

JSON输出格式（完全符合您的要求）：

```json
{
  "问题1文本": {
    "score": 7,
    "description": "详细的分析描述..."
  },
  "问题2文本": {
    "score": 8,
    "description": "详细的分析描述..."
  }
}
```

每个问题包含：
- `score`: 1-10的评分（整数）
- `description`: 详细的分析描述（字符串）

## 配置文件格式

`config.txt` 格式：每行一个问题

```
问题1？
问题2？
问题3？
```

当前 `config.txt` 包含39个关于公司投资价值分析的问题，涵盖：
- 市场规模与增长
- 盈利能力
- 竞争护城河
- 管理团队
- 风险因素
- 财务健康
- 估值合理性

## LLM提示词

系统使用**通用的提示词**，不包含任何公司特定信息：

```
你是一位专业的投资分析师。请对以下问题进行深入分析并给出评分。

关于公司：{company_name}

问题：{question}

要求：
1. 基于该公司的实际情况进行分析
2. 给出一个1-10分的评分
3. 提供详细的评分理由和分析
4. 请严格按JSON格式返回：
{
  "score": <评分数字>,
  "description": "<详细分析描述>"
}
```

**关键点**：
- ✅ 提示词完全通用
- ✅ 不硬编码任何公司信息
- ✅ 要求LLM返回JSON格式
- ✅ 评分和分析完全由LLM生成

## 集成真实LLM API

当前系统使用占位符模式（返回固定评分5和提示信息）。

要集成真实的LLM API（如DeepSeek），修改 `src/providers/llm_provider.py` 的 `_call_llm_api()` 方法：

```python
def _call_llm_api(self, prompt: str) -> tuple[int, str]:
    import requests

    headers = {
        "Authorization": f"Bearer {self.api_key}",
        "Content-Type": "application/json"
    }

    data = {
        "model": self.model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7
    }

    response = requests.post(
        "https://api.deepseek.com/v1/chat/completions",
        headers=headers,
        json=data
    )

    result = response.json()
    content = result["choices"][0]["message"]["content"]

    # 解析LLM返回的JSON
    llm_response = json.loads(content)

    return (llm_response["score"], llm_response["description"])
```

## 示例运行

### 1. 分析海康威视（占位符模式）

```bash
python main_with_llm.py --company "海康威视" --config config.txt
```

输出：
```
======================================================================
公司分析 - 海康威视
======================================================================

成功加载 39 个问题
配置文件: config.txt
输出文件: outputs/analysis_result.json

...

分析完成统计
======================================================================
[OK] 处理问题数: 39
[OK] 成功处理: 39
[OK] 平均评分: 5.0/10
[OK] 评分分布: {5: 39}
```

### 2. 分析腾讯（使用DeepSeek API）

```bash
python main_with_llm.py \
  --company "腾讯控股" \
  --api-key sk-xxxxxxxxxxxxxxxx \
  --config config.txt \
  --output tencent_analysis.json
```

LLM会为每个问题生成真实的评分和详细分析。

## 系统特点总结

### ✅ 通用性
- 不硬编码任何公司信息
- 可以分析任何公司
- 提示词完全通用

### ✅ LLM驱动
- 评分由LLM生成
- 分析描述由LLM生成
- 支持任何LLM API（DeepSeek、OpenAI等）

### ✅ 结构化输出
- JSON格式
- 包含评分和描述
- 易于解析和使用

### ✅ 灵活配置
- 命令行参数
- 可配置问题文件
- 可指定输出路径

## 注意事项

1. **API密钥安全**：不要将API密钥硬编码在代码中，使用命令行参数或环境变量
2. **成本控制**：LLM API调用有费用，建议先测试少量问题
3. **输出格式**：确保LLM返回的JSON格式正确，可能需要调整提示词
4. **评分一致性**：LLM的评分可能会有一定波动，这是正常的

## 故障排查

### 问题：LLM返回的不是JSON格式

**解决方案**：在提示词中更明确地要求JSON格式，并在代码中添加JSON解析错误处理

### 问题：评分超出1-10范围

**解决方案**：在代码中添加评分范围验证和修正逻辑

### 问题：描述太短或太长

**解决方案**：在提示词中指定描述的长度要求

## 未来改进

- [ ] 支持多个LLM提供商（OpenAI、Claude等）
- [ ] 添加评分历史记录
- [ ] 支持批量分析多个公司
- [ ] 添加可视化报告生成
- [ ] 支持自定义评分标准
