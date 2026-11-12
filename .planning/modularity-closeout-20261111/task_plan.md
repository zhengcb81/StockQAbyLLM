# MC-QA 任务计划 — 执行 owner 与已付结果边界收口

计划根：`C:/Users/郑曾波/Projects/iqs-modularity-worktrees-20261111/MC-QA/.planning/modularity-closeout-20261111/`
包卡：`invest-quick-scan/docs/implementation/parallel-lanes/packages/2026-11-11-modularity-closeout/MC-QA.md`

## 本域目标

- StockQA 独占模型/搜索/额度/已付响应/逐题保存/outbox；消费者只见公开 DTO 与 opaque 句柄。
- canonical（facade 1.5）与 1.8 候选归拢为自包含 commit，9 份新接口 + 对口测试全部入库。
- `company_agent_public_api` 只回有限操作投影；值导出（LIMIT_CAPS）合法；owner module 对象不出境。
- 组合根不再检查 CA/RT 源码布局；companion 仅经版本化公开 facade 消费。
- 千行反射扫描收敛为域方向测试 + 版本化公开接口契约；保留真实业务负例。
- 不修改 IQS/CA/RT 源码；不跑收费 API；不刷新旧交付回执。

## 非目标

- 不代改消费者（IQS/CA/RT/SW）；真实三组件组合验收归总控。
- 不修复候选带入的全部历史 mypy 噪音以外的无关问题；不动 `nul`、`pilot_runs/`、个人记忆。

## 输入

- 源仓 canonical：`C:/Users/郑曾波/Projects/StockQAbyLLM` @ `4c24789`（77 tracked 修改 + 12 untracked 源/测试）。
- 1.8 候选：`invest-quick-scan/.tmp-stockqa-current-wt-20261110` @ `4c24789`（87 tracked + 17 untracked）。
- 归拢结论：候选为符号级超集（协议版本更高、api_* 导出、CompanyScanContract/task factory/合同测试独有）；18 个双改文件按候选解决，独审确认。

## RED/GREEN 步骤（实际执行）

1. 新公开边界反例 RED（5 失败）：facade 返 module、`.__dict__` 可见、快照未 detach → 修 facade 投影 + 快照深复制 → GREEN。
2. 单测顶层 `import iqs_company_agent_lab` → 公共 fake + 真包材料组合测试（缺失即 skip）。
3. 组合根 `_modules` 改 facade 协议校验 → 配套 4 个新测试 GREEN。
4. 已付恢复 `_store` 缺陷：测试 RED（AttributeError）→ store 由组合根显式接线 → GREEN；`work_store` 先用后定义（NameError）→ `recovery_store` → GREEN。
5. 双主体 store 矩阵测试（两公司同题不同 work item、progress 不互串）。
6. 外部 IQS 根：`IQS_ORIGINAL_ROOT`/`IQS_ROUTE_TEST_CODE_ROOT` 显式配置，缺省 skip 不 fail。

## 公开依赖

- 消费 IQS：scripts/answer_validation（经 `IQS_ORIGINAL_ROOT`）、schemas/quick_scan（经 `IQS_ROUTE_TEST_CODE_ROOT`）。
- 消费 CA/RT：仅 `<pkg>.api` facade（CA 2.3.0 / RT 1.4.0 floor）。
- IQS ports 要求 QA facade ≥1.7；本包交付 1.8（同 major 兼容）。

## 当前状态

实现 + 独审 + 修复完成；等待独审复核 `72fae07`。

## 单一 Next Step

独审复核通过后：写 handoff.md，交回总控（组合验收 I1-I4 用本分支 commit）。
