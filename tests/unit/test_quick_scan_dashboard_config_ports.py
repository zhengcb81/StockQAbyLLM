"""Exercise fixed configuration owner commands with temporary local inputs only."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.utils.quick_scan_work_store import QuickScanWorkStore
from tests.unit.test_llm_config import _llm_config_content, _quick_scan_policy

ROOT = Path(__file__).resolve().parents[2]


def _invoke(module, path, request, *, flag="--config"):
    process = subprocess.run(
        [sys.executable, "-B", "-X", "utf8", "-m", f"src.utils.{module}", flag, str(path)],
        cwd=ROOT,
        input=json.dumps(request),
        capture_output=True,
        encoding="utf-8",
        timeout=45,
        check=False,
    )
    assert process.stdout, process.stderr
    return process.returncode, json.loads(process.stdout)


def test_policy_command_preserves_private_config_and_rejects_stale_revision(tmp_path):
    path = tmp_path / "llm_apis.json"
    path.write_text(json.dumps(_llm_config_content(_quick_scan_policy())), encoding="utf-8")
    before = path.read_bytes()
    request = {"protocol": "stockqa.model_policy_request/1.0.0", "operation": "read"}
    code, response = _invoke("quick_scan_model_policy_cli", path, request)
    assert code == 0
    assert response["protocol"] == "stockqa.model_policy_response/1.0.0"
    assert response["operation"] == "read"
    assert response["provider_config_refs"] == ["backup", "primary"]
    assert "fixture-primary-key" not in json.dumps(response)
    assert response["API_requests"] == response["network_calls"] == 0
    assert path.read_bytes() == before

    request.update(
        operation="save",
        revision_sha256=response["revision_sha256"],
        policy=_quick_scan_policy(("backup", "primary")),
    )
    code, response = _invoke("quick_scan_model_policy_cli", path, request)
    assert code == 0 and response["policy"] == request["policy"]
    assert response["effective"] == "next_run"
    assert json.loads(before)["providers"] == json.loads(path.read_bytes())["providers"]
    after = path.read_bytes()
    code, response = _invoke("quick_scan_model_policy_cli", path, request)
    assert code == 2 and response["error_code"] == "settings_changed"
    assert path.read_bytes() == after


@pytest.mark.parametrize("mode", ["current_permissions", "paid_response_recovery"])
def test_permissions_command_never_registers_or_creates_missing_admission(tmp_path, mode):
    path = tmp_path / "missing.json"
    code, response = _invoke(
        "quick_scan_registered_permissions_cli",
        path,
        {
            "protocol": "stockqa.registered_permissions_request/1.0.0",
            "confirmation_id": "rc2_" + "a" * 64,
            "mode": mode,
        },
    )
    assert code == 2 and response["error_code"]
    assert response["API_requests"] == response["network_calls"] == 0
    assert not list(tmp_path.iterdir())


def test_permissions_command_checks_real_existing_journal_without_initializing_it(tmp_path):
    path = tmp_path / "admission.json"
    work_store = tmp_path / "work.sqlite"
    budget = tmp_path / "budget.json"
    QuickScanWorkStore(work_store)
    budget.write_text("{}", encoding="utf-8")
    path.write_text(
        json.dumps(
            dict(
                protocol="stockqa.refresh_admission_config/1.0.0",
                consumer_id="dashboard-test",
                stockwiki_code_root=str(tmp_path),
                stockwiki_workspace_root=str(tmp_path),
                iqs_code_root=str(tmp_path),
                iqs_release_root=str(tmp_path),
                execution_inputs_root=str(tmp_path),
                runs_dir=str(tmp_path),
                work_store=str(work_store),
                budget_policy_file=str(budget),
            )
        ),
        encoding="utf-8",
    )
    before = work_store.read_bytes()
    code, response = _invoke(
        "quick_scan_registered_permissions_cli",
        path,
        {
            "protocol": "stockqa.registered_permissions_request/1.0.0",
            "confirmation_id": "rc2_" + "a" * 64,
            "mode": "current_permissions",
        },
    )
    assert code == 2
    assert response["error_code"] == "refresh_execution_registered_action_required"
    assert work_store.read_bytes() == before
