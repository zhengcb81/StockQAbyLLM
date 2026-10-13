"""Synthetic transport failures cannot authenticate an owner decision."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.utils.quick_scan_owner_refresh import OwnerRefreshRejected
from src.utils.quick_scan_refresh_consent import OwnerConsentEndpoint


def client(tmp_path):
    from src.utils.quick_scan_work_decision_authority import (
        StockWikiWorkDecisionAuthority,
    )

    c = dict(
        stockwiki_code_root=str(tmp_path / "sw"),
        stockwiki_workspace_root=str(tmp_path / "owner"),
        iqs_code_root=str(tmp_path / "iqs"),
        iqs_release_root=str(tmp_path / "release"),
    )
    cli = tmp_path / "sw/stockwiki/quick_scan_refresh_owner_cli.py"
    cli.parent.mkdir(parents=True)
    cli.write_text("# synthetic transport only", encoding="utf-8")
    return StockWikiWorkDecisionAuthority(
        OwnerConsentEndpoint(c),
        work_store=tmp_path / "work.sqlite",
        budget_policy_file=tmp_path / "policy.json",
        consumer_id="invest-quick-scan",
        confirmation_id="rc1_" + "a" * 64,
    )


@pytest.mark.parametrize(
    "fault",
    ["exit", "size", "duplicate", "invalid-utf8", "paid-refresh", "approved-bool", "timeout"],
)
def test_owner_transport_is_fixed_and_bad_wire_creates_no_grant(tmp_path, monkeypatch, fault):
    c = client(tmp_path)
    calls = []
    monkeypatch.setenv("ZAI_API_KEY", "synthetic-never-a-real-key")

    def completed(argv, **kw):
        calls.append((argv, kw))
        if fault == "timeout":
            raise subprocess.TimeoutExpired(argv, 80)
        raw = b"{}"
        code = 0
        if fault == "exit":
            code = 2
        if fault == "size":
            raw = b" " * (8 * 1024 * 1024 + 1)
        if fault == "duplicate":
            raw = b'{"authority":true,"authority":false}'
        if fault == "invalid-utf8":
            raw = b"\xff"
        if fault == "paid-refresh":
            raw = b'{"protocol":"stockwiki.refresh_approval_inspection/1.1.0"}'
        if fault == "approved-bool":
            raw = b'{"approved":true,"API_requests":false}'
        return SimpleNamespace(stdout=raw, stderr=b"fixture", returncode=code)

    monkeypatch.setattr("src.utils.quick_scan_work_decision_authority.subprocess.run", completed)
    with pytest.raises(OwnerRefreshRejected):
        c.authorize({"kind": "known_response_resolution"})
    argv, options = calls[0]
    assert "--inspect-work-decision" in argv and "--inspect-confirmation" not in argv
    assert "stockwiki.quick_scan_refresh_owner_cli" in argv
    assert options["cwd"] == c.owner.config["stockwiki_code_root"]
    assert "ZAI_API_KEY" not in options["env"]
    assert options.get("shell", False) is False


@pytest.mark.parametrize("bad", ["../x", "rc1_" + "A" * 64, "rc1_1", True, None])
def test_invalid_owner_id_cannot_reach_native_process(tmp_path, bad):
    from src.utils.quick_scan_work_decision_authority import (
        StockWikiWorkDecisionAuthority,
    )

    c = client(tmp_path)
    with pytest.raises((ValueError, TypeError, OwnerRefreshRejected)):
        StockWikiWorkDecisionAuthority(
            c.owner,
            work_store=c.work_store,
            budget_policy_file=c.budget_policy_file,
            consumer_id=c.consumer_id,
            confirmation_id=bad,
        )
