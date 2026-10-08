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
