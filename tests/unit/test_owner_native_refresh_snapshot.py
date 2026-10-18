"""Borrowed native inspection snapshot remains read-only, no nested BEGIN."""

from src.utils.quick_scan_refresh_inspect import inspect_refresh
from tests.unit.test_quick_scan_refresh_inspect import setup


def test_native_public_quote_borrows_outer_snapshot_without_reopening_transaction(tmp_path):
    store, request, item, policy = setup(tmp_path)
    before = store.path.read_bytes()
    result = inspect_refresh(store.path, request, policy=policy)
    assert result["work_items"]["IQS_05"]["work_item_id"] == item["work_item_id"]
    assert result["API_requests"] == 0 and result["reservation_created"] is False
    assert store.path.read_bytes() == before
