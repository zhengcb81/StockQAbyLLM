# Progress — G2-SQA-CHECKS

## Session 2026-10-07 (S1) — 完整执行

### 0. 基线

- owner root `StockQAbyLLM` @ `6a9ff13864ebb160d5c4ab3cf2f42155d9f4aa99`，起始 7 项 owner untracked
- worktree `C:/Users/郑曾波/Projects/_g2/StockQAbyLLM`，分支 `codex/g2-stockqa-checks`
- 环境：Python 3.13.9（`C:\Miniconda\python.exe`）

### 1. RED 记录（先建立真实失败）

| 轮次 | command | exit | count | seconds |
|---|---|---|---|---|
| RED-1 | `python -B -m pytest tests/unit/test_ci_checks.py tests/integration/test_ci_checks_cli.py -q --tb=line -p no:cacheprovider -p no:base_url -o addopts="" --basetemp ../_tmp/g2red/r1` | 1 | 14 failed, 5 passed, 32 errors | 60.19 |
| RED-2 | 同上（`--tb=no`），此时为“永不失败”占位实现 + 新薄 wrapper | 1 | 40 failed, 11 passed | 3.73 |

RED-2 的 11 个 pass 中：
- 3 个是业务节点存在性守卫（`test_daily_unit_files_exist` / `test_public_cli_nodes_exist` /
  `test_cross_process_budget_node_exists`），本就应通过；
- 8 个是**空过**的弱断言，据此加固：
  `--list-steps` 改为 `json.loads` 解析、pytest 步骤断言真实 `1 passed` / `3 passed`、
  smoke 断言 `step=smoke`、wrapper 断言 `mode==default`、收集失败断言 `test_broken` + `step=pytest`。

### 2. GREEN 记录（最终，pre-commit 收敛之后）

| 节点 | command | exit | count | seconds |
|---|---|---|---|---|
| A1 新编排用例 | `python -B -m pytest tests/unit/test_ci_checks.py tests/integration/test_ci_checks_cli.py -q --tb=short -p no:cacheprovider --basetemp <独占>` | 0 | 53 passed | 37.26 (wall 41) |
| A2 default | `python -B scripts/checks.py` | 0 | 254 passed in 43.14s | 60.65 (wall 60) |
| A3 full | `python -B scripts/checks.py --full` | 0 | 935 passed in 102.36s | 113.51 (wall 113) |
| A4 static-only（冷缓存） | `python -B scripts/checks.py --static-only` | 0 | black/isort/mypy/bandit | 12.89 |
| A4 static-only（热缓存） | 同上 | 0 | 同上 | 3.41 |
| A5 metrics | `python -B scripts/checks.py --metrics --output-dir <独占>` | 0 | 935 passed；coverage 87%；pylint 9.31/10（诊断）；radon 2 份报告 | 157.92 (wall 158) |
| A6 ruff（本卡新文件） | `python -B -m ruff check scripts/checks.py tests/unit/test_ci_checks.py tests/integration/test_ci_checks_cli.py` | 0 | All checks passed | <1 |
| A7 空白检查 | `git diff --cached --check` | 0 | 无空白错误 | <1 |
| A8 离线继承 | `STOCKQA_RUN_LIVE_E2E=1 python -B scripts/checks.py --only pytest --pytest-target tests/unit/test_search_service.py` | 0 | 3 passed | 2.78 |
| A10 残留 | `ls -d coverage.xml htmlcov .coverage reports .pytest_cache` | — | 无 | — |

### 3. wrapper 负例（真实非零证明）

| 命令 | exit |
|---|---|
| `bash scripts/run_ci.sh --no-such-flag` | 2 |
| `bash scripts/run_ci.sh --only no_such_step` | 2 |
| `bash scripts/run_ci.sh --list-steps` | 0 |
| `cmd /c scripts\run_ci.bat --no-such-flag` | 2 |
| `cmd /c scripts\run_ci.bat --only no_such_step` | 2 |
| `cmd /c scripts\run_ci.bat --list-steps` | 0 |
| `bash <repo>/scripts/run_ci.sh --list-steps`（仓外 cwd） | 0 |
| `cmd /c <repo>\scripts\run_ci.bat --list-steps`（仓外 cwd） | 0 |
| `--only pytest --pytest-target <收集失败文件>`（经 sh wrapper） | 非 0（pytest exit 2） |
| `--only bandit --timeout 0.001`（CLI 级超时） | 124 |

### 4. 真实 CLI 与预算/恢复证明

- CLI smoke（default 与 full 每次都跑）：输出 `stockqa-cli-smoke-ok`，
  在 TemporaryDirectory 子进程内 `import main`、`import main_with_llm`、
  `from src.runners.basic_runner import BasicRunner`、`import src.core.models`、
  `import src.config.{settings,config_manager,llm_config}`；仓库根 `logs/` 快照前后一致。
- 日常责任包含 Q07/预算/并发：
  `tests/unit/test_q07_checkpoint.py`、`tests/unit/test_quick_scan_budget.py`、
  `tests/unit/test_q09_budget_concurrency.py`。
- `--full` 含真跨进程节点：
  `tests/integration/test_quick_scan_budget.py::test_two_processes_share_budget_and_real_dispatch_concurrency_slots`。
- 6 个公开 CLI 精选节点全在日常包内（见 `findings.md` §5 与 `DAILY_TEST_TARGETS`）。

### 5. 日常责任包最终节点名单（16 项）

```text
tests/unit/test_models.py
tests/unit/test_config_manager.py
tests/unit/test_llm_config.py
tests/unit/test_basic_runner.py
tests/unit/test_main_with_llm.py
tests/unit/test_q07_checkpoint.py
tests/unit/test_quick_scan_budget.py
tests/unit/test_q09_budget_concurrency.py
tests/unit/test_ci_checks.py                        （新）
tests/integration/test_ci_checks_cli.py             （新）
tests/integration/test_quick_scan_cli.py::test_public_cli_exports_eight_score_entity_timestamp_model_and_search_receipt
tests/integration/test_quick_scan_cli.py::test_public_cli_refuses_to_infer_question_id
tests/integration/test_quick_scan_cli.py::test_public_cli_persists_every_failed_transport_retry_without_error_details
tests/integration/test_quick_scan_cli.py::test_public_cli_does_not_retry_uncertain_server_error_without_reconciliation
tests/integration/test_quick_scan_cli.py::test_public_cli_fake_health_schema_rejects_before_http
tests/integration/test_quick_scan_cli.py::test_public_cli_pauses_after_unpriced_attempt_without_sending_backup
```

### 6. pre-commit（改动文件）

`python -m pre_commit run --files <本卡改动文件>` → **PC_EXIT=0**，全 hook Passed。
（`mixed-line-ending` 前两轮自动修复了 Edit 工具写入的 CRLF，第三轮收敛。）
`pre-commit run --all-files` **NOT RUN**：会重排本卡写集之外的文件，避免越权改写。

### 7. 清理

- 删除：`_g2/_tmp/{g2base,g2red,g2green,g2_acceptance,g2_final,g2_metrics,g2_checks_scratch}`
- 删除 worktree 内本轮生成的 `logs/`、`coverage.xml`、`htmlcov/`、`.coverage`、`reports/`、`.pytest_cache/`、`__pycache__/`
- owner root 未被本卡写入（详见 HANDOFF `owner_root_untouched` 说明）

## Errors Encountered

| Error | Attempt | Resolution |
|---|---|---|
| `ScopeMismatch` base_url（14–18 errors） | 1 | 未声明插件 `pytest-base-url`；addopts + checks.py `-p no:base_url` |
| `mypy --strict` 53 errors | 1 | 弃用散落 `--strict`，以 pyproject 为准 |
| `run_ci.bat` 中文注释被 cmd 解析成命令 | 1 | bat 改纯 ASCII 注释 |
| `run_ci.bat` 路径 `..\scripts` 缺反斜杠 | 1 | 补 `\` |
| `_run_tool_step() missing 1 required positional argument` | 1 | `STEP_RUNNERS` 用 `functools.partial` |
| 默认 pytest 产出 coverage 产物 | 1 | 移除 pyproject addopts 的 `--cov*`；restore 兜底并返回 1 |
| 诊断中文乱码 | 1 | `PYTHONIOENCODING=utf-8`（checks 子进程 + 测试 `_run`） |
| 无引号 `scripts\run_ci.bat` 被 bash 吃掉 `\r` | 1 | 验证命令加引号（wrapper 无问题） |
| pre-commit `mixed line ending` ×2 | 3 | 连跑 pre-commit 直至全绿 |
| `%TEMP%` 下 pytest 收集 12s/文件 | 1 | 本卡 scratch 改 `<repo>/../_tmp/g2_checks_scratch` |
