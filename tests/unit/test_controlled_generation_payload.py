"""Actual request-body controls over synthetic native work; no HTTP."""

import copy

import pytest

from src.utils.quick_scan_work_transport import (
    bind_owner_refresh_guard,
    bind_quick_scan_question_pack,
    bind_quick_scan_work,
)
from tests.unit.test_controlled_refresh_binding import create, prepared


def payload():
    return dict(model="mimo-v2.6-flash", thinking={"type": "enabled"}, max_completion_tokens=32768)


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "thinking",
        "tokens",
        "boolean_tokens",
        "model",
        "provider",
        "no_guard",
        "single_dispatch",
    ],
)
def test_frozen_controlled_work_checks_actual_payload_before_attempt_or_budget_write(
    tmp_path, fault
):
    from src.utils.quick_scan_work_transport import (
        QuickScanWorkPersistenceError,
        assert_controlled_generation_payload,
    )

    store, _, _, _, binding, spec = prepared(tmp_path)
    item = create(store, binding, spec)
    lease = store.claim(item["work_item_id"], lease_seconds=300)
    data = payload()
    provider = "mimo"
    if fault == "thinking":
        data["thinking"] = {"type": "disabled"}
    elif fault == "tokens":
        data["max_completion_tokens"] = 8192
    elif fault == "boolean_tokens":
        data["max_completion_tokens"] = True
    elif fault == "model":
        data["model"] = "mimo-v2.6-pro"
    elif fault == "provider":
        provider = "minimax"
    before = store.path.read_bytes()

    def check():
        assert_controlled_generation_payload(
            data, provider=provider, protocol="mimo_chat_completions"
        )

    frame = (
        bind_quick_scan_work(store, item["work_item_id"], lease)
        if fault == "single_dispatch"
        else bind_quick_scan_question_pack(store, ((item["work_item_id"], lease),))
    )
    with frame:
        if fault == "no_guard":
            with pytest.raises(QuickScanWorkPersistenceError, match="controlled_generation"):
                check()
        else:
            with bind_owner_refresh_guard(lambda: None):  # explicitly fixture-only
                if fault == "none":
                    check()
                else:
                    with pytest.raises(
                        QuickScanWorkPersistenceError, match="controlled_generation"
                    ):
                        check()
    assert store.path.read_bytes() == before and store.list_attempts(item["work_item_id"]) == []


def test_unbound_legacy_payload_is_untouched():
    from src.utils.quick_scan_work_transport import assert_controlled_generation_payload

    assert_controlled_generation_payload({"legacy": True}, provider="unknown", protocol="unknown")
