# Findings & Decisions - StockQAbyLLM 七维度审查（2026-02-10）
<!--
  WHAT: 七维度审查发现的问题和决策记录
  WHY: 作为改进的知识库，防止遗漏关键问题
  WHEN: 创建于2026-02-10，基于7个并行审查agents的结果
-->

## Requirements
基于七维度全面审查，需要改进的方面：
- **代码质量**: 修复13个类型检查错误，通过所有质量门
- **架构设计**: 解决全局状态、上帝类、代码重复问题
- **测试覆盖**: 从63%提升到87%，覆盖关键未测试模块
- **安全**: 修复3个中等优先级安全漏洞
- **依赖**: 完善依赖声明，修复CVE漏洞
- **文档**: 添加英文文档和API文档生成
- **LLM集成**: 添加限速、降级、成本追踪

## Research Findings

### 七维度审查摘要（2026-02-10）

#### 1. 代码质量审查 (8.43/10)
**严重问题:**
- 13个mypy类型检查错误（main.py:106,122; main_with_llm.py:128,146-150）
- 2个文件需要black格式化（main.py, main_with_llm.py）
- 8个未使用的导入（main_with_llm.py）

**警告级别:**
- 40+处日志应使用lazy格式化（W1203）
- 13处空f-string（F541）
- 循环导入（config模块间）

#### 2. 架构设计审查 (B+ 82/100)
**严重问题:**
- 全局Container单例（container.py:161）
- QAEngine耦合控制台输出（qa_engine.py:109）
- LLMProvider上帝类（401行，多职责）
- 同步/异步LLM代码重复（~300行）

**违反原则:**
- 单一职责原则（LLMProvider, QAEngine）
- 依赖倒置原则（runners直接实例化依赖）
- 接口隔离原则（get_provider_name强制实现）

#### 3. 测试覆盖审查 (B+, 63%)
**0%覆盖率模块（关键）:**
- Container: 75行未测试
- BatchStrategy: 119行未测试
- Formatter: 62行未测试

**低覆盖率模块:**
- LLMRunner: 13%（169行中仅22行）

**优势:**
- 257个测试全部通过
- 边缘测试覆盖良好
- Mock策略合理

#### 4. 安全审查 (B+)
**中等优先级:**
- JSON反序列化缺少验证
- 路径遍历风险（文件名未清洗）
- API密钥日志可能泄露

**依赖漏洞:**
- aiohttp 3.13.2: 4个CVE

**优势:**
- Bandit扫描0问题
- HTTPS强制
- 凭证管理良好

#### 5. 依赖审查 (C)
**严重问题:**
- pyproject.toml仅声明httpx，缺少requests等
- 无lock文件
- aiohttp存在CVE

**配置问题:**
- 未使用python-dotenv加载.env
- 硬编码配置值

#### 6. 文档审查 (7.5/10)
**优势:**
- 中文文档全面（3000+行）
- 内联文档优秀
- 架构/开发文档完整

**缺失:**
- 无英文README
- 无生成的API文档
- 缺少快速入门指南
- 无视觉示例

#### 7. LLM集成审查 (6.5/10)
**严重缺失:**
- 无Provider降级机制
- 无速率限制
- 无成本追踪
- 无请求缓存

**代码问题:**
- 指数退避实现错误（线性而非指数）
- 同步/异步大量重复

## Technical Decisions
| Decision | Rationale |
|----------|-----------|
| 拆分LLMProvider为4个类 | LLMClient(通信), LLMResponseParser(解析), LLMRetryStrategy(重试), LLMProvider(编排) |
| 创建ProgressReporter接口 | 将控制台输出从核心逻辑分离，便于测试和UI替换 |
| 提取共享LLM逻辑到BaseLLMProvider | 消除300行重复代码，统一响应解析逻辑 |
| 实现ProviderCascade | 主Provider失败时自动降级到备用Provider |
| 添加RateLimiter工具类 | Token bucket算法，防止API配额耗尽 |

## Issues Encountered
| Issue | Resolution |
|-------|------------|
| 循环导入: config_manager ↔ config_provider | 考虑将共享代码提取到独立的utils模块 |
| 全局Container影响测试 | 改用依赖注入传递container或使用context manager |
| LLMProvider职责过多 | 按照HTTP/解析/重试/编排四个职责拆分 |

## Resources
**审查报告来源:**
- Agent #1 (abc5af3): Code Quality & Standards Review
- Agent #2 (a6574c5): Architecture & Design Review
- Agent #3 (a83a3e2): Test Coverage & Quality Review
- Agent #4 (a6c4e2a): Security & Vulnerability Review
- Agent #5 (a1074db): Dependencies & Configuration Review
- Agent #6 (aa32135): Documentation & Usability Review
- Agent #7 (a948e8f): LLM Integration Review

**关键文件位置:**
- 类型错误: main.py:106,122; main_with_llm.py:128,146-150
- 全局状态: src/core/container.py:161-174
- 上帝类: src/providers/llm_provider.py (401行)
- 控制台耦合: src/core/qa_engine.py:109-162
- 未测试模块: container.py, batch_strategy.py, formatter.py

## Priority Matrix
<!-- 按影响和紧急程度分类 -->
| 问题 | 影响 | 紧急 | 优先级 |
|------|------|------|--------|
| 类型检查错误 | 阻塞CI | 高 | P0 |
| 格式问题 | 阻塞CI | 高 | P0 |
| CVE漏洞 | 安全风险 | 中 | P0 |
| 路径遍历 | 安全风险 | 中 | P1 |
| 0%覆盖率模块 | 质量风险 | 中 | P1 |
| 上帝类 | 可维护性 | 低 | P1 |
| 代码重复 | 维护成本 | 低 | P2 |
| 无速率限制 | 成本风险 | 低 | P2 |
| 缺少英文文档 | 可用性 | 低 | P3 |

---
*基于7个并行审查agents的结果*
*审查日期: 2026-02-10*
