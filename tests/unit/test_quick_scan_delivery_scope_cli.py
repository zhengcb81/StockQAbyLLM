"""Subprocess contract for the StockQA-owned delivery read command."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit.test_quick_scan_work_store import _checkpointed_work_item

ROOT = Path(__file__).resolve().parents[2]
MODULE = "src.utils.quick_scan_delivery_scope_cli"
REQUEST = "stockqa.delivery_scope_request/1.0.0"
RESPONSE = "stockqa.delivery_scope_response/1.0.0"


def invoke(ledger, request):
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "USERPROFILE", "TEMP", "TMP"}
    }
    return subprocess.run(
        [sys.executable, "-B", "-X", "utf8", "-m", MODULE, "--ledger", str(ledger)],
        cwd=ROOT,
        input=request if isinstance(request, str) else json.dumps(request),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        env=env,
        check=False,
    )


def test_cli_reads_original_work_without_policy_or_writes(tmp_path):
    store, item, _, _ = _checkpointed_work_item(tmp_path)
    before = store.path.read_bytes()
    response = invoke(
        store.path,
        {
            "protocol": REQUEST,
            "operation": "inspect",
            "scope": {"entity_id": item["entity_id"], "question_ids": [item["question_id"]]},
        },
    )
    assert response.returncode == 0, response.stderr
    assert json.loads(response.stdout) == {
        "protocol": RESPONSE,
        "operation": "inspect",
        "status": "ok",
        "result": {
            "items": [],
            "work_items": [
                {
                    "question_id": item["question_id"],
                    "work_item_id": item["work_item_id"],
                    "generation": item["generation"],
                    "status": "result_ready",
                }
            ],
            "consumers": [],
        },
        "API_requests": 0,
        "network_calls": 0,
    }
    assert store.path.read_bytes() == before


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_version",
        "unknown_operation",
        "work_override",
        "mode_override",
        "duplicate_key",
        "no_ledger",
        "no_confirmation_export",
        "no_confirmation_receivers",
    ],
)
def test_cli_refuses_invalid_inputs_before_selecting_work(tmp_path, mutation):
    store, item, _, _ = _checkpointed_work_item(tmp_path)
    before = store.path.read_bytes()
    request = {
        "protocol": REQUEST,
        "operation": "inspect",
        "scope": {"entity_id": item["entity_id"], "question_ids": [item["question_id"]]},
    }
    ledger = store.path
    if mutation == "unknown_version":
        request["protocol"] = "stockqa.delivery_scope_request/2.0.0"
    elif mutation == "unknown_operation":
        request["operation"] = "dispatch"
    elif mutation == "work_override":
        request["scope"]["work_ids"] = [item["work_item_id"]]
    elif mutation == "mode_override":
        request["scope"]["receiver_only"] = True
    elif mutation == "duplicate_key":
        request = json.dumps(request)[:-1] + ', "operation": "export"}'
    elif mutation == "no_ledger":
        ledger = tmp_path / "unregistered.sqlite"
    else:
        request["operation"] = mutation.removeprefix("no_confirmation_")

    response = invoke(ledger, request)
    assert response.returncode == 2, response.stderr
    assert json.loads(response.stdout) == {
        "protocol": RESPONSE,
        "status": "rejected",
        "error_code": "original_delivery_export_rejected",
        "API_requests": 0,
        "network_calls": 0,
    }
    assert store.path.read_bytes() == before
    assert not (tmp_path / "unregistered.sqlite").exists()
