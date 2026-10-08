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

## 未做（保持 not_run）

真实 StockWiki 联合验收、G3、F05、owner golden、在线 live E2E、SW/Lab 两包。
