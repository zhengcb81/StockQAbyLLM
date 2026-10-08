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

## 整改批次 2（2026-10-08 卡）四组 + 交接收尾

状态口径同上；`logs/red-*` 为修复前原始失败，`logs/green-*` / 门日志为修复后。

| 整改卡断言 | 真实测试 selector / 执行 | 日志 | 状态 |
|---|---|---|---|
| 组1 逐题 metadata 与冻结 manifest 核一致（field/construct/scope/definition/semantic/rubric/template/module/method/cohort/cutoff/entity） | `tests/unit/test_quick_scan_c06_authority_binding.py::test_per_question_metadata_is_bound_to_the_frozen_manifest`（14 参数例）、`::test_a_tampered_question_beyond_the_first_is_also_refused`、`::test_manifest_field_tamper_still_breaks_the_manifest_hash_binding`、`::test_identity_bytes_binding_is_still_checked`；bind 侧同规则 `test_quick_scan_observation_context.py::test_bind_question_context_uses_the_same_manifest_rule_set`（4 参数例） | `logs/red-unit-remediation.log`（RED：31 failed/63 passed）→ `logs/full-gate-remediation-2026-10-08.log`（GREEN：1083 passed） | passed |
| 组1 错定义例真实子进程 CLI：HTTP0、key 读取 0、无费用预约/成功 checkpoint | `tests/integration/test_qa_c06_02_subprocess_cli.py::test_real_subprocess_wrong_template_metadata_is_rejected_before_key_http`（exit 1、stub 发送 0、`key-opens.jsonl` 不存在、DB/输出文件未创建） | `logs/red-subprocess-remediation.log`（RED：CLI 曾 exit 0 并发 31）→ `logs/full-gate-remediation-2026-10-08.log` | passed |
| 组1 正常 v2/v1 原路径通过；原 prompt/hash/identity 负例继续有效 | `::test_pure_metadata_v2_authority_still_loads_unchanged`、`::test_v1_authority_keeps_its_historical_load_path` + 原 `test_changed_manifest_cannot_rebind_context` 等全部原用例 | 同上门日志 | passed |
| 组2 foreign run/scan 拒绝（持久 block，不改 fixture 标签） | `tests/unit/test_quick_scan_c06_complete_seal.py::test_foreign_run_and_scan_context_cannot_seal_this_checkpoint`（`action=blocked`、`block_code=c06_run_scan_unbound`、无新 revision） | `logs/red-unit-remediation.log`（RED）→ 门日志 | passed |
| 组2 正常 DB/Observation 有一致可核映射；独立 run/attempt ID 区分 | `::test_save_persists_the_frozen_run_scan_mapping_for_the_actual_work`、`::test_independent_runs_and_attempts_keep_distinct_ids_after_mapping`、`::test_context_attached_after_the_checkpoint_seals_when_its_run_is_known`；真实 CLI 断言 `test_qa_c06_02_subprocess_cli.py::test_real_subprocess_cold_warm_and_seal_with_the_stub_only_at_http`（每 work 同时持 `fixture-run/fixture-scan` 与 `run-*/scan-l02`） | 门日志 | passed |
| 组2 另进程撤 stub warm/seal 恢复成功、HTTP0 | 同上子进程测试：cold（stub=on）→ warm/seal（stub=off），`model_calls_planned=0`、`model_calls=0`、发送账本字节不变、网络账本不存在 | 门日志 | passed |
| 组3 两个原例 RED→GREEN + 嵌套重复 1 例；valid v1/v2/body 不变 | `test_quick_scan_c06_authority_binding.py::test_authority_with_a_duplicate_schema_version_is_refused`、`::test_authority_with_a_nested_duplicate_key_is_refused`、`::test_authority_with_a_non_finite_number_is_refused`；`test_quick_scan_observation_context.py::test_duplicate_score_in_standard_body_is_refused`、`::test_nested_duplicate_key_in_standard_body_is_refused`、`::test_non_finite_number_in_standard_body_is_refused`、`::test_strict_json_loads_rejects_duplicates_and_non_finite_at_every_level` + 正例不变三例 | `logs/red-unit-remediation.log`（RED）→ 门日志 | passed |
| 组3 真实 CLI 拒绝重复 authority 在 key/HTTP 前 | `test_qa_c06_02_subprocess_cli.py::test_real_subprocess_duplicate_authority_json_key_is_rejected_before_key_http` | `logs/red-subprocess-remediation.log`（RED：exit 0）→ 门日志 | passed |
| 组3 invalid body 明确拒绝/阻断、不补分、不重问 | `test_qa_c06_02_subprocess_cli.py::test_real_subprocess_corrupt_standard_body_is_rejected_without_reasking`（honest-unknown checkpoint、score null、侧表缺失、交付持久 block `c06_standard_answer_unavailable`、每题恰 1 发、其余 30 题正常封存） | 门日志 | passed |
| 组4 当前伪造案例禁止修改（claim+started_at 全层重签仍拒） | IQS 原字节 `controller_cases.py::test_supersede_cannot_change_full_claim_or_original_start`：`7 failed/2 passed → 9 passed`；仓内同形 `test_quick_scan_c06_complete_seal.py::test_supersede_rejects_each_forged_complete_observation_field[claim_and_start]` | `logs/red-boundary-remediation.log` → `logs/green-boundary-remediation.log` | passed |
| 组4 分别单改 claim / typed metric / metadata / started_at 也拒；无新 revision、head 不变 | `::test_supersede_rejects_each_forged_complete_observation_field[claim/typed_metric/metadata/started_at]` + `::test_prepare_refuses_a_forged_complete_observation_before_any_head_exists`（断言 `list_delivery_revisions == []`、head 包字节不变、侧表正文不变） | `logs/red-unit-remediation.log`（RED）→ 门日志 | passed |
| 组4 合法 compact→完整升级、旧 ACK 拒/同 ACK 幂等、send_uncertain 先对账、delivered 只读继续通过 | 原 `test_compact_package_is_superseded_and_the_old_head_ack_cannot_settle_it`、`test_send_uncertain_is_reconciled_before_a_new_head_is_written`、`test_result_delivery_outbox_restarts_replays_exact_ack_and_never_reasks`、E2E delivered 只读断言——全部在 1083 门内 | 门日志 | passed |
| 旧 v5 源造旧包/ACK 库 → v6 原字段/费用/hash 不变；迁移中途异常全 rollback（回归保留） | 原字节 `controller_cases.py::test_true_v5_delivered_package_ack_preserved_by_v6`、`::test_v5_to_v6_mid_migration_failure_rolls_back_every_change`（9 例中 2 例，修复前后均 GREEN）+ 仓内 v1/v2/v4 迁移用例 | `logs/green-boundary-remediation.log`（9 passed 含此 2 例）、门日志 | passed |
| 真实子进程 cold/warm/seal（stub 仅 HTTP 边界） | `test_qa_c06_02_subprocess_cli.py::test_real_subprocess_cold_warm_and_seal_with_the_stub_only_at_http` | 门日志 | passed |
| 一批验证（相关 unit/integration/真实 CLI/旧迁移/替代链 + 仓既定门 + pre-commit） | `python -B scripts/checks.py --full --timeout 300` → 1083 passed；`python -m pre_commit run --files <精确交付清单>` → 全 hook pass | `logs/full-gate-remediation-2026-10-08.log`、`logs/pre-commit-remediation-2026-10-08.log` | passed |
| 交接收尾：changed_paths 实填、authorized_paths 纯路径、TEMP 残余清单、冻结 CRLF 双口径与还原命令 | `handoff.json` `scope.changed_paths`（= `git diff --name-only 09f68a6…acb7dbf`）、`isolation.md` 两节、`logs/cleanup-remediation-receipt.json` | `logs/cleanup-remediation-receipt.json` | passed |

### 未单独确认项（如实报告）

| 项 | 说明 |
|---|---|
| 总控批次中“1 个 async 未确认” | 指 `tests/unit/test_quick_scan_budget.py::test_async_search_settles_the_same_durable_budget_ledger` 在总控 guard 拦 socketpair 时未跑完；本批标准离线门（不装 guard）**GREEN**，计入 1083；本批自己的子进程 guard 按精确 ephemeral socketpair（`127.0.0.1/::1:0`）放行并有记录 |
| 真实 StockWiki 联合 E2E / G3 / F05 / owner golden / 线上 live | 仍 `not_run`（总控），见下表 |

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
