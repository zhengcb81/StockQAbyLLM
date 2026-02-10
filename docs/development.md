# StockQAbyLLM 开发者指南

本文档帮助开发者快速上手 StockQAbyLLM 项目的开发。

## 目录

- [环境设置](#环境设置)
- [项目结构](#项目结构)
- [开发工作流](#开发工作流)
- [调试技巧](#调试技巧)
- [测试指南](#测试指南)
- [代码规范](#代码规范)
- [常见开发任务](#常见开发任务)

## 环境设置

### 系统要求

- Python 3.9 或更高版本
- Git
- pip 或 conda

### 安装步骤

1. **克隆仓库**

```bash
git clone https://github.com/yourusername/StockQAbyLLM.git
cd StockQAbyLLM
```

2. **创建虚拟环境**

使用 venv：
```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows
```

使用 conda：
```bash
conda create -n stockqa python=3.9
conda activate stockqa
```

3. **安装依赖**

```bash
pip install -e ".[dev]"
```

这将安装：
- 核心依赖
- 开发工具（pytest, black, mypy, pylint）
- 测试依赖

4. **验证安装**

```bash
python -m pytest tests/ -v
```

所有测试应该通过。

### 配置开发环境

1. **复制环境变量模板**

```bash
cp .env.example .env
```

2. **编辑 .env 文件**

添加你的 API 密钥和配置。

3. **创建配置文件**

```bash
cp config.txt.example config.txt
cp llm_apis.json.example llm_apis.json
```

## 项目结构

```
StockQAbyLLM/
├── src/                    # 源代码
│   ├── cli/               # 命令行接口
│   ├── config/            # 配置管理
│   ├── core/              # 核心业务逻辑
│   ├── interfaces/        # 接口定义
│   ├── providers/         # LLM 提供者
│   ├── runners/           # 运行器
│   ├── services/          # 服务实现
│   └── utils/             # 工具函数
├── tests/                 # 测试代码
│   ├── unit/             # 单元测试
│   ├── integration/      # 集成测试
│   └── fixtures/         # 测试夹具
├── docs/                  # 文档
├── examples/              # 示例代码
├── scripts/               # 工具脚本
├── logs/                  # 日志文件
├── outputs/               # 输出结果
├── main.py               # 入口文件
├── config.txt            # 问题配置
├── llm_apis.json         # LLM API 配置
├── pyproject.toml        # 项目配置
└── README.md             # 项目说明
```

## 开发工作流

### 1. 创建功能分支

```bash
git checkout -b feature/your-feature-name
```

### 2. 编写代码

- 遵循代码规范（见[代码规范](#代码规范)）
- 添加类型注解
- 编写文档字符串
- 编写测试

### 3. 运行测试

```bash
# 运行所有测试
pytest tests/ -v

# 运行特定测试
pytest tests/unit/test_llm_provider.py -v

# 运行并显示覆盖率
pytest --cov=src --cov-report=html
```

### 4. 代码质量检查

```bash
# 格式化代码
black src/ tests/

# 类型检查
mypy src/ --strict

# 代码质量检查
pylint src/
```

### 5. 提交代码

```bash
git add .
git commit -m "feat: 添加新功能"
git push origin feature/your-feature-name
```

## 调试技巧

### 使用 pdb 调试

```python
import pdb; pdb.set_trace()
```

或使用 breakpoint()（Python 3.7+）：

```python
breakpoint()
```

### 使用 IPython 调试

```python
from IPython import embed; embed()
```

### 日志调试

启用详细日志：

```bash
python main.py --mode basic --verbose
```

或在代码中：

```python
from src.utils.logger import get_logger

logger = get_logger(__name__, verbose=True)
logger.debug("调试信息")
```

### VS Code 调试配置

创建 `.vscode/launch.json`：

```json
{
    "version": "0.2.0",
    "configurations": [
        {
            "name": "Python: 基础模式",
            "type": "python",
            "request": "launch",
            "program": "main.py",
            "args": ["--mode", "basic", "--config", "config.txt"],
            "console": "integratedTerminal",
            "justMyCode": false
        },
        {
            "name": "Python: LLM 模式",
            "type": "python",
            "request": "launch",
            "program": "main.py",
            "args": ["--mode", "llm", "--config", "config.txt"],
            "console": "integratedTerminal",
            "justMyCode": false,
            "env": {
                "PYTHONPATH": "${workspaceFolder}"
            }
        }
    ]
}
```

## 测试指南

### 编写单元测试

```python
import pytest
from unittest.mock import Mock, patch
from src.core.qa_engine import QAEngine

class TestQAEngine:
    def test_init(self):
        """测试初始化。"""
        engine = QAEngine()
        assert engine is not None

    @patch('src.core.qa_engine.SearchService')
    def test_process_question(self, mock_search):
        """测试问题处理。"""
        # 配置 mock
        mock_search.return_value.search.return_value = [
            {"title": "测试", "snippet": "测试答案"}
        ]

        engine = QAEngine()
        result = engine.process_question("测试问题")

        assert result is not None
```

### 编写集成测试

```python
import pytest
from pathlib import Path

class TestQAPipeline:
    def test_end_to_end_pipeline(self, tmp_path):
        """测试端到端流程。"""
        # 创建临时配置文件
        config_file = tmp_path / "test_config.txt"
        config_file.write_text("测试问题\n")

        # 运行完整流程
        runner = BasicRunner()
        exit_code = runner.run(str(config_file))

        assert exit_code == 0
```

### 使用 Fixtures

```python
@pytest.fixture
def mock_qa_engine():
    """创建 mock QAEngine。"""
    engine = Mock()
    engine.process_questions.return_value = Mock()
    return engine

def test_with_fixture(mock_qa_engine):
    """使用 fixture 的测试。"""
    result = mock_qa_engine.process_questions(["问题"])
    assert result is not None
```

### 参数化测试

```python
@pytest.mark.parametrize("input,expected", [
    ("简单问题", True),
    ("", False),
    ("   ", False),
])
def test_question_validation(input, expected):
    """参数化测试。"""
    result = validate_question(input)
    assert result == expected
```

## 代码规范

### 类型注解

```python
from typing import List, Dict, Optional, Any

def process_questions(
    questions: List[str],
    config: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """处理问题列表。

    Args:
        questions: 问题列表
        config: 可选配置

    Returns:
        处理结果列表
    """
    pass
```

### 文档字符串

使用 Google 风格：

```python
def complex_calculation(value: int) -> float:
    """执行复杂计算。

    这个函数执行一个复杂的数学计算，涉及多个步骤。
    详细说明算法逻辑...

    Args:
        value: 输入值，必须是正整数

    Returns:
        计算结果，始终为正浮点数

    Raises:
        ValueError: 如果 value 不是正整数
        RuntimeError: 如果计算过程中发生错误

    Example:
        >>> result = complex_calculation(5)
        >>> print(f"结果: {result:.2f}")
        结果: 12.34

    Note:
        这是一个计算密集型操作，对于大数值可能需要较长时间
    """
    pass
```

### 错误处理

```python
# 具体异常处理
try:
    result = process_data(data)
except (ValueError, TypeError) as e:
    logger.error(f"数据处理错误: {e}")
    raise

# 保留异常链
try:
    result = api_call()
except APIError as e:
    raise ProcessingError("处理失败") from e
```

## 常见开发任务

### 添加新的搜索引擎

1. 在 `src/providers/` 中创建新模块
2. 实现 `SearchProvider` 接口
3. 在 `SearchService` 中注册

### 添加新的 LLM 提供者

1. 在 `llm_apis.json` 中添加配置
2. 确保符合格式规范

### 添加新的命令行选项

1. 在 `main.py` 中添加参数
2. 在相应的 Runner 中处理
3. 更新文档

### 修改数据模型

1. 在 `src/core/models.py` 中修改
2. 更新序列化/反序列化逻辑
3. 更新测试

## 性能分析

### 使用 cProfile

```bash
python -m cProfile -o profile.stats main.py --mode basic
python -m pstats profile.stats
```

### 使用内存分析器

```bash
pip install memory_profiler
python -m memory_profiler main.py --mode basic
```

## 发布流程

1. **更新版本号**

编辑 `pyproject.toml` 中的版本号。

2. **更新 CHANGELOG.md**

添加新版本的变更记录。

3. **创建 Git 标签**

```bash
git tag -a v1.0.0 -m "发布 v1.0.0"
git push origin v1.0.0
```

4. **构建发布包**

```bash
pip install build
python -m build
```

5. **发布到 PyPI**（如果需要）

```bash
pip install twine
python -m twine upload dist/*
```

## 获取帮助

- 查看 [架构文档](architecture.md)
- 查看 [常见问题](faq.md)
- 查看 [故障排查](troubleshooting.md)
- 在 GitHub Issues 中提问

---

*文档版本: 1.0*
*最后更新: 2026-02-09*
