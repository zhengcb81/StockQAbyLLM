"""统一检查入口 ``scripts/checks.py`` 的编排反例单元测试。

这些用例先于实现建立（RED），覆盖卡 G2-SQA-CHECKS 第 5 节要求的
"子命令返回 7 / 收集失败 / 不存在 / 超时不能被编排报绿"、"默认无报告副作用"、
"低 coverage/pylint 只诊断而真实失败仍失败"、"真实 CLI 模块导入错误非零"、
"导入 logs scratch 最终恢复"。

只使用本卡短临时目录（`<repo>/../_tmp/g2_checks_scratch`），不触碰 owner 数据。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKS_PATH = REPO_ROOT / "scripts" / "checks.py"
SCRATCH_ROOT = REPO_ROOT.parent / "_tmp" / "g2_checks_scratch" / "unit"

SPACE_DIR_NAME = "含 空格"

DAILY_UNIT_FILES = (
    "tests/unit/test_models.py",
    "tests/unit/test_config_manager.py",
    "tests/unit/test_llm_config.py",
    "tests/unit/test_basic_runner.py",
    "tests/unit/test_main_with_llm.py",
    "tests/unit/test_q07_checkpoint.py",
    "tests/unit/test_quick_scan_budget.py",
    "tests/unit/test_q09_budget_concurrency.py",
    "tests/unit/test_ci_checks.py",
)

PUBLIC_CLI_NODES = (
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


def _load_checks():
    if not CHECKS_PATH.is_file():
        raise AssertionError(f"缺少统一检查入口：{CHECKS_PATH}")
    spec = importlib.util.spec_from_file_location("stockqa_checks_under_test", CHECKS_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"无法为 {CHECKS_PATH} 构造 import spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checks():
    """按需加载 `scripts/checks.py`；入口缺失时整模块报错（RED）。"""
    return _load_checks()


@pytest.fixture(scope="module")
def scratch():
    """本卡短临时目录：通过 / 真失败 / 收集失败 / 空格中文路径四种节点。"""
    root = SCRATCH_ROOT
    shutil.rmtree(root, ignore_errors=True)
    space = root / SPACE_DIR_NAME
    space.mkdir(parents=True, exist_ok=True)
    (root / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (root / "test_fail.py").write_text("def test_fail():\n    assert False\n", encoding="utf-8")
    (root / "test_broken.py").write_text(
        "import no_such_module_xyz\n\n\ndef test_broken():\n    pass\n",
        encoding="utf-8",
    )
    (space / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    yield root
    shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------------------
# 编排：真实退出码不能被吞
# --------------------------------------------------------------------------------------


def test_run_step_propagates_exit_code_seven(checks):
    result = checks.run_step(
        [sys.executable, "-c", "import sys; sys.exit(7)"],
        cwd=REPO_ROOT,
        name="probe",
        timeout=60,
    )
    assert result.exit_code == 7, f"编排吞掉了子命令退出码 7：{result.exit_code}"


def test_run_step_timeout_is_not_reported_green(checks):
    result = checks.run_step(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=REPO_ROOT,
        name="slow",
        timeout=0.3,
    )
    assert result.exit_code != 0, "超时被报成通过"
    assert result.exit_code == checks.TIMEOUT_EXIT_CODE
    assert result.seconds < 25, "超时没有真正终止子进程"


def test_run_step_missing_executable_is_not_reported_green(checks):
    result = checks.run_step(
        ["definitely_not_a_real_executable_xyz"],
        cwd=REPO_ROOT,
        name="missing",
        timeout=60,
    )
    assert result.exit_code != 0, "工具不存在被报成通过"


def test_run_pytest_collection_error_is_not_reported_green(checks, scratch):
    result = checks.run_pytest(
        REPO_ROOT, [str(scratch / "test_broken.py")], timeout=300, capture=True
    )
    assert result.exit_code != 0, "pytest 收集失败被编排报绿"


def test_run_pytest_unknown_target_is_not_reported_green(checks, scratch):
    result = checks.run_pytest(
        REPO_ROOT, [str(scratch / "no_such_test_file.py")], timeout=300, capture=True
    )
    assert result.exit_code != 0, "pytest 目标不存在被报成通过"


def test_run_pytest_failing_test_still_fails(checks, scratch):
    result = checks.run_pytest(
        REPO_ROOT, [str(scratch / "test_fail.py")], timeout=300, capture=True
    )
    assert result.exit_code != 0, "真实用例失败被报成通过"


def test_run_pytest_accepts_path_with_spaces_and_chinese(checks, scratch):
    target = scratch / SPACE_DIR_NAME / "test_ok.py"
    assert target.is_file()
    result = checks.run_pytest(REPO_ROOT, [str(target)], timeout=300, capture=True)
    assert result.exit_code == 0, f"空格/中文路径执行失败：exit={result.exit_code}"


# --------------------------------------------------------------------------------------
# 默认 / full / static / metrics 的责任边界
# --------------------------------------------------------------------------------------


def test_default_plan_is_offline_and_excludes_live(checks):
    plan = checks.build_plan("default")
    payload = plan.to_json_dict()
    assert payload["offline"] is True
    args = payload["pytest"]["args"]
    assert "no:base_url" in args, "未禁用环境未声明的 pytest-base-url 插件"
    assert "not live and not benchmark" in args
    assert "--ignore=tests/live" in args
    assert "--ignore=tests/benchmarks" in args
    assert payload["pytest"]["env"]["STOCKQA_RUN_LIVE_E2E"] == "0", "默认未强制离线"
    for arg in args:
        assert not arg.startswith("--cov"), f"默认 pytest 注入了覆盖率产物：{arg}"
        assert "cov-fail-under" not in arg, "默认 pytest 保留了覆盖率数字门"
    joined = " ".join(payload["pytest"]["targets"])
    assert "tests/live" not in joined
    assert "tests/benchmarks" not in joined


def test_build_env_forces_offline_even_when_inherited(checks, monkeypatch):
    monkeypatch.setenv("STOCKQA_RUN_LIVE_E2E", "1")
    env = checks.build_env()
    assert env["STOCKQA_RUN_LIVE_E2E"] == "0", "继承的 STOCKQA_RUN_LIVE_E2E=1 没有被覆盖"
    assert str(checks.REPO_ROOT) in env["PYTHONPATH"].split(os.pathsep)


def test_run_pytest_command_carries_offline_guards(checks, scratch):
    result = checks.run_pytest(REPO_ROOT, [str(scratch / "test_ok.py")], capture=True)
    command = " ".join(result.command)
    assert "no:base_url" in command
    assert "not live and not benchmark" in command
    assert "tests/live" in command
    assert "--cov" not in command
    assert result.exit_code == 0, result.output


def test_default_targets_cover_required_daily_nodes(checks):
    plan = checks.build_plan("default")
    targets = tuple(plan.to_json_dict()["pytest"]["targets"])
    for node in DAILY_UNIT_FILES + PUBLIC_CLI_NODES:
        assert node in targets, f"日常责任包缺少节点：{node}"


def test_full_plan_targets_are_offline_unit_and_integration(checks):
    plan = checks.build_plan("full")
    payload = plan.to_json_dict()
    assert payload["pytest"]["scope"] == "full"
    assert payload["pytest"]["targets"] == ["tests/unit", "tests/integration"]
    assert payload["pytest"]["env"]["STOCKQA_RUN_LIVE_E2E"] == "0"
    args = payload["pytest"]["args"]
    assert "--ignore=tests/live" in args
    assert "--ignore=tests/benchmarks" in args
    assert "no:base_url" in args


def test_static_plan_has_no_pytest_and_no_smoke(checks):
    plan = checks.build_plan("static-only")
    steps = plan.to_json_dict()["steps"]
    assert "pytest" not in steps
    assert "smoke" not in steps
    assert "black" in steps
    assert "mypy" in steps


def test_metrics_plan_has_no_numeric_gate_and_reports_into_output_dir(checks, tmp_path):
    out = tmp_path / "指标 输出 目录"
    plan = checks.build_plan("metrics", output_dir=str(out))
    payload = plan.to_json_dict()
    assert payload["output_dir"] == str(out)
    for arg in payload["pytest"]["args"]:
        assert "cov-fail-under" not in arg, "metrics 仍保留覆盖率数字门"
    assert payload["pytest"]["scope"] == "full"
    assert "pylint" in payload["steps"]
    assert "radon" in payload["steps"]


# --------------------------------------------------------------------------------------
# 参数校验：不存在 / 不合法必须非零
# --------------------------------------------------------------------------------------


def test_main_rejects_unknown_flag(checks):
    assert checks.main(["--definitely-unknown-flag"]) == 2


def test_main_rejects_unknown_only_step(checks):
    assert checks.main(["--only", "no_such_step"]) == 2


def test_main_rejects_only_step_outside_selected_mode(checks):
    assert checks.main(["--static-only", "--only", "pytest"]) == 2


def test_main_rejects_output_dir_without_metrics(checks, tmp_path):
    assert checks.main(["--output-dir", str(tmp_path / "out")]) == 2


def test_main_rejects_metrics_without_output_dir(checks):
    assert checks.main(["--metrics"]) == 2


def test_main_rejects_non_positive_timeout(checks):
    assert checks.main(["--timeout", "0"]) == 2


def test_main_rejects_pytest_target_when_pytest_not_selected(checks, tmp_path):
    target = tmp_path / "test_ok.py"
    target.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    assert checks.main(["--static-only", "--pytest-target", str(target)]) == 2


def test_list_steps_emits_plan_without_running(checks, capsys):
    code = checks.main(["--list-steps"])
    assert code == 0
    payload = capsys.readouterr().out
    assert '"mode"' in payload
    assert '"steps"' in payload


# --------------------------------------------------------------------------------------
# 诊断 vs 真失败
# --------------------------------------------------------------------------------------


def test_pylint_message_exit_is_diagnostic_but_tool_failure_is_fatal(checks):
    warning_only = 4 | 8 | 16
    assert checks.pylint_failure_kind(warning_only) == "diagnostic"
    assert checks.pylint_failure_kind(0) == "ok"
    assert checks.pylint_failure_kind(32) == "fatal"
    assert checks.pylint_failure_kind(1) == "fatal"
    assert checks.pylint_failure_kind(checks.NOT_FOUND_EXIT_CODE) == "fatal"
    assert checks.pylint_failure_kind(checks.TIMEOUT_EXIT_CODE) == "fatal"


def test_parse_pylint_score(checks):
    text = "Your code has been rated at 9.31/10 (previous run: 9.31/10, +0.00)\n"
    assert checks.parse_pylint_score(text) == pytest.approx(9.31)
    assert checks.parse_pylint_score("no score line here") is None


# --------------------------------------------------------------------------------------
# CLI smoke：真实模块导入 + logs scratch 恢复
# --------------------------------------------------------------------------------------


def test_smoke_import_failure_is_not_reported_green(checks):
    result = checks.run_smoke(
        REPO_ROOT,
        statements=("import module_that_does_not_exist_for_smoke",),
        timeout=300,
        capture=True,
    )
    assert result.exit_code != 0, "真实 CLI 模块导入错误被报成通过"


def test_smoke_success_cleans_scratch_and_keeps_repo_logs_untouched(checks, tmp_path):
    logs_dir = REPO_ROOT / "logs"
    before = sorted(p.name for p in logs_dir.iterdir()) if logs_dir.is_dir() else None
    result = checks.run_smoke(REPO_ROOT, timeout=300, capture=True, scratch_parent=tmp_path)
    assert result.exit_code == 0, f"CLI smoke 失败：exit={result.exit_code}"
    assert list(tmp_path.iterdir()) == [], "导入 logs 的 scratch 未被清理"
    after = sorted(p.name for p in logs_dir.iterdir()) if logs_dir.is_dir() else None
    assert after == before, "CLI smoke 污染了仓库根 logs/"


def test_smoke_failure_still_cleans_scratch(checks, tmp_path):
    result = checks.run_smoke(
        REPO_ROOT,
        statements=("import module_that_does_not_exist_for_smoke",),
        timeout=300,
        capture=True,
        scratch_parent=tmp_path,
    )
    assert result.exit_code != 0
    assert list(tmp_path.iterdir()) == [], "失败路径的 scratch 未被清理"


def test_smoke_imports_required_cli_modules(checks):
    for module in checks.SMOKE_IMPORTS:
        assert module.split(".")[0] in {"main", "main_with_llm", "src"}


# --------------------------------------------------------------------------------------
# 报告副作用与恢复
# --------------------------------------------------------------------------------------


def test_pyproject_default_addopts_has_no_report_flags(checks):
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    marker = "[tool.pytest.ini_options]"
    assert marker in text
    start = text.index(marker)
    rest = text[start + len(marker) :]
    end = rest.find("\n[")
    section = rest if end == -1 else rest[:end]
    for forbidden in ("--cov", "--junitxml", "--html", "cov-fail-under"):
        assert forbidden not in section, f"默认 pytest 配置仍包含 {forbidden}"


def test_restore_report_artifacts_removes_only_new_files(checks, tmp_path):
    keep = tmp_path / "keep.txt"
    keep.write_text("owner data", encoding="utf-8")
    before = checks.snapshot_report_artifacts(tmp_path)
    (tmp_path / "coverage.xml").write_text("<coverage/>", encoding="utf-8")
    (tmp_path / "htmlcov").mkdir()
    (tmp_path / "htmlcov" / "index.html").write_text("x", encoding="utf-8")
    removed = checks.restore_report_artifacts(tmp_path, before)
    assert set(removed) >= {"coverage.xml", "htmlcov"}
    assert keep.is_file(), "恢复过程删除了非报告文件"
    assert not (tmp_path / "coverage.xml").exists()
    assert not (tmp_path / "htmlcov").exists()


def test_ensure_output_dir_supports_spaces_and_chinese(checks, tmp_path):
    out = tmp_path / "含 空格" / "指标输出"
    resolved = checks.ensure_output_dir(out)
    assert resolved == out
    assert out.is_dir()


# --------------------------------------------------------------------------------------
# 业务责任节点必须仍在日常包内（防"删用例凑绿"）
# --------------------------------------------------------------------------------------


def test_daily_unit_files_exist(checks):
    for rel in DAILY_UNIT_FILES:
        assert (REPO_ROOT / rel).is_file(), f"日常责任文件缺失：{rel}"


def test_public_cli_nodes_exist(checks):
    source = (REPO_ROOT / "tests/integration/test_quick_scan_cli.py").read_text(encoding="utf-8")
    for node in PUBLIC_CLI_NODES:
        name = node.split("::", 1)[1]
        assert f"def {name}(" in source, f"公开 CLI 节点缺失：{name}"


def test_cross_process_budget_node_exists(checks):
    source = (REPO_ROOT / "tests/integration/test_quick_scan_budget.py").read_text(encoding="utf-8")
    assert "def test_two_processes_share_budget_and_real_dispatch_concurrency_slots(" in source
