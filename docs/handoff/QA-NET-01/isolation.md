# QA-NET-01 隔离与清理

## 1. 前后状态摘要

| 项 | 开工（= inputs.lock 观察值） | 交付时 |
|---|---|---|
| 仓库 | `C:/Users/郑曾波/Projects/StockQAbyLLM` | 同 |
| 分支 / HEAD | `master` @ `6a9ff13864ebb160d5c4ab3cf2f42155d9f4aa99` | 同（**未提交**，见 open item） |
| 工作树 | dirty，7 个未跟踪项 | dirty，27 条状态行（4 个既有跟踪文件被本包修改 + 原 7 个未跟踪项**原样保留** + 本包新增未跟踪文件） |
| manifest_sha256 | `dc0b3cad08c2a1b6bc17c5667e708e3f913e707d1c165b7471d53638255cba32` | 见 `handoff.json` 的 `worktree_after`（算法：`sha256("\n".join(porcelain_v1_lines) + "\n")`，已与 lock 值复算一致） |
| 其他 3 个仓 | StockWiki / Theme / Industry 只读 | **未打开写入** |
| IQS | 只读 | 只读；仅在一次 `question_sets.py compose` 中运行，`--out-dir` 指向系统 TEMP |

既有 7 个未跟踪项逐一核对仍在：`.codegraph/`、`.workbuddy-ai/`、`nul`、`pilot_runs/b2a_2026-10-03/`、
`pilot_runs/g2b_alphabet_2026-10-04/`、`pilot_runs/l02_2026-10-04/`、`progress_update.txt`。
**未检查其内容、未清理、未提交。**

## 2. 临时根 / DB / 缓存 / 端口

| 类别 | 实际情况 |
|---|---|
| 测试临时根 | pytest 默认 basetemp `%TEMP%\pytest-of-郑曾波\pytest-NNN`（每个测试用例独立 `tmp_path`，含自己的 `quick_scan_work.sqlite`、题文件、manifest、权威文档） |
| 本包清理 | 会话时间窗（mtime < 180 min）内的 22 个 `pytest-NNN` 根经 `shutil.rmtree` 删除（两轮：18 + 4，另见“收尾补充”3 个，合计 25）；另删除本包创建的 `%TEMP%\qs_compose_probe`、`%TEMP%\blackprobe`、`%TEMP%\dbg*.py` |
| 清理中止（如实记录） | 对 `pytest-921` 的首次删除被系统拒绝（`PermissionError ... \publication-registry0\publications.jsonl`，该内容**不是本包产物**）→ 判定为其他进程共享状态，**当次立即停止删除并原样保留**；该根随后由 pytest 自身的 basetemp 轮转在其后的会话启动时移除（非本包清理脚本所为）。一次重试删除 `pytest-936` 时首次同样报锁、重试成功 |
| 归属方法 | 仅按“名称为 pytest 编号根 + mtime 落在本包会话窗口”判定归属；共享 TEMP 下无法可靠归属的目录（如有）不主动搜索、不递归清理 |
| 收尾补充 | 交付后最后一次定向测试又产生 3 个根（`pytest-15`/`-16`/`-17`，编号序列与先前不同），按同一时间窗规则于会话结束时删除；删除未遇锁，但**归属同样只凭时间窗判断，无法进一步核实**，如实记录于此 |
| 仓内数据库 | 测试只使用 `tmp_path` 下的 SQLite；仓根 `quick_scan_work.sqlite` / `quick_scan_health.sqlite` **未被本包测试创建或修改**（测试均 `monkeypatch.chdir(tmp_path)`） |
| 缓存 | `coverage.xml` / `coverage.json` / `htmlcov/` 由 pytest-cov 重写（均在 `.gitignore` 中，非交付物） |
| 仓内日志 | `logs/stock_qa_20261007.log`（`.gitignore` 的 `logs/` 规则内）被本包测试运行**追加**写入；为避免丢失同日其他进程日志，**未删除** |
| 端口 | 0（未启动任何服务/浏览器/worker 进程） |
| 进程 | 测试进程随 pytest 会话结束；未派生长期子进程 |

## 3. 网络 / 搜索 / 模型 / 付费次数

| 计数 | 值 | 依据 |
|---|---|---|
| 出站 HTTP 请求 | **0** | 公开 CLI E2E 在 `src.providers.llm_client.http_client_manager` 边界注入 stub；`session.post.call_count` 被逐用例断言（冷跑 2、warm 0、拒绝路径 0） |
| 真实搜索调用 | **0** | 未配置任何已准入外部 route；`external_dispatch_enabled=false` 时入口在 HTTP 前失败关闭 |
| MCP 握手 / tools/list | **0** | 未发起；parser 只做离线解析 |
| 模型（LLM）调用 | **0（真实）** | 所有“模型调用”均发生在 stub 的 HTTP 边界内 |
| 付费调用 | **0** | `live=off`；`--spend-authorization` 使用固定 fixture 快照 |
| 官方文档在线核查 | **0** | 本包未做在线核查，全部依据 inputs.lock 冻结的 IQS 只读工件 |

`handoff.json.verification` 中 `external_writes=false`、`network_calls=false`、`paid_calls=false` 与上表一致。

## 4. 清理证明与异常残留

* 交付日志先保存到 `docs/handoff/QA-NET-01/logs/`（10 个原始文件），再执行临时根清理。
* 删除前逐项确认：路径位于系统 TEMP、名称为 pytest 编号根或本包具名目录、mtime 落在本包会话窗口。
* **异常残留（明示）**：
  1. `%TEMP%\pytest-of-郑曾波\pytest-921` —— 本包删除时被其他进程锁定、含非本包产物，**当次未删除并立即停止清理**；后续由 pytest 自身 basetemp 轮转移除。交付时 `%TEMP%\pytest-of-郑曾波` 已无 `pytest-*` 子目录（下次 pytest 会话会按其保留策略重新创建）；
  2. `logs/stock_qa_20261007.log` —— 仓内共享日志，仅追加，未删除；
  3. `coverage.xml` / `coverage.json` / `htmlcov/` —— gitignored 的测试覆盖率副产物，重写但未清理；
  4. `git status` 中 7 个既有未跟踪项 —— 按指令原样保留。
* API 费用为 0，不存在“通过删文件回滚费用”的情形。

## 5. 未越界的写入清单

本包写入仅限：`main_with_llm.py`、`src/runners/llm_runner.py`、`src/utils/quick_scan_work_store.py`、`.gitignore`
（以上为既有跟踪文件的修改），以及 `src/config/`、`src/providers/`、`src/utils/`、`examples/`、`tests/`、
`docs/handoff/QA-NET-01/` 下的新文件（逐条见 `artifacts.json`）。
`out_of_scope_writes=[]`：未写 IQS 中央 PWF、其他包代码、生产 DB、用户配置/密钥文件、安装镜像。
