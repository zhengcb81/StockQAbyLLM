# 贡献指南

感谢您对 StockQAbyLLM 项目的关注！我们欢迎各种形式的贡献。

## 贡献方式

### 报告问题

如果您发现了 bug 或有功能建议：

1. 检查 [Issues](https://github.com/yourusername/StockQAbyLLM/issues) 确认问题尚未被报告
2. 创建新 Issue，包含：
   - 清晰的标题
   - 详细的问题描述
   - 复现步骤
   - 预期行为 vs 实际行为
   - 环境信息（操作系统、Python 版本等）

### 提交代码

#### 开发环境设置

1. Fork 项目仓库
2. 克隆您的 fork：
   ```bash
   git clone https://github.com/yourusername/StockQAbyLLM.git
   cd StockQAbyLLM
   ```

3. 创建虚拟环境：
   ```bash
   python -m venv venv
   source venv/bin/activate  # Linux/Mac
   venv\Scripts\activate     # Windows
   ```

4. 安装开发依赖：
   ```bash
   pip install -e ".[dev]"
   ```

#### 代码规范

我们遵循以下代码规范：

- **PEP 8**: Python 代码风格指南
- **类型注解**: 使用类型提示提高代码可读性
- **Docstring**: 使用 Google 风格的文档字符串
- **Black**: 代码格式化工具
- **Pylint**: 代码质量检查（目标评分 ≥ 8.0）
- **Mypy**: 类型检查（严格模式，0 错误）

#### 开发流程

1. 创建功能分支：
   ```bash
   git checkout -b feature/your-feature-name
   # 或
   git checkout -b fix/your-bug-fix
   ```

2. 编写代码和测试：
   - 确保新功能有相应的测试
   - 运行测试确保通过：
     ```bash
     pytest tests/ -v
     ```
   - 检查代码覆盖率：
     ```bash
     pytest --cov=src --cov-report=html
     ```

3. 代码质量检查：
   ```bash
   # 格式化代码
   black src/ tests/

   # 类型检查
   mypy src/ --strict

   # 代码质量检查
   pylint src/
   ```

4. 提交代码：
   ```bash
   git add .
   git commit -m "类型: 简短描述"

   # 推送到您的 fork
   git push origin feature/your-feature-name
   ```

#### 提交信息规范

遵循 [Conventional Commits](https://www.conventionalcommits.org/) 规范：

```
<类型>(<范围>): <简短描述>

<详细描述>

<页脚>
```

**类型（type）：**
- `feat`: 新功能
- `fix`: 修复 bug
- `docs`: 文档更新
- `style`: 代码格式（不影响功能）
- `refactor`: 重构
- `test`: 添加或修改测试
- `chore`: 构建过程或辅助工具变动

**示例：**
```
feat(llm): 添加新的 LLM 提供者支持

- 添加了对 OpenAI API 的支持
- 实现了自动重试机制
- 添加了相应的单元测试

Closes #123
```

### Pull Request

1. 访问原仓库并创建 Pull Request
2. 填写 PR 模板：
   - 描述您的更改
   - 关联相关 Issue
   - 确认所有检查通过
3. 等待代码审查

#### PR 审查标准

- [ ] 代码通过所有测试
- [ ] 代码覆盖率不降低
- [ ] 代码格式符合规范
- [ ] 类型检查通过（0 错误）
- [ ] Pylint 评分 ≥ 8.0
- [ ] 有适当的文档和注释
- [ ] 更新了相关文档

## 测试要求

### 单元测试

- 每个新功能应有相应的单元测试
- 使用 pytest 测试框架
- 测试文件命名：`test_<module_name>.py`
- 测试类命名：`Test<ClassName>`

```python
class TestMyClass:
    def test_method_success(self):
        """测试方法成功场景。"""
        result = my_class.method()
        assert result == expected

    def test_method_failure(self):
        """测试方法失败场景。"""
        with pytest.raises(ValueError):
            my_class.method(invalid_input)
```

### 集成测试

- 测试模块间的交互
- 放置在 `tests/integration/` 目录

### 测试覆盖率目标

- 整体覆盖率 ≥ 70%
- 新模块覆盖率 ≥ 80%

## 文档要求

### 代码文档

- 所有公共模块、类、方法应有 docstring
- 使用 Google 风格的文档字符串：

```python
def process_question(question: str) -> QAResult:
    """处理单个问题并返回答案。

    Args:
        question: 要处理的问题文本

    Returns:
        包含答案和元数据的 QAResult 对象

    Raises:
        ValueError: 如果问题为空或仅包含空白字符
        RuntimeError: 如果处理过程中发生错误

    Example:
        >>> result = process_question("什么是股票？")
        >>> print(result.answer)
        "股票是..."
    """
```

### 项目文档

- 更新 README.md（如果有用户可见的更改）
- 更新 CHANGELOG.md
- 必要时更新相关文档

## 设计原则

1. **简单优于复杂**: 优先选择简单、易懂的解决方案
2. **显式优于隐式**: 代码行为应清晰明确
3. **可测试性**: 代码应易于测试
4. **向后兼容**: 避免破坏性更改
5. **性能考虑**: 权衡性能和可维护性

## 获取帮助

- 查看 [FAQ](docs/faq.md)
- 查看 [故障排查](docs/troubleshooting.md)
- 在 Issues 中提问

## 许可证

通过贡献代码，您同意您的贡献将使用与项目相同的 [MIT License](LICENSE) 发布。

---

再次感谢您的贡献！🎉
