# Task Plan: StockQAbyLLM 项目改进计划（Phase 10-13）
<!--
  WHAT: 基于2026-03-24全面代码分析的改进计划
  WHY: 解决设计、架构、实现、测试、文档方面的新问题
  WHEN: 创建于2026-03-24
-->

## Goal
解决全面分析发现的问题：统一配置管理、修复接口不一致、消除代码重复、完善测试覆盖、添加API文档，将项目从A-级提升到A+级。

## Current Phase
Phase 10 (pending) - 配置管理统一

## Previous Phases (Completed)
- Phase 6: 文档完善 ✅
- Phase 7: CI/CD改进 ✅
- Phase 8: 架构微调 ✅
- Phase 9: 最终验证 ✅

## Phases

### Phase 10: 配置管理统一（预计2天）
<!-- WHAT: 合并分散的配置管理类 -->

#### 10.1 分析现有配置管理结构
- [ ] 列出所有配置相关文件和类
- [ ] 识别职责重叠的部分
- [ ] 绘制配置管理依赖图
- **Dependencies:** 无
- **Success Criteria:** 清晰的配置管理现状报告
- **Status:** pending

#### 10.2 设计统一配置架构
- [ ] 设计统一的 `ConfigManager` 接口
- [ ] 定义配置源优先级（环境变量 > 文件 > 默认值）
- [ ] 设计配置验证机制
- **Dependencies:** 10.1
- **Success Criteria:** 配置架构设计文档
- **Status:** pending

#### 10.3 实现统一配置管理器
- [ ] 合并 `ConfigManager`、`LLMConfig`、`json_config_manager`
- [ ] 实现配置源优先级逻辑
- [ ] 添加配置缓存机制
- [ ] 保持向后兼容
- **Dependencies:** 10.2
- **Success Criteria:** 单一配置管理类处理所有配置
- **Status:** pending

#### 10.4 迁移现有代码
- [ ] 更新 `BasicRunner` 使用新配置管理器
- [ ] 更新 `LLMRunner` 使用新配置管理器
- [ ] 更新 `BaseLLMProvider` 使用新配置管理器
- [ ] 删除旧配置类（或标记废弃）
- **Dependencies:** 10.3
- **Success Criteria:** 所有配置访问通过统一接口
- **Status:** pending

#### 10.5 更新测试
- [ ] 更新配置管理器测试
- [ ] 添加配置优先级测试
- [ ] 验证向后兼容性
- **Dependencies:** 10.4
- **Success Criteria:** 测试全部通过，覆盖率不下降
- **Status:** pending

- **Phase 10 Overall Status:** pending

---

### Phase 11: 接口一致性修复（预计1天）
<!-- WHAT: 修复SearchProvider接口不一致问题 -->

#### 11.1 分析接口不一致问题
- [ ] 列出所有 `SearchProvider` 实现
- [ ] 识别返回类型不一致的方法
- [ ] 评估修改影响范围
- **Dependencies:** 无
- **Success Criteria:** 接口一致性分析报告
- **Status:** pending

#### 11.2 统一SearchProvider接口
- [ ] 修复 `BaseLLMProvider.search()` 返回类型为 `List[SearchResult]`
- [ ] 更新所有调用点
- [ ] 添加接口契约测试
- **Dependencies:** 11.1
- **Success Criteria:** 所有SearchProvider实现返回一致类型
- **Status:** pending

#### 11.3 验证修复
- [ ] 运行类型检查（mypy --strict）
- [ ] 运行所有测试
- [ ] 验证集成测试
- **Dependencies:** 11.2
- **Success Criteria:** mypy 0错误，测试100%通过
- **Status:** pending

- **Phase 11 Overall Status:** pending

---

### Phase 12: 代码重复消除（预计1天）
<!-- WHAT: 消除MockSearchProvider等重复代码 -->

#### 12.1 识别代码重复
- [ ] 使用工具扫描重复代码
- [ ] 列出所有 `MockSearchProvider` 定义位置
- [ ] 识别其他重复模式
- **Dependencies:** 无
- **Success Criteria:** 代码重复分析报告
- **Status:** pending

#### 12.2 提取共享测试组件
- [ ] 创建 `tests/helpers/mock_providers.py`
- [ ] 迁移所有 `MockSearchProvider` 到共享文件
- [ ] 创建 `tests/helpers/mock_llm.py`（如有需要）
- [ ] 更新所有测试文件导入
- **Dependencies:** 12.1
- **Success Criteria:** Mock类定义在单一位置
- **Status:** pending

#### 12.3 验证测试通过
- [ ] 运行所有测试
- [ ] 验证覆盖率不下降
- [ ] 检查测试运行时间
- **Dependencies:** 12.2
- **Success Criteria:** 测试100%通过，覆盖率不下降
- **Status:** pending

- **Phase 12 Overall Status:** pending

---

### Phase 13: 测试和文档增强（预计2天）
<!-- WHAT: 补充边界测试和API文档 -->

#### 13.1 添加边界条件测试
- [ ] 创建 `tests/unit/test_edge_cases.py`
- [ ] 添加超长输入测试
- [ ] 添加特殊字符测试
- [ ] 添加并发访问测试
- [ ] 添加资源耗尽测试
- **Dependencies:** 无
- **Success Criteria:** 边界测试覆盖主要模块
- **Status:** pending

#### 13.2 增强集成测试
- [ ] 分析现有集成测试覆盖
- [ ] 添加端到端测试场景
- [ ] 添加错误恢复测试
- [ ] 使用VCR或Mock记录外部API调用
- **Dependencies:** 13.1
- **Success Criteria:** 集成测试覆盖核心流程
- **Status:** pending

#### 13.3 生成API文档
- [ ] 检查现有mkdocs配置
- [ ] 为所有公共API添加docstring
- [ ] 运行 `mkdocs build` 生成文档
- [ ] 部署到GitHub Pages
- **Dependencies:** 无
- **Success Criteria:** API文档可在线访问
- **Status:** pending

#### 13.4 更新使用示例
- [ ] 检查 `examples/` 目录代码
- [ ] 修复过时的示例代码
- [ ] 添加配置说明文档
- [ ] 验证所有示例可运行
- **Dependencies:** 13.3
- **Success Criteria:** 所有示例可正常运行
- **Status:** pending

#### 13.5 添加架构文档
- [ ] 创建 `docs/architecture.md`
- [ ] 添加组件交互图（Mermaid）
- [ ] 添加数据流图
- [ ] 添加部署架构图
- **Dependencies:** 13.3
- **Success Criteria:** 架构文档完整
- **Status:** pending

- **Phase 13 Overall Status:** pending

---

### Phase 14: 代码质量优化（预计1天）
<!-- WHAT: 修复其他代码质量问题 -->

#### 14.1 修复导入路径问题
- [ ] 移除 `main.py` 中的 `sys.path.insert`
- [ ] 使用包安装或 `pyproject.toml` 配置
- [ ] 验证所有导入正常工作
- **Dependencies:** 无
- **Success Criteria:** 标准Python包结构
- **Status:** pending

#### 14.2 修复硬编码URL
- [ ] 将 `base_llm_provider.py` 中的硬编码URL移到配置
- [ ] 支持通过环境变量覆盖
- [ ] 添加URL验证
- **Dependencies:** 14.1
- **Success Criteria:** 所有URL可配置
- **Status:** pending

#### 14.3 资源管理优化
- [ ] 为 `logger.py` 添加上下文管理器
- [ ] 确保文件句柄正确关闭
- [ ] 添加资源泄漏检测测试
- **Dependencies:** 无
- **Success Criteria:** 无资源泄漏
- **Status:** pending

#### 14.4 应用settings.py常量
- [ ] 在验证逻辑中使用 `MAX_QUESTION_LENGTH`
- [ ] 在格式化中使用 `JSON_INDENT`
- [ ] 消除所有魔法数字
- **Dependencies:** 无
- **Success Criteria:** 无魔法数字
- **Status:** pending

- **Phase 14 Overall Status:** pending

---

### Phase 15: 最终验证和发布（预计半天）
<!-- WHAT: 确保所有改进完成并发布新版本 -->

- [ ] 运行完整测试套件
- [ ] 验证所有质量门通过
  - Pylint: ≥9.5
  - Mypy: 0 errors (--strict)
  - Bandit: 0 高危
  - Coverage: ≥90%
- [ ] 生成改进报告
- [ ] 更新CHANGELOG.md
- [ ] 打版本标签（v0.3.0）
- **Dependencies:** Phase 10, 11, 12, 13, 14
- **Success Criteria:** 所有检查通过，项目达到A+级
- **Status:** pending

---

## Key Questions
1. 配置管理统一后是否需要迁移脚本？ → 评估现有配置文件格式
2. SearchProvider接口修改是否破坏性？ → 需要评估API兼容性
3. 测试覆盖率目标是多少？ → 建议90%
4. 是否需要在v0.3.0前完成所有改进？ → 分批发布更稳妥

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| 先统一配置管理 | 影响面最大，优先解决 |
| 保持向后兼容 | 避免破坏现有用户 |
| 测试覆盖率目标90% | 比当前86%提升4% |
| 版本号v0.3.0 | 重大架构改进 |

## Quality Gates Verification
<!-- 每个阶段后验证这些指标 -->
| 指标 | 当前 | 目标 | 验证命令 | 状态 |
|------|------|------|----------|------|
| 测试覆盖率 | 86% | ≥90% | pytest --cov=src | pending |
| Pylint分数 | 10.00 | ≥9.5 | pylint src/ | ✅ |
| Mypy错误 | 0 | 0 (--strict) | mypy --strict src/ | pending |
| 安全扫描 | 0高危 | 0高危 | bandit -r src/ | ✅ |
| 配置管理类数 | 3 | 1 | 人工检查 | pending |
| 接口一致性 | 不一致 | 一致 | mypy检查 | pending |
| 代码重复率 | 中等 | ≤5% | 人工检查 | pending |
| API文档覆盖 | 部分 | ≥95% | mkdocs build | pending |

## Dependencies Graph
```
Phase 10.1 (分析配置结构)
    ↓
Phase 10.2 (设计统一架构)
    ↓
Phase 10.3 (实现统一管理器)
    ↓
Phase 10.4 (迁移现有代码) → Phase 10.5 (更新测试)
    ↓
Phase 11.1 (分析接口问题) → Phase 11.2 (统一接口) → Phase 11.3 (验证修复)
    ↓
Phase 12.1 (识别重复代码) → Phase 12.2 (提取共享组件) → Phase 12.3 (验证测试)
    ↓
Phase 13.1 (边界测试) → Phase 13.2 (集成测试)
    ↓
Phase 13.3 (API文档) → Phase 13.4 (更新示例) → Phase 13.5 (架构文档)
    ↓
Phase 14.x (代码质量优化) ────────┘ (并行)
    ↓
Phase 15 (最终验证和发布)
```

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
| 待记录 | - | - |

## Notes
- 每个子任务完成后更新状态：pending → in_progress → complete
- 在做重大决定前重读本计划（注意力管理）
- 记录所有错误 - 它们帮助避免重复
- 永远不要重复失败的行动 - 改变方法
- Phase 10是阻塞项，必须先完成
- Phase 11-14可以部分并行
