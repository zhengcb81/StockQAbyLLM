# QA-C06-02：完整观察、耐久封包与向后兼容接线

基线 `master@09f68a69bbdbf76e3a4fff63043cdd4815572e5b`，本包在其上连续实施；本卡交接的代码
commit 见 `handoff.json` 的 `snapshot.result_commit`（`handoff.json` / `artifacts.json` 在该 commit
之后生成，见同目录第二次提交）。唯一工作目录 `C:/Users/郑曾波/Projects/StockQAbyLLM`，唯一 QA writer。

**原 Q10 整体、真实 StockWiki 接收 / G3 / F05 未做，填 `not_run`（见 `case-map.md`）。**

## 最终行为

1. **完整标准 C06 走真实公开路径。** authority **2.0.0** 携带冻结 IQS `observation_context`；
   与其一同加载的 manifest/identity 在任何 key 读取与 HTTP 之前交叉核验（两种 manifest hash、
   逐题 frozen/stripped prompt、identity 原字节）。有完整标准答案时，`main_with_llm.py`
   真实运行 → 完整 checkpoint（紧凑 + 侧表）→ 完整 Observation 封存 → outbox/ACK。
   golden 见 `golden/`（synthetic，非 owner golden）。
2. **越界字段在真实消费入口被拒。** authority 顶层 / context 顶层 / 逐题 / metadata 内的
   `answer`、`execution`、`observed_at`、`observation_id` 即使重签 `observation_context_sha256`
   也拒绝；纯 metadata 的 v2 仍通过。**RED 见 `logs/red-embedded-answer.log`
   （1 failed / 44 deselected），GREEN 见 `logs/green-embedded-answer.log`（1 passed / 44 deselected）。**
3. **缺信息持久阻断，绝不重问 LLM。** 缺冻结 context → `c06_observation_context_unavailable`；
   缺完整标准答案 → `c06_standard_answer_unavailable`；两者都缺先记 context 阻断。work item
   始终停在 `result_ready`。补齐后 `--seal-deliveries` **模型 0** 落封（`model_calls: 0`）。
   已封存的历史紧凑包只读：不降级成 block，也不由历史极简答案“自动升级”成完整标准（
   `historical_package_readonly`）。
4. **完整正文不截断。** 标准答案原文（>5000 字节）原样写入 `quick_scan_standard_answer`
   侧表；紧凑 checkpoint 的 `description` 只放 `summary`，仍受 ≤5000 约束。compact 只是摘要，
   不由 URL 补 title/claim，不默认 confidence/basis。
5. **v1/v2 双通道、未知版本不降级。** v1 文档保持原字段集与原紧凑封包行为；v2 走完整观察；
   `schema_version` 不在 {1.0.0, 2.0.0} 一律 `AuthorityUnavailable`，绝不按 v1 读。
6. **schema v5 → v6 向前迁移。** 新增不可变 `quick_scan_observation_context` /
   `quick_scan_work_context` / `quick_scan_standard_answer` 侧表与
   `quick_scan_delivery_revision` 追加式修订链。迁移在单事务内完成（失败整体回滚），旧
   checkpoint / 旧 sealed 包 / 旧 ACK / 费用账本 payload 与 hash 一字节未改；已封存的旧包被
   编目为 revision 1。同任务绑定不同 context → `WorkConflictError`。
7. **修订链与 ACK 负例。** 旧包（revision 1）→ 完整包（revision 2，`supersedes_revision=1`）；
   head 永远是 MAX(revision) 且与 `quick_scan_result_delivery.package_sha256` 一致。旧 head 的
   ACK 落不到新 head（package_id 不匹配即拒）；假 payload hash / 他人 store_id / 跨包 ACK 一律拒绝；
   相同 ACK 重放幂等；`send_uncertain` 必须先对账（`confirm_result_delivery_not_sent`）才允许
   supersede；已 delivered 永不重投、不再计费。
8. **重启 / 丢 ACK / warm 全部 0 费用。** `--seal-deliveries` `model_calls=0`；warm 运行
   `model_calls_planned=0`、HTTP 0。

## 实际验证（同一批集中门）

`python -B scripts/checks.py --full --timeout 300`
→ `mode=full result=pass exit=0 steps=black,isort,mypy,bandit,pytest,smoke`，
**1037 passed, 0 failed, 0 skipped**（原 QA-NET-01 基线 982 + 本包新增/扩展用例），
原始日志 `logs/full-gate-GREEN.log`。真实模型 / 搜索 / 付费请求 / 下载次数 **0**；
`STOCKQA_RUN_LIVE_E2E=0`。静态门单独一次亦 pass（同一脚本 `--static-only`）。

新增与扩展用例（selector 见 `case-map.md`）：

- `tests/unit/test_quick_scan_observation_context.py`（+`embedded_answer` 入口反例）
- `tests/unit/test_quick_scan_c06_complete_seal.py`（7 例：完整封存/重放、context 冲突、
  缺标准答案阻断→补包 0 费用、compact→revision2 + 旧 head ACK 负例、send_uncertain 先对账、
  历史包只读、v6 侧表形状）
- `tests/integration/test_qa_c06_02_e2e.py`（3 例：完整 C06 CLI E2E（31 题冷发/暖 0/封存 0/
  ACK 幂等与负例）、篡改 v2 authority HTTP 前拒绝、authority↔manifest 绑定 HTTP 前拒绝）
- `tests/unit/test_quick_scan_work_store.py` / `test_quick_scan_budget.py`：schema 断言改用
  `SCHEMA_VERSION`，v3/v4 降级 fixture 补齐 v5/v6 剥离（迁移语义未改）

## 边界与兼容

见 `compatibility-matrix.md`。要点：公共 ExchangePackage 1.0.0 / Observation 1.1.0 /
Answer 1.0.0 未改；旧 authority1.0 schema 原样保留（新增 `quick_scan_c06_authority.v2.schema.json`）；
无 `const` 原地改写；未知 authority 版本 fail-closed；v1 权威继续产出原紧凑包。

**回退方式：禁用新 v2 写能力并保留侧表/历史**——把 `--c06-authority` 指回 1.0.0 文档（或删除
2.0.0 文档）即回到 v1 紧凑路径；侧表与修订链保持可读。**不得降库（`user_version` 回写）或删数据回退。**

## 已做 / 未做

已做：上述 1–8 全部；交接全套 + `compatibility-matrix.md` + 完整标准 C06 golden。

未做（`not_run`）：真实 StockWiki observation-import → ACK → UI 联合验收（总控）、G3、F05、
真实 owner identity golden、SW-REPAIR-02 / EVID-LAB-01 相关项、在线 live E2E。

## 准确入口命令

```powershell
# 集中门（唯一一次全量回归 + 静态）
python -B scripts/checks.py --full --timeout 300

# 本包 RED→GREEN 复现
python -B -X utf8 -m pytest -p no:base_url tests/unit/test_quick_scan_observation_context.py -k embedded_answer -q

# 本包新增用例
python -B -X utf8 -m pytest -p no:base_url tests/unit/test_quick_scan_c06_complete_seal.py tests/integration/test_qa_c06_02_e2e.py -q

# 完整标准 C06 公开生成命令（合成输入，HTTP 边界 stub；见 golden/golden.json 的 generator_command）
python -B -X utf8 main_with_llm.py --company "Fixture Corp" --entity-id ENT_CONTEXT_FIXTURE --provider openai --config <tmp>/questions.json --output <tmp>/result.json --require-search --identity-snapshot <tmp>/identity.json --spend-authorization <tmp>/spend_authorization.json --question-manifest <tmp>/manifest.json --security-scope-id SEC_CONTEXT_FIXTURE --c06-authority <tmp>/quick_scan_c06_authority.json

# 重启补封（模型 0）
python -B -X utf8 main_with_llm.py --seal-deliveries --c06-authority <tmp>/quick_scan_c06_authority.json
```

最小离线复现只需第一条集中门命令；它包含全部 unit/integration 与静态门，无需网络与 key。

## 下一步与回退

先按 `artifacts.json` 与本目录核验；真实 StockQA → StockWiki 导入/ACK/恢复/UI 由总控联合执行。
不得自动扩大到 200 家 L03、不得改 IQS 公共 schema、不得把 synthetic golden 当 owner golden。
回退按上文“禁用 v2 写能力”，任何情况下不得 `PRAGMA user_version` 回写或删除侧表/修订链。
