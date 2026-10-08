# task_plan — QA-C06-02（PLAN_ID: QA-C06-02）

> pin：`docs/handoff/QA-C06-02/planning/`。本包不回落到其它 PLAN_ID。

## 目标

原 Q10 剩余整改：完整标准答案 → 完整 Observation → 耐久封存 → v1/v2 迁移与替代链/ACK 负例，
全程零收费/零网络，经真实公开 CLI 与同批集中门。

## 步骤（连续推进，TDD）

1. [x] 读卡 / handoff-rules / interfaces / inputs.lock / c06-authority-v2-input-map；核对基线
       `master@09f68a69…` 与 16 条 status（9 overlay + 7 旧未跟踪）。
2. [x] 写 RED：`test_embedded_answer_is_refused_at_the_real_consumption_entry`
       （1 failed / 44 deselected），存 `../logs/red-embedded-answer.log`。
3. [x] GREEN：`quick_scan_observation_context._metadata` 拒绝 4 个 detached 字段 +
       新增 `bind_context_to_work_item`；v2 loader（`quick_scan_c06_authority.py` +
       `quick_scan_c06_authority.v2.schema.json`）；存 `../logs/green-embedded-answer.log`。
4. [x] work_store `SCHEMA_VERSION 5→6`：context / work_context / standard_answer 侧表 +
       `quick_scan_delivery_revision` 修订链 + 迁移播种 + 打开期一致性校验 +
       `attach_standard_inputs` 补包入口。
5. [x] adapter：`build_complete_c06_package`（冻结 context + 完整答案 +
       `attempt.send_intent_at`）；v1 紧凑路径保持不变。
6. [x] seal：v2 分支（阻断码 / 补包 0 费用 / supersede / 历史只读），v1 分支原样。
7. [x] runner + provider：`standard_answer_transport` contextvar 显式开关、
       `_standard_transport` 拆分 summary/body、`_preflight_standard_answer` 预检、
       authority↔manifest/identity 绑定在任何 HTTP 之前。
8. [x] 测试：新增 `tests/unit/test_quick_scan_c06_complete_seal.py`（7）、
       `tests/integration/test_qa_c06_02_e2e.py`（3）；扩 `test_quick_scan_observation_context.py`；
       修 2 个迁移 fixture 的 v5/v6 剥离与 `SCHEMA_VERSION` 断言。
9. [x] 集中门一次：`python -B scripts/checks.py --full --timeout 300` → pass，1037 passed。
10. [x] golden 导出（合成，`golden_class=synthetic_only`）。
11. [x] 交接全套 + `compatibility-matrix.md`。
12. [x] 两次提交（代码/测试/日志/golden → 生成 artifacts+handoff → 二次提交）。

## 批次 2 — 2026-10-08 整改卡（remediation-2026-10-08）

输入：IQS [整改卡](../../../../../invest-quick-scan/docs/implementation/reviews/QA-C06-02/remediation-2026-10-08.md)、
[集中验收](../../../../../invest-quick-scan/docs/implementation/reviews/QA-C06-02/acceptance-2026-10-08.md)、
[独审](../../../../../invest-quick-scan/docs/implementation/reviews/QA-C06-02/independent-review-2026-10-08.md)。
起点头：`a12bc29`（master，工作树仅 7 旧未跟踪）。四组代码 + 交接同批、一批验证、一个集中审查后提交。

本批报备的精确测试文件：
- 新增 `tests/unit/test_quick_scan_c06_authority_binding.py`（组1 + 组3 loader 单元）
- 新增 `tests/integration/test_qa_c06_02_subprocess_cli.py`（真实子进程 cold/warm/seal、
  错 metadata、重复 authority、损坏正文 E2E）
- 扩 `tests/unit/test_quick_scan_c06_complete_seal.py`（组2 mapping/foreign、组4 伪造分量）
- 扩 `tests/unit/test_quick_scan_observation_context.py`（组3 正文严格 JSON、组1 共享规则）

源码只沿原卡允许路径：`quick_scan_c06_authority.py`、`quick_scan_observation_context.py`、
`quick_scan_work_store.py`、`quick_scan_delivery_seal.py`、`quick_scan_result_outbox.py`
（共享严格 JSON）、必要 `llm_runner.py`。无卡外新文件（guard/stub 只写入测试临时根）。

### 步骤（TDD 连续推进）

13. [x] RED 基线：独占副本（HEAD 字节）+ 原字节 `controller-cases-executed.py` 跑 9 例，
       复现 7 RED / 2 GREEN，日志存 `../logs/red-boundary-remediation.log`。
14. [x] 组1：共享 manifest 逐题规则（field/construct/scope/definition/semantic/rubric/
       template/module/method/cohort/cutoff/entity + prompt）供 loader `_crosscheck_manifest`
       与 `bind_question_context` 共用；单测 RED→GREEN；错 metadata 真实子进程 CLI 在
       key/HTTP/费用前拒。
15. [x] 组2：save 时把冻结 context 的 run/scan 对持久写入 `work_run_ref`（与 attempt 同事务），
       seal 时核 mapping；foreign 拒、正常有可核映射；不改 fixture 标签。
16. [x] 组3：共享 `strict_json_loads`（复用 llm_response_parser 重复键能力 + 拒非有限数），
       loader 与 `parse_standard_answer` 入口（含嵌套）生效；损坏正文显式拒绝，不退 legacy compact。
17. [x] 组4：prepare/supersede 对完整 Observation 用侧表 context+body 与原 attempt
       重建并要求逐字相等；失败事务无半修订；legacy compact 路径独立保留。
18. [x] 新增子进程 E2E：真实 cold（仅 HTTP 边界 stub）→ 另进程 warm/seal（撤 stub 仍恢复成功，
       HTTP0）；错 metadata/重复 authority 子进程 HTTP0、key0、无费用预约、无成功 checkpoint；
       损坏正文不补分/不重问。
19. [x] GREEN：同一独占副本流程重跑 9 例 → 9/9，日志存
       `../logs/green-boundary-remediation.log`；旧 v5 迁移 2 例保持 GREEN（回归保留）。
20. [x] 交接收尾：handoff.changed_paths 实填、authorized_paths 纯路径、TEMP 残余清单、
       冻结 CRLF 工作树 SHA vs Git blob SHA + 还原命令、isolation/summary/case-map/artifacts 更新。
21. [x] 一批验证：相关 unit/integration/真实 CLI 子进程/替代链/旧迁移 +
       `python -B scripts/checks.py --full --timeout 300` + `pre-commit run --files <交付清单>`。
22. [x] 集中审查（全量 diff 复读）→ 代码/测试/日志一次提交，交接证据二次提交。

## 未做（保持 not_run）

真实 StockWiki 联合验收、G3、F05、owner golden、在线 live E2E、SW/Lab 两包。

### 批次 2 执行结果（2026-10-08）

- RED：原字节 9 例 `7 failed/2 passed`；新单测 `31 failed/63 passed`；子进程 4 例 4 failed。
- GREEN：原字节 9 例 `9 passed`（含 2 个真实 v5 迁移回归）；门
  `python -B scripts/checks.py --full --timeout 300` → `1083 passed`（black/isort/mypy/bandit/
  pytest/smoke 全 pass）；`pre-commit run --files <交付清单>` 全 hook pass。
- 代码/测试/日志提交：`acb7dbfae0e6a45221924e2d1d5742a4999f4c9d`（在其上生成 artifacts/handoff
  后二次提交）。
- 清理：独占副本 ×2 + 可证归属 `pytest-149` 已删，回执
  `../logs/cleanup-remediation-receipt.json`（逐文件 SHA、CIM 进程 0、删后目录不存在）；
  `pytest-147/148` 归属不可证 → 列出不删。

## 批次 3 — 第二轮剩余整改（remaining-repair-2026-10-08）

基线核对：HEAD=`361a721`（=卡上收到交接）、工作树仅原 7 未跟踪、无并行变化 → 直接开工。
六个失败 case 收敛为四组同链边界；建议改动面 context/outbox/work_store 及其测试/交接。

### 步骤（TDD 连续推进）

28. [x] 报备本轮精确文件：源码 `quick_scan_observation_context.py`、`quick_scan_result_outbox.py`、
       `quick_scan_work_store.py`、`quick_scan_delivery_seal.py`；测试 `tests/unit/
       test_quick_scan_work_store.py`（fixture 对齐 compact 形状）与**新文件**
       `tests/integration/test_qa_c06_02_remaining_boundaries.py`（镜像 7 例，不动总控
       247 选择器内文件的用例数量）；交接沿 `docs/handoff/QA-C06-02/`。
29. [x] RED：独占根 `qa-c0602r2-c2f788`，原字节 `remaining_cases.py` + 镜像
       → `13 failed / 3 passed`（`../logs/red-remaining-cases-r2.log`）；受影响 247 基线
       `247 passed`（`../logs/baseline-affected-247-r2.log`）。
30. [x] QR1B：共享 manifest 规则加 `security_id`/`segment_id` ↔ profile 挂牌核对。
31. [x] QR2B：唯一 `_unmapped_run_scan_pairs` 助手，seal 与 prepare/supersede 同门。
32. [x] QR3B：`strict_json_loads` 增 `parse_float` 有限性（1e400 等嵌套/数组同拒）。
33. [x] QR4B：缺侧表的完整 write 直接拒绝；work_store ACK fixture 观察对齐 compact 八字段。
34. [x] GREEN：`remaining+镜像16 passed`、原 9 反例 `9 passed`、受影响 `247 passed`、
       改动模块其余消费方 `190 passed`、`--static-only` pass、pre-commit 精确清单 pass。
35. [x] 清理：两独占根删除，回执**含逐文件清单**（`../logs/cleanup-remediation-r2-receipt.json`）；
       第一轮回执缺逐文件清单如实声明 `not_reconstructable`；冻结 CRLF 5 SHA 复验未变。
36. [x] 集中审查（本轮 src+test diff 253 行复读）→ 代码/日志提交 `b6eaa08` → 交接与
       artifacts 更新后二次提交。

## Next Step

交总控按 remaining-repair 卡复验（原字节 7 例 + 受影响 247 + 本交接），再决定既有联合 12 组执行；
QA↔SW 联合节点与 F05 仍开放。
