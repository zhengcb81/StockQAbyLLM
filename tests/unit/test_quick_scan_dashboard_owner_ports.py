"""StockQA owns delivery mutations and the read-only continuation command."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.utils.quick_scan_work_store import QuickScanWorkStore
from tests.unit.test_quick_scan_work_store import (
    _ack_for_package,
    _c06_package_for_checkpoint,
    _checkpointed_work_item,
)

ROOT = Path(__file__).resolve().parents[2]
CONTROL = "src.utils.quick_scan_delivery_control_cli"
CONTINUATION = "src.utils.quick_scan_continuation_bank_cli"


def invoke(module, arguments, value):
    env = {
        name: item
        for name, item in os.environ.items()
        if name.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "USERPROFILE", "TEMP", "TMP"}
    }
    return subprocess.run(
        [sys.executable, "-B", "-X", "utf8", "-m", module, *map(str, arguments)],
        cwd=ROOT,
        input=json.dumps(value),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=25,
        env=env,
        check=False,
    )


def test_owner_commands_bind_then_apply_the_original_ack(tmp_path):
    store, item, checkpoint, _ = _checkpointed_work_item(tmp_path)
    package = _c06_package_for_checkpoint(checkpoint)
    store.prepare_result_delivery(item["work_item_id"], package)
    ack = _ack_for_package(package)
    mapping = {ack["observation_id"]: item["work_item_id"]}
    request = {"protocol": "stockqa.delivery_control_request/1.0.0"}
    bind = invoke(
        CONTROL,
        ["--ledger", store.path],
        {
            **request,
            "operation": "bind",
            "payload": {
                "work_item_ids": mapping,
                "target": ack["consumer"],
                "source_ref": "test:original",
            },
        },
    )
    assert bind.returncode == 0, bind.stderr
    assert json.loads(bind.stdout)["status"] == "bound"
    acknowledge = {
        **request,
        "operation": "acknowledge",
        "payload": {
            "work_item_ids": mapping,
            "receipt": {"acks": [ack]},
        },
    }
    accepted = invoke(CONTROL, ["--ledger", store.path], acknowledge)
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout) == {
        "protocol": "stockqa.delivery_control_response/1.0.0",
        "operation": "acknowledge",
        "status": "delivered",
        "items": [
            {
                "work_item_id": item["work_item_id"],
                "observation_id": ack["observation_id"],
                "state": "delivered",
            }
        ],
        "API_requests": 0,
        "network_calls": 0,
    }
    before = store.path.read_bytes()
    repeated = invoke(CONTROL, ["--ledger", store.path], acknowledge)
    assert repeated.stdout == accepted.stdout and repeated.returncode == 0
    assert store.path.read_bytes() == before


@pytest.mark.parametrize(
    "fault",
    [
        "bad_version",
        "extra_field",
        "duplicate_ack",
        "duplicate_work",
        "missing_ledger",
        "empty_ledger",
    ],
)
def test_owner_mutations_reject_bad_contract_before_opening_work(tmp_path, fault):
    store, item, checkpoint, _ = _checkpointed_work_item(tmp_path)
    package = _c06_package_for_checkpoint(checkpoint)
    store.prepare_result_delivery(item["work_item_id"], package)
    ack = _ack_for_package(package)
    before = store.path.read_bytes()
    request = {
        "protocol": "stockqa.delivery_control_request/1.0.0",
        "operation": "acknowledge",
        "payload": {
            "work_item_ids": {ack["observation_id"]: item["work_item_id"]},
            "receipt": {"acks": [ack]},
        },
    }
    ledger = store.path
    if fault == "bad_version":
        request["protocol"] = "stockqa.delivery_control_request/2.0.0"
    elif fault == "extra_field":
        request["payload"]["lease_override"] = True
    elif fault == "duplicate_ack":
        request["payload"]["receipt"]["acks"].append(ack)
    elif fault == "duplicate_work":
        request["payload"]["work_item_ids"]["other_observation"] = item["work_item_id"]
    elif fault == "missing_ledger":
        ledger = tmp_path / "missing.sqlite"
    else:
        ledger = tmp_path / "empty.sqlite"
        ledger.write_bytes(b"")
    rejected = invoke(CONTROL, ["--ledger", ledger], request)
    assert rejected.returncode == 2, rejected.stderr
    result = json.loads(rejected.stdout)
    assert result["status"] == "rejected" and result["API_requests"] == result["network_calls"] == 0
    assert store.path.read_bytes() == before and not (tmp_path / "missing.sqlite").exists()
    if fault == "empty_ledger":
        assert ledger.read_bytes() == b""


@pytest.mark.parametrize("arg", ["--ledger", "--admission-config"])
def test_continuation_reads_an_existing_owned_empty_ledger(tmp_path, arg):
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    before = store.path.read_bytes()
    config = tmp_path / "admission.json"
    config.write_text(
        json.dumps(
            {"protocol": "stockqa.refresh_admission_config/1.0.0", "work_store": str(store.path)}
        ),
        encoding="utf-8",
    )
    response = invoke(
        CONTINUATION,
        [arg, config if arg == "--admission-config" else store.path],
        {
            "protocol": "stockqa.continuation_bank_request/1.0.0",
            "binding": {
                "entity_id": "ENT_TEST",
                "subject_key": "ASJ_TEST@1",
                "perimeter_sha256": "a" * 64,
                "identity_revision": 1,
                "information_cutoff": "2026-11-09",
                "scope_bindings": {"Q1": {"scope": "entity", "scope_id": "ENT_TEST"}},
            },
        },
    )
    assert response.returncode == 0, response.stderr
    assert json.loads(response.stdout) == {
        "protocol": "iqs.original_work_bank_bindings/1.0.0",
        "rows": [],
        "API_requests": 0,
        "database_writes": 0,
    }
    assert store.path.read_bytes() == before
