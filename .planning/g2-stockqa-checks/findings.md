# Findings — G2-SQA-CHECKS

信息日期：2026-10-07。全部为本 worktree `C:/Users/郑曾波/Projects/_g2/StockQAbyLLM` 实读/实测。

## 1. 基线

- base_commit `6a9ff13864ebb160d5c4ab3cf2f42155d9f4aa99`，分支 `codex/g2-stockqa-checks`
- owner root `StockQAbyLLM` 只读；7 项 untracked 保持不动
- 环境：Python 3.13.9（`C:\Miniconda\python.exe`）
  - black 26.5.1 / mypy 1.19.0 / pylint 4.0.4 (astroid 4.0.2) / isort 7.0.0
  - pytest 9.1.1 / pytest-cov 7.0.0 / pytest-asyncio 1.3.0 / pytest-html 4.1.1 / pytest-benchmark 5.2.3
  - bandit 1.9.3 / radon 6.0.1 / ruff 0.15.18 / mkdocs 1.6.1
  - **`pip-audit` 未安装**（requirements-lock.txt 声明 `pip_audit==2.10.0`）
  - **环境存在未声明插件 `pytest-base-url`**（pyproject dev extras 与 requirements-lock 均未声明）

## 2. 当前入口与门禁矩阵（改造前）

| 入口 | 现状问题 |
|---|---|
| `.github/workflows/ci.yml` | 9 runner / 8 次安装；matrix `3.10,3.11` 每 push 重复；`pylint --exit-zero` 却另写 `|| true` 虚报；`mypy --strict`（与 pyproject 配置不一致）；`--cov-fail-under=87` 数字门；build job 空 `import src` 未证明 CLI 可用；security-scan 每 push 重复 runner |
| `scripts/run_ci.sh` | `... 2>&1 \| tee reports/x` 使 `if` 判定 tee 退出码 → **吞掉真实 pytest/mypy/black 失败**；`pylint --fail-under=8.0 --exit-zero` 恒真；覆盖率门 60 与 CI 87 不一致；生成 `reports/` 副作用 |
| `scripts/run_ci.bat` | 另复制一套 7 步逻辑，门禁数字又不同；生成 `reports\` |
| `.pre-commit-config.yaml` | 每 commit `always_run` 全套 pytest（`tests/unit/`）、`always_run` **联网** pip-audit、bandit 生成 `bandit-report.json` 报告；pylint `--fail-under=9.0` 数字门；black rev `24.10.0` 与锁 `26.5.1` 不一致；mypy mirror `v1.14.1` 与实测 `1.19.0` 不一致 |
| `.github/workflows/release.yml` | 第二套标准：`--cov-fail-under=70`、`pylint --fail-under=8.0`、`mypy --strict`、`black --check` 各自复制 |
| `.github/workflows/security.yml` | 每 push/pull_request 重复 security runner；`pip-audit` **裸跑审计整个开发环境**而非项目依赖 |
| `.github/workflows/docs.yml` | 每次代码 push 都 build+deploy 文档 |
| `pyproject.toml` | `[tool.pytest.ini_options] addopts` 默认注入 `--cov --cov-report=xml --cov-report=html` → 任何裸 `pytest` 都产生 `coverage.xml`/`htmlcov`；`[tool.quality-gates]` 87/9.0/strict/15 数字门 |

## 3. 实测发现的真实问题

### 3.1 `pytest-base-url` 插件与用例参数同名 → ScopeMismatch ERROR（真实收集失败）

`tests/unit/test_llm_client.py:1249` 等用例把参数命名为 `base_url`
（`@pytest.mark.parametrize("base_url", ...)`），而环境里未声明的 `pytest-base-url`
插件注册了 **session-scoped** `base_url` fixture，触发：

```
ScopeMismatch: You tried to access the function scoped fixture base_url
with a session scoped request object.
```

实测：

| 命令 | 结果 |
|---|---|
| `pytest tests/unit -o addopts=""` | `783 passed, 14 errors` |
| `pytest tests/unit tests/integration -o addopts=""` | `864 passed, 18 errors` |
| 加 `-p no:base_url` | `882 passed`（0 error） |

`pytest-base-url` 不在 pyproject dev extras、不在 `requirements-lock.txt`，属环境污染；
既有 `.pre-commit-config.yaml` 已用 `-p no:base_url` 规避。**结论：统一入口显式 `-p no:base_url`**，
不删任何用例。

### 3.2 `mypy src/ --strict` 在 master 上已红

```
mypy src/ --strict      → exit 1，Found 53 errors in 8 files
mypy src/  (pyproject)  → exit 0，Success: no issues found in 47 source files
```

→ 收敛为 **pyproject 单份配置**（不散落额外 `--strict`），与卡 §3 一致。

### 3.3 其他工具基线（全部真实通过）

| 工具 | 命令 | exit | 结果 |
|---|---|---|---|
| black | `black --check src/ tests/` | 0 | 100 files unchanged，0.9s |
| isort | `isort --profile black --check-only src/ tests/` | 0 | 干净，1.1s |
| mypy | `mypy src/` | 0 | 47 files，24.5s |
| bandit | `bandit -r src/ -q -f screen` | 0 | 0 issues，1.7s |
| pylint | `pylint src/` | 28 (4+8+16) | 9.31/10（无 1/2/32 位）→ 数值诊断 |
| ruff | `ruff check scripts` | 1 | **3 处既有问题**：`scripts/insert_methods.py:5` F401；`scripts/validate_and_fix_skipped.py:98,100` F541（不在本卡写集） |
| ruff | `ruff check src/ tests/` | 1 | 73 errors → ruff 不能成为项目级门禁 |

### 3.4 CLI smoke 可行性

`src/utils/logger.py:17-18` 模块导入即 `LOG_DIR = Path("logs"); LOG_DIR.mkdir(exist_ok=True)`
（相对 cwd），`main_with_llm.py:33` 模块级 `logger = get_logger(__name__)`。

实测：在 `TemporaryDirectory` 作 cwd + `PYTHONPATH=<repo>` 下
`import main, main_with_llm; from src.runners.basic_runner import BasicRunner;
from src.core import models; from src.config import settings, config_manager, llm_config`
→ 成功，且 `logs/` 只落在 scratch（随后可删）。

### 3.5 测量数据（用作责任包选型，不作通过门）

| 集合 | 结果 | 耗时 |
|---|---|---|
| 8 个 unit 责任文件 | 195 passed | 8.78s |
| 6 个公开 CLI 节点 | 6 passed | 0.87s |
| `tests/unit` 全量（`-p no:base_url`） | 783 passed | 42.2s |
| `tests/unit tests/integration`（`-p no:base_url`） | 882 passed | 46.7s |
| `test_quick_scan_budget.py::test_two_processes_share_budget_and_real_dispatch_concurrency_slots` | 1 passed | 2.75s |

## 4. 改造方案要点

1. **唯一 Python 定义** `scripts/checks.py`；`run_ci.sh` / `run_ci.bat` 变薄转发。
2. 模式：`default` / `--static-only` / `--full` / `--metrics --output-dir`。
3. 真失败（非零）：black、isort、mypy、bandit、pytest、CLI smoke、工具缺失/启动失败/超时。
   诊断（不设数字门）：pylint 分数、radon、coverage。
4. pytest 固定 `-p no:base_url -m "not live and not benchmark" --ignore=tests/live
   --ignore=tests/benchmarks`，并把 `STOCKQA_RUN_LIVE_E2E` 置 `0`，强制离线。
5. 默认不写任何 report；`--metrics` 才把 coverage/pylint/radon 写进显式 `output-dir`，
   运行前后校验/恢复仓库根的报告残留。
6. hook 只留便宜静态（black/isort/mypy/文件完整性/detect-secrets），去掉 always_run pytest、
   联网 pip-audit、bandit 报告生成与 pylint 数字门。
7. security.yml 只保留 weekly + workflow_dispatch，pip-audit 改为 `-r requirements-lock.txt`。

## 5. 既有工程入口断言

`grep` 全 `tests/`：**没有任何用例断言 `.github/*`、`run_ci.*`、`[tool.quality-gates]`、
`addopts`、`cov-fail-under`**。因此本卡无需修改既有业务用例，只新增
`tests/unit/test_ci_checks.py` 与 `tests/integration/test_ci_checks_cli.py`。
