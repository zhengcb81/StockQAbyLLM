"""Rejected native consent must not create a ledger or migrate existing work."""

from src.utils.quick_scan_owner_refresh import OwnerRefreshRejected


def test_failed_owner_inspection_does_not_initialize_or_create_ledger(
    tmp_path, monkeypatch, capsys
):
    from src.utils.quick_scan_work_decision_apply_cli import main

    ledger = tmp_path / "must-not-exist.sqlite"
    calls = []

    def rejected(self):
        raise OwnerRefreshRejected("synthetic no confirmation")

    monkeypatch.setattr(
        "src.utils.quick_scan_work_decision_authority.StockWikiWorkDecisionAuthority.inspect_confirmation",
        rejected,
    )
    monkeypatch.setattr(
        "src.utils.quick_scan_work_decision_apply_cli.QuickScanWorkStore",
        lambda *a, **kw: calls.append((a, kw)),
    )
    args = [
        "--ledger",
        str(ledger),
        "--policy",
        str(tmp_path / "policy.json"),
        "--owner-code-root",
        str(tmp_path / "sw"),
        "--owner-workspace",
        str(tmp_path / "owner"),
        "--iqs-code-root",
        str(tmp_path / "iqs"),
        "--iqs-release-root",
        str(tmp_path / "release"),
        "--consumer-id",
        "invest-quick-scan",
        "--confirmation-id",
        "rc1_" + "a" * 64,
    ]
    assert main(args) == 2
    assert calls == [] and not ledger.exists()
    assert '"dispatch_started": false' in capsys.readouterr().out
