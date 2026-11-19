# F2-STOCKQA-FEEDBACK 交接 — StockQA 逐题结算与恢复反馈

2026-11-19。本卡在本仓现有恢复线上接续完成，未另开工作树。

| 项目 | 值 |
|---|---|
| Lane | `F2-STOCKQA-FEEDBACK` |
| Owner | `stockqa` |
| 工作目录 | `C:/Users/郑曾波/Projects/StockQAbyLLM` |
| 分支 | `codex/qa-content-filter-resolution-01` |
| base（开工 HEAD） | `9be7176a256a3b90a394169d169d9542592c1cef` |
| 恢复线收口 | `9361aa8`（把 `checkpoint_record` 纳入只读视图契约，协议 1.3.0） |
| implementation | `ab42dd9e43d77589f5e770dfe9034deb0cf6593e` |
| reviewed_commit | `ab42dd9e43d77589f5e770dfe9034deb0cf6593e`（最终独审核对，见下） |
| 样例 | `.planning/modularity-closeout-20261111/F2-STOCKQA-FEEDBACK-sample.jsonl` |

## 交付内容

1. `src/utils/quick_scan_feedback_observation.py`（新）：只读投影逐题原生凭证并导出
   `feedback-observation/1` JSONL。复用 work journal（`work_item` / `attempt` /
   `work_event`）、已付可见回执（`quick_scan_attempt_response` + 原生
   `response_record` 校验）、付费可见日志（`get_research_visible_output`，只取是否存在，
   不复制正文）、`answer_checkpoint`（原生 `checkpoint_record` 校验）、
   `quick_scan_budget_attempt/terminal` 与 outbox（`get_result_delivery`）。
2. `src/utils/quick_scan_refresh_inspect_cli.py`：在既有 `print` 之前调用
   `emit_refresh_observation`。可选 `RESEARCH_FEEDBACK_OUT` /
   `RESEARCH_FEEDBACK_RUN_ID` / `RESEARCH_FEEDBACK_ATTEMPT_ID`；未设置不产出，
   写失败只丢诊断，stdout、stderr、退出码和账本不变。
3. `tests/unit/test_quick_scan_feedback_observation.py`（新，27 例）。
4. `skills/repair-stockqa-issue/SKILL.md`（新）：单根因修复技能，先读原生结算，
   再在公开入口写 RED，修共同层，跑合法相邻对照；未知费用/发送走只读对账。
5. `.planning/modularity-closeout-20261111/progress.md`：共因与验证范围补记。

开工核对：`git status --short --branch` 确认在途改动只有原恢复线的四个文件；
`python -B -m src.utils.quick_scan_refresh_inspect_cli --help` 确认公开参数未变
（`--ledger/--policy/--request/--schema-version/--admission-config`）；离线夹具
`tests/unit/test_received_without_answer.py` 与 `test_quick_scan_known_outcome.py`
给出“已收到并结算无答案”与“发送未知”的原生依据，本卡据此写 RED 与合法对照。

本次未发现新的生产缺陷：偏离都在新增投影自身，由独审逐项关闭（部分在首次合入后以新提交关闭，再由审查者核对最终 SHA），因此 PWF 只记共因与验证范围，未另立 `issues/` 卡。

## 公开调用方法

```powershell
$env:RESEARCH_FEEDBACK_OUT = "<本次专用输出>.jsonl"
$env:RESEARCH_FEEDBACK_RUN_ID = "<运行标识>"
$env:RESEARCH_FEEDBACK_ATTEMPT_ID = "<本次尝试标识>"
python -B -m src.utils.quick_scan_refresh_inspect_cli `
  --ledger <owner 账本> --policy <policy.json> --request <request.json>
```

- 输出为 UTF-8 JSONL，一行一个检查；同一 `(owner, run_id, attempt_id, step_id, check_id)`
  重复导出保持稳定，消费端按该身份去重。
- `step_id` 形如 `ENT_BYD/question-IQS_05-inspect`（跨主体、跨兄弟题区分）。
- `attempt_id` 默认取原生 `attempt.attempt_id`（真实重试天然另编号）；无尝试时用
  `inspect-g<generation>a0`；`RESEARCH_FEEDBACK_ATTEMPT_ID` 可覆盖。
- `evidence_ref` 为 `qa:<work_item_id>/g<generation>`，指向可只读复核的原生工作项。
  行内无凭据、回答正文、SQL 文件路径或未经证实的金额。

## 检查词表

| check_id | expected | actual | disposition |
|---|---|---|---|
| `settled-disposition` | `settled_no_answer` / `answer_readable` / `answer_unusable` / `answer_unsettled` / `send_unknown` / `not_attempted` / `cancelled` / `refused_settled` / `missing_receipt` / `unreadable_receipt` / `unrecognized_label` / `late_receipt_observed` / `late_receipt_unresolved` / `unclassified` | `failed_settled` / `answer_ready` / `answer_delivered` / `send_uncertain` / `retry_scheduled` / `cancelled` / `not_projected` / `unclassified` | 可比较项按词表判定 `expected`/`deviation`；待查项强制 `unknown` |
| `generation-binding` | `latest_unresolved_generation` | `stale_generation` | `deviation`（`issue_key=stale-generation-projection`） |
| `delivery-state` | `delivery_available` | `delivery_blocked` / `delivery_conflict` / `delivery_rejected` / `delivery_unreadable` | `unknown` |
| `retained-body` | `no_retained_body` | `visible_body_present` / `visible_body_unreadable` | `unknown`（已结算无答案但付费正文仍在，恢复候选） |
| `provider-label` | `recognized_label` | `unrecognized_label` | `unknown`（迟到回执占据结算行时单独报告新标签） |
| `cost-state` | `cost_unknown` | `cost_<budget_status>` / `cost_zero_unverified` / `cost_unverified` | `unknown` |
| `snapshot-consistency` | `single_snapshot` | `ledger_changed_between_reads` | `unknown` |

`issue_key` 只在可证偏差上出现：`settled-answer-replayed`、
`readable-answer-replayed`、`readable-answer-marked-settled`、
`unusable-answer-replayed`、`unusable-answer-presented-ready`、
`unknown-send-classified-settled`、`stale-generation-projection`。

## 实际命令与结果

工作树内命令，Python 为 `C:/Miniconda/python`（托管 Python 3.13.12 未装 pytest）。

| 命令 | 结果 |
|---|---|
| `python -B -m pytest -q tests/unit/test_paid_visible_journal.py tests/unit/test_quick_scan_refresh_inspect.py` | 45 passed（开工基线） |
| `python -B -m pytest -q tests/unit/test_quick_scan_module_boundaries.py tests/unit/test_quick_scan_public_api_contracts.py` | 26 passed |
| `python -B -m pytest -q tests/unit/test_quick_scan_work_store.py tests/unit/test_q07_checkpoint.py tests/unit/test_quick_scan_result_outbox.py` | 230 passed |
| `python -B -m pytest -q tests/unit/test_quick_scan_feedback_observation.py` | 27 passed |
| `python -B -m pytest -q tests/unit/test_quick_scan_feedback_observation.py tests/unit/test_paid_visible_journal.py tests/unit/test_quick_scan_refresh_inspect.py tests/unit/test_quick_scan_module_boundaries.py tests/unit/test_quick_scan_public_api_contracts.py` | 98 passed |
| `python -B -m pytest -q tests/unit/test_quick_scan_known_outcome.py -k "unknown_send or settled_completed_physical_outcome"` | 1 skipped：本机未安装 `subject_dispatch_contract`（IQS pure-contract runtime），整模块 importorskip |
| `python -B -m pytest -q tests/unit/test_unusable_checkpoint_retry.py` | 1 skipped：同一缺失依赖 |
| `python -B -m pytest -q tests/unit/test_received_without_answer.py` | 8 passed，1 failed（`test_v17_migration_...`，`quick-scan work database schema mismatch`）；在 `9be7176` 的干净工作树复跑同样失败，属既有缺陷，不在本卡写集合 |
| `ruff check` / `mypy`（新增与改动文件） | 通过 |
| `pre-commit`（提交时） | 全部通过 |

## 独审记录

独立审查 agent（未参与实现）对照本卡做两轮大节点审查，均以最终源码版本和复现命令为准。

**第一轮 `0c7b623`：REJECT。** findings：

1. major：不可用 checkpoint（研究型或合同不满足）落到 `answer_unsettled`，已结算但无可用答案被报成正常。
2. major：迟到回执带正文时 attempt 仍在 `uncertain`，被并入 `send_unknown`。
3. major：`expected` 与 `actual` 取自两次读快照，并发写者会被记成 `settled-answer-replayed`。
4. minor：`in_flight` 预留被系统性报成费用未知。
5. minor：`generation-binding`、`late_receipt_*`、`unreadable_receipt`、`cancelled`、`confirmed_failure` 无覆盖。
6. nit：`attempt_id` 用派生标签而非原生尝试标识。
7. nit：直接 SQL 与事件词表复制（接受，已在代码注释说明）。
8. nit：技能词表不完整。

**第二轮 `4a42b16`：REJECT。** 1–8 中 2–8 关闭；finding 1 的 `delivery_state` 仍是死字段——可读答案但 outbox 被 `c06_standard_answer_unavailable` 阻塞时仍显示“一切正常”。另发现漂移时跳过 `cost-state`（minor）。

**第三轮 `d481df3`：ACCEPT。** 新增 `delivery-state` 检查（blocked/conflict/rejected/unreadable → `unknown`，不改判结算），漂移时照常输出 `delivery-state` 与 `cost-state`；复核确认正常态 `ready`/`delivered`/`send_uncertain` 不误报，`generation-binding` 仍只在非漂移时输出。

**第四轮（全新独立 agent，不参考前三轮结论）`b06f23a`：INCOMPLETE。** 该轮逐句核对本卡，发现：

1. 最终 SHA 未被独审，交接的 `implementation`/`reviewed_commit` 仍停在 `d481df3`。
2. 迟到回执上的未识别标签被结算行吞掉（`late_receipt` 先于 `unrecognized_label`）。
3. 交接缺机器可读摘要块。
4. 样例没有费用未知行。
5. 交接的命令计数不是最终版本（25/96 vs 实际 26/97）。

处置：新增 `provider-label` 检查（迟到回执占据结算行时单独报告新标签）、样例补假零费用题、本文件补机器摘要与最终计数；`retained-body` 与上述改动一并提交为 `ab42dd9`，由该轮审查者复核。

**复核 `ab42dd9` + `217973b`：COMPLETE。** 五项逐一关闭；复核者另发现 `.secrets.baseline` 被本地扫描命令改写（工作树残留），已 `git checkout --` 恢复，未进入任何 F2 提交。审查者核对的实现 SHA 为 `ab42dd9e43d77589f5e770dfe9034deb0cf6593e`。

口径说明（审查者要求写入）：`settled-disposition` 的 `deviation` 只在**同一快照内**公开投影与原生结算矛盾时可证。若公开结果本身来自更早的快照，重投影会先发现漂移并只报 `snapshot-consistency=unknown`——这两种情形在数据上不可区分，当前选择是保守的 `unknown`，由消费端重读后判定。

## 逻辑引用

- 题目/尝试：`work_item(work_item_id, generation, question_id, scope, scope_id, status)`；
  `attempt(attempt_id, ordinal, phase, receipt_sha256, late_receipt_sha256)`。
- 结算：`work_event.event_type = "received_without_answer:<reason>"`（reason ∈
  `missing_question` / `invalid_question` / `answer_generator_rejected_member`）；
  `quick_scan_budget_attempt.status` 与 `quick_scan_budget_terminal.terminal_outcome`。
- checkpoint：`answer_checkpoint`（v1/v2 走 `checkpoint_record` 合同校验；v3 研究型
  记 `not_usable`）。
- 代际：同一 `(entity_id, question_id, scope, scope_id)` 下 `generation` 最大者；
  `owner_refresh_projection` 优先未决代际，观察侧再用同一算法重投影防串号。
- 原公开状态：`inspect_refresh` 的 `work_items[qid] = {work_item_id, generation, status}`。

## 本次真实入口样例

`F2-STOCKQA-FEEDBACK-sample.jsonl` 由真实 CLI（子进程，`-m
src.utils.quick_scan_refresh_inspect_cli`）在一次性合成账本上产出，共 5 行：
已结算无答案、可读答案、发送未知，以及一个假零费用题（`settled-disposition` +
`cost-state` 两行）：

```json
{"schema_version":"feedback-observation/1","owner":"stockqa","run_id":"f2-synthetic-inspect","attempt_id":"f2-synthetic-attempt-1","step_id":"ENT_BYD/question-IQS_05-inspect","check_id":"settled-disposition","expected":"settled_no_answer","actual":"failed_settled","disposition":"expected","evidence_ref":"qa:WORK_.../g1","version":"stockqa.refresh_inspection/1.0.0"}
{"schema_version":"feedback-observation/1","owner":"stockqa","run_id":"f2-synthetic-inspect","attempt_id":"f2-synthetic-attempt-1","step_id":"ENT_BYD/question-IQS_06-inspect","check_id":"settled-disposition","expected":"answer_readable","actual":"answer_ready","disposition":"expected","evidence_ref":"qa:WORK_.../g1","version":"stockqa.refresh_inspection/1.0.0"}
{"schema_version":"feedback-observation/1","owner":"stockqa","run_id":"f2-synthetic-inspect","attempt_id":"f2-synthetic-attempt-1","step_id":"ENT_BYD/question-IQS_07-inspect","check_id":"settled-disposition","expected":"send_unknown","actual":"send_uncertain","disposition":"expected","evidence_ref":"qa:WORK_.../g1","version":"stockqa.refresh_inspection/1.0.0"}
{"schema_version":"feedback-observation/1","owner":"stockqa","run_id":"f2-synthetic-inspect","attempt_id":"f2-synthetic-attempt-1","step_id":"ENT_BYD/question-IQS_08-inspect","check_id":"settled-disposition","expected":"settled_no_answer","actual":"failed_settled","disposition":"expected","evidence_ref":"qa:WORK_.../g1","version":"stockqa.refresh_inspection/1.0.0"}
{"schema_version":"feedback-observation/1","owner":"stockqa","run_id":"f2-synthetic-inspect","attempt_id":"f2-synthetic-attempt-1","step_id":"ENT_BYD/question-IQS_08-inspect","check_id":"cost-state","expected":"cost_unknown","actual":"cost_zero_unverified","disposition":"unknown","evidence_ref":"qa:WORK_.../g1","version":"stockqa.refresh_inspection/1.0.0"}
```

同一调用 stdout 的 `protocol=stockqa.refresh_inspection/1.0.0`、`API_requests=0`，
`work_items` 分别为 `failed` / `result_ready` / `uncertain` / `failed`，账本字节未变。

## 调用计数与费用

- 新增付费调用：0。新增网络调用：0。新增账本写入：0（测试逐次比较
  `ledger.sqlite` 字节；`inspect` 的 `API_requests` 恒为 0）。
- 费用未知保持未知：`cost-state` 只输出 `unknown`，不写金额、不推断币种；
  假零费用（已结算 `actual_cost_micros=0` 且无 `cost_source_ref`）报
  `cost_zero_unverified`，不折算为 0。

## 隔离与清理

- 全部夹具在 `tmp_path` 一次性目录；无生产账本、无真实密钥、无 provider 调用。
- 提交后工作树只剩任务开始前已存在的未跟踪目录（`.codegraph/`、`.workbuddy-ai/`、
  `.pytest-temp-i3-*`、`pilot_runs/*`、`nul`、`progress_update.txt`）。
- `.test-runs/` 下本轮 pytest 临时根（`f2-*`、`review*`、`audit*`、`fresh*`、
  `final-check`，仅合成 SQLite）删除时被本机 safe-delete 批量确认阈值拦截，未强删；
  目录未被跟踪，可直接整目录删除：`rm -rf .test-runs`。临时 git worktree
  `%TEMP%/stockqa-head-check` 与 `%TEMP%/f2-sample*` 已删除。

## 仍待 ROOT / 消费端验证

1. IQS 侧用公开 `feedback-observation/1` 消费这三类行，确认 `settled_no_answer`
   不再触发重发、`send_unknown` 不被当成无答案。
2. 真实供应商出现新标签（非 `completed`/`executed`）时，`unrecognized_label`
   的实际分布；本卡只用合成标签验证。
3. 真实生产账本上的 `cost-state` 对账：本卡只证明未知保持未知，未核对真实金额。
4. `test_quick_scan_known_outcome.py` 与 `test_unusable_checkpoint_retry.py` 需在装有
   `subject_dispatch_contract` 的环境复跑；本机缺失，记为 skipped 而非 passed。
5. 既有失败 `test_received_without_answer.py::test_v17_migration_preserves_rows_and_current_events_reject_sql_mutation`
   在 base `9be7176` 已复现，归原账本迁移线，不在本卡范围。
6. 本分支没有 upstream，未执行 push；按本仓发布安排需要推送时由 ROOT 决定。

## 机器摘要

完整 SHA 见上表；下面 JSON 使用 7 位短 SHA（本仓唯一），便于机器解析。

```json
{
  "lane": "F2-STOCKQA-FEEDBACK",
  "owner": "stockqa",
  "worktree": "C:/Users/郑曾波/Projects/StockQAbyLLM",
  "base": "9be7176",
  "implementation": "ab42dd9",
  "reviewed_commit": "ab42dd9",
  "tests": [
    {"command": "python -B -m pytest -q tests/unit/test_quick_scan_feedback_observation.py", "result": "27 passed"},
    {"command": "python -B -m pytest -q tests/unit/test_quick_scan_feedback_observation.py tests/unit/test_paid_visible_journal.py tests/unit/test_quick_scan_refresh_inspect.py tests/unit/test_quick_scan_module_boundaries.py tests/unit/test_quick_scan_public_api_contracts.py", "result": "98 passed"},
    {"command": "python -B -m pytest -q tests/unit/test_quick_scan_work_store.py tests/unit/test_q07_checkpoint.py tests/unit/test_quick_scan_result_outbox.py", "result": "230 passed"},
    {"command": "python -B -m pytest -q tests/unit/test_quick_scan_known_outcome.py -k \"unknown_send or settled_completed_physical_outcome\"", "result": "1 skipped (subject_dispatch_contract missing)"},
    {"command": "python -B -m pytest -q tests/unit/test_unusable_checkpoint_retry.py", "result": "1 skipped (subject_dispatch_contract missing)"},
    {"command": "python -B -m pytest -q tests/unit/test_received_without_answer.py", "result": "8 passed, 1 failed (pre-existing at base 9be7176)"}
  ],
  "sample_refs": [".planning/modularity-closeout-20261111/F2-STOCKQA-FEEDBACK-sample.jsonl"],
  "review_ref": ".planning/modularity-closeout-20261111/F2-STOCKQA-FEEDBACK-handoff.md#独审记录",
  "cleanup": ".test-runs/ only holds this round's synthetic SQLite; safe-delete threshold blocked bulk removal, rm -rf .test-runs is safe; temp worktree and temp sample dirs removed",
  "remaining": ["IQS consumer adoption and paid-result recovery", "real new provider label distribution", "real-ledger cost-state reconciliation", "rerun with subject_dispatch_contract installed", "branch not pushed"]
}
```

## ROOT 组合复验

首次正式交付 ca896a0，作者验收到 ab42dd9，ROOT 验收起点 8d27c36。补齐已发布 IQS 的同版公开 subject_dispatch_contract 后，实际旧付费回复恢复路径变红。普通 approved 入口进入已结算失败恢复时重复要求 recovery_only。保留原绑定、失败事件、租约和回执核验，把 recovery_only 继续用于原本受其约束的 leased 状态。单题原答案解析不合法时回告具体原因，合理兄弟题仍用原结果。

仓内验收 102 项，实际已收到恢复正反例 14 项。独立 reviewer 复跑另 14 项，核对原尝试、后续代隔离、坏包零写、撤销回滚及零新增 API、尝试和费用。ROOT 接受此既有工作线。原 master 的分歧由原线按自身计划处理。
