## 2026-11-21 F4 运行失败记录和原账回查

此前门禁简化已并入本仓发布线 f69913c5。F4 续作在本仓实施，公开入口、原生本地 outbox、ROOT 导入与独立验收分开跟踪。本节点的测试、发生定位和交接入口见 [F4-STOCKQA.md](F4-STOCKQA.md)。只有代码合入发布分支、独审和总控消费被实际复验后才分别登记相应状态。


## 2026-11-21 门禁简化与统一未发送恢复已独审通过

本节点完成：原正常两题包26次外部owner调用降为3次（2登记+1收费intent前权限）；固定绑定本地验证、已付复用不重新审批。未发送清理只用原binding在同一事务委托public release_unsent，删除transport约70行重复证明，覆盖prepare提交前/提交后未返回、入口校验、provider渲染失败。真实已发/已响应/未知由原native账本阻止误放，不新增schema/协议/授权。

公开1.3保持旧required面，新local validation按能力选择、缺省退回原check；一处public bounded cause/context遍历供core/lifecycle/transport共用，保留显式、隐式、from None及双link中的真实typed unknown。可选安全诊断失败不再阻清理，F4通用日志仍另harness负责。

直接相关181项通过；尾改48项/30.54秒（新daily两套43项+compat2+CI plan3），两套已进原日常CI，SQLite3.10缺细节仍可恢复。非作者独验17项、尾改10项及原public API3项通过，最终ACCEPT，无本组P1/P2；不将重叠计数相加。临时根均清理，测试无API/生产库写入。

下一步由IQS原公开任务继续中微16实体+2挂牌，保留原10已付与原8历史；F4不是前置。本节点源码/验证已形成自包含改动，发布版本由Git及IQS总控PWF登记；不以F4另线进度阻止发布。

## 2026-11-21 原扫描诊断与重复检查收口

总控原PWF收尾：中微两题在真实HTTP前失败，原日志只存异常类型，历史底层原因未知；不能推定为SQLite锁或权限拒绝。用户最新将F4交其他harness，本线程只修复杂设计/门禁与接续；已做F4-only源码与测试保存在IQS runs/amec-rescore-20261121/f4-handoff并从活跃树剥离，不能把其待办作为普通扫描前置。F4原79b0800指定settlement范围仍有效。

本轮保持原请求、账本、预算、旧回答及对外入口：

|工作|完成条件|状态|
|---|---|---|
|未发送恢复|prepared、无实际发送/响应的原pack原子归还；已发未知不重发|作者65项、非作者63项通过，ACCEPT；待组合发布|
|重复检查|固定绑定不反复外部核查；原子intent前一次当前权限检查，原预算/lease仍负责防重复|两题真实隔离RED共26次CLI；目标3次；实施中|
|F4交接|保留日志调查、候选和开放P2，另一harness接续；本组只保留自身必要安全诊断|已隔离；不继续实施、不阻塞门禁收口|
|统一未发判断|原binding给状态owner按真实native事实释放，无需prepared对象先返回或transport重复证明|独审3例RED；正收敛到原public release_unsent|

只跑受影响组合并做一次独立终审；不增加逐题审批或小节点门禁。通过后沿原IQS公开任务继续中微，不修改另一harness的三市场配置、工作区、冻结答案或全局policy。实际命令和计数写本目录progress；跨仓交付由IQS根PWF统筹。

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

原F4结算投影支持范围已通过79b0800发布并完成本机接线，company-wiki F4上线表已签收。独立便携账本和实际宿主中断恢复证据保留；普通扫描错误诊断的新增缺口见本页11-21段，不把原F4改称未上线。 [原执行版本和复验](F4-STOCKQA.md)
