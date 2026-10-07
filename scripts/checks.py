#!/usr/bin/env python3
"""StockQAbyLLM 统一工程检查入口（唯一 Python 定义点）。

用法::

    python -B scripts/checks.py                 # 默认短离线 CI
    python -B scripts/checks.py --static-only   # commit，仅静态
    python -B scripts/checks.py --full          # 大节点，全部离线 unit/integration
    python -B scripts/checks.py --metrics --output-dir <目录>
    python -B scripts/checks.py --list-steps    # 只打印计划（JSON），不执行

设计约束（卡 G2-SQA-CHECKS）：

* 所有步骤用 ``sys.executable -m ...`` 启动，真实退出码原样上抛；
* 不使用 ``|| true`` / ``--exit-zero`` / ``tee`` 吞掉真实检查错误；
* pylint / radon / coverage 只输出诊断，不设数字通过门；
* 默认与 ``--full`` 强制离线：排除 ``tests/live``、``tests/benchmarks``，
  并把 ``STOCKQA_RUN_LIVE_E2E`` 置 ``0``，即使外部环境继承 ``=1`` 也不生效；
* 默认不产生 coverage / XML / HTML / reports 产物，``--metrics`` 才写入显式
  ``--output-dir``；任何模式结束后都把仓库根恢复到运行前状态。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 子进程超时后的退出码（约定为 124，不会与任何真实工具退出码冲突到可混淆）。
TIMEOUT_EXIT_CODE = 124
#: 可执行文件 / 模块不存在。
NOT_FOUND_EXIT_CODE = 127
#: 其它启动失败。
SPAWN_ERROR_EXIT_CODE = 126

MODE_STATIC = "static-only"
MODE_DEFAULT = "default"
MODE_FULL = "full"
MODE_METRICS = "metrics"

STEP_BLACK = "black"
STEP_ISORT = "isort"
STEP_MYPY = "mypy"
STEP_BANDIT = "bandit"
STEP_PYTEST = "pytest"
STEP_SMOKE = "smoke"
STEP_PYLINT = "pylint"
STEP_RADON = "radon"

STEP_ORDER: Tuple[str, ...] = (
    STEP_BLACK,
    STEP_ISORT,
    STEP_MYPY,
    STEP_BANDIT,
    STEP_PYTEST,
    STEP_SMOKE,
    STEP_PYLINT,
    STEP_RADON,
)

_STATIC_STEPS = (STEP_BLACK, STEP_ISORT, STEP_MYPY, STEP_BANDIT)
_TESTING_STEPS = _STATIC_STEPS + (STEP_PYTEST, STEP_SMOKE)

MODE_STEPS: Mapping[str, Tuple[str, ...]] = {
    MODE_STATIC: _STATIC_STEPS,
    MODE_DEFAULT: _TESTING_STEPS,
    MODE_FULL: _TESTING_STEPS,
    MODE_METRICS: _TESTING_STEPS + (STEP_PYLINT, STEP_RADON),
}

#: 每步默认超时（秒）；``--timeout`` 会整体覆盖。
STEP_TIMEOUTS: Mapping[str, float] = {
    STEP_BLACK: 300.0,
    STEP_ISORT: 300.0,
    STEP_MYPY: 600.0,
    STEP_BANDIT: 600.0,
    STEP_PYTEST: 1800.0,
    STEP_SMOKE: 300.0,
    STEP_PYLINT: 900.0,
    STEP_RADON: 600.0,
}

# --------------------------------------------------------------------------------------
# 测试责任包
# --------------------------------------------------------------------------------------

#: 日常责任包（离线、短）：当前真实业务文件 + 本卡编排反例 + 公开 CLI 精选节点。
DAILY_TEST_TARGETS: Tuple[str, ...] = (
    "tests/unit/test_models.py",
    "tests/unit/test_config_manager.py",
    "tests/unit/test_llm_config.py",
    "tests/unit/test_basic_runner.py",
    "tests/unit/test_main_with_llm.py",
    "tests/unit/test_q07_checkpoint.py",
    "tests/unit/test_quick_scan_budget.py",
    "tests/unit/test_q09_budget_concurrency.py",
    "tests/unit/test_ci_checks.py",
    "tests/integration/test_ci_checks_cli.py",
    "tests/integration/test_quick_scan_cli.py"
    "::test_public_cli_exports_eight_score_entity_timestamp_model_and_search_receipt",
    "tests/integration/test_quick_scan_cli.py::test_public_cli_refuses_to_infer_question_id",
    "tests/integration/test_quick_scan_cli.py"
    "::test_public_cli_persists_every_failed_transport_retry_without_error_details",
    "tests/integration/test_quick_scan_cli.py"
    "::test_public_cli_does_not_retry_uncertain_server_error_without_reconciliation",
    "tests/integration/test_quick_scan_cli.py::test_public_cli_fake_health_schema_rejects_before_http",
    "tests/integration/test_quick_scan_cli.py"
    "::test_public_cli_pauses_after_unpriced_attempt_without_sending_backup",
)

#: 大节点：全部离线 unit/integration。
FULL_TEST_TARGETS: Tuple[str, ...] = ("tests/unit", "tests/integration")

#: 永远排除（可能调用真实收费 provider 或做性能基准）。
EXCLUDED_TEST_DIRS: Tuple[str, ...] = ("tests/live", "tests/benchmarks")

#: 强制离线的环境变量取值：任何继承来的 ``=1`` 都会被覆盖。
OFFLINE_ENV: Mapping[str, str] = {"STOCKQA_RUN_LIVE_E2E": "0"}

BASE_PYTEST_ARGS: Tuple[str, ...] = (
    "-q",
    "--tb=short",
    "-p",
    "no:base_url",
    "-p",
    "no:cacheprovider",
    "-m",
    "not live and not benchmark",
    f"--ignore={EXCLUDED_TEST_DIRS[0]}",
    f"--ignore={EXCLUDED_TEST_DIRS[1]}",
)

# --------------------------------------------------------------------------------------
# CLI smoke：真实入口模块导入
# --------------------------------------------------------------------------------------

SMOKE_IMPORTS: Tuple[str, ...] = (
    "main",
    "main_with_llm",
    "src.runners.basic_runner",
    "src.core.models",
    "src.config.settings",
    "src.config.config_manager",
    "src.config.llm_config",
)

SMOKE_STATEMENTS: Tuple[str, ...] = (
    "import main",
    "import main_with_llm",
    "from src.runners.basic_runner import BasicRunner",
    "import src.core.models",
    "import src.config.settings",
    "import src.config.config_manager",
    "import src.config.llm_config",
    "print('stockqa-cli-smoke-ok')",
)

# --------------------------------------------------------------------------------------
# 报告副作用
# --------------------------------------------------------------------------------------

#: 默认模式下绝不允许出现在仓库根的报告产物名。
REPORT_ARTIFACT_NAMES: Tuple[str, ...] = (
    "coverage.xml",
    "coverage.json",
    "junit.xml",
    "pytest.html",
    "htmlcov",
    "reports",
)
COVERAGE_DATA_PREFIX = ".coverage"

FILE_ATTRIBUTE_REPARSE_POINT = 0x400

PYLINT_FATAL = 1
PYLINT_USAGE_ERROR = 32
_PYLINT_SCORE_RE = re.compile(r"rated at\s+(\d+(?:\.\d+)?)\s*/\s*10")


# --------------------------------------------------------------------------------------
# 进程执行
# --------------------------------------------------------------------------------------
@dataclass
class StepResult:
    """一次子进程执行的可观测结果。"""

    name: str
    command: Tuple[str, ...]
    exit_code: int
    seconds: float
    output: str = ""


def run_step(
    command: Sequence[str],
    *,
    cwd: Path,
    name: str = "",
    timeout: float = 600.0,
    env: Optional[Mapping[str, str]] = None,
    capture: bool = False,
) -> StepResult:
    """执行一个子进程并原样返回其退出码。

    超时返回 :data:`TIMEOUT_EXIT_CODE`，可执行文件缺失返回
    :data:`NOT_FOUND_EXIT_CODE`，其它启动失败返回 :data:`SPAWN_ERROR_EXIT_CODE`；
    三者都是真实失败，绝不返回 0。
    """
    argv = [str(part) for part in command]
    started = time.monotonic()
    stdout = subprocess.PIPE if capture else None
    stderr = subprocess.STDOUT if capture else None
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            env=dict(env) if env is not None else None,
            timeout=timeout,
            stdout=stdout,
            stderr=stderr,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return StepResult(name, tuple(argv), TIMEOUT_EXIT_CODE, time.monotonic() - started, "")
    except FileNotFoundError:
        return StepResult(name, tuple(argv), NOT_FOUND_EXIT_CODE, time.monotonic() - started, "")
    except OSError:
        return StepResult(name, tuple(argv), SPAWN_ERROR_EXIT_CODE, time.monotonic() - started, "")
    output = proc.stdout or "" if capture else ""
    return StepResult(name, tuple(argv), proc.returncode, time.monotonic() - started, output)


def build_env(extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """构造子进程环境：强制离线并把仓根放进 ``PYTHONPATH``。"""
    env = dict(os.environ)
    env.update(OFFLINE_ENV)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(REPO_ROOT) + (os.pathsep + existing if existing else "")
    # 统一子进程标准流编码，避免 Windows 本地代码页把诊断输出变成乱码。
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if extra:
        env.update(extra)
    return env


# --------------------------------------------------------------------------------------
# pytest 步骤
# --------------------------------------------------------------------------------------


def build_pytest_args(
    *, output_dir: Optional[os.PathLike] = None, extra_args: Sequence[str] = ()
) -> Tuple[str, ...]:
    """构建 pytest 参数：离线守卫 + （仅 metrics）覆盖率产物重定向。"""
    args: List[str] = list(BASE_PYTEST_ARGS)
    if output_dir is not None:
        out = Path(output_dir)
        args += [
            "--cov=src",
            "--cov-report=term-missing",
            f"--cov-report=xml:{out / 'coverage.xml'}",
            f"--cov-report=html:{out / 'htmlcov'}",
            f"--cov-report=json:{out / 'coverage.json'}",
        ]
    args += [str(part) for part in extra_args]
    return tuple(args)


def run_pytest(
    repo_root: Path,
    targets: Sequence[str],
    *,
    extra_args: Sequence[str] = (),
    output_dir: Optional[os.PathLike] = None,
    timeout: float = 1800.0,
    capture: bool = False,
    env: Optional[Mapping[str, str]] = None,
) -> StepResult:
    """按统一离线参数执行 pytest，返回 pytest 的真实退出码。"""
    args = build_pytest_args(output_dir=output_dir, extra_args=extra_args)
    command = [sys.executable, "-B", "-m", "pytest", *args, *[str(t) for t in targets]]
    child_env = build_env()
    if output_dir is not None:
        child_env["COVERAGE_FILE"] = str(Path(output_dir) / ".coverage")
    if env:
        child_env.update(env)
    return run_step(
        command,
        cwd=Path(repo_root),
        name=STEP_PYTEST,
        timeout=timeout,
        env=child_env,
        capture=capture,
    )


# --------------------------------------------------------------------------------------
# CLI smoke
# --------------------------------------------------------------------------------------


def _logs_dir_snapshot(repo_root: Path) -> Optional[List[str]]:
    logs = Path(repo_root) / "logs"
    if not logs.is_dir():
        return None
    return sorted(entry.name for entry in logs.iterdir())


def run_smoke(
    repo_root: Path,
    *,
    statements: Sequence[str] = SMOKE_STATEMENTS,
    timeout: float = 300.0,
    capture: bool = False,
    scratch_parent: Optional[os.PathLike] = None,
) -> StepResult:
    """在独立 TemporaryDirectory 中导入真实 CLI 模块。

    ``src/utils/logger.py`` 导入即 ``Path("logs").mkdir()``，因此必须用临时目录作
    cwd，绝不让 ``logs/`` 落到仓库根；无论成功失败 scratch 都会被清理。
    """
    repo_root = Path(repo_root)
    before = _logs_dir_snapshot(repo_root)
    program = "\n".join(statements)
    parent = Path(scratch_parent) if scratch_parent is not None else None
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="g2_smoke_", dir=str(parent) if parent else None
    ) as tmp:
        result = run_step(
            [sys.executable, "-B", "-c", program],
            cwd=Path(tmp),
            name=STEP_SMOKE,
            timeout=timeout,
            env=build_env(),
            capture=capture,
        )
    after = _logs_dir_snapshot(repo_root)
    if after != before:
        before_set = set(before or [])
        for name in set(after or []) - before_set:
            target = repo_root / "logs" / name
            if _is_safe_to_remove(target, repo_root):
                target.unlink(missing_ok=True)
        return StepResult(
            result.name,
            result.command,
            SPAWN_ERROR_EXIT_CODE,
            result.seconds,
            result.output + "\nchecks: CLI smoke 污染了仓库根 logs/，已清理",
        )
    return result


# --------------------------------------------------------------------------------------
# 报告快照与恢复
# --------------------------------------------------------------------------------------


def snapshot_report_artifacts(root: os.PathLike) -> Set[str]:
    """返回仓库根当前存在的报告产物名集合。"""
    base = Path(root)
    found: Set[str] = set()
    for name in REPORT_ARTIFACT_NAMES:
        if (base / name).exists():
            found.add(name)
    for path in base.glob(COVERAGE_DATA_PREFIX + "*"):
        found.add(path.name)
    return found


def _is_safe_to_remove(path: Path, root: Path) -> bool:
    """删除前确认目标位于仓内且不是 symlink / reparse point。"""
    try:
        root_resolved = Path(root).resolve()
        self_resolved = path.resolve()
    except OSError:
        return False
    if self_resolved != root_resolved and root_resolved not in self_resolved.parents:
        return False
    try:
        stat = path.lstat()
    except OSError:
        return False
    if getattr(stat, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    if path.is_symlink():
        return False
    return True


def restore_report_artifacts(
    root: os.PathLike,
    before: Iterable[str],
    *,
    keep: Sequence[os.PathLike] = (),
) -> List[str]:
    """删除运行前不存在的报告产物，返回被删除的名字；不碰原有文件。"""
    base = Path(root)
    keep_resolved = []
    for item in keep:
        try:
            keep_resolved.append(Path(item).resolve())
        except OSError:
            continue
    removed: List[str] = []
    for name in sorted(snapshot_report_artifacts(base)):
        if name in set(before):
            continue
        target = base / name
        try:
            target_resolved = target.resolve()
        except OSError:
            continue
        if any(
            target_resolved == kept or kept in target_resolved.parents for kept in keep_resolved
        ):
            continue
        if not _is_safe_to_remove(target, base):
            continue
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target, ignore_errors=True)
        else:
            target.unlink(missing_ok=True)
        removed.append(name)
    return removed


def ensure_output_dir(path: "os.PathLike[str] | str") -> Path:
    """创建并返回指标输出目录（支持空格与中文路径）。"""
    out = Path(path).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    if not out.is_dir():
        raise NotADirectoryError(f"输出目录不可用：{out}")
    return out


# --------------------------------------------------------------------------------------
# 诊断策略（不设数字门）
# --------------------------------------------------------------------------------------


def pylint_failure_kind(exit_code: int) -> str:
    """区分 pylint 的真实工具故障与"只是有 lint 消息"。"""
    if exit_code == 0:
        return "ok"
    if exit_code in (TIMEOUT_EXIT_CODE, NOT_FOUND_EXIT_CODE, SPAWN_ERROR_EXIT_CODE):
        return "fatal"
    if exit_code & (PYLINT_FATAL | PYLINT_USAGE_ERROR):
        return "fatal"
    return "diagnostic"


def parse_pylint_score(text: str) -> Optional[float]:
    match = _PYLINT_SCORE_RE.search(text or "")
    return float(match.group(1)) if match else None


# --------------------------------------------------------------------------------------
# 计划
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Plan:
    """一次检查运行的完整计划（可序列化，用于 ``--list-steps``）。"""

    mode: str
    offline: bool
    steps: Tuple[str, ...]
    pytest_scope: str
    pytest_targets: Tuple[str, ...]
    pytest_args: Tuple[str, ...]
    pytest_env: Tuple[Tuple[str, str], ...]
    output_dir: Optional[str]

    def to_json_dict(self) -> Dict[str, object]:
        return {
            "mode": self.mode,
            "offline": self.offline,
            "steps": list(self.steps),
            "pytest": {
                "scope": self.pytest_scope,
                "targets": list(self.pytest_targets),
                "args": list(self.pytest_args),
                "env": dict(self.pytest_env),
            },
            "output_dir": self.output_dir,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_json_dict(), ensure_ascii=False, indent=2)


def build_plan(
    mode: str = MODE_DEFAULT,
    *,
    only: Optional[Sequence[str]] = None,
    pytest_targets: Optional[Sequence[str]] = None,
    output_dir: Optional[os.PathLike] = None,
) -> Plan:
    """构造检查计划；参数不合法时抛 :class:`ValueError`（由 ``main`` 转成退出码 2）。"""
    if mode not in MODE_STEPS:
        raise ValueError(f"未知模式：{mode}")
    if output_dir is not None and mode != MODE_METRICS:
        raise ValueError("--output-dir 只能与 --metrics 一起使用")
    if mode == MODE_METRICS and output_dir is None:
        raise ValueError("--metrics 需要显式 --output-dir")

    selected = list(MODE_STEPS[mode])
    if only:
        requested = []
        for step in only:
            if step not in STEP_ORDER:
                raise ValueError(f"未知检查步骤：{step}")
            if step not in selected:
                raise ValueError(f"步骤 {step} 不属于模式 {mode}")
            requested.append(step)
        selected = [step for step in selected if step in requested]

    scope = "full" if mode in (MODE_FULL, MODE_METRICS) else "daily"
    if pytest_targets:
        targets = tuple(str(item) for item in pytest_targets)
        scope = "custom"
    elif scope == "full":
        targets = FULL_TEST_TARGETS
    else:
        targets = DAILY_TEST_TARGETS

    if pytest_targets and STEP_PYTEST not in selected:
        raise ValueError("--pytest-target 需要本次运行包含 pytest 步骤")

    metric_output = Path(output_dir) if (output_dir is not None and mode == MODE_METRICS) else None
    pytest_env: List[Tuple[str, str]] = sorted(OFFLINE_ENV.items())
    if metric_output is not None:
        pytest_env.append(("COVERAGE_FILE", str(metric_output / ".coverage")))

    return Plan(
        mode=mode,
        offline=True,
        steps=tuple(selected),
        pytest_scope=scope,
        pytest_targets=targets,
        pytest_args=build_pytest_args(output_dir=metric_output),
        pytest_env=tuple(pytest_env),
        output_dir=str(output_dir) if output_dir is not None else None,
    )


# --------------------------------------------------------------------------------------
# 步骤执行
# --------------------------------------------------------------------------------------


def _tool_command(step: str) -> List[str]:
    commands: Dict[str, List[str]] = {
        STEP_BLACK: [
            sys.executable,
            "-B",
            "-m",
            "black",
            "--check",
            "src",
            "tests",
            "scripts",
        ],
        STEP_ISORT: [
            sys.executable,
            "-B",
            "-m",
            "isort",
            "--profile",
            "black",
            "--check-only",
            "src",
            "tests",
            "scripts",
        ],
        STEP_MYPY: [sys.executable, "-B", "-m", "mypy", "src"],
        STEP_BANDIT: [
            sys.executable,
            "-B",
            "-m",
            "bandit",
            "-r",
            "src",
            "-q",
            "-f",
            "screen",
        ],
    }
    return commands[step]


def _report_step(step: str, result: StepResult, detail: str = "") -> int:
    suffix = f" {detail}" if detail else ""
    if result.exit_code == 0:
        print(
            f"checks: step={step} exit=0 seconds={result.seconds:.2f}{suffix}",
            flush=True,
        )
    else:
        print(
            f"checks: step={step} exit={result.exit_code} seconds={result.seconds:.2f}{suffix}"
            " FAILED",
            flush=True,
            file=sys.stderr,
        )
    return result.exit_code


def _run_tool_step(step: str, plan: Plan, timeout: float) -> int:
    result = run_step(
        _tool_command(step),
        cwd=REPO_ROOT,
        name=step,
        timeout=timeout,
        env=build_env(),
    )
    return _report_step(step, result)


def _run_pytest_step(plan: Plan, timeout: float) -> int:
    command = [
        sys.executable,
        "-B",
        "-m",
        "pytest",
        *plan.pytest_args,
        *plan.pytest_targets,
    ]
    env = build_env(dict(plan.pytest_env))
    result = run_step(command, cwd=REPO_ROOT, name=STEP_PYTEST, timeout=timeout, env=env)
    return _report_step(STEP_PYTEST, result)


def _run_smoke_step(plan: Plan, timeout: float) -> int:
    result = run_smoke(REPO_ROOT, timeout=timeout)
    return _report_step(STEP_SMOKE, result)


def _run_pylint_step(plan: Plan, timeout: float) -> int:
    result = run_step(
        [sys.executable, "-B", "-m", "pylint", "src"],
        cwd=REPO_ROOT,
        name=STEP_PYLINT,
        timeout=timeout,
        env=build_env(),
        capture=True,
    )
    if plan.output_dir:
        ensure_output_dir(plan.output_dir)
        (Path(plan.output_dir) / "pylint.txt").write_text(result.output, encoding="utf-8")
    kind = pylint_failure_kind(result.exit_code)
    if kind == "fatal":
        print(result.output[-4000:], file=sys.stderr, flush=True)
        return _report_step(STEP_PYLINT, result, detail="pylint 未能产出诊断报告")
    score = parse_pylint_score(result.output)
    if score is None:
        print(result.output[-4000:], file=sys.stderr, flush=True)
        return _report_step(
            STEP_PYLINT,
            StepResult(result.name, result.command, SPAWN_ERROR_EXIT_CODE, result.seconds),
            detail="pylint 未产出评分",
        )
    print(
        f"checks: step={STEP_PYLINT} exit=0 seconds={result.seconds:.2f}"
        f" diagnostic_pylint_score={score:.2f}/10 (diagnostic only, not a gate)",
        flush=True,
    )
    return 0


def _run_radon_step(plan: Plan, timeout: float) -> int:
    if not plan.output_dir:
        return _report_step(
            STEP_RADON,
            StepResult(STEP_RADON, (), SPAWN_ERROR_EXIT_CODE, 0.0),
            detail="metrics 步骤缺少 --output-dir",
        )
    out_dir = ensure_output_dir(plan.output_dir)
    jobs = (
        (
            [sys.executable, "-B", "-m", "radon", "cc", "src", "-a"],
            out_dir / "radon_cc.txt",
        ),
        (
            [sys.executable, "-B", "-m", "radon", "mi", "src"],
            out_dir / "radon_mi.txt",
        ),
    )
    elapsed = 0.0
    for command, target in jobs:
        result = run_step(
            command,
            cwd=REPO_ROOT,
            name=STEP_RADON,
            timeout=timeout,
            env=build_env(),
            capture=True,
        )
        elapsed += result.seconds
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(result.output, encoding="utf-8")
        if result.exit_code != 0:
            print(result.output[-4000:], file=sys.stderr, flush=True)
            return _report_step(STEP_RADON, result, detail=f"report={target.name}")
    print(
        f"checks: step={STEP_RADON} exit=0 seconds={elapsed:.2f}"
        " reports=radon_cc.txt,radon_mi.txt (diagnostic only, not a gate)",
        flush=True,
    )
    return 0


STEP_RUNNERS = {
    STEP_BLACK: partial(_run_tool_step, STEP_BLACK),
    STEP_ISORT: partial(_run_tool_step, STEP_ISORT),
    STEP_MYPY: partial(_run_tool_step, STEP_MYPY),
    STEP_BANDIT: partial(_run_tool_step, STEP_BANDIT),
    STEP_PYTEST: _run_pytest_step,
    STEP_SMOKE: _run_smoke_step,
    STEP_PYLINT: _run_pylint_step,
    STEP_RADON: _run_radon_step,
}


def run_plan(plan: Plan, *, timeout: Optional[float] = None) -> int:
    """按顺序执行计划中的步骤，首个真实失败即停；结束后恢复仓库根。"""
    started = time.monotonic()
    keep: List[Path] = [Path(plan.output_dir)] if plan.output_dir else []
    before = snapshot_report_artifacts(REPO_ROOT)
    exit_code = 0
    completed: List[str] = []
    try:
        if plan.output_dir:
            ensure_output_dir(plan.output_dir)
        for step in plan.steps:
            step_timeout = STEP_TIMEOUTS[step] if timeout is None else timeout
            print(f"==> checks [{plan.mode}] step={step}", flush=True)
            runner = STEP_RUNNERS[step]
            code = runner(plan, step_timeout)
            completed.append(step)
            if code != 0:
                exit_code = int(code)
                break
    except OSError as exc:
        print(f"checks: {exc}", file=sys.stderr, flush=True)
        exit_code = SPAWN_ERROR_EXIT_CODE
    finally:
        stray = restore_report_artifacts(REPO_ROOT, before, keep=keep)
        if stray:
            print(
                "checks: 检测到仓库根出现报告产物并已清理：" + ", ".join(stray),
                file=sys.stderr,
                flush=True,
            )
            if exit_code == 0:
                exit_code = 1
    seconds = time.monotonic() - started
    result = "pass" if exit_code == 0 else "fail"
    print(
        f"checks: mode={plan.mode} result={result} exit={exit_code}"
        f" steps={','.join(completed) or '-'} seconds={seconds:.2f}",
        flush=True,
    )
    return exit_code


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def _positive_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"必须是数字：{raw}") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError(f"必须大于 0：{raw}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="checks.py",
        description="StockQAbyLLM 统一工程检查入口（唯一 Python 定义点）。",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--static-only",
        dest="mode",
        action="store_const",
        const=MODE_STATIC,
        help="仅静态检查",
    )
    mode_group.add_argument(
        "--full",
        dest="mode",
        action="store_const",
        const=MODE_FULL,
        help="大节点：全部离线 unit/integration",
    )
    mode_group.add_argument(
        "--metrics",
        dest="mode",
        action="store_const",
        const=MODE_METRICS,
        help="大节点：全部离线检查 + 诊断报告",
    )
    parser.set_defaults(mode=MODE_DEFAULT)
    parser.add_argument("--output-dir", default=None, help="诊断报告输出目录（仅 --metrics）")
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        metavar="STEP",
        help="只执行指定步骤（可重复）",
    )
    parser.add_argument(
        "--pytest-target",
        action="append",
        default=None,
        metavar="PATH",
        help="覆盖 pytest 目标（可重复）",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=None,
        metavar="SECONDS",
        help="覆盖每一步的超时秒数",
    )
    parser.add_argument("--list-steps", action="store_true", help="只打印计划 JSON，不执行")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:  # argparse 的 --help(0) / 错误(2)
        return int(exc.code or 0)

    try:
        plan = build_plan(
            args.mode,
            only=args.only,
            pytest_targets=args.pytest_target,
            output_dir=args.output_dir,
        )
    except ValueError as exc:
        print(f"checks: {exc}", file=sys.stderr, flush=True)
        return 2

    if args.list_steps:
        print(plan.to_json())
        return 0
    return run_plan(plan, timeout=args.timeout)


if __name__ == "__main__":
    sys.exit(main())
