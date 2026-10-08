# QA-C06-02 兼容矩阵

## 1. Authority 文档

| `schema_version` | 读取 | 封包行为 | 越界字段 | 未知版本 |
|---|---|---|---|---|
| `1.0.0`（`quick_scan_c06_authority.schema.json`，`const` 未改） | 原 loader 原样通过 | 原紧凑 C06 包（`build_c06_package`） | 顶层字段集不等即拒 | — |
| `2.0.0`（新增 `quick_scan_c06_authority.v2.schema.json`） | 严格字段集 + `validate_context_document` + 可选 manifest/identity 交叉核验 | 完整 Observation（`build_complete_c06_package`） | 顶层/context/逐题/metadata 四处注入均拒（重签 digest 也拒） | — |
| 其它任何值 | `AuthorityUnavailable("unsupported c06 authority schema_version: …")` | 不适用 | 不适用 | **fail-closed，绝不降级成 v1** |

## 2. Work store schema

| 打开时 `user_version` | 行为 | 数据影响 |
|---|---|---|
| 0（新库） | 建 v1…v6 全部对象 | 无历史 |
| 1 / 2 / 3 / 4 / 5 | 逐级 `_apply_v4/_v5/_v6_migration`，单事务 COMMIT/ROLLBACK | 旧 checkpoint / 旧 sealed 包 / 旧 ACK / 费用账本 **payload 与 hash 不变**；已封存包被写入 `quick_scan_delivery_revision` 作为 revision 1（只增行，不改旧行） |
| 6 | 直接 `_validate_schema(6)` | — |
| 其它 | `ValueError("unsupported quick-scan work database schema version")` | 不写 |

迁移失败（如 v1 含 result_ready 但无 checkpoint）→ 整体 ROLLBACK，`user_version` 保持原值、
无新表（`test_v1_migration_rejects_completed_work_without_checkpoint` 断言）。

## 3. 打包产物

| 产物 | 旧（v1 紧凑） | 新（v2 完整） |
|---|---|---|
| ExchangePackage envelope | 1.0.0 | 1.0.0（**未改**） |
| `observation.schema_version` | 不带（紧凑观察仅 entity/question/scope/answer-lite/execution） | 元数据来自 `metadata`，1.1.0 完整字段 |
| `observation.information_cutoff` | 无 | 有（来自冻结 metadata，**不借封包时钟**） |
| `observation.answer` | `{question_id,response_kind,score,summary,status,evidence:[{url}]}` | 完整 answer-content 1.0.0（evidence 带 id/title/url/published_at/claim，metrics、coverage 等全字段） |
| `execution.started_at` | 无 | `attempt.send_intent_at`（原派发时刻的 UTC ISO） |
| `observation_id` | `obs_sha(entity,question,scope)` | `obs_sha(完整观察内容)` —— 与 IQS canonical digest 同一算法 |
| `payload_sha256` / `item_id` / `package_id` / `delivery_key` | 不变算法 | 不变算法（`itm_`/`pkg_`/`canonical_sha256`/`sha256(pkg\nitm\tpayload)`） |

`validate_checkpoint_binding` 对两种观察都成立：`answer.summary == checkpoint.answer.description`、
evidence URL ⊆ provenance.source_urls、execution 九项与 provenance 一一对应。

## 4. 交付状态机与修订链

| 状态 | 允许的后续 | 本包改动 |
|---|---|---|
| `blocked`（无包） | `blocked` / `ready` | 新增可持久化阻断码；补齐后可 `ready` |
| `ready`（包=revision N） | `send_uncertain` / `delivered` / `rejected` / `conflict`；**v2 另允许 supersede → 新包=revision N+1** | 新增 |
| `send_uncertain` | `ready`（`confirm_result_delivery_not_sent`，四类“未发出”原因）/ `delivered` / `rejected` / `conflict` | 未改；**禁止 supersede，先对账** |
| `delivered` / `rejected` / `conflict` | 终态，ACK 不可改 | 未改；**禁止 supersede、禁止重投** |

- head 定义 = `MAX(revision)`，且必须与 `quick_scan_result_delivery.package_sha256` 一致
  （`_validate_v6_side_tables` 每次打开校验）。
- 包不可变触发器 v5→v6：语义“任何改写都 ABORT”，仅当新包恰等于 **当前 MAX 修订** 的完整八列时放行；
  部分列改写（如只改 `package_json`）仍 ABORT。
- 旧 head 的 ACK：`validate_import_ack` 按 head 行逐字段比对，`package_id` 不匹配即 `ValueError`
  → **旧 head ACK 落不到新 head**；终态重复 ACK 幂等返回同一条记录。

## 5. 读取方（StockWiki 方向）

| 项 | 兼容性 |
|---|---|
| 旧 v1 紧凑包 | 字节/`package_id`/`delivery_key` 不变，可继续导入 |
| 新 v2 完整包 | envelope 同 1.0.0，observation 满足既有 Observation 1.1.0；接收方需具备完整观察能力 |
| 旧 ACK | 保留只读；新 head 不接受旧 ACK |
| 公共 schema（Exchange/Observation/Answer） | **零改动**（`schemas/` 未触碰） |
| IQS 公共 C06 题义 / 问卷发布锁 | **零改动** |

## 6. 依赖

`pyproject.toml` 仅新增 `jsonschema[format-nongpl]>=4.23.0`（九件 overlay 原有声明）与
mypy 对 `jsonschema`/`referencing` 的 `ignore_missing_imports` 覆写。未安装、未升级任何全局包，
未新增其它依赖，未改动锁定文件 `requirements-lock.txt`。

## 7. 回退

**唯一回退方式：禁用新 v2 写能力，保留侧表与历史。**

1. 把 `--c06-authority` 指回 1.0.0 文档（或移除 2.0.0 文档，改用约定文件名下的 1.0.0 内容）；
2. 新运行即回到 v1 紧凑路径；已有 `quick_scan_*` 侧表与 `quick_scan_delivery_revision` 保持可读；
3. **禁止** `PRAGMA user_version=<5` 降库、**禁止**删除侧表/修订链/旧包/旧 ACK。
   降库会让 `_validate_schema` 与新触发器不匹配，且会破坏“历史 hash 不改”的承诺。
