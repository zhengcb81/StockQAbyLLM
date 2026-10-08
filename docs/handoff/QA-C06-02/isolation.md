# QA-C06-02 隔离与清理

## 临时根

| 用途 | 路径 | 归属 | 终态 |
|---|---|---|---|
| golden 导出 | `%TEMP%\qa-c06-02-golden-5k1050lz\` | 本包一次性创建 | **已删除**（`shutil.rmtree` 后复核目录不存在） |
| golden 导出脚本 | `%TEMP%\qa_c06_02_golden_export.py` | 本包一次性创建（不入仓） | **已删除** |
| pytest 用例临时根 | `%TEMP%\pytest-of-郑曾波\pytest-*\` | pytest 自管共享 TEMP | **未按编号清理**——共同规范明令“不按 mtime/pytest 编号/目录名清共享 TEMP”；pytest 自身按代数回收，本包只在用例内使用自己的 `tmp_path` |

用例内所有 DB / cache / logs / 子进程都在 pytest `tmp_path` 之下：
`quick_scan_work.sqlite`、`quick_scan_health.sqlite`、`llm_apis.json`、`manifest.json`、
`identity.json`、`quick_scan_c06_authority.json`、`questions.json`、`spend_authorization.json`、
`result.json`、`logs/`。测试根由 pytest 隔离，未落到仓内。

仓内既有 `quick_scan_work.sqlite`（`.gitignore` 精确例外 `/quick_scan_work.sqlite*`）是开工前
就存在的基线文件，本包**未写入、未删除**：所有用例都用 `monkeypatch.chdir(tmp_path)`，store 路径
是 `llm_config.config_file.parent / "quick_scan_work.sqlite"`，即临时根之下。

## 网络 / 收费 / 下载

| 项 | 实际次数 |
|---|---|
| 外部 HTTP 请求（模型） | **0**（HTTP 边界仅 `unittest.mock` 替换 `src.providers.llm_client.http_client_manager`） |
| 搜索调用 | **0**（`web_search_call` 由 stub 响应伪造，不发起网络） |
| 付费 API 调用 | **0** |
| 下载（财报/网页正文） | **0** |
| 环境 key | 未读取、未打印；用例内 `api_key` 为字面量 `offline-fixture-key` |
| `STOCKQA_RUN_LIVE_E22E` / live 开关 | `STOCKQA_RUN_LIVE_E2E=0`（`scripts/checks.py` 自带 OFFLINE_ENV） |

`python -B scripts/checks.py --full --timeout 300` 的 OFFLINE_ENV 同时注入离线守卫；
本包未申请、未使用任何在线授权（旧 B01 费用授权不复用）。

**监听端口**：0。用例不启动任何服务、线程或 server；只对 `requests` 会话做对象级 stub。
`tasklist` 可见的 python.exe 进程为编辑器语言服务/检查器常驻进程，非本包启动的测试进程；
pytest 进程在每次调用返回时已退出（exit 0）。

## 逐文件清理

| 路径 | 动作 |
|---|---|
| `%TEMP%\qa-c06-02-golden-*` | 删除（本包创建） |
| `%TEMP%\qa_c06_02_golden_export.py` | 删除（本包创建） |
| 仓内 `tests/`、`src/` 下无临时产物 | 无 |
| 本包交接目录 `docs/handoff/QA-C06-02/` | **保留**（交接物） |
| 九件 overlay 原字节 | 见下方“EOL 事件” |

## 过程异常（如实记录）

1. **pre-commit `mixed-line-ending` 改写了 6 个 overlay 输入的行尾。**
   `tests/fixtures/quick_scan_c06_manifest_v2_fixture.json`、
   `quick_scan_c06_authority_v2_fixture.json`、
   `src/config/quick_scan_observation.schema.json`、`quick_scan_answer_content.schema.json`、
   `quick_scan_metric_registry.json`、`pyproject.toml` 原为 CRLF，被 hook 就地转为 LF，
   导致 8 个用例立刻 RED（`manifest_file_sha256` 与文件字节不符）。
   **处置**：从 `inputs/qa-phase92-overlay/` 按原字节恢复前 5 个（SHA 分别回到
   `a7e80a5b…` / `e5bc782a…` / `6344d2af…` / `a83236c2…` / `790d0a82…`，与输入锁一致），
   复核 55 个相关用例全绿。`pyproject.toml` 由本包新增 mypy 覆写，内容本就变化，未回滚。
   **提交顺序**：`git add` → 第一次 `git commit`（hook 首轮把 5 个文件转 LF 后 fail）
   → 再次 `git add` → 第二次 `git commit`（hook 通过）→ **提交后按 overlay 原字节恢复工作树**。
   `core.autocrlf=true` 下索引存 LF、工作树恢复 CRLF，`git status` 归一化后为 clean。
   这正对应输入锁“Windows 工作树 SHA 与 Git blob SHA 不同口径；不能用 EOL 转换修复原输入”。
2. 早期一次 `black`（line-length 100，读 `pyproject.toml`）对 4 个文件重排；其中
   `src/utils/quick_scan_observation_context.py`、`tests/unit/test_quick_scan_observation_context.py`
   是九件 overlay 中本就不 black-clean 的两件。静态门要求 black 通过，故保留重排；
   与 overlay 原字节做过 AST 对比（`ast.dump` 等价），语义未变。
3. 未发生测试失败遗留：最终门 `result=pass`，失败用例 0。

## 未清理/未触碰

`.codegraph/`、`.workbuddy-ai/`、`nul`、`pilot_runs/b2a_2026-10-03/`、
`pilot_runs/g2b_alphabet_2026-10-04/`、`pilot_runs/l02_2026-10-04/`、`progress_update.txt`
——输入锁登记的 7 项旧未跟踪，未读内容、未删除、未提交。
其他仓（invest-quick-scan / StockWiki / company-wiki / Theme / Industry）只读，零写入。
