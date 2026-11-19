---
name: repair-stockqa-issue
description: 从本仓问题卡或 feedback-observation/1 观察行出发，复现并修复 StockQA 逐题结算、恢复与只读投影的合同偏离。用于先读原生结算证据、写 RED、再修共同责任层并用原公开入口复验；费用或发送状态未知时先走本仓只读对账，绝不重发付费请求。
---

# 修复 StockQA 问题

本技能一次只处理一个根因，且只改本仓（StockQAbyLLM）能负责的部分：逐题工作项、尝试、已付回执、checkpoint、预算结算、结果 outbox，以及它们对外的只读投影和 `feedback-observation/1` 侧车。

供应商真实调用、生产账本、IQS/Dashboard 的消费与展示归各自原负责线。本仓只消费它们的公开结果和引用，不复制对方正文、凭据或物理路径。

## 输入

从以下任一入口开始，不要求原回答正文、密钥或生产库路径：

- 本仓 PWF 下的问题卡：`.planning/<plan>/issues/<issue>.md`。
- 一次真实运行的 `feedback-observation/1` JSONL（`check_id` 为 `settled-disposition`、`generation-binding` 或 `cost-state`）。
- ROOT 给的公开复现引用：主体、题目、代际、请求结构、退出码和脱敏公开结果。

先确认三件事，再动手：

1. **预期**：原生结算类别（`settled_no_answer` / `answer_readable` / `send_unknown` / `not_attempted` / `answer_unsettled`）。
2. **实际**：公开 inspect 投影给出的状态词（`failed_settled` / `answer_ready` / `answer_delivered` / `send_uncertain` / `retry_scheduled` / `not_projected`）。
3. **版本与输入来源**：实现 SHA、`version` 字段（inspect 协议）、账本 schema 版本，以及输入是合成夹具还是真实账本。

## 读观察行

侧车由公开入口 `src.utils.quick_scan_refresh_inspect_cli` 产生，每次调用一个专用输出（`RESEARCH_FEEDBACK_OUT`），父子进程各自独占。

| check_id | expected | actual | 含义 |
|---|---|---|---|
| `settled-disposition` | 原生结算类别 | 公开投影状态词 | 每题一行；`disposition` 只表示“公开结果是否忠实反映原生结算” |
| `generation-binding` | `latest_unresolved_generation` | `stale_generation` | 只在投影选了旧代际而存在更新未决代际时出现 |
| `cost-state` | `cost_unknown` | `cost_<budget_status>` / `cost_zero_unverified` / `cost_unverified` | 费用未结清或已结算金额缺来源引用；永远记 `unknown`；`in_flight` 不报 |
| `delivery-state` | `delivery_available` | `delivery_blocked` / `delivery_conflict` / `delivery_rejected` / `delivery_unreadable` | outbox 已终结但送不到消费方；只记 `unknown`，不改判结算 |
| `retained-body` | `no_retained_body` | `visible_body_present` / `visible_body_unreadable` | 已结算无答案但付费可见日志仍留有正文（或读不出）；恢复候选，不是重发理由 |
| `provider-label` | `recognized_label` | `unrecognized_label` | 迟到回执占据结算行时，新标签单独报告；只记 `unknown` |
| `snapshot-consistency` | `single_snapshot` | `ledger_changed_between_reads` | 公开结果与本次重投影之间账本被改过；该题只报漂移，不判偏差 |

原生结算类别（`expected`）：`settled_no_answer`、`answer_readable`、`answer_unusable`、`answer_unsettled`、`send_unknown`、`not_attempted`、`cancelled`、`refused_settled`，以及待查的 `missing_receipt`、`unreadable_receipt`、`unrecognized_label`、`late_receipt_observed`、`late_receipt_unresolved`、`unclassified`。

公开投影状态词（`actual`）：`failed_settled`、`answer_ready`、`answer_delivered`、`send_uncertain`、`retry_scheduled`、`cancelled`、`not_projected`、`unclassified`。

判定规则：

- `settled_no_answer` + `failed_settled` + `expected`：已收到、已结算、无可用答案。该工作项**不可再次领取**，任何“再发一次”的建议都是偏离。
- `settled_no_answer` + `retry_scheduled` / `send_uncertain` + `deviation`，且 `issue_key=settled-answer-replayed`：公开投影把已结算题重新变成可发状态，会重复付费。本仓修投影或恢复分支。
- `answer_readable` + `retry_scheduled` + `deviation`（`issue_key=readable-answer-replayed`）：已有可读 checkpoint 却仍要求重发。先查 outbox 与恢复入口，不重发。
- `answer_readable` + `failed_settled` + `deviation`（`issue_key=readable-answer-marked-settled`）：可读答案被标成已结算无答案，恢复路径会丢掉已付结果。
- `answer_unusable` + `answer_ready` / `answer_delivered` + `deviation`（`issue_key=unusable-answer-presented-ready`）：checkpoint 不可用（研究型或不满足合同）却被当成可交付答案；先修 checkpoint 判定，不要把损坏答案送出去。
- `answer_unusable` + `failed_settled` + `expected`：不可用 checkpoint 与已结算无答案一致，无需动作。
- `send_unknown` + `failed_settled` + `deviation`（`issue_key=unknown-send-classified-settled`）：把未知发送抹成确定无答案，会丢失真实风险。恢复为 unknown，不改写成失败。
- `refused_settled`：已确认的供应商拒绝（401/403/404/429 等）。`retry_scheduled` 或 `failed_settled` 都合法，取决于原作业是否已终结；不改写成 unknown。
- `late_receipt_observed` / `late_receipt_unresolved`：迟到回执到达（带正文或只有哈希）。`disposition=unknown`，先只读对账；迟到正文可能正是缺失的答案，不得先重发。
- `unrecognized_label` / `missing_receipt` / `unreadable_receipt` / `unclassified`：`disposition=unknown`。这是**待查**，不是偏差，也不是零答案。
- `snapshot-consistency` 为 `unknown`：账本在两次读之间变化，先重跑公开 inspect 再判断，不得据此立卡。
- `delivery-state` 为 `unknown`：答案可读但 outbox 被 block/conflict/rejected，或交付记录损坏。先查投递层与恢复入口；该题的结算判定仍成立，不要把投递故障改写成无答案。
- `retained-body` 为 `unknown`：已结算无答案，但付费可见日志仍有正文（`visible_body_present`）或读不出（`visible_body_unreadable`）。这是恢复候选：先走本仓已付结果恢复入口只读核对，**不得**因为“可能有答案”而重发付费请求。
- `provider-label` 为 `unknown`：迟到回执上的 provider 标签不在已知词表（`completed` / `executed`）。保持未知，交给供应商/原 provider 线解释；不要据此判成无答案，也不要据此重发。
- `cost-state` 出现即费用未知：保留 `unknown`，按原作业只读对账；不得写成 0，也不得据此发起新付费尝试。
- 只有 `generation-binding` 偏离、`settled-disposition` 正常：修投影的代际选择，不动结算。

`issue_key` 是经 owner 认可的规则签名；没有 `issue_key` 的 `deviation` 先按原始 `evidence_ref` 复核，再决定是否立卡。

## 最小脱敏回放

只用本仓公开入口和一次性临时目录，不接真实 provider、不读生产账本、不使用真实密钥：

```powershell
python -B -m pytest -q -p no:cacheprovider --basetemp=<本轮临时根> tests/unit/test_quick_scan_feedback_observation.py
```

用合成账本产生一份真实入口观察（`<tmp>` 为本轮一次性目录）：

```powershell
$env:RESEARCH_FEEDBACK_OUT = "<tmp>\inspect.jsonl"
$env:RESEARCH_FEEDBACK_RUN_ID = "local-replay"
python -B -m src.utils.quick_scan_refresh_inspect_cli --ledger <tmp>\ledger.sqlite --policy <tmp>\policy.json --request <tmp>\request.json
```

现有母例（`tests/unit/test_quick_scan_feedback_observation.py`）：

| 场景 | 观察到的合同 |
|---|---|
| 已收到并结算、无 checkpoint | `settled_no_answer` / `failed_settled` / `expected`，工作项不可再领取 |
| 可读 checkpoint | `answer_readable` / `answer_ready` / `expected` |
| 发送后结果未知 | `send_unknown` / `send_uncertain` / `expected` |
| 有 `response_available` 但无回执 | `missing_receipt` / `unknown` |
| 迟到回执带正文 | `late_receipt_observed` / `unknown`，不并入 `send_unknown` |
| 未识别的 provider 标签 | `unrecognized_label` / `unknown`，不判为无答案 |
| 不可用 checkpoint | `answer_unusable`；投影成可交付即 `deviation` |
| 已确认的供应商拒绝 | `refused_settled` / `retry_scheduled` / `expected` |
| 旧代际已结算 + 新代际未开始 | 观察新代际，`evidence_ref` 指向 `g2`，不串号 |
| 假零费用 | `cost_zero_unverified` / `unknown` |
| 投影错误地重新发题 | `deviation` + `issue_key=settled-answer-replayed` |
| 可读答案但 outbox 被 block | `delivery-state` / `delivery_blocked` / `unknown`，结算仍 `expected` |
| 已结算无答案但留有付费正文 | `retained-body` / `visible_body_present` / `unknown`，先只读恢复 |
| 迟到回执带新标签 | `late_receipt_observed` + `provider-label` / `unknown`，两个事实分开 |
| 两次读之间账本被改 | `snapshot-consistency` / `unknown`，不判偏差 |
| 侧车写失败 / 账本损坏 | 业务结果与退出码不变，只少诊断 |

新增母例时扩展该测试文件的合成账本，不改生产代码来迎合夹具。

## 责任判定

| 现象 | 首先归属 | 处置 |
|---|---|---|
| 逐题结算、尝试代际、checkpoint 判定、outbox 状态错误 | StockQAbyLLM | 本仓修共同责任层和测试 |
| 公开投影把已结算题重新变成可发 | StockQAbyLLM | 修投影/恢复分支，补 RED |
| 侧车字段、稳定性、写失败隔离 | StockQAbyLLM | 修 `quick_scan_feedback_observation.py` 与 CLI 适配 |
| 真实供应商返回新标签的含义 | 供应商 / 原 provider 线 | 保持 `unknown`，不在本仓猜类别 |
| 生产账本费用、真实用量 | 原作业 owner | 只读对账，引用原作业；本仓不复制金额 |
| IQS/Dashboard 展示与消费 | IQS / Dashboard | 交 ROOT 带公开 DTO 和观察引用 |

外仓原因必须连回原始 `evidence_ref`（`qa:<work_item_id>/g<generation>`），本仓不复制对方正文、路径或凭据。

## RED → GREEN

1. 先在 `tests/unit/test_quick_scan_feedback_observation.py` 或对应定向文件写 RED：走真实 `main(argv)` / `inspect_refresh` 边界，断言第一次偏离合同的位置。
2. 确认 RED 是合同问题而不是夹具问题：同一场景在未改代码的基线上也应失败。
3. 修共同责任层（`src/utils/quick_scan_feedback_observation.py`、`src/utils/quick_scan_refresh_inspect_cli.py` 或对应 owner 模块），不修测试来消除 RED。
4. 定向 GREEN 后跑相邻合法对照：
   ```powershell
   python -B -m pytest -q tests/unit/test_paid_visible_journal.py tests/unit/test_quick_scan_refresh_inspect.py
   python -B -m pytest -q tests/unit/test_quick_scan_feedback_observation.py tests/unit/test_quick_scan_module_boundaries.py tests/unit/test_quick_scan_public_api_contracts.py
   ```
5. 记录修复版本（SHA）、回归用例、观察 `version` 与消费端待验/已验状态。同一版本、同一输入、同一失败不触发循环重跑；有新证据时用新的 `attempt_id` 关联首笔发生，并保留原 `evidence_ref`。

## 写回

- 具体缺陷：在本计划 `issues/` 留最小脱敏复现、责任仓、实际修复和复验版本。
- 计划状态：更新本仓 PWF 的 `progress.md` / `findings.md`，写清命令、通过/失败/跳过数和清理结果。
- 跨仓组合、真实 provider 复验和消费端采用由 ROOT 回告；本仓只交公开观察、版本和可复验引用。
