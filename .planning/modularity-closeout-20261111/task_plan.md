## 2026-11-20 暂定评分与证据强弱分离

按用户要求，新单题、打包和格式修复提示不再把“题目所需粒度的数据不齐全”直接变为无分。有意义的部分依据允许 `status=scored`、1–10暂定分、`confidence=low`，并在现有 `missing_fields`、`coverage.partial` 和理由中说明假设及缺口。无判断依据及不适用仍可空分；事实关系不评分，缺失财务数值及引用不补造。

实现只修改新提示构造，不改变status/null合同、历史答案或outbox。LLMProvider标准pack追加的同类强制空分句也已移除。新TDD、原解析/pack及原outbox请求bytes接续共207项隔离测试通过，Black/isort与diff检查通过，总控独立复审ACCEPT，提交并线由总控统一记录。四个旧提示测试失败已按现有公开owner投影及当前metric坐标含义修正，无生产接口扩展；测试临时根已删除，0真实API。

## 2026-11-20 公开语义接口兼容

`original_semantic_resources` 复用现有 `supports_public_api`，按 `iqs.answer-validation-api` 协议族、同主版本及最低 1.1.0 判断；1.1.1/1.2.0 等兼容实现不再因小版本标签被拒。原代码来源、公开导出操作及实际输入绑定继续核实。8 项版本定向、整个 owner refresh port 文件 24 项通过，独立审查 ACCEPT。未调用真实 API。

## 2026-11-16 已付回执与恢复边界

从 `14f1a1c` 的内容过滤恢复向前收敛。中性无答案决议以原始 HTTP 已收到、实付已结算、原请求绑定、缺有效 checkpoint 和 owner 授权为准。具体解析失败与输出截断仍用各自的证据，供应商状态原样保留。后续 retry 沿现有公开授权和代际路径。

## 2026-11-15 总控状态同步 — MC-QA

本包独立审查 72fae07 为 ACCEPT；固定组合在 StockQA 源码 pin f6897ac 验证 I1–I3。其后的 69406e8 及本次后续提交只同步 PWF Markdown，不改变或重验产品代码。当前另有 4 个源码/测试 WIP，由原 writer 持有且不属于固定组合。

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

## 11-14 固定组合来源

MC-QA 已完成并进入原固定组合。独立复核 72fae07 为 ACCEPT，StockQA f6897ac 是 I1–I3 的实际测试 pin。最新中性恢复演进及测试见 11-16 记录。

## 单一 Next Step

沿公开恢复接口接续真实任务。现有 provider、Runtime 和 Company Agent 继续消费正常回执及状态，新的 provider 标签由 StockQA 中性决议机制处理。原 checkout 的四个 WIP 由对应 owner 继续维护。


### 2026-11-20 F4-STOCKQA

本仓候选补齐原已收结算的分诊、派生终态、只读复验、可恢复回告。独立便携账本和实际宿主中断恢复已跑通，交 ROOT 完成本机上线和组合签收。 [执行版本和复验](F4-STOCKQA.md)
