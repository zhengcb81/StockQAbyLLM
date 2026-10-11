"""Native IPC shape faults stop before durable receipt admission; no HTTP."""

import json
import sys
from types import SimpleNamespace

import pytest

from src.utils import quick_scan_subject_owner as owner


def invoke(tmp_path, monkeypatch, receipt):
    config = dict(
        stockwiki_code_root=str(tmp_path),
        stockwiki_workspace_root=str(tmp_path / "synthetic-owner"),
        iqs_code_root=str(tmp_path / "synthetic-iqs"),
        iqs_release_root=str(tmp_path / "synthetic-release"),
        subject_key="SYNTHETIC_UNIT_SUBJECT",
        scope="entity",
        scope_id="ENT_SYNTHETIC",
        runs_dir=str(tmp_path / "ipc"),
    )
    current = dict(expected_decision_id="SYNTHETIC_UNIT_DECISION", anchor_version=1)
    calls = []

    def native_double(command, **kwargs):
        calls.append(command)
        assert command[:6] == [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            "-m",
            "stockwiki.quick_scan_subject_dispatch_cli",
        ]
        assert kwargs.get("shell", False) is False
        assert kwargs["cwd"] == config["stockwiki_code_root"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                dict(
                    protocol="stockwiki.subject_dispatch_cli/1.0.0",
                    status="registered",
                    paid_dispatch_started=False,
                    receipt=receipt,
                )
            ).encode("utf-8"),
        )

    monkeypatch.setattr(owner.subprocess, "run", native_double)
    try:
        return owner.register(config, current, {"fixture": "synthetic IPC only"}, process_env={})
    finally:
        assert len(calls) == 1


@pytest.mark.parametrize("receipt", [None, [], False, "not-an-object"])
def test_non_object_native_receipt_is_rejected_before_caller_can_persist(
    tmp_path, monkeypatch, receipt
):
    with pytest.raises(ValueError, match="subject_dispatch_owner_receipt_invalid"):
        invoke(tmp_path, monkeypatch, receipt)


def test_object_receipt_reaches_original_durable_validator_unchanged(tmp_path, monkeypatch):
    receipt = dict(fixture="synthetic shape only; not an authenticated owner receipt")
    assert invoke(tmp_path, monkeypatch, receipt) == receipt
