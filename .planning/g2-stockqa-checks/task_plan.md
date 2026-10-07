# Task Plan: G2-SQA-CHECKS — StockQA 工程入口与 workflow 统一

- PLAN_ID: `g2-stockqa-checks`
- PWF_PLAN_ROOT: `.planning`
- Lane: `G2-SQA-CHECKS`
- Repo root (worktree): `C:/Users/郑曾波/Projects/_g2/StockQAbyLLM`
- Branch: `codex/g2-stockqa-checks`
- Base commit: `6a9ff13864ebb160d5c4ab3cf2f42155d9f4aa99`
- Card: `docs/plans/narrative-evidence-pilot-2026-09-26/harness_lanes/g2_stockqa_engineering_checks.md`

## Goal

把 StockQAbyLLM 所有工程检查入口（CI workflow、本地 sh/bat、pre-commit hook、release 验证）
收敛到**唯一 Python 定义** `scripts/checks.py`，只保留真实失败（格式/类型/测试/来源/预算/恢复/密钥），
取消每 commit 全套与数字阈值门，sh/bat 只做转发，禁止 `|| true` / `--exit-zero` / `tee` 吞退出码。

## Non-goals / 禁止

- 不写 `src/**` 业务、不改模型/预算/身份/恢复规则
- 不动 `pilot_runs/`、`.codegraph/`、`.workbuddy-ai/`、`nul`、`progress_update.txt` 等 owner 资料
- 不合 master、不打 tag、不触发真实 release/PyPI/部署
- 不删真实业务反例；不为凑绿改业务用例
- 不联网调用付费 provider（默认与 --full 强制离线）

## Phases

### Phase 0 — 基线与只读勘察  Status: complete
- 建 worktree `_g2/StockQAbyLLM` + 分支 `codex/g2-stockqa-checks`
- 建 PWF 目录 `.planning/g2-stockqa-checks/`
- 实读 4 workflow、run_ci.sh/bat、.pre-commit-config.yaml、pyproject.toml、CONTRIBUTING、README、docs/development
- 入口/门禁矩阵与实测写入 `findings.md`

### Phase 1 — RED：编排反例先失败  Status: complete
- 新建 `tests/unit/test_ci_checks.py`、`tests/integration/test_ci_checks_cli.py`
- RED-1（入口不存在）：exit=1，`14 failed, 5 passed, 32 errors`，60.19s
- RED-2（占位“永不失败”实现 + 新薄 wrapper）：exit=1，`40 failed, 11 passed`，3.73s
  并据此加固 8 处原本会空过的断言（JSON 解析 / 真实 pytest 输出 / step 标记）

### Phase 2 — 实现 `scripts/checks.py`  Status: complete
- 四模式 default / --static-only / --full / --metrics --output-dir；`--only` / `--pytest-target` /
  `--timeout` / `--list-steps`（JSON 计划）
- `sys.executable -m ...`、真实退出码、超时 124、缺失 127、启动失败 126
- 强制离线（`-p no:base_url`、`-m "not live and not benchmark"`、双 `--ignore`、
  `STOCKQA_RUN_LIVE_E2E=0` 覆盖继承值）
- 默认 pytest 无 coverage/XML/HTML/reports；`--metrics` 写显式 output-dir
- 运行前后快照并恢复仓库根报告产物（含 symlink/reparse 保护）
- CLI smoke 在 TemporaryDirectory 子进程中导入真实模块，清理 scratch，不污染仓库根 logs/

### Phase 3 — wrapper 收敛  Status: complete
- `run_ci.sh`：`set -euo pipefail` + 定位仓根 + `exec python -B checks.py "$@"`
- `run_ci.bat`：ASCII 注释（cmd 代码页）+ 定位仓根 + `"%*"` + `endlocal & exit /b %G2_RC%`

### Phase 4 — workflow 同步  Status: complete
- `ci.yml`：单 job、单 Python、一次安装、调 `checks.py`；full/matrix/metrics 仅 workflow_dispatch
- `release.yml`：validate 委托 `--full`；test-release 去掉重复 `pytest tests/ -v`
- `security.yml`：仅 weekly + workflow_dispatch；`pip-audit -r requirements-lock.txt`
- `docs.yml`：按文档相关路径触发，权限/部署逻辑未动

### Phase 5 — hook 与配置  Status: complete
- `.pre-commit-config.yaml`：只留便宜静态（文件完整性 + black/isort/mypy + detect-secrets）；
  移除 always_run pytest、联网 pip-audit、bandit 报告、pylint 数字门；
  black 24.10.0→26.5.1、isort 6.0.1→7.0.0、mypy v1.14.1→v1.19.0
- `pyproject.toml`：去 `[tool.quality-gates]`；addopts 去 coverage 产物并加 `-p no:base_url`；dev 增 isort
- `requirements-lock.txt`：增 `isort==7.0.0`
- README / README_EN / CONTRIBUTING / docs/development 命令与文案同步

### Phase 6 — 集中验收  Status: complete
- A1 新编排用例 53 passed / 37.26s / exit 0
- A2 default 254 passed / 60.65s / exit 0
- A3 full 935 passed / 113.51s / exit 0
- A4 static-only 3.41s(cold 12.89s) / exit 0
- A5 metrics 935 passed / 157.92s / coverage 87% / pylint 9.31(诊断) / exit 0
- A6 ruff 新文件 0；A7 git diff --check 0；A8 继承 `STOCKQA_RUN_LIVE_E2E=1` 仍离线 exit 0
- A9 sh/bat 负例（未知 flag=2、未知 step=2、list-steps=0、仓外 cwd=0）
- A10 无 coverage/htmlcov/.coverage/reports/.pytest_cache 残留
- pre-commit（改动文件）全绿 PC_EXIT=0

### Phase 7 — 交付与冻结  Status: complete
- 普通 commit 到 `codex/g2-stockqa-checks`，push 同名分支
- `docs/implementation/g2-stockqa-checks/HANDOFF.md` + `handoff.json`
- 清理本轮 scratch/logs/cache，freeze 写集

## Next Step

写集已冻结。交付完成，无后续步骤；MAIN 按
`docs/implementation/g2-stockqa-checks/HANDOFF.md` + `handoff.json` 接入总 G2B。
交付 commit：`d989ea73bdcd2a3ac62f5ba80bc701bc2162082e`（代码）+
`d647be3`（handoff），分支 `codex/g2-stockqa-checks` 已 push。

## Decisions Made

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | worktree 独占于 `_g2/StockQAbyLLM` | 卡片要求，不动 owner root |
| 2 | 唯一 Python 定义在 `scripts/checks.py` | 消除 sh/bat/CI 三份复制逻辑 |
| 3 | pylint/radon/coverage 数值降为诊断 | 用户已要求取消数字阈值门，但保留真实失败 |
| 4 | 显式 `-p no:base_url`（pyproject addopts + checks.py） | 环境未声明插件与用例参数同名，属环境污染，不删用例 |
| 5 | mypy 收敛到 pyproject 单配置，移除散落 `--strict` | `mypy --strict` 在 master 上已红（53 errors） |
| 6 | ruff 不进入项目门禁 | `src/ tests/` 有 73 处既有问题，只能用于本卡新文件 |
| 7 | hook 移除 pylint 数字门与 bandit 报告 | “便宜静态 + 无报告生成”，bandit 由 checks.py 承担 |
| 8 | 测试 scratch 用 `<repo>/../_tmp/g2_checks_scratch` | OS `%TEMP%` 下 pytest 收集异常慢（12s vs 0.1s），且不污染仓库 |

## Errors Encountered

| Error | Attempt | Resolution |
|-------|---------|------------|
| `ScopeMismatch` base_url（14–18 errors） | 1 | 定位为未声明插件 `pytest-base-url`；pyproject addopts + checks.py 加 `-p no:base_url` |
| `mypy --strict` 53 errors | 1 | 弃用散落 `--strict`，以 pyproject 为准（真实通过） |
| `run_ci.bat` 报 `'定位仓根…REM' 不是内部或外部命令` | 1 | cmd 以本地代码页解析 UTF-8 中文注释 → bat 改纯 ASCII 注释 |
| `run_ci.bat` 找不到 `..\scripts\checks.py` | 1 | `REPO_ROOT` 与 `scripts` 之间漏了 `\` → 补上 |
| `TypeError: _run_tool_step() missing 1 required positional argument` | 1 | `STEP_RUNNERS` 改用 `functools.partial(_run_tool_step, STEP)` |
| 默认 pytest 仍产出 `coverage.xml/htmlcov/.coverage` | 1 | pyproject addopts 仍有 `--cov*` → 移除；restore 机制同时兜底并返回 1 |
| 诊断输出中文乱码（子进程 cp936 vs UTF-8 捕获） | 1 | `build_env()` 与测试 `_run()` 设 `PYTHONIOENCODING=utf-8` |
| `cmd //c scripts\run_ci.bat` 无引号路径被 bash 吃掉反斜杠 | 1 | 验证命令加引号（wrapper 本身无问题） |
| pre-commit `mixed line ending` 连续失败 | 3 | Edit 工具写入 LF 到 CRLF 文件 → 连跑 3 次 pre-commit 直至收敛全绿 |
