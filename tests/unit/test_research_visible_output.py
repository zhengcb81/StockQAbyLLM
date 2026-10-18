"""Native SQLite durability, privacy and crash recovery (no live network)."""

import copy
import json
import sqlite3

import pytest

from src.providers.llm_research_client import (
    ResearchChatSession,
    ResearchResponseValidationError,
)
from src.utils.quick_scan_work_store import QuickScanWorkStore, WorkConflictError
from src.utils.quick_scan_work_transport import QuickScanWorkPersistenceError
from tests.unit.test_llm_research_client import (
    TOOLS,
    budget,
    client,
    deny_network,
    response,
    transport,
)


def test_budget_wire_output_survives_reopen_without_second_post(tmp_path, transport):
    wire, sent = transport
    wire.payloads = [response(tools=True)]
    with budget(tmp_path) as store:
        result = ResearchChatSession(client(), system_prefix="stable", tools=TOOLS).step(
            user_context="record A"
        )
    reopened = QuickScanWorkStore(store.path)
    saved = reopened.get_research_visible_output(result["receipt"]["work_attempt_id"])
    assert saved["visible_output"] == {
        k: result[k] for k in ("content", "tool_calls", "finish_reason")
    }
    assert saved == reopened.get_research_visible_output(result["receipt"]["attempt_id"])
    assert (
        saved["receipt_sha256"]
        == reopened.get_attempt_response(result["receipt"]["work_attempt_id"])["receipt_sha256"]
    )
    assert len(sent) == 1
    with sqlite3.connect(store.path) as conn:
        dump = "\n".join(conn.iterdump())
    assert "PRIVATE_REASONING_CANARY" not in dump
    assert "SECRET_KEY_CANARY" not in dump
    assert "reasoning_content" not in json.dumps(saved)


def test_work_bound_wire_output_atomic_native_accounting(tmp_path, transport):
    from src.utils.quick_scan_work_transport import bind_quick_scan_work
    from tests.unit.test_quick_scan_work_transport import _create_claimed_item

    wire, sent = transport
    wire.payloads = [response()]
    with budget(tmp_path) as store:
        # Use the same original SQLite, rather than a second ledger.
        from tests.unit.test_quick_scan_question_pack_store import _members

        member = _members(store, count=1)[0]
        with bind_quick_scan_work(store, member["work_item_id"], member["lease"]):
            result = ResearchChatSession(client(), system_prefix="stable", tools=TOOLS).step(
                user_context="one"
            )
    saved = QuickScanWorkStore(store.path).get_research_visible_output(
        result["receipt"]["attempt_id"]
    )
    assert saved["visible_output"]["content"] == result["content"]
    assert store.list_attempts(member["work_item_id"])[0]["phase"] == "response_available"
    assert store.get_quick_scan_budget_status("research-test")["requests"] == 1
    assert len(sent) == 1


def test_question_pack_wire_shared_output_and_mapping(pack, transport, monkeypatch):
    from src.providers import llm_research_client as research_mod
    from tests.unit.test_quick_scan_question_pack_transport import _bindings

    monkeypatch.setattr(research_mod, "_utc_now", lambda: "2027-01-15T08:00:00Z")
    wire, sent = transport
    wire.payloads = [response(actual_model="mimo-v2.6-flash")]
    c = client()
    c.model, c.provider_name, c.base_url = (
        "mimo-v2.6-flash",
        "mimo",
        "https://api.xiaomimimo.com/v1/chat/completions",
    )
    with _bindings(pack):
        result = ResearchChatSession(c, system_prefix="stable", tools=TOOLS).step(
            user_context="answer this pack"
        )
    store, members, _ = pack
    physical = result["receipt"]["work_attempt_id"]
    saved = QuickScanWorkStore(store.path).get_research_visible_output(physical)
    assert saved["visible_output"]["content"] == result["content"]
    assert store.get_research_visible_output(result["receipt"]["attempt_id"]) == saved
    assert len(result["receipt"]["question_pack_transport"]["members"]) == len(members)
    assert len(sent) == 1
    with sqlite3.connect(store.path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM quick_scan_research_visible_output").fetchone()[0]
            == 1
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM quick_scan_attempt_response").fetchone()[0]
            == len(members) + 1
        )


@pytest.mark.parametrize("kind", ["model", "length", "thinking", "schema", "reasoning_protocol"])
def test_invalid_response_usage_is_charged_without_visible_output(tmp_path, transport, kind):
    value = response()
    if kind == "model":
        value["model"] = "unapproved-model"
    elif kind == "length":
        value["choices"][0]["finish_reason"] = "length"
    elif kind == "thinking":
        value["choices"][0]["message"]["content"] = "<think>PRIVATE_REASONING_CANARY</think>answer"
    elif kind == "schema":
        value = response(tools=True, arguments='{"key":7}')
    else:
        value["choices"][0]["message"]["reasoning_content"] = {"private": "CANARY"}
    transport[0].payloads = [value]
    with budget(tmp_path) as store:
        with pytest.raises(ResearchResponseValidationError) as error:
            ResearchChatSession(client(), system_prefix="stable", tools=TOOLS).step(
                user_context="one"
            )
        assert "CANARY" not in str(error.value)
        status = store.get_quick_scan_budget_status("research-test")
        assert status["requests"] == 1 and status["unreconciled_attempts"] == 0
        ref = error.value.attempt_receipt["attempt_id"]
        assert store.get_research_visible_output(ref) is None
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM quick_scan_attempt_response").fetchone()[0] == 1
        assert (
            conn.execute("SELECT actual_cost_micros FROM quick_scan_budget_attempt").fetchone()[0]
            == 1000
        )
        assert "PRIVATE_REASONING_CANARY" not in "\n".join(conn.iterdump())
    assert len(transport[1]) == 1


def test_insert_failure_rolls_back_original_receipt_and_cost_without_repost(
    tmp_path, transport, monkeypatch
):
    import src.utils.quick_scan_research_output as mod

    transport[0].payloads = [response()]

    def fail(*args):
        raise sqlite3.OperationalError("synthetic write failure")

    monkeypatch.setattr(mod, "persist", fail)
    with budget(tmp_path) as store:
        session = ResearchChatSession(client(), system_prefix="stable", tools=TOOLS)
        with pytest.raises(QuickScanWorkPersistenceError):
            session.step(user_context="one")
        with pytest.raises(RuntimeError):
            session.step(user_context="retry")
        assert store.get_quick_scan_budget_status("research-test")["in_flight"] == 1
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM quick_scan_attempt_response").fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM quick_scan_research_visible_output").fetchone()[0]
            == 0
        )
        row = conn.execute(
            "SELECT status,actual_cost_micros,reserved_cost_micros FROM quick_scan_budget_attempt"
        ).fetchone()
        assert row == ("in_flight", None, 500000)
    assert len(transport[1]) == 1


def test_visible_output_same_duplicate_idempotent_different_rejected(tmp_path, transport):
    from src.utils.quick_scan_research_output import persist

    transport[0].payloads = [response()]
    with budget(tmp_path) as store:
        result = ResearchChatSession(client(), system_prefix="stable", tools=TOOLS).step(
            user_context="one"
        )
    ref = result["receipt"]["work_attempt_id"]
    visible = store.get_research_visible_output(ref)["visible_output"]
    with store._transaction() as conn:
        persist(store, conn, ref, visible)
    with pytest.raises(WorkConflictError):
        with store._transaction() as conn:
            persist(store, conn, ref, {**visible, "content": "different"})
    assert store.get_research_visible_output(ref)["visible_output"] == visible
    assert len(transport[1]) == 1


@pytest.mark.parametrize(
    "column,value",
    [
        ("payload_sha256", "a" * 64),
        ("request_payload_sha256", "b" * 64),
        ("actual_model", "fake-model"),
        ("receipt_sha256", "c" * 64),
        ("response_sha256", "d" * 64),
        ("payload_json", "{}"),
    ],
)
def test_corrupt_output_rejects_without_replay_or_network(tmp_path, transport, column, value):
    transport[0].payloads = [response()]
    with budget(tmp_path) as store:
        result = ResearchChatSession(client(), system_prefix="stable", tools=TOOLS).step(
            user_context="one"
        )
    with sqlite3.connect(store.path) as conn:
        conn.execute("DROP TRIGGER quick_scan_research_visible_output_no_update")
        conn.execute(f"UPDATE quick_scan_research_visible_output SET {column}=?", (value,))
    with pytest.raises(ValueError):
        store.get_research_visible_output(result["receipt"]["attempt_id"])
    assert len(transport[1]) == 1


def test_unknown_ref_and_transport_timeout_have_no_replay(tmp_path, transport):
    import requests

    transport[0].payloads = [requests.Timeout("PRIVATE_REASONING_CANARY")]
    with budget(tmp_path) as store:
        with pytest.raises(Exception):
            ResearchChatSession(client(), system_prefix="stable", tools=TOOLS).step(
                user_context="one"
            )
    assert store.get_research_visible_output("unknown") is None
    with sqlite3.connect(store.path) as conn:
        ref = conn.execute("SELECT budget_attempt_id FROM quick_scan_budget_attempt").fetchone()[0]
    assert store.get_research_visible_output(ref) is None
    assert len(transport[1]) == 1


def test_v22_migration_preserves_original_receipt_without_backfill(tmp_path, transport):
    transport[0].payloads = [response()]
    with budget(tmp_path) as store:
        result = ResearchChatSession(client(), system_prefix="stable", tools=TOOLS).step(
            user_context="one"
        )
    with sqlite3.connect(store.path) as conn:
        original = conn.execute("SELECT * FROM quick_scan_attempt_response").fetchall()
        conn.execute("DROP TABLE quick_scan_research_intent")
        conn.execute("DROP TABLE quick_scan_research_turn")
        conn.execute("DROP TABLE quick_scan_research_session")
        conn.execute("DROP TABLE quick_scan_research_visible_output")
        conn.execute("PRAGMA user_version=22")
    reopened = QuickScanWorkStore(store.path)
    assert reopened.get_research_visible_output(result["receipt"]["attempt_id"]) is None
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 23
        assert conn.execute("SELECT * FROM quick_scan_attempt_response").fetchall() == original
    assert len(transport[1]) == 1


# Fixture from the original pack authority tests: full native membership and
# owner receipt invariants, never a substitute store or fake owner bypass.
from tests.unit.test_quick_scan_question_pack_transport import pack


def test_ambiguous_sdk_id_never_guesses_between_physical_attempts(tmp_path, transport, monkeypatch):
    import uuid
    from types import SimpleNamespace

    from src.providers import llm_research_client as mod

    monkeypatch.setattr(
        mod,
        "uuid",
        SimpleNamespace(uuid4=lambda: uuid.UUID("11111111-1111-4111-8111-111111111111")),
    )
    transport[0].payloads = [response(), response()]
    with budget(tmp_path) as store:
        one = ResearchChatSession(client(), system_prefix="stable", tools=TOOLS).step(
            user_context="one"
        )
        two = ResearchChatSession(client(), system_prefix="stable", tools=TOOLS).step(
            user_context="two"
        )
    assert store.get_research_visible_output(one["receipt"]["work_attempt_id"]) is not None
    assert store.get_research_visible_output(two["receipt"]["work_attempt_id"]) is not None
    with pytest.raises(ValueError, match="ambiguous"):
        store.get_research_visible_output(one["receipt"]["attempt_id"])
    assert len(transport[1]) == 2


def test_pack_schema_failure_retains_each_native_receipt_and_one_charge(
    pack, transport, monkeypatch
):
    from src.providers import llm_research_client as mod
    from tests.unit.test_quick_scan_question_pack_transport import _bindings

    monkeypatch.setattr(mod, "_utc_now", lambda: "2027-01-15T08:00:00Z")
    transport[0].payloads = [
        response(actual_model="mimo-v2.6-flash", tools=True, arguments='{"key":99}')
    ]
    c = client()
    c.model, c.provider_name, c.base_url = (
        "mimo-v2.6-flash",
        "mimo",
        "https://api.xiaomimimo.com/v1/chat/completions",
    )
    with _bindings(pack):
        with pytest.raises(ResearchResponseValidationError) as error:
            ResearchChatSession(c, system_prefix="stable", tools=TOOLS).step(user_context="pack")
    store, members, _ = pack
    assert store.get_research_visible_output(error.value.attempt_receipt["attempt_id"]) is None
    assert store.get_quick_scan_budget_status("pack-transport")["requests"] == 1
    with sqlite3.connect(store.path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM quick_scan_attempt_response").fetchone()[0]
            == len(members) + 1
        )
        assert (
            conn.execute("SELECT actual_cost_micros FROM quick_scan_budget_attempt").fetchone()[0]
            == 30000
        )
    assert len(transport[1]) == 1


def test_expired_work_lease_retains_native_late_receipt_without_output(tmp_path, transport):
    from src.utils.quick_scan_work_transport import (
        QuickScanWorkUncertainError,
        bind_quick_scan_work,
    )
    from tests.unit.test_quick_scan_question_pack_store import _members

    clock = [1800000000.0]
    transport[0].payloads = [response()]
    with budget(tmp_path) as store:
        store.clock = lambda: clock[0]
        member = _members(store, count=1)[0]
        old_post = transport[0].post.side_effect

        def post(*args, **kwargs):
            value = old_post(*args, **kwargs)
            clock[0] += 1000
            return value

        transport[0].post.side_effect = post
        with bind_quick_scan_work(store, member["work_item_id"], member["lease"]):
            with pytest.raises(QuickScanWorkUncertainError) as error:
                ResearchChatSession(client(), system_prefix="stable", tools=TOOLS).step(
                    user_context="one"
                )
        assert store.get_research_visible_output(error.value.attempt_receipt["attempt_id"]) is None
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM quick_scan_attempt_response").fetchone()[0] == 1
        assert (
            conn.execute("SELECT actual_cost_micros FROM quick_scan_budget_attempt").fetchone()[0]
            == 1000
        )
    assert len(transport[1]) == 1


def test_echoed_endpoint_credential_is_charged_but_never_persisted(tmp_path, transport):
    value = response()
    value["choices"][0]["message"]["content"] = "echo SECRET_KEY_CANARY"
    transport[0].payloads = [value]
    with budget(tmp_path) as store:
        with pytest.raises(ResearchResponseValidationError) as error:
            ResearchChatSession(client(), system_prefix="stable", tools=TOOLS).step(
                user_context="one"
            )
        assert "SECRET_KEY_CANARY" not in str(error.value)
        assert store.get_research_visible_output(error.value.attempt_receipt["attempt_id"]) is None
    with sqlite3.connect(store.path) as conn:
        assert "SECRET_KEY_CANARY" not in "\n".join(conn.iterdump())
        assert (
            conn.execute("SELECT actual_cost_micros FROM quick_scan_budget_attempt").fetchone()[0]
            == 1000
        )


def test_pack_insert_failure_rolls_back_all_member_receipts_and_cost(pack, transport, monkeypatch):
    import src.utils.quick_scan_research_output as mod
    from src.providers import llm_research_client as research_mod
    from tests.unit.test_quick_scan_question_pack_transport import _bindings

    monkeypatch.setattr(research_mod, "_utc_now", lambda: "2027-01-15T08:00:00Z")

    def fail(*args):
        raise sqlite3.OperationalError("synthetic output write failure")

    monkeypatch.setattr(mod, "persist", fail)
    transport[0].payloads = [response(actual_model="mimo-v2.6-flash")]
    c = client()
    c.model, c.provider_name, c.base_url = (
        "mimo-v2.6-flash",
        "mimo",
        "https://api.xiaomimimo.com/v1/chat/completions",
    )
    with _bindings(pack):
        with pytest.raises(QuickScanWorkPersistenceError):
            ResearchChatSession(c, system_prefix="stable", tools=TOOLS).step(user_context="pack")
    store, members, _ = pack
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM quick_scan_attempt_response").fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM quick_scan_research_visible_output").fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM attempt WHERE phase='send_intent'").fetchone()[
            0
        ] == len(members)
        assert conn.execute(
            "SELECT status,actual_cost_micros FROM quick_scan_budget_attempt"
        ).fetchone() == ("in_flight", None)
    assert len(transport[1]) == 1


def test_work_insert_failure_rolls_back_original_response_and_cost(
    tmp_path, transport, monkeypatch
):
    import src.utils.quick_scan_research_output as mod
    from src.utils.quick_scan_work_transport import bind_quick_scan_work
    from tests.unit.test_quick_scan_question_pack_store import _members

    transport[0].payloads = [response()]

    def fail(*args):
        raise sqlite3.OperationalError("synthetic output write failure")

    monkeypatch.setattr(mod, "persist", fail)
    with budget(tmp_path) as store:
        member = _members(store, count=1)[0]
        with bind_quick_scan_work(store, member["work_item_id"], member["lease"]):
            with pytest.raises(QuickScanWorkPersistenceError):
                ResearchChatSession(client(), system_prefix="stable", tools=TOOLS).step(
                    user_context="one"
                )
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM quick_scan_attempt_response").fetchone()[0] == 0
        assert conn.execute("SELECT phase FROM attempt").fetchone()[0] == "send_intent"
        assert conn.execute(
            "SELECT status,actual_cost_micros FROM quick_scan_budget_attempt"
        ).fetchone() == ("in_flight", None)
    assert len(transport[1]) == 1


def test_bound_external_evidence_is_sent_and_projected_from_original_owner(
    tmp_path, transport, monkeypatch
):
    import hashlib

    from src.providers import llm_research_client as mod
    from tests.unit.test_quick_scan_external_context import (
        _external_answer_bindings,
        _external_answer_fixture,
    )

    wire, sent = transport
    args, context, _, _, _, model, url, _, clock = _external_answer_fixture(
        tmp_path, monkeypatch, provider="mimo", protocol="mimo_chat_completions"
    )
    monkeypatch.setattr(mod.http_client_manager, "get_metered_sync_session", lambda: wire)
    wire.payloads = [response(actual_model=model)]
    c = client()
    c.model, c.provider_name, c.base_url = model, "mimo", url
    with _external_answer_bindings(args, context, "mimo", model):
        result = ResearchChatSession(c, system_prefix="trusted", tools=TOOLS).step(
            user_context="question"
        )
    body = sent[0]["json"]
    assert context["context_sha256"] in body["messages"][-1]["content"]
    assert "Margin disclosure" in body["messages"][-1]["content"]
    assert "untrusted data" in body["messages"][0]["content"]
    actual = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    assert (
        result["receipt"]["request_payload_sha256"] == hashlib.sha256(actual.encode()).hexdigest()
    )
    ref = result["receipt"]["work_attempt_id"]
    proof = args["store"].get_external_context_use(ref)["proof"]
    assert result["execution_metadata"]["external_context_use"] == proof
    assert "external_context_use" not in result["receipt"]
    assert result["receipt"]["response_status"] == "completed"
    assert (
        args["store"].get_research_visible_output(ref)["visible_output"]["content"]
        == result["content"]
    )
    assert proof["context_sha256"] == context["context_sha256"]
    assert len(sent) == 1


@pytest.mark.parametrize("marker", ["reflection_content", "REASONING_CONTENT", "</antml:thinking>"])
def test_controller_tool_private_markers_cannot_be_durable(tmp_path, transport, marker):
    tools = copy.deepcopy(TOOLS)
    tools[0]["function"]["name"] = "submit_answers"
    arguments = (
        {marker: "PRIVATE_REASONING_CANARY"}
        if not marker.startswith("<")
        else {"key": marker + "PRIVATE_REASONING_CANARY"}
    )
    value = response(tools=True, arguments=json.dumps(arguments))
    value["choices"][0]["message"]["tool_calls"][0]["function"]["name"] = "submit_answers"
    transport[0].payloads = [value]
    with budget(tmp_path) as store:
        with pytest.raises(ResearchResponseValidationError) as error:
            ResearchChatSession(
                client(), system_prefix="stable", tools=tools, tool_argument_validation="controller"
            ).step(user_context="one")
        assert "PRIVATE_REASONING_CANARY" not in str(error.value)
        assert store.get_research_visible_output(error.value.attempt_receipt["attempt_id"]) is None
    with sqlite3.connect(store.path) as conn:
        assert "PRIVATE_REASONING_CANARY" not in "\n".join(conn.iterdump())
        assert (
            conn.execute("SELECT actual_cost_micros FROM quick_scan_budget_attempt").fetchone()[0]
            == 1000
        )


def test_rehashed_visible_payload_still_requires_original_native_receipt_digest(
    tmp_path, transport
):
    import hashlib

    from src.utils.quick_scan_research_output import visible_output_sha256

    transport[0].payloads = [response()]
    with budget(tmp_path) as store:
        result = ResearchChatSession(client(), system_prefix="stable", tools=TOOLS).step(
            user_context="one"
        )
    physical = result["receipt"]["work_attempt_id"]
    saved = store.get_research_visible_output(physical)
    original_digest = result["receipt"]["research_visible_output_sha256"]
    assert original_digest == visible_output_sha256(saved["visible_output"])
    assert (
        store.get_attempt_response(physical)["receipt"]["research_visible_output_sha256"]
        == original_digest
    )
    with sqlite3.connect(store.path) as conn:
        conn.execute("DROP TRIGGER quick_scan_research_visible_output_no_update")
        forged = copy.deepcopy(saved)
        forged["visible_output"]["content"] = "forged but rehashed visible content"
        encoded = json.dumps(
            forged, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        conn.execute(
            "UPDATE quick_scan_research_visible_output SET payload_json=?,payload_sha256=?",
            (encoded, hashlib.sha256(encoded.encode()).hexdigest()),
        )
    with pytest.raises(ValueError, match="provenance"):
        store.get_research_visible_output(physical)
    assert len(transport[1]) == 1


def test_historical_native_receipt_cannot_gain_fabricated_visible_output(
    tmp_path, transport, monkeypatch
):
    from src.utils.quick_scan_research_output import persist
    from src.utils.quick_scan_work_transport import QuickScanSendAttempt

    original = QuickScanSendAttempt.record_response

    def legacy_record(handle, *, receipt, research_visible_output=None, **kwargs):
        # Reproduce a pre-v23 real research receipt through the ORIGINAL
        # native budget outcome/SQLite path, not an unrelated protocol fixture.
        receipt.pop("research_visible_output_sha256", None)
        return original(handle, receipt=receipt, **kwargs)

    monkeypatch.setattr(QuickScanSendAttempt, "record_response", legacy_record)
    transport[0].payloads = [response()]
    with budget(tmp_path) as store:
        result = ResearchChatSession(client(), system_prefix="stable", tools=TOOLS).step(
            user_context="one"
        )
    physical = result["receipt"]["work_attempt_id"]
    original_receipt = store.get_attempt_response(physical)["receipt"]
    assert original_receipt["search_protocol"] == "deepseek_chat_completions"
    assert "research_visible_output_sha256" not in original_receipt
    with pytest.raises(WorkConflictError, match="native receipt digest"):
        with store._transaction() as conn:
            persist(
                store,
                conn,
                physical,
                {"content": "invented", "tool_calls": [], "finish_reason": "stop"},
            )
    assert store.get_research_visible_output(physical) is None
    assert len(transport[1]) == 1


def test_pack_foreign_actual_model_keeps_known_charge_receipt_and_unknown_barrier(
    pack, transport, monkeypatch
):
    from src.providers import llm_research_client as mod
    from tests.unit.test_quick_scan_question_pack_transport import _bindings

    monkeypatch.setattr(mod, "_utc_now", lambda: "2027-01-15T08:00:00Z")
    transport[0].payloads = [response(actual_model="mimo-v2.6-pro")]
    c = client()
    c.model, c.provider_name, c.base_url = (
        "mimo-v2.6-flash",
        "mimo",
        "https://api.xiaomimimo.com/v1/chat/completions",
    )
    with _bindings(pack):
        session = ResearchChatSession(c, system_prefix="stable", tools=TOOLS)
        with pytest.raises(ResearchResponseValidationError) as error:
            session.step(user_context="pack")
        with pytest.raises(RuntimeError):
            session.step(user_context="retry")
    store, members, _ = pack
    assert store.get_research_visible_output(error.value.attempt_receipt["attempt_id"]) is None
    with sqlite3.connect(store.path) as conn:
        assert (
            conn.execute("SELECT actual_cost_micros FROM quick_scan_budget_attempt").fetchone()[0]
            == 30000
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM quick_scan_attempt_response").fetchone()[0]
            == len(members) + 1
        )
        assert conn.execute("SELECT COUNT(*) FROM attempt WHERE phase='uncertain'").fetchone()[
            0
        ] == len(members)
        assert (
            conn.execute("SELECT terminal_outcome FROM quick_scan_budget_terminal").fetchone()[0]
            == "unknown"
        )
    assert len(transport[1]) == 1
