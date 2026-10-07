# 当前接口补充（2026-10-07，优先于下方历史规格）

公共C06/manifest/search-policy schema版本不变，未增加公共extensions。源码提交84e24ef；下表source content_hash已刷新至本次Git规范源码。manifest计划新增可选routing_fingerprint_by_question并为非base generation保留每题覆盖；旧题routing不可变。admission回执新增external_dispatch_reason，当前任何external模式都external_dispatch_enabled=false且HTTP0（未实现生产dispatcher）。

真实HTTP内部私有work_transport只由client生成，不取模型回答字段；含最终work_attempt_id/final_receipt。private receipt仍检查质量，store仍核work/lease/model/request/hash/phase。未知/未计价请求不得重派，checkpoint拒绝公开error。legacy单route修复不扩大到configured v2。更多最终行为与未完成项见summary.md和remediation-2026-10-07.md。

---

# QA-NET-01 interfaces

源仓：`C:/Users/郑曾波/Projects/StockQAbyLLM`（`master`，base `6a9ff13864ebb160d5c4ab3cf2f42155d9f4aa99`）。
所有命令在该仓根目录、离线运行；`live=off`，本包未发出任何真实 provider / 搜索 / MCP 请求。

## 1. 公开 CLI 签名（本包新增/扩展的参数）

```text
# 逐题执行 + 冻结问卷 + 检查点→C06 封存
python -B -X utf8 main_with_llm.py ^
  --company "<公司名>" --entity-id "ENT_<uuid>" --provider <provider_config_ref> ^
  --config <compose输出>/questions.json --output <out>.json --require-search ^
  --identity-snapshot <W04 identity-export-g2b.json> ^
  --spend-authorization <spend_authorization.json> ^
  [--question-manifest <compose输出>/manifest.json] ^
  [--security-scope-id SEC_<listing>] ^
  [--c06-authority quick_scan_c06_authority.json] ^
  [--search-policy quick_scan_search_policy.json]

# 重启补封入口（模型请求恒为 0，不需要 --company/--batch）
python -B -X utf8 main_with_llm.py --seal-deliveries [--c06-authority <path>]
```

参数约束（全部在任何 HTTP 之前校验，违反即 exit 1、发送 0）：

| 参数 | 约束 |
|---|---|
| `--question-manifest` | 必须与 `--require-search`、`--identity-snapshot` 同时使用 |
| `--security-scope-id` | 仅在 `--question-manifest` 下生效 |
| `--search-policy` | 必须与 `--require-search` 同时使用 |
| `--seal-deliveries` | 不能与 `--company` / `--batch` 同时使用 |
| `--c06-authority`（显式） | 文件缺失/非法 → 入口失败关闭（exit 1），不猜补 |
| `--c06-authority`（缺省） | 约定路径 `./quick_scan_c06_authority.json`；不存在 → 检查点落 durable block |

## 2. 消费的 IQS 接口（只读，均未修改）

| 接口 | 版本/观察值 | content_hash (SHA-256) | 说明 |
|---|---|---|---|
| 冻结问卷 manifest（`question_sets.py compose` 产出） | `schema_version=3.1.0`、`template_version=3.2.0`、`answer_format=screening-1`、`metric_contract_version=1.0.0` | 逐次由运行时计算（`manifest_sha256` 打印在计划回执中） | 问题 ID / module_id / semantic_sha256 / prompt_sha256 / definition_sha256 / scope 冻结 |
| `references/search-and-llm-playbook.md` | 1.0.0（2026-10-07） | `a5f4da87ea84df95e3757267ff40bc76e3fd7c0d6cdf755ea29ce1c877f44141` | 本包全部搜索/证据/失败分类依据 |
| `examples/search-and-llm-policy.template.json` | `template_version=1.0.0`、`template_only=true` | `494d389333cac50f1093dee3fb9dbcf5934647b93eb9c0befbe872c7fce404f8` | **导入即拒绝**（`template_not_executable`），不是执行授权 |
| `schemas/quick_scan/exchange.schema.json`（C06） | 1.0.0 | `efdf0e3427d1bbb73892e9573fbf3b1f0389218e3f5a0c8dff4ca0cd63764122` | 只消费，未改动 |
| `schemas/quick_scan/query.schema.json`（C07） | 1.0.0 | `e7fc264b43d85e4cdd5c82a71b9fedb579dd0fc1196f3c5fde661f14fa1756f8` | 只消费，未改动 |
| `docs/implementation/tasks.json` | 观察版 | `a7c6167d4bf9aca56b529bd85bd70b0ae01a99125033e06dcbcdcbb06731bf60` | Q02/Q10/Q13 卡 |

### 2.1 冻结 manifest 的可运行生成命令（IQS 根目录，只读产出到临时目录）

```powershell
python -B -X utf8 scripts/question_sets.py compose --profile examples/profile.json --mode quick --answer-format screening-1 --out-dir <TEMP>\qs_compose --max-questions 3
```

产出 `manifest.json` + `questions.json`。StockQA 侧双向绑定校验：

* `sha256(questions.json[i].text) == manifest.questions[i].prompt_sha256`（31/31 实测一致）
* 反向：题集 ID 顺序、每题 `sha256(text)` 必须与 manifest 完全一致

## 3. 本包产出的接口（StockQA 版本化工件）

| 接口 ID | 版本 | 文件 / 载体 | content_hash (SHA-256) |
|---|---|---|---|
| `stockqa.quick_scan_c06_authority/1.0.0` | 1.0.0 | `src/config/quick_scan_c06_authority.schema.json` | `16e04f844445221b370c9c3e50d84e5d28797b4b9e2a93ee94ca0ed210257de2` |
| `stockqa.quick_scan_search_policy/1.0.0` | 1.0.0 | `src/config/quick_scan_search_policy.schema.json` | `e525f6dcb2a0116ca64327b8e2da21cfb970ec18619e8cfcffc485b8c9f19876` |
| 示例搜索策略（惰性） | 1.0.0 | `examples/quick_scan_search_policy.example.json` | `ea00ad2a34e2b2fe87f5ede32a7dab54327850cae7a9988d10d08158510e7e57` |
| `stockqa.consumes_iqs_question_manifest/1.0.0` | 1.0.0 | `src/utils/quick_scan_question_manifest.py` | `72d106709887eda10a506bd39012c4a2aae9eff280f9cfb97cce28374eb76734` |
| `stockqa.question_manifest_plan/1.0.0` | 1.0.0 | 同上（计划回执 schema） | 同上 |
| `stockqa.evidence_package/1.0.0` | 1.0.0 | `src/utils/quick_scan_evidence.py` | `3946e8708dda5b9c8ecd90527e36ac08ae99625b46e317f7e51ffae3824f6e26` |
| `stockqa.search_policy_admission/1.0.0` | 1.0.0 | `src/config/quick_scan_search_policy.py` | `021d1378bdd3ffcccac47198187a3f88855f553751d95cc25a543e1c52aa1763` |
| `stockqa.seal_deliveries/1.0.0` | 1.0.0 | `src/utils/quick_scan_delivery_seal.py` | `d81f73e114a669882f91f2bac1653be0017f1f0b2dc464eb1373260ca66c4e99` |
| 分层搜索判定 `STAGES` | 1（代码常量） | `src/providers/search_capability.py` | `44e31f7ce41dc633c6dc624f4fa0fc3a10b3d15b9ac78f54f818eb5602072d08` |
| Anthropic 续写分类 | 1（代码常量） | `src/providers/continuation_protocol.py` | `50929ae60f2933d76943aaa5bc937070e5924baa0dd7c3c135719f67b51634d5` |
| 四路由离线 parser | 1（代码常量） | `src/providers/external_search_parsers.py` | `777f330247b7b0cf51d34ce46d03a5886ceedcc38a8304f15788f8db84bf381e` |

两个 schema 文件以 JSON Schema draft-07 形式发布；StockQA **不引入 jsonschema 运行时依赖**，加载器按同一约束手工校验（`_validate` / `_validate_shape`），失败给出有界 reason code。

## 4. 输出回执（stdout 单行 JSON）

| 回执 schema | 触发 | 关键字段 |
|---|---|---|
| `stockqa.question_manifest_plan/1.0.0` | `--question-manifest` 且进入 identity 生命周期时，**派发前**打印 | `manifest_sha256`、`counts`、`model_calls_planned`、`generation_by_question`、逐题 `action/scope/generation/work_item_id` |
| `stockqa.search_policy_admission/1.0.0` | `--search-policy` | `policy_sha256`、`mode`、`admitted_routes`、`route_rejections`、`external_dispatch_enabled` |
| `stockqa.seal_deliveries/1.0.0` | `--seal-deliveries` | `authority_sha256`、`sealed/blocked/already_sealed/untouched`、`model_calls=0` |

增量计划动作枚举：`dispatch` / `expired_dispatch`（唯一可能消耗模型调用）；`reuse` / `seal_only` / `reconcile` / `in_flight` / `skip_not_applicable` / `skip_cancelled` / `deferred_scope_unbound`（均 0 模型调用）。

## 5. 搜索 route 支持 / 未启用原因

| Route | 状态 | 依据 / 原因 |
|---|---|---|
| MiMo 原生（`mimo-v2.6-flash/pro/pro-ultraspeed`，Chat Completions + `web_search`/`force_search`） | **已接线（本包之前既有，Q02 历史验收）** | 本包未改动其协议；`verify_native_search_receipt` 仅增加分层判定 |
| MiniMax-M3 原生（Responses / Anthropic Messages） | **已接线（既有）** | 同上；`completed` 搜索事件 + 来源 URL 判定复用 |
| DeepSeek Responses `web_search` | **拒为 native route** | 官方兼容表 ignored；2026-10-07 复测无搜索事件（IQS `search-policy.md` 记录）。`native_search_route_supported("deepseek_responses") == (False, "responses_web_search_ignored")` |
| DeepSeek Anthropic Messages 续写 | **未启用（partial）** | `classify_anthropic_messages_response` 离线实现并通过正反例；`continuation_admission(protocol_verified=False/…)` → `admitted=false`、`max_uses_is_not_a_cost_bound=true`。协议续写与可执行费用上界未确认，保持未启用，不编造最终答案或搜索数 |
| Brave | **parser/证据/准入离线验证；执行未启用** | 无 storage entitlement 确认（`storage_rights.confirmed=false` → `storage_rights_unconfirmed`）、无已核计价（`cost_bound_verified=false` → `cost_bound_unverified`）→ 未准入，发送 0 |
| Tavily | 同上 | 同上 |
| Z.ai REST | 同上 | 同上；探针只证明当时可检索，未证明计价/存储权 |
| Z.ai Streamable HTTP MCP | 同上 | 同上；`tools/list` 真实工具名/schema 已在 parser 中按观测形状实现 |
| Z.ai legacy SSE | **未开发** | 本包无准入证据，按包指令不优先 |
| B01-b Brave/Tavily 实验 runner | 只读历史 | `pilot_runs/**` 未改动（inputs.lock 7 项原样保留） |

**外部检索 → context → LLM 的发送接线不在本包**：准入闸门已接入 runner（未准入即拒绝、发送 0），parser 与证据规整已离线验收，但把 evidence context 注入回答 prompt 的生产路径留待下一批，避免交付半接线的计费路径。

## 6. 最终答案 / 证据 proof 映射

| 层 | 判定函数 | 落到 C06 的字段 |
|---|---|---|
| 传输/attempt | 现有 transport（未改） | `execution.request_id` / `attempt_id` |
| 搜索执行（native） | `verify_native_search_receipt` | `execution.search_status="executed"`、`execution.search_receipt_id` |
| 搜索执行（external） | `verify_external_retrieval`（`origin="external"`、request_id 匹配、可解析条目） | **不写入** C06 execution——外部回执不能伪装成原生工具事件；本路径未启用 |
| 证据 | `verify_evidence_binding` + `build_evidence_package` | `answer.evidence[].url ⊆ provenance.source_urls`；`evidence_sha256` 为 StockQA 内部版本化产出，非 C06 顶层字段 |
| 最终答案 | `verify_final_answer` / `classify_anthropic_messages_response` | `answer.summary` / `score` / `status` |

反例：HTTP 200 无搜索事件、手写 URL、模型自称已搜索 → `search_not_executed` / `search_unverified`，**不计分**（LLM-02）。`stop_reason=tool_use` → `continue_tool_use`，`final_text=None`，**不标完成**。

## 7. C06 golden 生成命令

StockQA 侧不持有 consumer golden（StockWiki W05/W10 才拥有）。本包可复现的**包字节生成 + 独立校验**命令：

```powershell
# 产出一个真实 C06 v1 包（临时根内）并用 outbox 校验器独立验证
python -B -X utf8 -m pytest -p no:base_url tests/unit/test_qa_net01_c06_seal.py::test_runner_seals_checkpoint_into_valid_c06_package -q --no-cov

# 公开 CLI 全链（runner → checkpoint → sealed package → warm 0 请求 → --seal-deliveries）
python -B -X utf8 -m pytest -p no:base_url tests/integration/test_qa_net01_cli_e2e.py::test_cli_e2e_native_path_seals_c06_and_warm_run_sends_zero -q --no-cov
```

producer→consumer 的跨仓 golden（StockWiki 真实导入/ACK）**本包未运行、未伪造**，按交接规范由总控集中联调。

## 8. 正例 / 反例

| 正例 | 反例 |
|---|---|
| 冻结 manifest + 题文件双向一致 → 派发计划打印 | 改 prompt 不改 hash → `manifest_prompt_hash_mismatch`，HTTP 0 |
| 权威文档齐全 → 检查点即刻封存 `ready` | 权威缺失 → `blocked/c06_authority_unavailable`，work 仍 `result_ready` |
| 重启 `--seal-deliveries` → `model_calls=0`、幂等 | 同一检查点换权威重建 → `immutable` 拒绝（既有 Q10 用例） |
| 4 旧题 + 新增/过期题 → `model_calls_planned=2` | 结果不明题 → `reconcile`，不重派发 |
| 搜索策略已准入 → 打印 `external_dispatch_enabled=true` | 模板标记 / 未准入 external → exit 1、HTTP 0 |

## 9. 兼容与降级

* **无破坏**：既有 CLI 参数、C06 公共 schema、work/outbox 状态机、模型策略与预算语义均未改动；新增参数全部可选。
* **降级路径**：不给 `--question-manifest` → 退回原题文件加载（但 C06 封存 glue 仍生效）；不给 `--c06-authority` → durable block（DB-07 预期行为）；不给 `--search-policy` → 完全原生路径。
* **black 26.5.1 机械重排**：`src/runners/llm_runner.py`、`src/utils/quick_scan_work_store.py` 除本包改动外被 `black` 重排了既有行——这与实施期间另一 lane 将 `.pre-commit-config.yaml` 的 black rev 升到 `26.5.1` 并同步进 `requirements-lock.txt` 的决定一致（旧 24.10.0 格式本来就会被 26.5.1 拒绝）；纯格式、零语义变化，全量测试与静态门在重排后复跑通过。另按新门补跑了 `isort --profile black`（7.0.0）。
* `.gitignore` 仅新增 8 条精确 `!` 例外（两个新 schema、一个示例策略、handoff 的 `handoff.json`/`artifacts.json`、交付日志目录与 `*.log`），未整类取消忽略。
