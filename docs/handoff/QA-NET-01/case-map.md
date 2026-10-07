# 当前整改case映射（2026-10-07）

最终统一门982 tests已过；下方历史27/962/986各为原owner不同批次，不能当本次最终数或跨仓验收。初始旧F2 lifecycle-only正例不符合真实备用attempt要求，保留为原始RED，不削弱store迎合它。

| 断言 | 当前可复现选择器（StockQA相对路径） |
|---|---|
| 真正备用HTTP/预算/checkpoint/C06/warm0 | tests/integration/test_qa_net01_transport_e2e.py::test_actual_two_route_fallback_checkpoint_seal_and_zero_http_resume |
| 未知/未计价/异常usage不盲发备用 | tests/integration/test_qa_net01_transport_e2e.py::test_uncertain_or_unpriced_primary_never_blindly_sends_backup |
| 明确非零拒绝usage有成本 | tests/integration/test_qa_net01_transport_e2e.py::test_rejected_primary_with_reported_nonzero_usage_is_not_free |
| attempt/model/hash伪改拒保存，原记录不变 | tests/integration/test_qa_net01_transport_e2e.py::test_transport_checkpoint_refuses_mutated_binding_without_re_recording |
| 2xx5001字符保存拒绝公开error | tests/integration/test_qa_net01_transport_e2e.py::test_checkpoint_refusal_is_public_error_without_losing_transport |
| 私有receipt仍需要搜索/response/model质量 | tests/integration/test_qa_net01_transport_e2e.py::test_private_http_receipt_still_requires_checkpoint_quality |
| 新模块保留旧题routing/只派新题 | tests/integration/test_qa_net01_cli_e2e.py::test_new_module_keeps_old_answers_hydratable_with_fresh_output |
| gen2完成后重启HTTP0 | tests/integration/test_qa_net01_cli_e2e.py::test_completed_generation_two_restarts_without_reverting_to_one |
| 改prompt不能跨旧uncertain收费 | tests/unit/test_qa_net01_question_manifest.py::test_changed_prompt_waits_for_uncertain_prior_attempt |
| provisional/model mismatch error且不重发 | tests/unit/test_q06_work_binding.py::test_e2e_public_cli_identity_snapshot_glue_no_redispatch |

---

# QA-NET-01 case-map

图例：**owner** = 该 case 的 `owner_task`；本包只对自己 owner 的 case 负责，跨 owner case 仅引用既有证据，不自行签收。
日志均为 `logs/` 下的原始 stdout/stderr（RED 与 GREEN 分开保存）。
新搜索故障断言按包指令编为**本包场景**，不改写中央 366 case，不 reopen 已验收的 Q02 历史快照。

## Q13（owner=Q13）

| case | 原子断言 | 公开路径 | 精确选择器 | 日志 | 结果 |
|---|---|---|---|---|---|
| MOD-06 | 旧 4 题新增模型调用 0；只派 2 道新增题 | `main_with_llm.py --question-manifest` → `plan_manifest_dispatch` | `tests/unit/test_qa_net01_question_manifest.py::test_mod_06_only_new_and_expired_questions_are_planned_for_dispatch` | `RED_batchB_question_manifest.log` / `GREEN_batchB_question_manifest.log` | passed |
| MOD-06 | 结果不明先对账、未适用题不派 | 同上 | `tests/unit/test_qa_net01_question_manifest.py::test_mod_06_uncertain_and_not_applicable_are_never_redispatched` | 同上 | passed |
| MOD-06 | 重复导入仅一份不可变观察、费用预留不重置 | 既有 work_store/outbox（本包未改语义） | `tests/unit/test_q10_delivery.py::test_job_07_delivery_recovery_zero_llm_and_idempotent_ack` | `GREEN_full_suite.log` | passed（既有证据，引用） |
| MOD-09 | 题面被改而 hash 未改 → 拒绝、付费发送 0 | `load_question_manifest` / `bind_manifest_to_questions` | `tests/unit/test_qa_net01_question_manifest.py::test_mod_09_tampered_prompt_and_duplicate_ids_are_refused` | `GREEN_batchB_question_manifest.log` | passed |
| MOD-09 | 两模块替换同一核心题 → manifest 冲突拒绝 | 同上 | `tests/unit/test_qa_net01_question_manifest.py::test_mod_09_cross_module_replacement_conflict_is_refused` | 同上 | passed |
| MOD-09 | 题集重排/改写 → 绑定拒绝、不退回自由文本 | 同上 | `tests/unit/test_qa_net01_question_manifest.py::test_question_file_binding_rejects_reordered_or_reworded_prompts` | 同上 | passed |
| MOD-09 | 公开 CLI 在任何 HTTP 前拒绝篡改 manifest | `main_with_llm.py --question-manifest` | `tests/integration/test_qa_net01_cli_e2e.py::test_cli_e2e_tampered_manifest_is_refused_before_any_http` | `RED_batchD_cli_e2e.log` / `GREEN_batchD_cli_e2e.log` | passed |
| JOB-03（owner=Q07，Q13 引用） | 6 题 4 成功，恢复只补 2 题 | 既有 hydrate + 本包增量计划 | `tests/unit/test_q07_checkpoint.py::test_job_03_hydrates_saved_and_only_refills_missing` | `GREEN_full_suite.log` | passed（既有证据，引用） |
| JOB-03（Q13 侧增量） | 计划层 `reuse=4 / dispatch=1 / expired=1` | `plan_manifest_dispatch` | `tests/unit/test_qa_net01_question_manifest.py::test_mod_06_only_new_and_expired_questions_are_planned_for_dispatch` | `GREEN_batchB_question_manifest.log` | passed |
| Q13 附加 | 未绑定 security scope 的题不派发 | `manifest_scope_bindings` | `tests/unit/test_qa_net01_question_manifest.py::test_unbound_security_scope_questions_are_deferred` | 同上 | passed |
| Q13 附加 | 已封存为 `seal_only`（0 模型调用） | `plan_manifest_dispatch` | `tests/unit/test_qa_net01_question_manifest.py::test_manifest_directory_input_and_seal_only_action` | 同上 | passed |

## Q10（owner=Q10；底座按包指令直接复用，本包只补 runner glue）

| case | 原子断言 | 公开路径 | 精确选择器 | 日志 | 结果 |
|---|---|---|---|---|---|
| JOB-07 | 投递恢复期间 LLM 新增 0；同字节/同 key re-arm；ACK 幂等 | 既有 store 原语（复用） | `tests/unit/test_q10_delivery.py::test_job_07_delivery_recovery_zero_llm_and_idempotent_ack` | `GREEN_full_suite.log` | passed（既有证据，引用） |
| JOB-07（本包 glue） | 检查点落定即封存；warm 重跑 0 HTTP；`--seal-deliveries` `model_calls=0` | `main_with_llm.py` → `after_question` → `seal_result_delivery` | `tests/integration/test_qa_net01_cli_e2e.py::test_cli_e2e_native_path_seals_c06_and_warm_run_sends_zero` | `RED_batchD_cli_e2e.log` / `GREEN_batchD_cli_e2e.log` | passed |
| JOB-08 | 错误 ACK / 非法转移被拒 | 既有 store 原语（复用） | `tests/unit/test_q10_delivery.py::test_job_08_wrong_ack_and_illegal_transition_are_refused` | `GREEN_full_suite.log` | passed（既有证据，引用） |
| DB-07 | 缺权威 → durable block、work 仍 `result_ready`、不猜补、可后补封存 | `seal_result_delivery` | `tests/unit/test_qa_net01_c06_seal.py::test_missing_authority_records_durable_block_then_seals_on_restart` | `RED_batchA_c06_seal.log` / `GREEN_batchA_c06_seal.log` | passed |
| DB-07（公开入口） | 无 `--c06-authority` 的真实 CLI 运行 → 2 个 blocked；权威到位后补封 0 模型 | `main_with_llm.py` / `--seal-deliveries` | `tests/integration/test_qa_net01_cli_e2e.py::test_cli_e2e_missing_authority_blocks_then_restart_seals` | `GREEN_batchD_cli_e2e.log` | passed |
| DB-07（既有） | adapter 缺字段即 block、不伪造包 | 既有 adapter | `tests/unit/test_q10_delivery.py::test_db_07_missing_authority_blocks_and_never_fabricates` | `GREEN_full_suite.log` | passed（既有证据，引用） |
| PAR-10 | 假 ACK 不结束不确定态、不重 POST | 既有 store 原语（复用） | `tests/unit/test_q10_delivery.py::test_par_10_fake_ack_never_resolves_uncertainty` | `GREEN_full_suite.log` | passed（既有证据，引用） |
| Q10 附加 | runner 产出的包通过独立 `validate_exchange_package` | `build_c06_package` + outbox | `tests/unit/test_qa_net01_c06_seal.py::test_runner_seals_checkpoint_into_valid_c06_package` | `GREEN_batchA_c06_seal.log` | passed |
| Q10 附加 | 非法/残缺权威文档失败关闭 | `load_c06_authority` | `tests/unit/test_qa_net01_c06_seal.py::test_invalid_authority_documents_fail_closed` | 同上 | passed |
| Q10 附加 | `unknown` 答案状态不可打包 | `build_c06_package` | `tests/unit/test_qa_net01_c06_seal.py::test_unknown_answer_status_cannot_be_packaged` | 同上 | passed |

**Q10 RED 证据**：`RED_batchA_c06_seal.log` —— stash `src/runners/llm_runner.py` + `main_with_llm.py` 后 4 例中 3 例失败
（`QuickScanWorkLifecycle.__init__() got an unexpected keyword argument 'c06_authority'`），证明 glue 在 HEAD 上不存在。

## Q02（owner=Q02，增量路径）

| case | 原子断言 | 公开路径 | 精确选择器 | 日志 | 结果 |
|---|---|---|---|---|---|
| LLM-02 | HTTP 200 + 模型自称已搜 + 手写 URL → 不计为有效联网评分 | `verify_native_search_receipt` | `tests/unit/test_qa_net01_search_boundary.py::test_llm_02_http_200_without_search_is_never_a_search_receipt` | `GREEN_batchC_search_boundary.log` | passed |
| LLM-11 | 不支持路线计费前标不可用；外部回执不能冒充原生；`request_id` 绑定 | `native_search_route_supported` / `verify_external_retrieval` | `tests/unit/test_qa_net01_search_boundary.py::test_llm_11_unsupported_and_unverifiable_routes_stay_unavailable` | 同上 | passed |
| Q02 附加（本包场景 S-01） | 嵌套 JSON ≤3 层 / 超层 / 业务错 / 非 JSON body | 四路由离线 parser | `tests/unit/test_qa_net01_search_boundary.py::test_nested_json_layers_business_errors_and_byte_cap` | 同上 | passed |
| Q02 附加（本包场景 S-02） | HTTP200 无结构 → `parse_failure`；真空列表 → `empty`；少于 count 不补齐 | 同上 | `tests/unit/test_qa_net01_search_boundary.py::test_offline_parsers_classify_success_business_error_and_parse_failure` | 同上 | passed |
| Q02 附加（本包场景 S-03） | 片段 500 上限、公司 30,000 上限、URL 去重、错实体/晚于截止日剔除、无日期不支持主张 | `build_evidence_package` | `tests/unit/test_qa_net01_search_boundary.py::test_evidence_caps_dedupe_entity_and_as_of_filtering` | 同上 | passed |
| Q02 附加（本包场景 S-04） | 网页命令当数据透传、不执行；公司预算截断 | 同上 | `tests/unit/test_qa_net01_search_boundary.py::test_company_budget_truncates_and_snippet_commands_stay_data` | 同上 | passed |
| Q02 附加（本包场景 S-05） | TTL/locale/filter/top_k/depth/adapter 版本任一变化 → 缓存键失效 | `search_cache_key` | `tests/unit/test_qa_net01_search_boundary.py::test_search_cache_key_invalidates_on_every_material_input` | 同上 | passed |
| Q02 附加（本包场景 S-06） | IQS 可填写模板不可作为执行授权 | `load_search_policy` | `tests/unit/test_qa_net01_search_boundary.py::test_iqs_template_is_never_an_executable_policy` | 同上 | passed |
| Q02 附加（本包场景 S-07） | 凭据缺失/费用上界未核/计价未知/存储权未确认/停用 → 未准入、发送 0 | 同上 | `tests/unit/test_qa_net01_search_boundary.py::test_route_admission_fails_closed_on_cost_rights_and_credentials` | 同上 | passed |
| Q02 附加（本包场景 S-08） | 随包示例策略即使有凭据也 0 准入 | 同上 | `tests/unit/test_qa_net01_search_boundary.py::test_shipped_example_policy_never_admits_a_route` | 同上 | passed |
| Q02 附加（本包场景 S-09） | CLI：未准入 external → exit 1、HTTP 0；已准入但dispatcher未实现 → 回执禁执行且HTTP0 | `main_with_llm.py --search-policy` | `tests/integration/test_qa_net01_cli_e2e.py::test_cli_e2e_search_policy_admission_gates_before_any_http` | `RED_batchC_search_policy_cli.log` / `GREEN_batchD_cli_e2e.log` | passed |
| Q02 附加（本包场景 S-10） | CLI：带模板标记的策略 → exit 1、HTTP 0 | 同上 | `tests/integration/test_qa_net01_cli_e2e.py::test_cli_e2e_search_policy_template_marker_is_refused` | 同上 | passed |
| Q02 附加（本包场景 S-11） | `tool_use` 只续写不冒充最终答案；tool_result 无归属 → invalid；截断 → incomplete | `classify_anthropic_messages_response` | `tests/unit/test_qa_net01_search_boundary.py::test_deepseek_continuation_is_classified_but_not_admitted` | `GREEN_batchC_search_boundary.log` | passed |

**Q02 RED 证据**：`RED_batchC_search_policy_cli.log` —— stash runner/CLI 后 2 例失败
（`main_with_llm.py: error: unrecognized arguments: --search-policy`），证明准入闸门在 HEAD 上不存在。

## 跨 owner case（仅引用，不签收）

| case | owner | 引用证据 | 结果 |
|---|---|---|---|
| LLM-08（正文/凭据不落盘） | Q05 | `tests/unit/test_q05_content_boundary.py`，见 `GREEN_full_suite.log` | passed（既有，引用） |
| Q02 历史快照（MiniMax/MiMo 原生接线、366 中央 case） | Q02/S02 | 未改动；`GREEN_full_suite.log` 中既有用例 | passed（既有，引用） |
| 跨仓真实 StockWiki 导入/ACK（W05/W10） | 总控 | 本包未运行 | not_run |

## 本包场景计数

* 本包新增可执行断言：**27**（`tests/unit/test_qa_net01_*.py` 22 + `tests/integration/test_qa_net01_cli_e2e.py` 5）
* RED 日志：**4**（batchA / batchB / batchC / batchD）
* GREEN 日志：**8**（batchA/B/C/D、`GREEN_full_suite.log`（986 passed/4 skipped）、
  `GREEN_checks_full.log`（仓统一门 `scripts/checks.py --full`，962 passed，exit 0）、
  `GREEN_static_gates.log`、`GREEN_precommit.log`）
* 未运行（明确原因）：真实 provider / 真实搜索 / MCP 握手 / 跨仓导入 = `live=off`（本包默认离线，未获计费与启动授权）
