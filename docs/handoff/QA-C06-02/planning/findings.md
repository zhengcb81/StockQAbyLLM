# findings — QA-C06-02

1. **RED 的确切形状**：`metadata` 里塞 `answer`/`execution`/`observed_at`/`observation_id`
   后重签 `observation_context_sha256`，`validate_context_document` 会通过——因为这 4 个键在
   Observation schema 的 `properties` 里合法，只是被从 `required` 摘掉，`additionalProperties:false`
   拦不住。修复是显式 `set(metadata) & absent` 检查。context 顶层与逐题级本就被集合校验拦住。
   复现：`pytest -p no:base_url tests/unit/test_quick_scan_observation_context.py -k embedded_answer -q`
   → RED `1 failed, 44 deselected`；GREEN `1 passed, 44 deselected`（45 collected，恰好对应卡上数字）。

2. **当前库版本是 5 不是 6**：卡里“设计映射里的 V6 是目标，不凭字符串假定当前库已到 V6”
   已核实——`SCHEMA_VERSION` 原为 5，本包升到 6。4 处 `PRAGMA user_version == 5` 硬断言与
   2 个“降级 fixture 未剥离 v5/v6 对象”的用例是唯一需要改的既有测试。

3. **`send_intent_at` 是 epoch float，`response_completed_at` 是 ISO 字符串**。封存时用
   `datetime.fromtimestamp(epoch, tz=utc).isoformat(timespec="seconds")` 派生 `started_at`
   ——派生自原值，不是封包时钟。合成 fixture 里两者必须自洽，否则 `end < start` 会被
   `build_observation` 拒绝；E2E 用真实 `_utc_now()`，单测用 `CLOCK=1_790_506_800.0`（2026-09-27T11:00Z）。

4. **`validate_standard_answer` 要求 `content['summary'] == normalized_answer['description']`**
   ⇒ 紧凑 checkpoint 的 `description` 必须是 summary，完整正文只能进侧表。这自然满足
   “超过旧 description 长度仍不截断正文”与“compact 只是摘要”。

5. **answer schema 的 status 枚举是 `scored/answered/insufficient_evidence/not_applicable/
   search_unavailable`，没有 `unknown`**；checkpoint 的枚举是 `scored/unknown/insufficient_evidence/
   not_applicable`。交集只有 3 个 → v2 完整封存只可能产出这 3 种；`unknown` / `search_unavailable`
   在 v2 下必然阻断（`_STATUS_MAP` 不含 → `MissingC06Fields`）。这是契约决定的边界，不是本包放宽。

6. **包不可变触发器的坑**：只比对 `NEW.package_sha256` 会放过“只改 `package_json` 一列”的改写
   （其余列仍是旧值且等于 MAX 修订）。必须八列全部等于 MAX 修订**且** `package_sha256` 与 OLD 不同。
   `test_result_delivery_package_and_event_ledger_are_database_immutable` 抓出了这个洞。

7. **ACK 对“全新的 ready 交付”允许任意 `store_id`**（首 ACK 胜出），因此“他人 store_id 负例”
   必须落在**已 delivered** 的行上（终态 ACK 不可改），或用跨包 ACK（`package_id` 不匹配）。
   卡上“ACK 全部键/hash/store 精确匹配才事务落定”指事务内的逐字段比对，不是首登記 store 白名单。

8. **`mixed-line-ending` 会吃掉 overlay 原字节**。9 件 overlay 中 6 件是 CRLF，pre-commit 转 LF 后
   `manifest_file_sha256` 立刻不匹配 → 8 个用例 RED。已按 `inputs/qa-phase92-overlay/` 原字节恢复
   （见 `../isolation.md` 异常 1）。这正印证输入锁“不能用 EOL 转换修复原输入”。

9. **harness 的独立 worktree 与本仓不同源**：harness 报告 HEAD `f937b2d`、
   `src/utils/quick_scan_observation_context.py` not found 等，本仓 `git cat-file -t f937b2d`
   为 “Not a valid object name”，HEAD 是 `09f68a6`。本包按卡以本仓为准交付；
   本仓 `git status` 与 harness 报告的 11 modified / 28 行一致，差异仅在其克隆的未同步副本。

10. **门**：`python -B scripts/checks.py --full --timeout 300` →
    `mode=full result=pass exit=0 steps=black,isort,mypy,bandit,pytest,smoke`，1037 passed。
    `pre-commit run --files …` 全部 hook 通过（mixed-line-ending 首轮修复后复跑通过）。

11. **组2 的正确形态是“映射”而不是“等值”**：冻结 context（owner 命名空间 `fixture-run/
    fixture-scan`）与实际 `work_run_ref`（`run-<ts>/scan-l02`）永远不相等——验收已实证。
    解法：带 context 的 `save_answer_checkpoint` 在与 attempt 同事务内把 context 的
    run/scan 对 `INSERT OR IGNORE` 进 `work_run_ref`（attach 只核不写），seal 前逐题核存在。
    foreign 例因此在 seal 被 `c06_run_scan_unbound` 持久阻断；正常例获得可核映射。
    未改任何 fixture 标签（`git status` 证明 fixtures 零改动）。
12. **`_checkpointed(with_context=False)` 原来传 body 会先炸**：`_prepare_standard_inputs`
    规定“完整正文必须带冻结 context”，所以原 helper 在 with_context=False+with_answer=True
    时于 setup 即 ValueError（总控 RED 日志即此）。helper 修正为 context/answer 成对
    （无 context 则不落侧表、留给 attach），产品规则不动——这不是改标签掩盖缺校验，缺的
    seal 校验照样新增并通过。
13. **Windows 本机 asyncio socketpair 是 `('::1', 0)` 不是 `('127.0.0.1', 0)`**：总控修正版
    guard 的放行条件在本机不命中，导致首轮子进程 RED 里网络账本误报。放行集合改为两个精确
    ephemeral 回环地址后正常；其余地址仍记账+拒绝。
14. **损坏正文的端到端落点是“honest-unknown + 持久 block”而不是“无 checkpoint”**：
    `llm_provider` 对无法解析的结构化回答落 `ParsedLLMAnswer(None, "无法验证…", "unknown")`
    （Q06 既有契约），checkpoint 记 score=null/unknown、无侧表，v2 封存
    `c06_standard_answer_unavailable` 持久阻断；每题恰 1 发（不重问）。loader 层的显式
    `ValueError` 由单测直接证明（`parse_standard_answer` 重复键/嵌套/NaN 三例）。
15. **组4 检查的边界=“自称完整 且 有侧表”**：无侧表旧库/fixture 的完整形状包（ACK/不可变
    等 6 个原用例）必须保持原能力 → 无侧表时沿历史 checkpoint 绑定；有侧表时重建比对。
    精确字段集 `_COMPACT_OBSERVATION_FIELDS` 区分 compact 与完整主张。
16. **日志按仓 hook 归一化是提交的必经步骤**：原始 pytest 输出含行尾空格/混合行尾，
    `trailing-whitespace` + `mixed-line-ending --fix=lf` 会就地改（无内容行增删）；IQS intake
    原始字节不碰。

17. **QR4B 的“完整主张”判别仍用 `_COMPACT_OBSERVATION_FIELDS` 二分**，没有另造第三套分类：
    非精确 compact 八字段 = 完整写入主张 → 必须有侧表+映射+原 attempt 才能落库。总控 6 个
    反例包都由真实 `build_complete_c06_package` 构造（必含主张字段）→ 被拒；work_store 的
    8 个既有 ACK/派发调用点用 `_c06_package_for_checkpoint`——其富观察是测试资产而非
    持久完整写入，故把该 fixture 对齐为历史 compact 形状（一处改动、8 调用点共享），
    断言语义（ACK/幂等/终态/不可变/证据负例）逐条不变，247 计数也不变。
18. **QR2B 与 QR1B 都刻意“共享一处规则”**：run/scan 抽 `_unmapped_run_scan_pairs`（work_store
    模块级，seal 与 store 双调用）；挂牌绑定进既有 `check_context_matches_manifest`
    （loader 与 bind_question_context 双走）——没有第二份实现可漂移。
19. **`json.loads` 的 `parse_float` 才是溢出口**：`1e400` 不触发 `parse_constant`（那只管
    NaN/Infinity 字面量），`float("1e400")` 静默得 `inf`；有限性检查必须放在 `parse_float`
    钩子里、共享 decoder 内，嵌套与数组自然覆盖。
20. **镜像测试放新文件而不是 247 选择器内**：总控按固定 9 文件路径跑 247，镜像若塞进这些
    文件会把计数改掉；新文件 `tests/integration/test_qa_c06_02_remaining_boundaries.py`
    使 247 保持可比，同时仓内保有同一回归。
21. **预建 `--basetemp` 会撞 guard 的 `\?\` 扩展路径**：pytest 清理已存在的 basetemp 用
    Windows 扩展前缀路径，`Path.resolve()` 后不再 `is_relative_to(OWNED)` → 8 例 setup 全挂。
    受影响选择器还必须 `-p no:base_url`（否则 pytest-base-url `ScopeMismatch` 4 错）。
    两处都是本轮 controller 自身问题，已记 isolation 6a，不计产品 RED。
22. **清理回执这次落逐文件清单**（`[relpath,bytes,sha256]` + 清单 SHA + CIM 0 + 删后不存在）；
    第一轮只有聚合 hash，按卡声明 `not_reconstructable`、不回填。共享 TEMP 现由并行 lane
    滚动占用（177+），147/148 已被 pytest 代数保留自行回收——本包两轮都只删自己证明归属的根。
