# MC-QA 进度记录

## 2026-11-16 已付回执合同演进

- 原补丁可关闭已收到的 content_filter。RED 复现同样已实付的陌生标签及空标签仍被拒，两个新正例转绿。中性结案继续使用已收到且已实付的原回执和 owner 授权，原始标签、任务绑定、费用和输出保持可追溯。
- 实际恢复构造覆盖逐题保存、同一原尝试、重复恢复与事务撤权。撤权反例在 SQLite 尝试插入 checkpoint 后触发并核对回滚。测试守卫使用自己的临时目录和最小环境，验证对网络、外部 SQLite 与外部文件写入的拒绝。
- 从本轮隔离工作树运行 `tests/unit/test_quick_scan_known_outcome.py`，**57 passed（118.18s）**。独立复核通过，包含未知发送、具体诊断证据、回滚及有效题继续恢复。历史 I1–I3 pin 仍按原报告引用。

## 2026-11-11

### 归拢

- 3-way 合并脚本（base=HEAD blob, ours=canonical, theirs=candidate）跑 87 个双改路径 + 17 个 untracked：
  53 same / 13 theirs-only / 16 merged-clean / 18 CONFLICT；untracked 中 6 new-same / 7 new-DIFFER。
- 18 个冲突路径逐个做符号级超集验证（`ast` 顶层名对比）：除 `quick_scan_shared_company.py`
  （canonical 独有 `_require_company_scan_task_port`/CA 源路径常量——正是包卡要移除的）外，
  其余 17 个候选均为严格超集 → 全部按候选解决。
- 合并后校验：worktree 104 个改动路径与候选字节一致（CRLF 归一）。
- 工作树 EOL 陷阱：初始用 `core.eol lf` 落盘导致字节 hash 夹具失败（候选磁盘为
  autocrlf=true 的 CRLF）。恢复 `autocrlf=true` 并把 437 个未改动 LF blob 文件重写为 CRLF；
  `git diff` 证实无内容漂移。

### 测试与提交

- 短组（包卡入口 + 新文件）：99 passed → 收敛后 99 passed → 含回归组 336 passed / 41 skipped。
- mypy（5 个触碰文件）：候选 24 errors → 0 errors（净修 4 个 `_store` attr-defined + 候选既有噪音）。
- pre-commit（black/isort/mypy/mixed-line-ending/secrets）：全部通过。
- 提交：`9585431`（实现，110 文件 +9434/−1772）。

### 独审

- 审查 agent（独立会话）读完整 diff `4c24789..9585431`，亲跑短组 + mypy：
  **ACCEPT-WITH-FIXES**。
  - 已验证正确：facade 有限投影（含值导出、module 出境阻断）、组合根 facade 协议校验、
    发送/重放分离未被削弱、owner port 不透明性与幂等 adapt、SQL 迁移逐字保留、
    归拢无缺漏（字节对比）、无秘密。
  - F1(major)：companion facade import 守卫随扫描器收敛而丢失，无替代 → 已补
    （含 `importlib.import_module` 属性调用形态，反例探针验证）。
  - F2(major)：owner export 检查退化为仅 ImportFrom 名 → 已补模块别名属性链检查。
  - F3(minor)：vacuous `undeclared_calls` 断言 → 已删（同属防御由 F2/私有成员检查承担）。
  - F4(minor, 既有)：SQL 匹配需尾空格 → 补 padding。
- 修复提交：`72fae07`；边界测试 12 passed；F1/F2 反例探针通过。
- 复核（同一审查 agent，针对 delta）：**ACCEPT**——F1 五种违规形态 + 对照组合法形态、
  F2 别名属性链绕过均被捕获且定位正确；真实 src 树 33 passed 零误报。
  非阻塞残留 R1（别名动态 loader）/R2（全路径 Attribute 链）记录于 findings，不修关包。

### 真实缺陷修复记录（候选带入，RED→GREEN）

1. `LLMRunner._recover_shared_company_answers` 引用未接线的 `self._store`
   （候选为过守卫把 `session.service.store` 改成 `self._store` 却没接线）→
   组合根在 `adapt_owner_refresh` 前取 `prepared_session.service.store` 显式传参；
   opaque port 不携带 store，测试 fake 无 service/store 属性以钉死回归。
2. 恢复预算路由引用先于定义的 `work_store`（recovery 路径 NameError）→ `recovery_store`。
3. `test_paid_runner_independence` 的 owner double 缺 `snapshot()`（port 化前形态）→
   更新为 detached snapshot 形态。

### 环境事实

- 本机 Python 3.13.9；`iqs_company_agent_lab`/`iqs_agent_runtime_lab` 未安装 → 真包组合测试 skip。
- `IQS_ORIGINAL_ROOT`/`IQS_ROUTE_TEST_CODE_ROOT` 未配置 → 对应测试干净 skip（配置后 13 passed 验证过）。
- 测试临时根统一 `--basetemp` 到 `%TEMP%/iqs-mcqa-merge/bt*`，未触生产库/密钥/收费 API。
## 2026-11-15 总控固定组合状态同步

- 总控报告 invest-quick-scan/docs/implementation/parallel-lanes/packages/2026-11-11-modularity-closeout/fixed-combination-2026-11-14.md 在 StockQA f6897ac5c437f6baa36bae336d2c47a7e1722ad1 上通过 I1/I2/I3。独立总组合边界审查 final_boundary_review 为 ACCEPT；本包自己的审查 72fae07 已在本目录 Findings/交接记录为 ACCEPT。
- 本轮状态同步前，canonical master@f6897ac 有 4 个源码/测试 tracked WIP：src/utils/quick_scan_refresh_inspect.py、tests/unit/test_quick_scan_public_api_contracts.py、tests/unit/test_quick_scan_refresh_inspect.py、tests/unit/test_unusable_checkpoint_retry.py。这些文件不在固定组合中；不据固定组合推断其行为已测。
- 本次仅同步计划与证据引用，不改 StockQA 源码或上述 WIP，不调用模型/搜索 API。

## 2026-11-15 PWF-only follow-up

- StockQA PWF-only commit 69406e8 is pushed to origin/master. This follow-up clarifies that f6897ac is the tested source pin and subsequent commits contain only PWF text. Four source/test WIP files remain unchanged and outside the fixed combo; no product tests rerun.

## 2026-11-19 F2-STOCKQA-FEEDBACK 逐题结算观察

共因：公开只读 inspect 只回答“能不能发”，不回答“这题之前发生过什么”。已收到并结算但无可用答案、可读答案、发送后结果未知三种状态在消费方看来同形，前者被重发会重复付费，后者被当成确定无答案会抹掉真实风险。原有 work journal、已付可见回执、checkpoint、预算结算和 outbox 已经各自持有事实，缺的是把它们投影成逐题、可跨仓回告的稳定观察。

验证范围：只读投影 + 公开 CLI 旁路 + 单根因修复技能。共享检查器只承接恢复线已完成的 `checkpoint_record` 契约（`9361aa8`，协议 1.3.0），本卡不再改它。

- 实现：`ab42dd9`（base `9be7176`）。新增 `src/utils/quick_scan_feedback_observation.py`、
  `tests/unit/test_quick_scan_feedback_observation.py`（27 例）、
  `skills/repair-stockqa-issue/SKILL.md`；`quick_scan_refresh_inspect_cli` 在打印前调用旁路。
- 词表：`settled_no_answer` / `answer_readable` / `answer_unusable` / `send_unknown` /
  `late_receipt_observed` / `unrecognized_label` 等；`deviation` 只在同一快照内可证，
  跨快照漂移报 `snapshot-consistency=unknown`；`delivery-state`、`retained-body`、
  `provider-label` 分别覆盖 outbox 阻塞、已付正文可恢复、迟到回执新标签。
- 结果：定向组合 98 passed（含边界与公开契约扫描）；账本 230 passed；新增 27 passed。
  旁路写失败、账本被删、未设 `RESEARCH_FEEDBACK_OUT` 时 stdout/退出码/账本字节均不变。
- 未决：`test_quick_scan_known_outcome.py`、`test_unusable_checkpoint_retry.py` 因本机缺
  `subject_dispatch_contract` 记为 skipped；`test_received_without_answer.py` 的 v17 迁移
  用例在 base `9be7176` 干净工作树同样失败，归原迁移线。
- 交接：`.planning/modularity-closeout-20261111/F2-STOCKQA-FEEDBACK-handoff.md` 与同名
  `-sample.jsonl`（真实 CLI 子进程产出的五行脱敏观察，含费用未知样本）。
- 独审：`0c7b623` REJECT → `4a42b16` REJECT → `d481df3` ACCEPT；
  另一名全新独立 agent 复核 `b06f23a` 为 INCOMPLETE（迟到新标签、机器摘要、费用样本、
  最终 SHA 未审），修复后由该审查者核对 `ab42dd9`。
- 清理：pytest 临时根均在 `.test-runs/`（仅合成 SQLite），批量删除被本机 safe-delete
  阈值拦截，未强删；临时 worktree 与临时样例目录已删。
