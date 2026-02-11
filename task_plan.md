# Task Plan: StockQAbyLLM 项目全面审查改进（第二轮）
<!--
  WHAT: 基于2026-02-10七维度全面审查结果的改进计划
  WHY: 将最新审查发现的问题转化为可执行的改进计划
  WHEN: 创建于2026-02-10
-->

## Goal
基于七维度审查结果，将StockQAbyLLM项目从当前状态提升到A级代码质量：修复类型错误、提升测试覆盖率到87%+、解决安全漏洞、完善依赖管理、添加英文文档。

## Current Phase
Phase 8

## Phases

### Phase 1: 紧急修复（第1周）
<!-- WHAT: 修复阻塞质量门的关键问题 -->
- [x] 修复13个mypy类型检查错误（main.py, main_with_llm.py）
- [x] 代码格式化（black修复main.py和main_with_llm.py）
- [x] 清理main_with_llm.py的8个未使用导入
- [x] 更新aiohttp到3.13.3+修复4个CVE
- [x] 验证所有质量门通过
- **Status:** complete

### Phase 2: 架构重构（第2-3周）
<!-- WHAT: 解决架构层面的设计和代码质量问题 -->
- [x] 拆分LLMProvider上帝类（401行 → 4个专注类）
- [x] 提取QAEngine中的控制台输出到ProgressReporter接口
- [x] 移除全局Container状态，改用依赖注入
- [x] 提取同步/异步LLM共享逻辑到BaseLLMProvider
- [x] 修复循环导入问题
- **Status:** complete

### Phase 3: 测试覆盖率提升（第3-4周）
<!-- WHAT: 为关键未测试模块添加测试 -->
- [x] 添加Container测试（0% → 目标90%+）
- [x] 添加BatchStrategy测试（0% → 目标90%+）
- [x] 添加Formatter测试（0% → 目标90%+）
- [x] 添加LLMRunner集成测试（13% → 目标80%+）
- [ ] 删除低价值配置测试（~15个）
- [x] 验证整体覆盖率达到87%（实际达到88%）
- **Status:** complete

### Phase 4: 安全加固（第4周）
<!-- WHAT: 修复安全漏洞和加固安全措施 -->
- [x] 实现文件名清洗函数防止路径遍历
- [x] 添加JSON配置schema验证
- [x] 实现RateLimiter类（Token bucket算法）
- [x] 添加输入长度验证并拒绝超长问题
- [x] 完善pyproject.toml依赖声明（添加requests等）
- **Status:** complete

### Phase 5: LLM集成增强（第5周）
<!-- WHAT: 改进LLM调用的可靠性和可观测性 -->
- [x] 实现ProviderCascade降级机制
- [x] 添加Token使用和成本追踪类
- [x] 使System Prompt可配置（移到llm_apis.json）
- [x] 实现请求缓存层（带TTL）
- [x] 添加请求关联ID追踪
- [x] 修复指数退避实现错误（当前是线性）
- **Status:** complete

### Phase 6: 文档完善（第6周）
<!-- WHAT: 补充缺失的文档和国际化 -->
- [ ] 创建英文README (README_EN.md)
- [ ] 创建5分钟快速入门指南 (docs/QUICKSTART.md)
- [ ] 设置Sphinx/MkDocs生成API文档
- [ ] 添加使用示例和教程
- [ ] 添加视觉示例（截图、输出示例）
- **Status:** pending

### Phase 7: CI/CD改进（第7周）
<!-- WHAT: 强化自动化和质量门 -->
- [ ] 添加pip-audit/safety依赖扫描到CI
- [ ] 配置pre-commit hooks
- [ ] 添加自动化API文档构建
- [ ] 配置覆盖率报告自动生成
- [ ] 添加性能基准测试
- **Status:** pending

### Phase 8: 最终验证和交付
<!-- WHAT: 确保所有改进完成并达到目标 -->
- [ ] 运行完整测试套件验证
- [ ] 确认所有质量门通过
- [ ] 生成最终审查报告
- [ ] 创建改进前后对比文档
- [ ] 交付给用户
- **Status:** pending

## Key Questions
1. 是否需要保持向后兼容性？ → 是，尽量避免破坏性变更
2. 优先级如何权衡？ → 质量门 > 安全 > 架构 > 功能增强
3. 测试策略如何调整？ → 删除低价值测试，专注关键路径覆盖
4. 文档国际化范围？ → 至少README和快速入门需要英文版

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| 使用渐进式重构而非重写 | 项目已有良好基础，逐步改进风险更低 |
| 优先修复质量门阻塞问题 | 没有通过质量门无法进行其他改进 |
| 拆分LLMProvider而非合并 | 职责分离更清晰，符合SOLID原则 |
| 删除低价值配置测试 | 提高测试信噪比，专注行为测试 |
| 添加ProgressReporter抽象 | 保持核心逻辑纯净化，便于测试和复用 |

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
| | 1 | |

## Notes
- 每个Phase完成后更新状态：pending → in_progress → complete
- 在做重大决定前重读本计划（注意力管理）
- 记录所有错误 - 它们帮助避免重复
- 永远不要重复失败的行动 - 改变方法

## Quality Gates Verification
<!-- 每个阶段后验证这些指标 -->
| 指标 | 当前 | 目标 | 验证命令 |
|------|------|------|----------|
| 测试覆盖率 | 63% | 87% | pytest --cov=src --cov-report=term-missing |
| Pylint分数 | 8.43 | 9.0+ | pylint src/ --fail-under=9.0 |
| Mypy错误 | 13 | 0 | mypy src/ strict mode |
| 安全扫描 | 3中 | 0 | bandit -r src/ |
| 依赖CVE | 4 | 0 | pip-audit |
