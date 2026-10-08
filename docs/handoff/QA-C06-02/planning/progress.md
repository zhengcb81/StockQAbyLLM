# progress — QA-C06-02

| # | 项 | 结果 |
|---|---|---|
| 0 | 读卡/规范/接口/输入锁/映射快照；核基线 16 条 status、9 件 overlay SHA | 完成（与 inputs.lock 一致） |
| 1 | RED `-k embedded_answer`（1 failed / 44 deselected），存日志 | 完成 → `../logs/red-embedded-answer.log` |
| 2 | 消费入口拒绝 4 个 detached 字段；纯 metadata v2 仍过 | 完成 → GREEN `1 passed / 44 deselected` → `../logs/green-embedded-answer.log` |
| 3 | authority v1/v2 loader + 新 v2 schema 工件；未知版本 fail-closed | 完成 |
| 4 | work_store v5→v6（侧表 + 修订链 + 迁移 + 打开期校验 + 补包入口） | 完成 |
| 5 | adapter `build_complete_c06_package`（v1 紧凑不变） | 完成 |
| 6 | seal v2 分支：阻断码 / 补包 LLM0 / supersede / 历史只读 | 完成 |
| 7 | runner + provider 显式标准答案传输与 HTTP 前绑定校验 | 完成 |
| 8 | 新增 10 个用例（7 unit + 3 integration），扩 1 个既有用例，修 2 个迁移 fixture | 完成 |
| 9 | 集中门 `checks.py --full --timeout 300` → pass，1037 passed | 完成 → `../logs/full-gate-GREEN.log` |
| 10 | golden 导出（synthetic_only）+ 输入 SHA | 完成 → `../golden/` |
| 11 | 交接：summary / interfaces / case-map / isolation / compatibility-matrix / planning | 完成 |
| 12 | EOL 事件处置：按 overlay 原字节恢复 5 个输入并复核 | 完成（见 `../isolation.md`） |
| 13 | 提交（代码一次 + 交接证据一次）与 `artifacts.json` / `handoff.json` | 见 `../handoff.json` |
| 14 | 真实 StockWiki 联合验收 / G3 / F05 | **not_run**（总控） |

失败用例：0。网络/付费/下载：0。临时根：本包自有路径已删，pytest 共享 TEMP 未按编号清理。
| 15 | 整改批次2 RED：原字节 9 例（独占副本 HEAD 字节）7 failed/2 passed | 完成 → `../logs/red-boundary-remediation.log` |
| 16 | 新单测 RED 31 failed/63 passed；子进程 4 例 RED | 完成 → `../logs/red-unit-remediation.log`、`../logs/red-subprocess-remediation.log` |
| 17 | 组1 共享 manifest 规则 + loader 严格 JSON 委托 | 完成 |
| 18 | 组2 `work_run_ref` 映射同事务持久化 + seal 核验（`c06_run_scan_unbound`） | 完成 |
| 19 | 组3 `strict_json_loads`（复用 parser hook + 拒非有限）+ 正文入口显式拒绝 | 完成 |
| 20 | 组4 prepare/supersede 完整观察重建比对（失败零半修订） | 完成 |
| 21 | 子进程 E2E：cold(仅 HTTP stub)/撤 stub warm/seal、错 metadata、重复 authority、损坏正文 | 完成 |
| 22 | GREEN：原字节 9 例 9 passed（含 v5 迁移回归） | 完成 → `../logs/green-boundary-remediation.log` |
| 23 | 一批验证：全量门 1083 passed + pre-commit 交付清单全 pass | 完成 → `../logs/full-gate-remediation-2026-10-08.log`、`../logs/pre-commit-remediation-2026-10-08.log` |
| 24 | 集中审查：源码全量 diff 复读（552 行 src diff）+ 交接全套更新 | 完成 |
| 25 | 清理：独占副本×2 + pytest-149 删除（SHA/CIM 回执），147/148 列出不删 | 完成 → `../logs/cleanup-remediation-receipt.json` |
| 26 | 提交：代码/测试/日志 `acb7dbf` → artifacts/handoff 二次提交 | 完成（见 `../handoff.json`） |
| 27 | 总控复验 / QA↔SW 联合 / G3 / F05 / owner golden / 线上 live | **not_run**（总控） |

整改批次失败用例：0（GREEN 后）。网络/付费/下载：0（子进程 guard 账本不存在）。
