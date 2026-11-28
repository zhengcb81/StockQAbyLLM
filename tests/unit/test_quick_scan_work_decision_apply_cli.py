"""Exercise native consent CLI refusals without an owner checkout or ledger."""

import pytest

from src.utils.quick_scan_owner_refresh import OwnerRefreshRejected
from src.utils.quick_scan_work_decision_apply_cli import main


@pytest.fixture
def decision_invocation(tmp_path):
    ledger = tmp_path / "must-not-exist.sqlite"
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
    return ledger, args


def test_failed_owner_inspection_does_not_initialize_or_create_ledger(
    decision_invocation, monkeypatch, capsys
):
    ledger, args = decision_invocation
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

    assert main(args) == 2
    assert calls == [] and not ledger.exists()
    assert '"dispatch_started": false' in capsys.readouterr().out


def test_apply_cli_rejects_preauthorized_grant_without_opening_owner_or_ledger(
    decision_invocation, monkeypatch, capsys
):
    ledger, args = decision_invocation

    def unexpected_inspection(self):
        pytest.fail("an unknown grant option reached the owner")

    monkeypatch.setattr(
        "src.utils.quick_scan_work_decision_apply_cli.StockWikiWorkDecisionAuthority.inspect_confirmation",
        unexpected_inspection,
    )
    with pytest.raises(SystemExit) as error:
        main([*args, "--grant", "synthetic-grant"])
    assert error.value.code == 2
    assert "unrecognized arguments: --grant" in capsys.readouterr().err
    assert not ledger.exists()
