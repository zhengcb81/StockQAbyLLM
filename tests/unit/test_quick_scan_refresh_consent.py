"""Transport-negative tests use synthetic owner bytes, never authenticate them."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.utils.quick_scan_owner_refresh import OwnerRefreshClient, OwnerRefreshRejected
from src.utils.quick_scan_refresh_consent import RefreshConsentClient


def client(tmp_path):
    owner = object.__new__(OwnerRefreshClient)
    owner.config = dict(
        stockwiki_code_root=str(tmp_path / "sw"),
        stockwiki_workspace_root=str(tmp_path / "owner"),
        iqs_code_root=str(tmp_path / "iqs"),
        iqs_release_root=str(tmp_path / "release"),
    )
    owner._environment = None
    cli = tmp_path / "sw/stockwiki/quick_scan_refresh_owner_cli.py"
    cli.parent.mkdir(parents=True)
    cli.write_text("# synthetic transport fixture only", encoding="utf-8")
    return RefreshConsentClient(
        owner,
        work_store=tmp_path / "work.sqlite",
        budget_policy_file=tmp_path / "budget.json",
        consumer_id="invest-quick-scan",
    )


@pytest.mark.parametrize(
    "confirmation,mode",
    [
        ("../../override", "fresh_registration"),
        ("rc1_" + "a" * 64, "consumer-yes"),
        ("rc1_" + "A" * 64, "current_permissions"),
    ],
)
def test_bad_id_or_mode_cannot_reach_subprocess_or_assert_registration(
    tmp_path, monkeypatch, confirmation, mode
):
    c = client(tmp_path)
    calls = []
    monkeypatch.setattr(
        "src.utils.quick_scan_refresh_consent.subprocess.run", lambda *a, **kw: calls.append(a)
    )
    with pytest.raises(OwnerRefreshRejected, match="inspection_request"):
        c.inspect(confirmation, mode=mode)
    assert calls == []


@pytest.mark.parametrize(
    "kind",
    ["exit", "size", "duplicate", "invalid_utf8", "old_protocol", "consumer_bool", "timeout"],
)
def test_untrusted_or_failed_output_is_rejected_without_json_authority(tmp_path, monkeypatch, kind):
    c = client(tmp_path)
    calls = []
    monkeypatch.setenv("ZAI_API_KEY", "synthetic-not-a-key")

    def completed(command, **kw):
        calls.append((command, kw))
        if kind == "timeout":
            import subprocess

            raise subprocess.TimeoutExpired(command, 80)
        raw = b"{}"
        code = 0
        if kind == "exit":
            code = 2
        elif kind == "size":
            raw = b" " * (8 * 1024 * 1024 + 1)
        elif kind == "duplicate":
            raw = b'{"authority":true,"authority":false}'
        elif kind == "invalid_utf8":
            raw = b"\xff"
        elif kind == "old_protocol":
            raw = json.dumps(dict(protocol="stockwiki.refresh_approval_inspection/1.0.0")).encode()
        elif kind == "consumer_bool":
            raw = json.dumps(dict(confirmed=True, registered_with_executor=True)).encode()
        return SimpleNamespace(stdout=raw, stderr=b"synthetic diagnostic", returncode=code)

    monkeypatch.setattr("src.utils.quick_scan_refresh_consent.subprocess.run", completed)
    with pytest.raises(OwnerRefreshRejected):
        c.inspect("rc1_" + "a" * 64, mode="fresh_registration")
    command, options = calls[0]
    assert command[:5][-2:] == ["utf8", "-m"]
    assert (
        "stockwiki.quick_scan_refresh_owner_cli" in command and "--admission-inspection" in command
    )
    assert "ZAI_API_KEY" not in options["env"] and options.get("shell", False) is False
    assert options["cwd"] == c.owner.config["stockwiki_code_root"]
