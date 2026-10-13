"""Real temporary native stores and public CLIs; synthetic consent, no API."""

import copy
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing

import pytest

from tests.integration.test_c06_refresh_admission import setup_admission
from tests.integration.test_c06_subject_public_e2e import OWN, actual, invoke
from tests.unit.test_quick_scan_budget import _policy


@pytest.mark.parametrize("actual", ["1.2.0"], indirect=True)
def test_cny_query26_native_preview_consent_and_public_admission_replay(actual, tmp_path):
    policy = _policy(model="fixture-model")
    policy["budget"]["currency"] = "CNY"
    prepared = setup_admission(actual, tmp_path, policy=policy, refresh_version="1.1.0")
    from stockwiki.quick_scan_refresh_records import RefreshRecordStore

    record = RefreshRecordStore(prepared["paths"]).load(prepared["consent"]["preview_id"])
    assert record["request"]["protocol"] == "iqs.refresh_request/1.1.0"
    assert record["request"]["source_query"]["schema_version"] == "2.6.0"
    assert record["preview"]["protocol"] == "stockwiki.refresh_preview/1.1.0"
    assert record["preview"]["quote"]["currency"] == "CNY"
    command = [
        sys.executable,
        "-B",
        "-X",
        "utf8",
        "-m",
        "src.utils.quick_scan_refresh_admission_cli",
        "--config",
        str(prepared["deployment_file"]),
        "--confirmation-id",
        prepared["consent"]["confirmation_id"],
    ]
    receipts = []
    for label in ("cny-admission-first", "cny-admission-replay"):
        receipt = invoke(command, tmp_path, label)
        assert receipt["API_requests"] == 0 and receipt["dispatch_started"] is False
        receipts.append(receipt)
    assert receipts[0]["status"] == "accepted" and receipts[1]["status"] == "already_registered"
    assert receipts[0]["registration"] == receipts[1]["registration"]
    with closing(sqlite3.connect(prepared["ledger"].path)) as con:
        assert con.execute("SELECT COUNT(*) FROM work_item").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM attempt").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM quick_scan_budget_attempt").fetchone()[0] == 0
    # A file-side currency substitution cannot relabel the native CNY policy,
    # reuse prior consent or reserve another paid attempt.
    budget_file = tmp_path / "budget.json"
    before = budget_file.read_bytes()
    bad = copy.deepcopy(policy)
    bad["budget"]["currency"] = "USD"
    budget_file.write_text(json.dumps(bad), encoding="utf-8")
    ledger_before = prepared["ledger"].path.read_bytes()
    try:
        result = subprocess.run(command, capture_output=True, env=dict(os.environ), timeout=80)
        assert (
            result.returncode == 2
            and json.loads(result.stdout)["registered_with_executor"] is False
        )
        assert prepared["ledger"].path.read_bytes() == ledger_before
    finally:
        budget_file.write_bytes(before)
