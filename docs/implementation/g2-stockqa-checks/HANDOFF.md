# G2-SQA-CHECKS 交接文档

- **Lane**: `G2-SQA-CHECKS`
- **卡片**: `docs/plans/narrative-evidence-pilot-2026-09-26/harness_lanes/g2_stockqa_engineering_checks.md`
- **仓库**: `https://github.com/zhengcb81/StockQAbyLLM.git`
- **独占 worktree**: `C:/Users/郑曾波/Projects/_g2/StockQAbyLLM`
- **分支**: `codex/g2-stockqa-checks`
- **base_commit**: `6a9ff13864ebb160d5c4ab3cf2f42155d9f4aa99`
- **delivery_commit(代码)**: `d989ea73bdcd2a3ac62f5ba80bc701bc2162082e`
- **信息日期**: 2026-10-07
- **机器可读版**: 同目录 `handoff.json`（`schema_version: g2-lane-handoff/1`）

---

## 1. 交付内容

### 1.1 唯一运行接口

```text
python -B scripts/checks.py                 # 默认短离线 CI
python -B scripts/checks.py --static-only   # commit，仅静态
python -B scripts/checks.py --full          # 大节点，全部离线 unit/integration
python -B scripts/checks.py --metrics --output-dir <本卡独占短目录>
python -B scripts/checks.py --list-steps    # 只打印计划 JSON，不执行
python -B scripts/checks.py --only <step> [--pytest-target PATH] [--timeout SECONDS]
```

步骤注册表：`black | isort | mypy | bandit | pytest | smoke | pylint | radon`

| 模式 | 步骤 | pytest 目标 |
|---|---|---|
| `--static-only` | black, isort, mypy, bandit | 无 |
| default | black, isort, mypy, bandit, pytest, smoke | 日常责任包 16 节点 |
| `--full` | 同上 | `tests/unit` + `tests/integration` |
| `--metrics` | 同上 + pylint, radon（另 pytest 带 coverage） | `tests/unit` + `tests/integration` |

**退出码语义（真实失败一律非零）**

| 情形 | 退出码 |
|---|---|
| 子命令/工具自身失败 | 原样上抛（如 pytest 1/2/4/5、black 1、mypy 1、bandit 1） |
| 子命令返回 7 | 7（编排不改写） |
| 超时 | 124 |
| 可执行/模块不存在 | 127 |
| 其它启动失败 | 126 |
| 参数不合法（未知 flag / 未知 step / step 不属于模式 / `--metrics` 缺 `--output-dir` 等） | 2 |
| 运行后仓库根出现新报告产物（已自动清理） | 1 |
| pylint 仅有 lint 消息（退出码位 2/4/8/16） | 0（诊断，打印 `diagnostic_pylint_score`） |
| pylint 致命/用法错误（位 1/32）、缺工具、超时 | 非 0 |

### 1.2 新增文件

- `scripts/checks.py`（877 行，唯一 Python 定义点）
- `tests/unit/test_ci_checks.py`（编排层反例，32 项）
- `tests/integration/test_ci_checks_cli.py`（进程级反例，21 项）
- `.planning/g2-stockqa-checks/{task_plan,findings,progress}.md`
- `docs/implementation/g2-stockqa-checks/{HANDOFF.md,handoff.json}`

### 1.3 改写文件

- `scripts/run_ci.sh`、`scripts/run_ci.bat` → 薄转发层
- `.github/workflows/{ci,security,release,docs}.yml`
- `.pre-commit-config.yaml`、`pyproject.toml`、`requirements-lock.txt`
- `README.md`、`README_EN.md`、`CONTRIBUTING.md`、`docs/development.md`

**未触碰**：`src/**` 业务、模型/预算/身份/恢复规则、任何 pilot/历史真实结果、
`.codegraph/`、`.workbuddy-ai/`、`nul`、`progress_update.txt`、owner 未跟踪的 `pilot_runs/*`。

---

## 2. RED → GREEN

### 2.1 RED

| 轮 | 前置状态 | command | exit | count | seconds |
|---|---|---|---|---|---|
| RED-1 | 统一入口不存在 | `python -B -m pytest tests/unit/test_ci_checks.py tests/integration/test_ci_checks_cli.py -q --tb=line -p no:cacheprovider -p no:base_url -o addopts="" --basetemp <独占>` | 1 | 14 failed, 5 passed, 32 errors | 60.19 |
| RED-2 | 占位实现“永不失败”+ 新薄 wrapper | 同上（`--tb=no`） | 1 | 40 failed, 11 passed | 3.73 |

RED-2 的 11 个 pass 拆解：3 个为业务节点存在性守卫（本应通过），8 个为**空过弱断言**，
据此加固为 JSON 解析、真实 pytest 计数输出、`step=` 标记、`mode==default`、
收集失败输出含 `test_broken` + `step=pytest`。

### 2.2 GREEN（pre-commit 收敛后的最终实测）

| 节点 | command | exit | count | seconds |
|---|---|---|---|---|
| A1 编排反例 | `python -B -m pytest tests/unit/test_ci_checks.py tests/integration/test_ci_checks_cli.py -q --tb=short -p no:cacheprovider --basetemp <独占>` | 0 | 53 passed | 37.26（墙钟 41） |
| A2 default | `python -B scripts/checks.py` | 0 | 254 passed in 43.14s | 60.65（墙钟 60） |
| A3 full | `python -B scripts/checks.py --full` | 0 | 935 passed in 102.36s | 113.51（墙钟 113） |
| A4 static-only | `python -B scripts/checks.py --static-only` | 0 | black/isort/mypy/bandit | 12.89（冷）/ 3.41（热） |
| A5 metrics | `python -B scripts/checks.py --metrics --output-dir <独占>` | 0 | 935 passed；coverage **87%**；pylint **9.31/10（诊断）**；radon 2 份报告 | 157.92（墙钟 158） |
| A6 ruff（本卡新文件） | `python -B -m ruff check scripts/checks.py tests/unit/test_ci_checks.py tests/integration/test_ci_checks_cli.py` | 0 | All checks passed | <1 |
| A7 空白 | `git diff --cached --check` | 0 | 无空白错误 | <1 |
| A8 离线继承 | `STOCKQA_RUN_LIVE_E2E=1 python -B scripts/checks.py --only pytest --pytest-target tests/unit/test_search_service.py` | 0 | 3 passed | 2.78 |
| A9 wrapper 负例 | 见 §3 | 见 §3 | — | — |
| A10 残留 | `ls -d coverage.xml htmlcov .coverage reports .pytest_cache` | — | **无** | — |
| pre-commit | `python -m pre_commit run --files <本卡改动文件>` | **0** | 全 hook Passed | — |

> 实测时长与用例计数仅作事实记录，未设任何固定数量/秒数通过门。

---

## 3. wrapper 负例（参数转发与非零透传）

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
| `bash run_ci.sh --only pytest --pytest-target <收集失败文件>` | 非 0（pytest exit 2） |
| `python checks.py --only bandit --timeout 0.001` | 124 |

空格/中文路径已覆盖：仓外 cwd `含 空格`、`--pytest-target <scratch>/含 空格/test_ok.py`、
`--output-dir <含 空格>/指标输出`。

---

## 4. 真实 CLI 与预算/恢复证明

- **CLI smoke**（default / full / metrics 每次执行）：子进程在 `TemporaryDirectory`
  中以仓根为 `PYTHONPATH` 执行
  `import main`、`import main_with_llm`、`from src.runners.basic_runner import BasicRunner`、
  `import src.core.models`、`import src.config.settings/config_manager/llm_config`，
  输出 `stockqa-cli-smoke-ok`；导入日志 `logs/` 只落在 scratch 并被清理，
  仓库根 `logs/` 快照前后完全一致（导入失败路径同样清理）。
- **预算/恢复（日常包）**：`tests/unit/test_q07_checkpoint.py`、
  `tests/unit/test_quick_scan_budget.py`、`tests/unit/test_q09_budget_concurrency.py`。
- **跨进程预算（仅 `--full` / `--metrics`）**：
  `tests/integration/test_quick_scan_budget.py::test_two_processes_share_budget_and_real_dispatch_concurrency_slots`。
- **6 个公开 CLI 精选节点**全部在日常包内（名单见 §5）。
- **未删除任何真实业务反例**：`test_daily_unit_files_exist`、`test_public_cli_nodes_exist`、
  `test_cross_process_budget_node_exists` 三个守卫用例保证节点存在性。

## 5. 日常责任包最终节点名单（16）

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

---

## 6. tool_versions

| 工具 | 版本 |
|---|---|
| Python | 3.13.9（`C:\Miniconda\python.exe`） |
| black | 26.5.1（锁 & pre-commit rev 已同步） |
| isort | 7.0.0（本次加入 dev extras + 锁 + pre-commit rev） |
| mypy | 1.19.0（pre-commit mirror `v1.19.0`） |
| pylint | 4.0.4（astroid 4.0.2） |
| bandit | 1.9.3 |
| radon | 6.0.1 |
| pytest | 9.1.1（pytest-cov 7.0.0、pytest-asyncio 1.3.0、pytest-benchmark 5.2.3、pytest-html 4.1.1） |
| pre-commit | 4.5.1 |
| ruff（仅验收用，非项目门禁） | 0.15.18 |
| pip-audit | **本机未安装**；锁钉 `2.10.0`，由 `security.yml` 自装 |

---

## 7. workflow 触发前后

| 文件 | 之前 | 之后 |
|---|---|---|
| `ci.yml` | push/PR 每次：matrix(3.10,3.11) 测试 + mypy --strict + pylint --exit-zero + black + bandit + radon + build(`import src`) + report，共 9 runner / 8 次安装；`--cov-fail-under=87` | push/PR：**单 job、单 Python(3.10)、一次安装**，调 `python -B scripts/checks.py`；`full` / Python 兼容矩阵 / `metrics` 仅由 `workflow_dispatch` 输入选择；codecov 与报告上传只在 `metrics` |
| `security.yml` | push + pull_request + 每周 cron + dispatch；裸 `pip-audit`（审计整个环境） | **仅每周 cron + workflow_dispatch**；`pip-audit -r requirements-lock.txt`（审计项目依赖）；bandit/密钥离线反例与报告上传保留 |
| `release.yml` | validate 自带第二套标准：`pytest --cov-fail-under=70`、`mypy --strict`、`pylint --fail-under=8.0`、black、bandit；test-release 再跑 `pytest tests/ -v` | validate 委托 `python -B scripts/checks.py --full`；test-release 保留 wheel 安装与真实 CLI 模块导入，去掉重复全套 pytest；build / create-release / publish-pypi 的步骤与权限保持原样 |
| `docs.yml` | 每次 push/PR 到 main/master 都 build + deploy | 仅 docs 相关路径（`docs/**`、`mkdocs.yml`、README、CONTRIBUTING、CHANGELOG、`pyproject.toml`、workflow 自身）触发；发布权限与 `peaceiris/actions-gh-pages` 逻辑未改 |
| `.pre-commit-config.yaml` | black 24.10.0 / isort 6.0.1 / mypy v1.14.1 + 每 commit `always_run` pytest 全套 + `always_run` 联网 pip-audit + bandit 写 `bandit-report.json` + pylint `--fail-under=9.0` | black 26.5.1 / isort 7.0.0 / mypy v1.19.0；保留文件完整性检查、格式、类型、`detect-secrets`；**移除** pytest、pip-audit、bandit 报告、pylint 数字门 |

本卡**未打 tag、未触发任何真实 release / PyPI / Pages 部署**。

---

## 8. 清理前后

**清理前**（本卡本轮生成）

- worktree：`logs/stock_qa_20261007.log`、`coverage.xml`、`htmlcov/`（早期基线诊断产生）、
  `src/**/__pycache__`、`.pytest_cache`
- 独占临时根 `C:/Users/郑曾波/Projects/_g2/_tmp/`：
  `g2base`、`g2red`、`g2green`、`g2_acceptance`、`g2_final`、`g2_metrics`、`g2_checks_scratch`、`probe`

**清理后**

- worktree `git status` 干净（无报告、无 cache、无 logs 残留）
- `_tmp/` 整目录已删除（其内容全部为本卡生成，删除前逐项核对前缀）
- owner root `StockQAbyLLM/logs/` 未被触碰（目录 mtime `2026-10-07 01:33`，早于本卡 09:41 启动）

**删除安全**：所有删除路径先做绝对路径前缀校验并排除 owner root，
目录删除前检查 `st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT`，
未做递归 owner 树清理。

> 记录一次自查失误：清理时误删了 master 中已跟踪的
> `pilot_runs/b01b_method_2026-10-07/__pycache__/runner.cpython-313.pyc`，
> 当即 `git checkout --` 恢复，`git status` 复核为干净。

---

## 9. 保护与计数

| 项 | 值 |
|---|---|
| `external_provider_calls` | **0** |
| `paid_calls` | **0** |
| `production_writes` | **0** |
| `raw_deleted` | **0** |
| `owner_root_untouched`（本卡写入次数） | **true / 0** |

**owner root 并发观察（非本卡造成）**：本卡执行期间，另一并发通道在 owner root
改动了 `main_with_llm.py`、`src/runners/llm_runner.py`、`src/utils/quick_scan_work_store.py`、
`.gitignore` 并新增多个 `src/**`、`tests/**` 未跟踪文件（mtime 10:49–11:10）。
本卡全程只在 `_g2/StockQAbyLLM` 施工，未对 owner root 执行任何写操作。

---

## 10. remaining_issues

1. `python -B -m ruff check scripts/` 有 **3 处既有问题**（`scripts/insert_methods.py:5` F401、
   `scripts/validate_and_fix_skipped.py:98,100` F541×2），不在本卡写集内，未修；
   ruff 也不在项目门禁里。本卡新文件 ruff 为 0。
2. `python -B -m ruff check src/ tests/` 有 **73 处既有问题** → ruff 不接入 `checks.py`，
   需另立卡片并先做业务侧清理。
3. 本机环境存在**未声明**插件 `pytest-base-url`（不在 dev extras、不在锁），
   与用例参数 `base_url` 同名触发 ScopeMismatch。已用
   `pyproject.toml` `addopts = ["-p","no:base_url"]` + `checks.py` 显式 `-p no:base_url`
   阻断；即使插件不存在该参数也无害（实测 exit 0）。长期解法是清掉环境污染或为其立卡。
4. `pip-audit` 本机未安装，本地无法验证 `security.yml` 的审计步骤；
   该步骤保持既有 `continue-on-error: true` 的“周报”语义（本卡只改触发时机与审计范围），
   MAIN 合入后建议在首次手动 dispatch 时确认。
5. `pre-commit run --all-files` **NOT RUN**（会重排本卡写集之外的文件，避免越权）；
   已用 `pre-commit run --files <本卡改动文件>` 验证，PC_EXIT=0。
6. `ci.yml` 的 Python 兼容矩阵（3.11/3.12/3.13）**NOT RUN**——仅 `workflow_dispatch` 显式触发，
   本卡不触发任何真实 workflow。
7. `--metrics` 的 coverage 实测 87%（历史值），**不作为通过门**，仅诊断。

## 11. merge_notes

- MAIN 合入时请**保留** `scripts/checks.py` ↔ `run_ci.sh`/`run_ci.bat` 的转发契约，
  不要再往 wrapper 里复制检查逻辑，也不要恢复 `tee`/`--exit-zero`/`|| true`。
- 合入后请核对真实 CI：日常 push 应只有 **1 个 job / 1 次安装 / 1 个 Python**；
  `full`、兼容矩阵、`metrics` 只能由 `workflow_dispatch` 触发。
- `pyproject.toml` 变更要点：删除 `[tool.quality-gates]`、删除默认 `--cov*` addopts、
  新增 `-p no:base_url`、dev extras 新增 `isort`；`requirements-lock.txt` 新增 `isort==7.0.0`。
- release 验证已改为 `python -B scripts/checks.py --full`，**第二套 70%/8.0 标准已移除**，
  不要再补回来。
- 本卡只交付**工程检查**。工程检查全绿 **不等于** 投资业务完成，也 **不等于** 整个 CWP 计划完成。
- 本分支未合 master、未打 tag、未发布。
