# S5-08 StockQA 执行装配与结果边界

基线 `0674d510cf6aff25d07b06439cbc35c51c66e948`。工程工作在 `codex/s5-stockqa-lifecycle` 分支。正式诊断发布引用继续指向基线。

## 职责

| 现役依赖 | 所属责任 | 公共交接 |
| --- | --- | --- |
| runner 日志读取 lifecycle 的 `_lease_seconds` | CLI 在装配阶段校验新 claim 的租约输入，原生命周期依输入申请 | 使用同次已验证的输入输出计划日志 |
| runner 输出读取 lifecycle 的 `_store` | 工作账本负责真实 attempt 和 checkpoint，结果序列化只需只读证明投影 | 装配时从原账本建立既有 `QuickScanResultProofPort`，正常与 paid recovery 均用此端口。保留旧调用已有兼容入口 |
| runner 输出读取 lifecycle 的 `_owner_refresh` | native owner 负责原选择、结算及复用，装配阶段已有其受限公开端口 | 使用当前装配出的原 owner port，原 result builder 验真和透传 |

这三处位于同一源文件的两个责任对象之间，实际运行可以用公开操作生命周期替身复现。把输出交接改回已存在的公共协议，减少生命周期对展示阶段的绑定。后续的 `registered_research_work` 包含原工作与授权配置、工作账本阶段端口尚未细分。另有延迟导入和仓内规模治理，按实际阻塞逐片推进。

## 验收

- 用真实 main + 合成身份、路由及 HTTP，分别隔离掉一个字段，证明 `_lease_seconds` 进入处理前失败，`_store` 可在实际运输后导致原输出中止。复验已验证的 61 秒只适用新 claim，原支付预算计数始终只有一次，输出阶段只持有公开证明端口。
- 复用现有跨模块 C06 原生 owner 测试，验证一次真实合成运输、原 owner 封存、keyless 恢复零 POST 和可读输出。依赖复用已冻结本地 StockWiki 和 IQS，场景根独立并拦截网络。
- 运行既有工作生命周期、权威边界、证据输出与本仓日常编排。将已确认的行为纳入现有测试选择。

## 作者验证

原 main 的两条 RED 分别触发租约日志和原运输后输出。修复后的工作生命周期、费用、原 owner 和结果证据组合 234 项通过，4 个需另一个外部运行根的可选项保持跳过。现有统一静态编排全部通过。

本仓正式每日 pytest 面板用原清单通过 424 项，用时 209.29 秒。改动增加一个关键路径反例，沿用原有预算、费用和执行检查。

## C06 独立夹具

在 StockQA 专用临时根准备物理 `qa`、`sw`、`iqs` 和 `guard`。QA 测试进程及子级只读检视的代码根共用同一物理 `qa` 目录，分别与新旧候选运行。StockWiki 仅取 `d09e2be` 的代码、配置、框架、合成身份种子及市场注册表，IQS 取 `2d803a9` 的源码、模板、schemas 和历史 producer。Windows 下使用既有 baseline 校验所要求的原字节。使用 `tests/fixtures/delegated_guard/sitecustomize.py` 拒绝网络和隔离根外状态写入。`IQS_C06_RUN_INTEGRATION=1`、`IQS_C06_OWN` 指向本次独占根，以 `qa` 为工作目录跑现有 `tests/integration/test_controlled_pack_main_public.py`。原部署、生产状态和正式结算独立。

两个物理场景均完成身份解析、原授权确认、正式注册和同确认回放。基线在真实 main 的输出阶段记录三处私有读取并失败（758.27 秒）。候选按原流程形成 `result_ready`，合成 HTTP 只调用一次。撤去测试供应商配置和预算授权文件后，原已付作业恢复及输出通过，`API_requests=0`（776.57 秒）。这是沿原既有选择执行的大节点合成验收，本仓日常责任包沿用原入口。
