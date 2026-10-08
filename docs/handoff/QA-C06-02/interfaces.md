# QA-C06-02 接口

## 消费的接口（输入）

| 接口 | 版本 / SHA | 来源 | 说明 |
|---|---|---|---|
| 冻结问卷 manifest | `tests/fixtures/quick_scan_c06_manifest_v2_fixture.json`，原文件 SHA256 `a7e80a5bcaf2cb2499fa24f091b129d7e1a2ebbf6e91a5666359c5e91c90270c`；canonical 内容 SHA `b525e300b20d74f7443e4fecbef8bddec83c144a2576fbad7eda012201245182` | IQS 私有 v2 输入映射快照（`inputs/iqs-phase92/`） | 两种 hash 分别绑定“原文件字节”与“canonical 内容”，不混用 |
| 私有 authority 2.0.0 | `tests/fixtures/quick_scan_c06_authority_v2_fixture.json`，原文件 SHA256 `e5bc782afd245374fff3bc224f04cb9dbcc0fb22b76d62faf2a16917db1ded43`；`observation_context_sha256` = canonical 内容 SHA | 同上 | context 每题只有 `metadata`/`frozen_prompt_sha256`/`work_prompt_sha256` |
| Observation schema | 1.1.0，canonical SHA `ab547ecdd81ee1b302aaef30941fb20ac1563b9122267f8858bcb91f8f2d34e0`；文件 SHA `6344d2afc9e0d937e05baba8aef27ad15c91770ab7ecb2ddbd549e5ab4fb70ec` | `src/config/quick_scan_observation.schema.json`（IQS 协议快照） | 未知 hash fail-closed |
| Answer schema | 1.0.0，canonical SHA `cefa301b35235c15f188bfede2706837ea695e10b656a28684eecd0159434fcd`；文件 SHA `a83236c2aba50115038cd2d404d5810bd50791c2b3f089f252b77b6a6d322547` | `src/config/quick_scan_answer_content.schema.json` | 同上 |
| Metric registry | 1.0.0，文件 SHA `790d0a8295771c372d8266ab1f0883c93b2bb22f4f81ab482dd8d95054bf0e75` | `src/config/quick_scan_metric_registry.json` | 仅 `custom.*` 允许在注册表外 |
| 身份文件 | owner 导出原字节 SHA256（运行时计算） | `--identity-snapshot` | IQS 只绑 opaque 字节，不签 verified |
| IQS 产物脚本 | `inputs/iqs-phase92/{scripts/standard_answers.py,scripts/c06_authority.py,tests/test_c06_authority.py}` | IQS 快照，只读参考 | 未复制到 StockQA 生产源码 |

输入哈希与身份认证是**不同**的事：SHA 匹配只证明字节一致，不构成实体事实或 verified 身份。

## 产出的接口

| 产出 | 版本 | 位置 |
|---|---|---|
| ExchangePackage | 1.0.0（未改） | `quick_scan_result_delivery.package_json` / golden |
| Observation | 1.1.0（完整观察，未改公共 schema） | `package.items[0].observation` |
| Answer content | 1.0.0（未改） | `observation.answer` |
| Authority v1 | 1.0.0（原 schema 保留，未改 `const`） | `src/config/quick_scan_c06_authority.schema.json` |
| **Authority v2** | **2.0.0（新增）** | `src/config/quick_scan_c06_authority.v2.schema.json`，`$id=stockqa.quick_scan_c06_authority/2.0.0` |
| Observation context | `stockqa.quick_scan_observation_context/1.0.0` | authority 2.0.0 的 `observation_context` |
| Work store | `SCHEMA_VERSION = 6`（v5→v6 向前迁移） | `src/utils/quick_scan_work_store.py` |
| 阻断码 | `c06_authority_unavailable` / `c06_adapter_missing_fields` / `c06_observation_context_unavailable` / `c06_observation_context_conflict` / `c06_run_scan_unbound`（整改新增） / `c06_standard_answer_unavailable` / `c06_attempt_send_intent_unavailable` | `src/utils/quick_scan_delivery_seal.py` |
| 修订链 | `quick_scan_delivery_revision`（revision / supersedes_revision / supersedes_package_id） | 同上 + work store |
| Golden | `stockqa.qa_c06_02_golden/1.0.0`（**synthetic_only**） | `golden/golden.json`、`golden/complete_c06_package.json`、`golden/standard_answer.json` |

## 生成命令

完整标准 C06（合成输入、HTTP 边界 stub，命令参数来自隔离副本 `main_with_llm.py --help`
与 `tests/integration/test_qa_net01_cli_e2e.py` 的既有用法，未新增任何 flag）：

```powershell
python -B -X utf8 main_with_llm.py --company "Fixture Corp" --entity-id ENT_CONTEXT_FIXTURE --provider openai --config <tmp>/questions.json --output <tmp>/result.json --require-search --identity-snapshot <tmp>/identity.json --spend-authorization <tmp>/spend_authorization.json --question-manifest <tmp>/manifest.json --security-scope-id SEC_CONTEXT_FIXTURE --c06-authority <tmp>/quick_scan_c06_authority.json
```

重启补封（模型 0）：

```powershell
python -B -X utf8 main_with_llm.py --seal-deliveries --c06-authority <tmp>/quick_scan_c06_authority.json
```

RED→GREEN selector（批次 1）：

```powershell
python -B -X utf8 -m pytest -p no:base_url tests/unit/test_quick_scan_observation_context.py -k embedded_answer -q
```

RED→GREEN selector（整改批次 2，四组反例 + 真实子进程）：

```powershell
python -B -X utf8 -m pytest -p no:base_url tests/unit/test_quick_scan_c06_authority_binding.py tests/unit/test_quick_scan_c06_complete_seal.py tests/unit/test_quick_scan_observation_context.py tests/integration/test_qa_c06_02_subprocess_cli.py -q
```

输入来源：`tests/fixtures/quick_scan_c06_manifest_v2_fixture.json`（原字节）、
运行时生成的 `identity.json` / `quick_scan_c06_authority.json`（fixture authority 重签到该
identity 字节）、`questions.json`（由 manifest 逐题 prompt 生成）。全部 SHA 见
`golden/golden.json.inputs`。

## 正例 / 反例

* 正例：`tests/integration/test_qa_c06_02_e2e.py::test_cli_e2e_complete_standard_c06_seals_warms_and_settles`
  （完整观察、31 题冷发、warm 0、seal 0、ACK 幂等）；`tests/unit/test_quick_scan_c06_complete_seal.py::test_complete_standard_answer_seals_one_revision_and_replays_idempotently`。
* 反例（HTTP 前拒绝）：同文件 `test_cli_e2e_tampered_v2_authority_is_refused_before_any_http`、
  `test_cli_e2e_authority_manifest_binding_is_checked_before_any_http`；
  `test_quick_scan_observation_context.py::test_embedded_answer_is_refused_at_the_real_consumption_entry`。
* 反例（封存/ACK）：`test_quick_scan_c06_complete_seal.py` 的 `test_missing_standard_answer_blocks_then_supplements_with_zero_model_calls`、
  `test_compact_package_is_superseded_and_the_old_head_ack_cannot_settle_it`、
  `test_send_uncertain_is_reconciled_before_a_new_head_is_written`、
  `test_legacy_checkpoint_without_inputs_keeps_its_historical_package_read_only`、
  `test_a_second_frozen_context_for_the_same_task_is_refused`。
* 反例（整改批次 2）：`test_quick_scan_c06_authority_binding.py::test_per_question_metadata_is_bound_to_the_frozen_manifest`（14 参数例）、
  `::test_authority_with_a_duplicate_schema_version_is_refused`、`::test_authority_with_a_nested_duplicate_key_is_refused`、
  `test_quick_scan_observation_context.py::test_duplicate_score_in_standard_body_is_refused` 等正文严格三例；
  `test_quick_scan_c06_complete_seal.py::test_foreign_run_and_scan_context_cannot_seal_this_checkpoint`
  （block `c06_run_scan_unbound`）、`::test_supersede_rejects_each_forged_complete_observation_field`（5 参数例）、
  `::test_prepare_refuses_a_forged_complete_observation_before_any_head_exists`；
  `test_qa_c06_02_subprocess_cli.py` 的错 metadata / 重复 authority（key 读取 0、HTTP 0、费用预约 0）
  与损坏正文（不补分、不重问、每题恰 1 发）四例。

## 退出码

* `0`：运行/封存成功（含全部 reuse、全部 already_sealed）。
* `1`：入口 fail-closed（authority 无效、manifest/authority 绑定不符、题面篡改、策略未准入）。
* `2`：B01-a 花费授权缺失/无效（沿用既有行为，未改）。

## 尚缺接口

* 真实 StockWiki `observation-import` → ACK → 查询/UI 的联合验收（总控）。
* `--seal-deliveries` 尚不接受 `--question-manifest`/`--identity-snapshot`：补封依赖运行期
  已持久化的 context 侧表，不重新读题面文件。若未来需要“换 manifest 重封”，需新增显式参数。
* external retrieval 生产发送仍未实现（QA-NET-01 遗留，不在本卡范围）。
