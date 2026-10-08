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
