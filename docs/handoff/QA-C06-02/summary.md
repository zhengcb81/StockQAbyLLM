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

## 整改批次 2（2026-10-08 卡：四组绑定缺口）

总控集中验收/独审提出的四组缺口同批修复，全部沿原允许路径，无卡外新文件：

1. **题义/发布绑定（P1）**：`load_c06_authority` 的 manifest 交叉核验改为复用
   `bind_question_context` 的同一套共享规则（`check_context_matches_manifest` +
   `manifest_question_bindings`）——逐题 field/construct/scope/definition/semantic/
   rubric/template/module/method/cohort/cutoff/entity 与冻结 manifest 逐字段核一致，
   第二套遗漏校验不复存在；错误 template/definition/semantic 的真实子进程 CLI 在
   key 读取、HTTP、费用预约之前 `exit 1`（DB/输出文件均未创建）。
2. **run/scan 绑定（P1）**：带 context 的 `save_answer_checkpoint` 在与 attempt 同一事务里
   把冻结 context 的 run/scan 对写入 `work_run_ref`（owner 命名空间 ↔ 实际派发命名空间的
   不可变映射，不替换任何一侧）；封存前逐题核该映射，foreign run/scan → 持久阻断
   `c06_run_scan_unbound`。attach 路径只核不写，绝不发明映射；独立 run/attempt ID 不合并。
3. **严格 JSON（P2）**：新增共享 `strict_json_loads`（复用响应解析器的重复键 hook +
   拒绝 NaN/Infinity），authority loader 与 `parse_standard_answer` 两个入口（含嵌套层级）
   生效。重复 schema_version / 嵌套重复键 / 非有限数一律拒绝；损坏正文显式
   `ValueError("… not strict JSON …")`，不再静默取最后一个值、不退 legacy compact 提交成功；
   纯散文与合法 v1/v2/正文行为不变。
4. **持久 full head 绑定（P1）**：`prepare_result_delivery` / `supersede_result_delivery`
   对“自称完整 Observation”的包，用不可变侧表 context + 完整正文 + 原成功 attempt
   的 send_intent 重建并要求逐字节相等；任何单改 claim / typed metric / metadata /
   started_at 都在事务内拒绝，**不产生半修订、head 不动**。compact 包与无侧表的旧库
   保持原历史绑定规则；compact→完整升级、旧 ACK 拒/同 ACK 幂等、send_uncertain 先对账、
   delivered 只读全部继续通过。
5. **真实子进程 CLI E2E**（新增 `tests/integration/test_qa_c06_02_subprocess_cli.py`）：
   实际子进程 cold（仅 HTTP 边界 stub，31 发可数）→ **另进程撤 stub** warm（reuse 31、
   新 HTTP 0）→ 另进程 seal（`model_calls=0`，already_sealed 31）；错 metadata 与重复
   authority 子进程 `exit 1`、HTTP 0、key 读取 0、无费用预约/无成功 checkpoint；
   损坏正文不补分、不重问（每题恰 1 次发送，v2 持久 block `c06_standard_answer_unavailable`）。

RED→GREEN 证据：原字节 9 例集中反例 `7 failed/2 passed → 9 passed`
（`logs/red-boundary-remediation.log` → `logs/green-boundary-remediation.log`，含真实 v5
迁移 2 例保持 GREEN 的回归）；新单测 `31 failed/63 passed` RED（`logs/red-unit-remediation.log`）；
子进程 4 例 RED（`logs/red-subprocess-remediation.log`，其中首跑含 guard 对 Windows
asyncio socketpair 的环境修正记录）→ 门内全 GREEN。原始总控 RED 证据仍在 IQS intake
（`controller-boundaries.stdout.log` 等），本仓不改其字节。

## 第二轮剩余整改（2026-10-08 remaining-repair 卡：四组同链边界）

总控复验结论 `partial_verified/changes_requested` 后的六个失败 case 收敛为四个同链边界，
同批修复，未新增验收门（代码结果 `b6eaa08`，基线 `361a721`）：

1. **QR1B 具体挂牌 ID 在入口绑定（P1）**：共享规则 `check_context_matches_manifest` 在
   entity/scope 之外同时核 `metadata.security_id`/`segment_id` 与 `manifest.profile` 的
   对应挂牌——IQS_22 改 `SEC_FOREIGN` 并重签 context 后，loader 在 key/HTTP/费用预约/
   checkpoint **之前**拒绝（真实子进程 CLI：exit 1、HTTP stub 发送 0、key 打开账本不存在、
   DB/输出文件未创建）；正常 entity/security 保持通过（fixture 无 segment 题，规则已实现）。
   未改任何冻结 fixture 标签。
2. **QR2B 所有新完整 write 共享 run/scan 门（P1）**：seal 的映射检查抽成唯一
   `_unmapped_run_scan_pairs` 助手，`prepare_result_delivery` / `supersede_result_delivery`
   同事务调用——已被 `c06_run_scan_unbound` 阻断的外来 context 无法再经公开 prepare 落
   ready；失败不增 revision、不替换 blocked/head、不自动重问。合法晚 attach 与已有映射不变。
3. **QR3B strict JSON 拒数值溢出（P2）**：共享 `strict_json_loads` 增加 `parse_float` 有限性
   检查，`1e400` 一类溢出字面量（普通 `json.loads` 静默变 `inf`）在任意嵌套/数组层拒绝；
   正常有限数与普通文字维持旧约定，坏正文不退可计分 compact、不补默认分。
4. **QR4B 缺持久完整输入的新完整 write 必须拒绝（P1）**：`_assert_complete_observation_binding`
   对自称完整的包在缺 context/answer 侧表时**直接抛出**（不再 fail-open 回落历史规则），
   并同样核 run/scan 映射；历史已 sealed 读取、compact v1 写入、合法补齐升级、delivered
   只读保持兼容——为此把 work_store 的 ACK/派发 fixture 观察对齐为历史 compact 形状
   （八个字段，测的是交付机制而非完整写入），不插默认侧表、不改旧记录。

RED→GREEN：固定原字节 `remaining_cases.py` + 仓内镜像
`13 failed/3 passed → 16 passed`（`logs/red-remaining-cases-r2.log` →
`logs/green-remaining-cases-r2.log`）；原 9 反例 `9 passed`
（`logs/green-original-nine-r2.log`）；受影响 247 基线与修复后均 `247 passed`
（`logs/baseline-affected-247-r2.log`、`logs/green-affected-247-r2.log`）；
改动模块的其余消费方定向批次 `190 passed`（`logs/green-extended-affected-r2.log`）；
`checks.py --static-only` pass（`logs/green-static-only-r2.log`）、pre-commit 精确清单 pass
（`logs/pre-commit-r2.log`）。按卡“已收 1083 全套日志、不无故重跑全套”，本轮未重跑全量门。

## 实际验证（同一批集中门）

批次 1（原交付）：`python -B scripts/checks.py --full --timeout 300`
→ `mode=full result=pass exit=0 steps=black,isort,mypy,bandit,pytest,smoke`，
**1037 passed, 0 failed, 0 skipped**（原 QA-NET-01 基线 982 + 本包新增/扩展用例），
原始日志 `logs/full-gate-GREEN.log`（**保留**）。真实模型 / 搜索 / 付费请求 / 下载次数 **0**；
`STOCKQA_RUN_LIVE_E2E=0`。静态门单独一次亦 pass（同一脚本 `--static-only`）。

批次 2（2026-10-08 整改卡，代码 commit `acb7dbf`）：同一条仓既定门再跑一次
→ `mode=full result=pass exit=0 steps=black,isort,mypy,bandit,pytest,smoke seconds=145.74`，
**1083 passed, 0 failed**（= 1037 + 整改新增 46 个用例），
日志 `logs/full-gate-remediation-2026-10-08.log`；pre-commit 对精确交付路径清单 pass，
日志 `logs/pre-commit-remediation-2026-10-08.log`。

新增与扩展用例（selector 见 `case-map.md`）：

- `tests/unit/test_quick_scan_observation_context.py`（+`embedded_answer` 入口反例）
- `tests/unit/test_quick_scan_c06_complete_seal.py`（7 例：完整封存/重放、context 冲突、
  缺标准答案阻断→补包 0 费用、compact→revision2 + 旧 head ACK 负例、send_uncertain 先对账、
  历史包只读、v6 侧表形状）
- `tests/integration/test_qa_c06_02_e2e.py`（3 例：完整 C06 CLI E2E（31 题冷发/暖 0/封存 0/
  ACK 幂等与负例）、篡改 v2 authority HTTP 前拒绝、authority↔manifest 绑定 HTTP 前拒绝）
- `tests/unit/test_quick_scan_work_store.py` / `test_quick_scan_budget.py`：schema 断言改用
  `SCHEMA_VERSION`，v3/v4 降级 fixture 补齐 v5/v6 剥离（迁移语义未改）

整改批次 2 新增/扩展（2026-10-08 卡报备清单）：

- **新增** `tests/unit/test_quick_scan_c06_authority_binding.py`（22 例：14 个逐题 metadata
  篡改参数例 + 越首题例 + identity/manifest 负例 + v1/v2 正例 + 重复键/嵌套重复键/非有限数）
- **新增** `tests/integration/test_qa_c06_02_subprocess_cli.py`（4 例：真实子进程
  cold→另进程 warm→另进程 seal、错 metadata、重复 authority、损坏正文，全部子进程执行）
- **扩展** `tests/unit/test_quick_scan_c06_complete_seal.py`（+6 defs / +10 收集例：run/scan
  映射持久化、foreign 拒绝、晚 attach 已知 run 放行、独立 run/attempt、伪造
  claim/typed metric/metadata/started_at 参数例、prepare 伪造拒绝）
- **扩展** `tests/unit/test_quick_scan_observation_context.py`（+7 defs / +10 收集例：正文
  严格 JSON 三例 + 合法正文/散文不变 + `strict_json_loads` 直测 + bind 侧共享规则参数例）

## 边界与兼容

见 `compatibility-matrix.md`。要点：公共 ExchangePackage 1.0.0 / Observation 1.1.0 /
Answer 1.0.0 未改；旧 authority1.0 schema 原样保留（新增 `quick_scan_c06_authority.v2.schema.json`）；
无 `const` 原地改写；未知 authority 版本 fail-closed；v1 权威继续产出原紧凑包。

**回退方式：禁用新 v2 写能力并保留侧表/历史**——把 `--c06-authority` 指回 1.0.0 文档（或删除
2.0.0 文档）即回到 v1 紧凑路径；侧表与修订链保持可读。**不得降库（`user_version` 回写）或删数据回退。**

## 已做 / 未做

已做：上述 1–8 全部；交接全套 + `compatibility-matrix.md` + 完整标准 C06 golden。
整改批次 2：四组绑定缺口 + 真实子进程 CLI E2E + 交接收尾（changed_paths 实填、纯
authorized_paths、TEMP 残余清单、冻结 CRLF 双口径与还原命令）同批完成。

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

# 整改批次 2 用例（四组绑定 + 真实子进程 E2E）
python -B -X utf8 -m pytest -p no:base_url tests/unit/test_quick_scan_c06_authority_binding.py tests/unit/test_quick_scan_c06_complete_seal.py tests/unit/test_quick_scan_observation_context.py tests/integration/test_qa_c06_02_subprocess_cli.py -q

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
