"""统一入口与 sh/bat wrapper 的进程级反例（RED→GREEN）。

覆盖卡 G2-SQA-CHECKS 第 5 节：
- 实际 sh/bat 转发参数与非零退出码
- 仓外 cwd、空格/中文路径
- 默认/各模式无报告副作用
- CLI 级超时、收集失败不能被编排报绿

本文件只用 ``--only`` / ``--list-steps`` / 非法参数，绝不调用 default/full/metrics
完整模式，避免递归执行自身。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKS_SCRIPT = REPO_ROOT / "scripts" / "checks.py"
SH_WRAPPER = REPO_ROOT / "scripts" / "run_ci.sh"
BAT_WRAPPER = REPO_ROOT / "scripts" / "run_ci.bat"
SCRATCH_ROOT = REPO_ROOT.parent / "_tmp" / "g2_checks_scratch" / "integration"

SPACE_DIR_NAME = "含 空格"
FAST_REPO_TARGET = "tests/unit/test_search_service.py"

REPORT_ARTIFACTS = ("coverage.xml", "coverage.json", "junit.xml", "pytest.html")
REPORT_DIRS = ("htmlcov", "reports")
COVERAGE_DATA_PREFIX = ".coverage"


def _find_bash() -> str:
    bash = shutil.which("bash")
    assert bash, "本机缺少 bash，无法验证 scripts/run_ci.sh"
    return bash


def _find_cmd() -> str:
    if os.name != "nt":
        pytest.skip("cmd.exe 仅在 Windows 存在")
    cmd = os.environ.get("COMSPEC") or shutil.which("cmd")
    assert cmd, "本机缺少 cmd.exe，无法验证 scripts/run_ci.bat"
    return cmd


def _run(cmd, *, cwd, timeout=900):
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return subprocess.run(
        list(cmd),
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _report_residue() -> set[str]:
    found = set()
    for name in REPORT_ARTIFACTS + REPORT_DIRS:
        if (REPO_ROOT / name).exists():
            found.add(name)
    for path in REPO_ROOT.glob(f"{COVERAGE_DATA_PREFIX}*"):
        found.add(path.name)
    return found


@pytest.fixture(scope="module")
def scratch():
    root = SCRATCH_ROOT
    shutil.rmtree(root, ignore_errors=True)
    space = root / SPACE_DIR_NAME
    space.mkdir(parents=True, exist_ok=True)
    (root / "test_broken.py").write_text(
        "import no_such_module_xyz\n\n\ndef test_broken():\n    pass\n",
        encoding="utf-8",
    )
    (space / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    yield root
    shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------------------
# sh wrapper：定位仓根、转发全部参数、原样返回退出码
# --------------------------------------------------------------------------------------


def test_sh_wrapper_returns_zero_for_list_steps(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    proc = _run([_find_bash(), str(SH_WRAPPER), "--list-steps"], cwd=outside)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["mode"] == "default"


def test_sh_wrapper_forwards_unknown_flag_as_two(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    proc = _run([_find_bash(), str(SH_WRAPPER), "--no-such-flag"], cwd=outside)
    assert proc.returncode == 2, f"wrapper 吞掉了 argparse 退出码：{proc.returncode}"


def test_sh_wrapper_forwards_unknown_step_as_nonzero(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    proc = _run([_find_bash(), str(SH_WRAPPER), "--only", "no_such_step"], cwd=outside)
    assert proc.returncode != 0, "wrapper 把不存在的 step 报成通过"


def test_sh_wrapper_propagates_pytest_collection_failure(tmp_path, scratch):
    outside = tmp_path / "outside"
    outside.mkdir()
    broken = str(scratch / "test_broken.py")
    proc = _run(
        [
            _find_bash(),
            str(SH_WRAPPER),
            "--only",
            "pytest",
            "--pytest-target",
            broken,
        ],
        cwd=outside,
        timeout=600,
    )
    assert proc.returncode != 0, "wrapper 把 pytest 收集失败报成通过"
    combined = proc.stdout + proc.stderr
    assert "test_broken" in combined, combined
    assert "step=pytest" in combined, combined


def test_sh_wrapper_runs_from_cwd_with_spaces_and_chinese(tmp_path):
    outside = tmp_path / SPACE_DIR_NAME
    outside.mkdir()
    proc = _run([_find_bash(), str(SH_WRAPPER), "--list-steps"], cwd=outside)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    plan = json.loads(proc.stdout)
    assert plan["offline"] is True
    assert plan["pytest"]["env"]["STOCKQA_RUN_LIVE_E2E"] == "0"


def test_sh_wrapper_pytest_target_with_spaces_and_chinese(tmp_path, scratch):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = str(scratch / SPACE_DIR_NAME / "test_ok.py")
    proc = _run(
        [_find_bash(), str(SH_WRAPPER), "--only", "pytest", "--pytest-target", target],
        cwd=outside,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "1 passed" in proc.stdout, proc.stdout + proc.stderr


# --------------------------------------------------------------------------------------
# bat wrapper
# --------------------------------------------------------------------------------------


def test_bat_wrapper_returns_zero_for_list_steps(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    proc = _run([_find_cmd(), "/c", str(BAT_WRAPPER), "--list-steps"], cwd=outside)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["mode"] == "default"


def test_bat_wrapper_forwards_unknown_flag_as_two(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    proc = _run([_find_cmd(), "/c", str(BAT_WRAPPER), "--no-such-flag"], cwd=outside)
    assert proc.returncode == 2, f"bat wrapper 吞掉了退出码：{proc.returncode}"


def test_bat_wrapper_forwards_unknown_step_as_nonzero(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    proc = _run([_find_cmd(), "/c", str(BAT_WRAPPER), "--only", "no_such_step"], cwd=outside)
    assert proc.returncode != 0, "bat wrapper 把不存在的 step 报成通过"


# --------------------------------------------------------------------------------------
# 入口本身：仓外 cwd、报告副作用、超时
# --------------------------------------------------------------------------------------


def test_entry_runs_from_outside_repo_cwd(tmp_path):
    outside = tmp_path / SPACE_DIR_NAME
    outside.mkdir()
    proc = _run([sys.executable, "-B", str(CHECKS_SCRIPT), "--list-steps"], cwd=outside)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    plan = json.loads(proc.stdout)
    assert plan["mode"] == "default"
    assert plan["pytest"]["env"]["STOCKQA_RUN_LIVE_E2E"] == "0"


@pytest.mark.parametrize(
    ("flag", "expected_mode"),
    [
        ([], "default"),
        (["--static-only"], "static-only"),
        (["--full"], "full"),
    ],
)
def test_list_steps_reports_mode_without_side_effects(flag, expected_mode, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    before = _report_residue()
    proc = _run([sys.executable, "-B", str(CHECKS_SCRIPT), *flag, "--list-steps"], cwd=outside)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["mode"] == expected_mode
    assert _report_residue() == before, "--list-steps 产生了报告副作用"


def test_metrics_list_steps_requires_output_dir():
    proc = _run(
        [sys.executable, "-B", str(CHECKS_SCRIPT), "--metrics", "--list-steps"],
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 2, "缺少 --output-dir 的 metrics 模式没有被拒绝"


def test_default_pytest_step_writes_no_report_artifacts(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    before = _report_residue()
    proc = _run(
        [
            sys.executable,
            "-B",
            str(CHECKS_SCRIPT),
            "--only",
            "pytest",
            "--pytest-target",
            FAST_REPO_TARGET,
        ],
        cwd=outside,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "3 passed" in proc.stdout, proc.stdout + proc.stderr
    after = _report_residue()
    assert after == before, f"默认 pytest 步骤产生了报告副作用：{sorted(after - before)}"


def test_wrapper_does_not_create_reports_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    reports = REPO_ROOT / "reports"
    existed = reports.exists()
    proc = _run([_find_bash(), str(SH_WRAPPER), "--list-steps"], cwd=outside)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["mode"] == "default"
    assert reports.exists() == existed, "wrapper 仍会创建 reports/ 副作用目录"


def test_cli_step_timeout_is_not_reported_green(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    proc = _run(
        [
            sys.executable,
            "-B",
            str(CHECKS_SCRIPT),
            "--only",
            "bandit",
            "--timeout",
            "0.001",
        ],
        cwd=outside,
        timeout=600,
    )
    assert proc.returncode != 0, "CLI 级超时被报成通过"
    assert proc.returncode == 124, f"超时未返回 124，而是 {proc.returncode}"


def test_static_only_does_not_select_pytest():
    proc = _run(
        [sys.executable, "-B", str(CHECKS_SCRIPT), "--static-only", "--list-steps"],
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert "pytest" not in payload["steps"]


def test_smoke_step_passes_from_outside_cwd(tmp_path):
    outside = tmp_path / SPACE_DIR_NAME
    outside.mkdir()
    before = (
        sorted(p.name for p in (REPO_ROOT / "logs").iterdir())
        if (REPO_ROOT / "logs").is_dir()
        else None
    )
    proc = _run([sys.executable, "-B", str(CHECKS_SCRIPT), "--only", "smoke"], cwd=outside)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "step=smoke" in proc.stdout, proc.stdout + proc.stderr
    after = (
        sorted(p.name for p in (REPO_ROOT / "logs").iterdir())
        if (REPO_ROOT / "logs").is_dir()
        else None
    )
    assert after == before, "smoke 污染了仓库根 logs/"
