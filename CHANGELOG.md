# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned
- 全面代码质量改进 (Phase 1-8)
- 测试覆盖率提升至 70%+
- 文档完善和 API 文档生成
- CI/CD 自动化流程

## [0.1.0] - 2026-01-04

### Added
- 初始版本发布
- 基础问答功能
- 分层架构设计
- 数据模型 (Question, Answer, QAResult)
- 配置管理系统 (TXT, JSON)
- 日志系统
- 单元测试和集成测试

### Implemented
- `ConfigManager` - TXT 配置管理器
- `JSONConfigManager` - JSON 配置管理器
- `QAEngine` - 问答处理引擎
- `SearchProvider` - 搜索提供者接口
- `LLMProvider` - LLM API 提供者
- `AnswerGenerator` - 答案生成器
- `OutputValidator` - 输出验证器
- 异常处理体系

### Features
- 从配置文件批量加载问题
- 支持 TXT 和 JSON 配置格式
- 完善的错误处理和日志记录
- 类型安全 (Type Hints + Dataclasses)
- 命令行参数支持
- UTF-8 编码支持 (中文友好)

### Documentation
- README.md - 项目说明
- README_USAGE.md - 使用说明
- IMPLEMENTATION_PLAN.md - 实施计划
- LLM_API_CONFIG.md - LLM 配置指南
- REFACTORING_COMPLETE.md - 重构总结

### Testing
- 单元测试 (89个测试用例)
- 集成测试 (6个测试用例)
- 测试覆盖率: 58%

### Known Issues
- 3个测试失败 (test_qa_result_to_dict, test_batch_result_to_dict, test_backward_compatibility)
- 部分模块测试覆盖率较低 (LLMProvider: 11%, JSONConfigManager: 15%)
- 缺少 LICENSE 文件
- 缺少 CHANGELOG.md

### Technical Debt
- `main_with_llm.py` 过于庞大 (538行)
- `llm_demo.py` 过于庞大 (987行)
- `output_validator.py` 过于庞大 (442行)
- 根目录文件混乱 (18个脚本文件)
- `ConfigManager` 和 `JSONConfigManager` 代码重复 (81.9%)
- 高复杂度函数 (process_batch_stocks: 32, _call_llm_api: 16)
- 12处过度宽泛的异常捕获 (`except Exception`)
- API 密钥硬编码

---

## Version History

| Version | Date | Description |
|---------|------|-------------|
| 0.1.0 | 2026-01-04 | 初始版本，基础功能实现 |
| 0.2.0 | TBD | 质量改进版本 (计划中) |

---

## Links

- [Repository](https://github.com/yourusername/StockQAbyLLM)
- [Issue Tracker](https://github.com/yourusername/StockQAbyLLM/issues)
- [Documentation](https://github.com/yourusername/StockQAbyLLM/wiki)
