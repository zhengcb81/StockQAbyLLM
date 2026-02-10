# StockQAbyLLM

基于大语言模型的股票问答系统。

## 项目概述

StockQAbyLLM 是一个能够从配置文件批量处理问题并生成答案的问答系统。当前实现为架构重构版本，专注于代码质量、可测试性和可维护性。

**注意**: 当前版本的核心功能（网络搜索和 LLM 集成）仍为占位符实现，将在后续版本中完成。

## 功能特性

- ✅ 从配置文件批量加载问题
- ✅ 结构化的数据模型（Question, Answer, QAResult）
- ✅ 完善的错误处理和日志记录
- ✅ 类型安全（Type Hints + Dataclasses）
- ✅ 全面的单元测试和集成测试
- ✅ 向后兼容的原有接口
- ✅ 命令行参数支持
- ✅ UTF-8 编码支持（中文友好）

## 项目结构

```
StockQAbyLLM/
├── main.py                      # 程序入口
├── config.txt                   # 配置文件（问题列表）
├── pyproject.toml               # 项目配置
├── README.md                    # 项目文档
│
├── src/                         # 源代码
│   ├── core/                    # 核心业务逻辑
│   │   ├── models.py           # 数据模型
│   │   └── exceptions.py       # 自定义异常
│   ├── config/                  # 配置管理
│   │   ├── config_manager.py   # 配置管理器
│   │   └── settings.py         # 配置常量
│   └── utils/                   # 工具函数
│       └── logger.py           # 日志系统
│
├── tests/                       # 测试套件
│   ├── unit/                   # 单元测试
│   ├── integration/            # 集成测试
│   └── conftest.py             # pytest 配置
│
└── logs/                        # 日志文件
```

## 快速开始

### 安装

```bash
# 克隆仓库
git clone <repository-url>
cd StockQAbyLLM

# 创建虚拟环境（推荐）
python -m venv venv
source venv/bin/activate  # Linux/Mac
# 或
venv\Scripts\activate  # Windows

# 安装依赖（可选，用于开发）
pip install -e ".[dev]"
```

### 使用

1. **创建配置文件** (`config.txt`):

```
如何学习Python编程？
人工智能的发展趋势是什么？
量子计算机的工作原理是什么？
股票市场预测的最新方法有哪些？
```

2. **运行程序**:

```bash
# 基本用法（使用默认配置）
python main.py

# 指定配置文件
python main.py --config my_questions.txt

# 输出到文件
python main.py --output results.json

# 启用详细日志
python main.py --verbose
```

3. **查看结果**:

程序会输出 JSON 格式的结果：

```json
{
    "如何学习Python编程？": "这是关于 '如何学习Python编程？' 的答案（通过网络搜索获得）。",
    "人工智能的发展趋势是什么？": "这是关于 '人工智能的发展趋势是什么？' 的答案（通过网络搜索获得）。"
}
```

## 命令行参数

| 参数 | 简写 | 说明 | 默认值 |
|------|------|------|--------|
| `--config` | - | 配置文件路径 | `config.txt` |
| `--output` | - | 输出文件路径 | 控制台输出 |
| `--verbose` | `-v` | 启用详细日志 | 关闭 |
| `--help` | `-h` | 显示帮助信息 | - |

## 开发

### 运行测试

```bash
# 运行所有测试
pytest

# 运行单元测试
pytest tests/unit/

# 运行集成测试
pytest tests/integration/

# 生成覆盖率报告
pytest --cov=src --cov-report=html
```

### 代码格式化

```bash
# 使用 black 格式化代码
black src/ tests/

# 使用 pylint 检查代码质量
pylint src/

# 使用 mypy 进行类型检查
mypy src/
```

## 架构设计

### 分层架构

项目采用分层架构设计，关注点分离：

- **表示层** (`main.py`): 命令行接口和用户交互
- **业务逻辑层** (`core/`): 核心业务逻辑和数据模型
- **数据访问层** (`config/`): 配置管理和数据访问
- **工具层** (`utils/`): 通用工具函数（日志等）

### 设计模式

- **Dataclass Pattern**: 使用 `@dataclass` 定义数据模型
- **Repository Pattern**: `ConfigManager` 封装配置访问
- **Exception Hierarchy**: 自定义异常层次结构
- **Factory Pattern**: 对象创建通过工厂方法

### 测试策略

- **单元测试**: 测试单个组件的功能
- **集成测试**: 测试完整的业务流程
- **向后兼容测试**: 确保新版本与旧版本兼容

## 配置文件格式

配置文件 `config.txt` 使用简单的文本格式：

- 每行一个问题
- 自动去除首尾空格
- 自动忽略空行
- UTF-8 编码，支持中文和其他 Unicode 字符

示例：

```
问题1
问题2
问题3
```

## 日志

日志文件保存在 `logs/` 目录，按日期命名：

- 文件名: `stock_qa_YYYYMMDD.log`
- 格式: `时间 - 模块名 - 级别 - 消息`
- 级别: DEBUG, INFO, WARNING, ERROR

示例：

```
2026-01-04 10:30:45 - src.config.config_manager - INFO - 正在加载配置文件: config.txt
2026-01-04 10:30:45 - src.config.config_manager - INFO - 成功加载 4 个问题
```

## 错误处理

系统包含完善的错误处理机制：

- **FileNotFoundError**: 配置文件不存在
- **EmptyConfigError**: 配置文件为空
- **ValidationError**: 数据验证失败
- **ProcessingError**: 问题处理失败

所有错误都包含详细的错误消息和上下文信息。

## 技术栈

- **Python**: 3.8+
- **测试框架**: pytest
- **代码格式化**: black
- **类型检查**: mypy
- **日志**: 标准库 `logging` 模块

## 贡献指南

欢迎贡献代码！请遵循以下步骤：

1. Fork 本仓库
2. 创建特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 开启 Pull Request

### 代码规范

- 遵循 PEP 8 规范
- 使用 black 格式化代码
- 添加类型提示
- 编写单元测试
- 添加 docstring

## 版本历史

### v0.1.0 (2026-01-04)

**重构阶段 1 完成**:
- ✅ 建立分层架构
- ✅ 实现数据模型（Question, Answer, QAResult）
- ✅ 实现配置管理系统
- ✅ 实现日志系统
- ✅ 重写主入口点
- ✅ 添加全面测试（37 个测试，100% 通过）
- ✅ 向后兼容性验证
- ✅ 错误处理和类型安全

**计划中的功能**:
- [ ] 阶段 2: 核心业务逻辑分离（QAEngine）
- [ ] 阶段 3: 完整类型提示和错误处理
- [ ] 阶段 4: 文档和测试覆盖率提升
- [ ] 阶段 5: CI/CD 和质量门控
- [ ] 未来: 实现真实的网络搜索
- [ ] 未来: 集成 LLM API
- [ ] 未来: 添加股票特定功能

## 许可证

本项目采用 MIT 许可证 - 详见 LICENSE 文件

## 作者

郑曾波

## 致谢

- 感谢所有贡献者
- 感谢 Python 社区的优秀工具和库
