"""Real owned SQLite quote, no reservation, conversion or network."""

import pytest

from src.utils.quick_scan_refresh_inspect import inspect_refresh
from src.utils.quick_scan_work_store import QuickScanWorkStore
from tests.unit.test_quick_scan_budget import _policy
from tests.unit.test_quick_scan_refresh_inspect import setup


def test_explicit_quote_reads_cny_from_original_policy_without_writes(tmp_path):
    ledger, request, item, old = setup(tmp_path)
    # Separate native policy identity: currencies of an existing policy may
    # never be overwritten to make an old USD ledger appear to be CNY.
    policy = _policy()
    policy["policy_id"] = "cny-test-policy"
    policy["budget"]["currency"] = "CNY"
    ledger.configure_quick_scan_budget(policy)
    request["policy_id"] = "cny-test-policy"
    before = ledger.path.read_bytes()
    with pytest.raises(ValueError, match="currency_unsupported"):
        inspect_refresh(ledger.path, request, policy=policy)
    result = inspect_refresh(ledger.path, request, policy=policy, schema_version="1.1.0")
    assert result["protocol"] == "stockqa.refresh_inspection/1.1.0"
    assert result["quote"]["currency"] == "CNY" and result["reservation_created"] is False
    assert (
        result["quote"]["max_cost_micros"]
        == ledger.get_quick_scan_budget_status("cny-test-policy")["max_cost_micros"]
    )
    assert ledger.path.read_bytes() == before
    with pytest.raises(ValueError):
        inspect_refresh(ledger.path, request, policy=policy, schema_version="9.0.0")
    assert ledger.path.read_bytes() == before
