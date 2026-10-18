"""Original SDK/SQLite intent joins. Synthetic fixtures, no actual owner grant."""

import pytest

from src.providers.llm_research_client import ResearchChatSession
from src.utils.quick_scan_research_intent import bind_intent, get_intent_response
from src.utils.quick_scan_work_store import QuickScanWorkStore
from src.utils.quick_scan_work_transport import bind_quick_scan_work
from tests.unit.test_llm_research_client import (
    TOOLS,
    budget,
    client,
    deny_network,
    response,
    transport,
)
from tests.unit.test_quick_scan_question_pack_store import _members
from tests.unit.test_quick_scan_question_pack_transport import _bindings, pack


def test_received_work_response_native_intent_join_prevents_second_post(tmp_path, transport):
    wire, sent = transport
    wire.payloads = [response(), response()]
    with budget(tmp_path) as store:
        member = _members(store, count=1)[0]
        for repeat in range(2):
            with bind_quick_scan_work(store, member["work_item_id"], member["lease"]), bind_intent(
                store, "a" * 64, "b" * 64, [member["work_item_id"]]
            ):
                session = ResearchChatSession(client(), system_prefix="stable", tools=TOOLS)
                if repeat:
                    with pytest.raises(ValueError, match="already_sent_inspect_original"):
                        session.step(user_context="one")
                else:
                    first = session.step(user_context="one")
        original = get_intent_response(QuickScanWorkStore(store.path), "a" * 64, "b" * 64)
        assert original["native_receipt"]["attempt_id"] == first["receipt"]["attempt_id"]
        assert original["visible_output"]["visible_output"]["content"] == first["content"]
        assert original["work_item_ids"] == [member["work_item_id"]]
        assert store.get_quick_scan_budget_status("research-test")["requests"] == 1
    assert len(sent) == 1


def test_rehashed_join_cannot_substitute_original_native_members(tmp_path, transport):
    import json
    import sqlite3

    from src.utils.quick_scan_research_intent import _encode, _hash

    transport[0].payloads = [response()]
    with budget(tmp_path) as store:
        member = _members(store, count=1)[0]
        with bind_quick_scan_work(store, member["work_item_id"], member["lease"]), bind_intent(
            store, "a" * 64, "b" * 64, [member["work_item_id"]]
        ):
            ResearchChatSession(client(), system_prefix="stable", tools=TOOLS).step(
                user_context="one"
            )
        original = get_intent_response(store, "a" * 64, "b" * 64)
        forged = {
            key: original[key]
            for key in (
                "task_sha256",
                "intent_sha256",
                "native_attempt_id",
                "request_payload_sha256",
            )
        }
        forged["work_item_ids"] = ["WORK_foreign"]
        with sqlite3.connect(store.path) as connection:
            connection.execute("DROP TRIGGER quick_scan_research_intent_no_update")
            connection.execute(
                "UPDATE quick_scan_research_intent SET work_item_ids_json=?,binding_sha256=?",
                (_encode(forged["work_item_ids"]), _hash(forged)),
            )
        with pytest.raises(ValueError, match="original_members_mismatch"):
            get_intent_response(store, "a" * 64, "b" * 64)


def test_original_pack_response_native_intent_join_survives_reopen(pack, transport, monkeypatch):
    from src.providers import llm_research_client as module

    monkeypatch.setattr(module, "_utc_now", lambda: "2027-01-15T08:00:00Z")
    store, members, _ = pack
    workids = [work_id for work_id, _lease in members]
    wire, sent = transport
    wire.payloads = [response(actual_model="mimo-v2.6-flash")]
    current = client()
    current.model = "mimo-v2.6-flash"
    current.provider_name = "mimo"
    current.base_url = "https://api.xiaomimimo.com/v1/chat/completions"
    with _bindings(pack), bind_intent(store, "a" * 64, "c" * 64, workids):
        first = ResearchChatSession(current, system_prefix="stable", tools=TOOLS).step(
            user_context="pack"
        )
    original = get_intent_response(QuickScanWorkStore(store.path), "a" * 64, "c" * 64)
    assert original["native_attempt_id"] == first["receipt"]["work_attempt_id"]
    assert sorted(original["work_item_ids"]) == sorted(workids)
    assert original["visible_output"]["provider_attempt_id"] == first["receipt"]["attempt_id"]
    assert len(sent) == 1


def test_unknown_native_intent_is_inspected_without_retry(tmp_path, transport):
    wire, sent = transport
    wire.payloads = [TimeoutError("post outcome unknown"), response()]
    with budget(tmp_path) as store:
        member = _members(store, count=1)[0]
        with bind_quick_scan_work(store, member["work_item_id"], member["lease"]), bind_intent(
            store, "a" * 64, "b" * 64, [member["work_item_id"]]
        ):
            with pytest.raises(Exception):
                ResearchChatSession(client(), system_prefix="stable", tools=TOOLS).step(
                    user_context="one"
                )
            original = get_intent_response(store, "a" * 64, "b" * 64)
            assert original is not None and original["visible_output"] is None
            with pytest.raises(ValueError, match="already_sent_inspect_original"):
                ResearchChatSession(client(), system_prefix="stable", tools=TOOLS).step(
                    user_context="one"
                )
    assert len(sent) == 1


def test_foreign_intent_work_rejected_before_native_reservation(tmp_path, transport):
    with budget(tmp_path) as store:
        member = _members(store, count=1)[0]
        with bind_quick_scan_work(store, member["work_item_id"], member["lease"]), bind_intent(
            store, "a" * 64, "b" * 64, ["WORK_foreign"]
        ):
            with pytest.raises(ValueError, match="active_work_mismatch"):
                ResearchChatSession(client(), system_prefix="stable", tools=TOOLS).step(
                    user_context="one"
                )
        assert store.get_quick_scan_budget_status("research-test")["requests"] == 0
    assert not transport[1]
