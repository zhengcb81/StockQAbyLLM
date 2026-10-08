# QA-C06-02 case 映射

状态口径：`passed` = 本包实际跑过且通过；`failed` = 跑过且失败（无）；`skip` = 显式跳过（无）；
`not_run` = 本卡范围外或依赖未到位，**未签收**。日志相对本目录；hash 见 `artifacts.json`。

## 卡片“测试矩阵”逐行

| 卡片 case | 真实测试 selector | 日志 | 状态 |
|---|---|---|---|
| 单元：embedded_answer、metadata 偷塞 execution | `tests/unit/test_quick_scan_observation_context.py::test_embedded_answer_is_refused_at_the_real_consumption_entry` | `logs/red-embedded-answer.log`（RED：1 failed/44 deselected）→ `logs/green-embedded-answer.log`（GREEN：1 passed/44 deselected） | passed |
| 单元：重复 JSON 键 | `tests/unit/test_llm_client.py` / `tests/unit/test_q10_content_boundary.py`（既有重复键拒绝用例，本包未改） | `logs/full-gate-GREEN.log` | passed |
| 单元：错 schema / metric / prompt / identity | `tests/unit/test_quick_scan_observation_context.py::test_context_tampering_or_self_approval_is_refused`、`::test_changed_manifest_cannot_rebind_context`、`::test_binding_changes_are_not_silently_accepted`、`::test_metric_semantic_errors_are_refused` | `logs/full-gate-GREEN.log` | passed |
| 单元：正常 v2 与旧 v1 独立通过 | `tests/unit/test_qa_net01_c06_seal.py::test_runner_seals_checkpoint_into_valid_c06_package`（v1）、`::test_invalid_authority_documents_fail_closed`（未知版本 fail-closed）、`tests/unit/test_quick_scan_observation_context.py::test_embedded_answer_is_refused_at_the_real_consumption_entry`（v2 正例） | `logs/full-gate-GREEN.log` | passed |
| 单元：标准答案 scored / unknown / N-A | `test_quick_scan_observation_context.py::test_actual_full_answer_is_preserved_without_generated_defaults`、`::test_incomplete_or_conflicting_standard_answer_is_refused[unknown]`、`::test_nonscored_states_remain_null_without_fallback` | `logs/full-gate-GREEN.log` | passed |
| 单元：单位/期间/引用矛盾 | `test_quick_scan_observation_context.py::test_metric_semantic_errors_are_refused`、`::test_incomplete_or_conflicting_standard_answer_is_refused[reversed_period/unbound_url/future_source]` | `logs/full-gate-GREEN.log` | passed |
| 单元：完整 body 长于 5000 | `test_quick_scan_observation_context.py::test_full_standard_body_above_legacy_description_limit_is_not_truncated` + `test_qa_c06_02_e2e.py::test_cli_e2e_complete_standard_c06_seals_warms_and_settles`（`largest > 5000` 断言） | `logs/full-gate-GREEN.log` | passed |
| 单元：错误有具体 code | `quick_scan_delivery_seal.py` 的 `BLOCK_*` 常量 + `test_quick_scan_c06_complete_seal.py::test_missing_standard_answer_blocks_then_supplements_with_zero_model_calls` / `::test_legacy_checkpoint_without_inputs_keeps_its_historical_package_read_only` | `logs/full-gate-GREEN.log` | passed |
| 集成：旧库迁移 | `tests/unit/test_quick_scan_work_store.py::test_v1_migration_preserves_work_rows_and_adds_empty_checkpoint_table`、`::test_fixed_v2_migration_preserves_existing_rows_and_adds_budget_ledger`、`::test_schema_v4_upgrade_preserves_checkpoint_and_creates_delivery_ledger`、`tests/unit/test_quick_scan_budget.py::test_v3_migration_preserves_settlement_and_marks_legacy_outcome_unverifiable` | `logs/full-gate-GREEN.log` | passed |
| 集成：事务中断（迁移失败回滚） | `test_quick_scan_work_store.py::test_v1_migration_rejects_completed_work_without_checkpoint`（失败后 `user_version` 仍为 1、无新表） | `logs/full-gate-GREEN.log` | passed |
| 集成：同 context 重放 / 冲突 | `test_quick_scan_c06_complete_seal.py::test_complete_standard_answer_seals_one_revision_and_replays_idempotently`（重放）、`::test_a_second_frozen_context_for_the_same_task_is_refused`（冲突） | `logs/full-gate-GREEN.log` | passed |
| 集成：独立 run / attempt | `test_quick_scan_work_store.py::test_same_logical_item_attaches_two_runs_without_model_in_primary_key`、`test_quick_scan_observation_context.py::test_different_actual_attempt_changes_observation_id` | `logs/full-gate-GREEN.log` | passed |
| 集成：result_ready 缺字段→补齐（只有封包，LLM 0） | `test_quick_scan_c06_complete_seal.py::test_missing_standard_answer_blocks_then_supplements_with_zero_model_calls`（`report["model_calls"] == 0`） | `logs/full-gate-GREEN.log` | passed |
| 集成：旧 compact 缺信息保持 block | `test_quick_scan_c06_complete_seal.py::test_legacy_checkpoint_without_inputs_keeps_its_historical_package_read_only` | `logs/full-gate-GREEN.log` | passed |
| 集成：旧 sealed 历史不被改写 | 同上（`package_bytes` 不变、revision 数仍 1） | `logs/full-gate-GREEN.log` | passed |
| 集成：新 head / 旧 ACK | `test_quick_scan_c06_complete_seal.py::test_compact_package_is_superseded_and_the_old_head_ack_cannot_settle_it` | `logs/full-gate-GREEN.log` | passed |
| 集成：send_uncertain 先对账 | `test_quick_scan_c06_complete_seal.py::test_send_uncertain_is_reconciled_before_a_new_head_is_written` | `logs/full-gate-GREEN.log` | passed |
| 集成：丢 ACK / 重启 / 相同 ACK | `test_quick_scan_work_store.py::test_result_delivery_outbox_restarts_replays_exact_ack_and_never_reasks` + E2E 中的 replay 断言 | `logs/full-gate-GREEN.log` | passed |
| 集成：假 store / hash | `test_qa_c06_02_e2e.py` 的 forged `payload_sha256`、他人 `store_id`、跨包 ACK 三个负例 | `logs/full-gate-GREEN.log` | passed |
| 隔离 CLI E2E：真实 runner/store/adapter/seal，HTTP 边界 stub | `tests/integration/test_qa_c06_02_e2e.py`（3 例） | `logs/full-gate-GREEN.log` | passed |
| 隔离 CLI E2E：冷第一次发送可数、恢复/warm 0、临时根清理 | 同上（`len(http["sent"]) == 31`、warm 0、seal `model_calls == 0`；清理见 `isolation.md`） | `logs/full-gate-GREEN.log` | passed |

## 卡片“连续实施步骤” 1–8

| 步骤 | 证据 | 状态 |
|---|---|---|
| 1 越界字段真实消费入口拒绝 + 纯 metadata 仍通过 | `logs/red-embedded-answer.log` → `logs/green-embedded-answer.log` | passed |
| 2 v1/v2 loader（逐题定义、两种 manifest hash、identity 原字节、schema/metric hash、scope/run/日期、frozen/work prompt；未知版本不降级） | `src/utils/quick_scan_c06_authority.py`、`tests/unit/test_qa_net01_c06_seal.py::test_invalid_authority_documents_fail_closed`、E2E 的 authority↔manifest 绑定反例 | passed |
| 3 work_store 向前迁移 + 不可变 context/标准答案侧表 + 同任务冲突拒绝 + 事务回滚 | `quick_scan_work_store.py` `_DDL_V6_ADDITIONS`/`_apply_v6_migration`；迁移与冲突用例见上表 | passed |
| 4 runner 显式开启标准答案传输、description 携带完整标准 JSON、>5000 不截断 | `src/providers/base_llm_provider.py::standard_answer_transport`、`src/runners/llm_runner.py::_standard_transport`；E2E `largest > 5000` | passed |
| 5 adapter 只取冻结 context+完整答案+真实 attempt；started_at=send_intent_at；observation ID=IQS canonical digest | `src/utils/quick_scan_c06_adapter.py::build_complete_c06_package`；E2E `started_at`/`observation_id` 断言 | passed |
| 6 有效全 Observation 才封存；缺字段持久 block；补包模型 0 | `quick_scan_delivery_seal.py`；`test_missing_standard_answer_blocks_then_supplements_with_zero_model_calls` | passed |
| 7 新 revision/head/supersedes 加法记录，旧包/ACK/hash 只读，旧 head ACK 不落定新 head，幂等 | `test_compact_package_is_superseded_and_the_old_head_ack_cannot_settle_it` 等 | passed |
| 8 真实公开 CLI 运行→完整 checkpoint→封存→outbox/ACK 恢复 | `tests/integration/test_qa_c06_02_e2e.py` + `golden/` | passed |

## 本卡明确未做（`not_run`）

| case | 原因 |
|---|---|
| 真实 StockWiki observation-import → ACK → 查询/UI 联合验收 | 总控集中执行；worker 不写 StockWiki |
| G3 门 | 依赖联合验收，worker 不自关 |
| F05 门 | 依赖事实模块与 SW 侧，不在本卡范围 |
| 真实 owner identity / owner golden | 本包只有 synthetic，合成不得升级为 owner golden |
| SW-REPAIR-02 六项反例 | 另一施工包 |
| EVID-LAB-01 离线评测 | 另一施工包 |
| 线上 live E2E（`STOCKQA_RUN_LIVE_E2E`） | 默认 live=off，未申请在线授权 |
| 原 Q10 全部历史 case（QA-NET-01 已签部分之外） | 子集交付：不签整个 Q10 |
