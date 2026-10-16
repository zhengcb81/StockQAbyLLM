import copy
import json
import pickle
import sqlite3
from contextlib import contextmanager
from unittest.mock import Mock

import pytest
import requests

from src.providers.llm_client import LLMClient, LLMTransportAttemptError
from src.providers.llm_research_client import ResearchChatSession
from src.utils.quick_scan_work_store import QuickScanWorkStore
from src.utils.quick_scan_work_transport import (
    QuickScanWorkUncertainError,
    bind_quick_scan_budget,
    bind_quick_scan_route,
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "Read one record",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
                "additionalProperties": False,
            },
        },
    }
]


def response(
    *, tools=False, cache=True, actual_model="deepseek-flash", arguments='{"key":"A"}', finish=None
):
    msg = {
        "role": "assistant",
        "content": None if tools else '{"answer":42}',
        "reasoning_content": "PRIVATE_REASONING_CANARY",
    }
    if tools:
        msg["tool_calls"] = [
            {
                "type": "function",
                "id": "call_1",
                "function": {"name": "lookup", "arguments": arguments},
            }
        ]
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "completion_tokens_details": {"reasoning_tokens": 12},
    }
    if cache:
        usage.update(prompt_cache_hit_tokens=64, prompt_cache_miss_tokens=36)
    return {
        "id": "response-test",
        "model": actual_model,
        "usage": usage,
        "choices": [
            {
                "index": 0,
                "finish_reason": finish or ("tool_calls" if tools else "stop"),
                "message": msg,
            }
        ],
    }


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    import socket

    monkeypatch.setattr(
        socket, "create_connection", Mock(side_effect=AssertionError("no live network"))
    )
    monkeypatch.setattr(
        socket.socket, "connect", Mock(side_effect=AssertionError("no live network"))
    )


@pytest.fixture
def transport(monkeypatch):
    from src.providers import llm_research_client as mod

    session = Mock()
    sent = []

    def post(*args, **kwargs):
        sent.append(copy.deepcopy(kwargs))
        value = session.payloads.pop(0)
        if isinstance(value, Exception):
            raise value
        r = Mock(status_code=200, headers={"x-request-id": "req-test"})
        r.content = json.dumps(value).encode()
        r.json.return_value = copy.deepcopy(value)
        r.raise_for_status.return_value = None
        return r

    session.payloads = []
    session.post.side_effect = post
    monkeypatch.setattr(mod.http_client_manager, "get_metered_sync_session", lambda: session)
    return session, sent


@contextmanager
def budget(tmp_path, *, cost=True, cap=6, sealed_prior_unknown=False, independent_task=None):
    store = QuickScanWorkStore(tmp_path / "ledger.sqlite")
    policy = {
        "configured": True,
        "policy_id": "research-test",
        "policy_version": "v1",
        "budget": {
            "currency": "USD",
            "max_cost": 5,
            "max_requests": cap,
            "max_cost_per_attempt": 0.5,
        },
        "cost_policy": {
            "pricing_basis": "verified_rate_card",
            "pricing_ref": "test-only-price",
            "reserve_before_dispatch": True,
            "unknown_actual_cost_action": "retain_reservation_and_pause",
        },
        "dispatch": {"max_in_flight_total": 1},
        "quota_groups": [{"id": "test", "max_in_flight": 1}],
        "routes": [
            {
                "id": "research",
                "provider_config_ref": "deepseek",
                "model": "deepseek-flash",
                "quota_group": "test",
                "max_in_flight": 1,
                "eligible": True,
                "unavailable_reason": None,
            }
        ],
    }

    def resolver(receipt):
        if not cost or not receipt.get("usage"):
            return None
        return {
            "actual_cost": 0.001,
            "pricing_ref": "test-only-price",
            "source_ref": "unit-test",
        }

    if sealed_prior_unknown:
        snapshot = store.configure_quick_scan_budget(policy)["policy_snapshot_sha256"]
        store.reserve_budget_attempt(
            policy,
            budget_attempt_id="DISPATCH_prior_unknown",
            route_id="research",
            provider="deepseek",
            model_requested="deepseek-flash",
            quota_group="test",
        )
        store.record_budget_outcome(
            "DISPATCH_prior_unknown", outcome="unknown", http_status_code=None
        )
        store.seal_uncertain_budget_attempt(
            "DISPATCH_prior_unknown",
            source_task={"task_id": "prior-task", "task_sha256": "b" * 64},
            audit={
                "operator_ref": "synthetic-test-review",
                "input_sha256": "a" * 64,
                "events_sha256": "d" * 64,
                "review_reason": "Synthetic prior process stopped; no retry; fee unknown retained",
                "no_retry": True,
                "original_process_stopped": True,
                "policy_snapshot_sha256": snapshot,
                "reserved_cost_micros": 500000,
            },
        )

    with bind_quick_scan_budget(
        store, policy, cost_resolver=resolver, independent_task=independent_task
    ), bind_quick_scan_route(
        route_id="research",
        provider="deepseek",
        model_requested="deepseek-flash",
        quota_group="test",
    ):
        yield store


def client():
    return LLMClient(
        "SECRET_KEY_CANARY",
        "deepseek-flash",
        "https://api.deepseek.com/chat/completions",
        provider_name="deepseek",
        timeout=1,
    )


def test_real_tool_round_trip_retains_thinking_only_in_memory(tmp_path, transport):
    wire, sent = transport
    wire.payloads = [response(tools=True), response()]
    session = ResearchChatSession(client(), system_prefix="Stable question set", tools=TOOLS)
    with budget(tmp_path) as store:
        a = session.step(user_context="Read record A")
        assert a["tool_calls"][0]["arguments"] == {"key": "A"}
        b = session.step(tool_results={"call_1": '{"value":42}'})
        assert b["content"] == '{"answer":42}'
        assert store.get_quick_scan_budget_status("research-test")["requests"] == 2
    body = sent[1]["json"]
    assert body["messages"][2]["reasoning_content"] == "PRIVATE_REASONING_CANARY"
    assert body["messages"][3] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": '{"value":42}',
    }
    assert body["thinking"] == {"type": "enabled"}
    assert "temperature" not in body and sent[0]["allow_redirects"] is False
    assert a["receipt"]["usage"]["input_tokens"] == 36
    assert a["receipt"]["usage"]["output_tokens"] == 20  # reasoning already included
    assert "PRIVATE_REASONING_CANARY" not in json.dumps([a, b])
    assert "PRIVATE_REASONING_CANARY" not in repr(session)
    assert "SECRET_KEY_CANARY" not in repr(session)
    with sqlite3.connect(store.path) as conn:
        assert "PRIVATE_REASONING_CANARY" not in "\n".join(conn.iterdump())
    with pytest.raises(TypeError):
        pickle.dumps(session)


def test_unbudgeted_call_never_posts(transport):
    session = ResearchChatSession(client(), system_prefix="rules", tools=TOOLS)
    with pytest.raises(RuntimeError):
        session.step(user_context="test")
    assert not transport[1]


@pytest.mark.parametrize("result", [{"fake": "42"}, {}, {"call_1": "42", "fake": "43"}])
def test_only_actual_pending_tool_ids_may_be_answered(tmp_path, transport, result):
    transport[0].payloads = [response(tools=True)]
    session = ResearchChatSession(client(), system_prefix="rules", tools=TOOLS)
    with budget(tmp_path):
        session.step(user_context="read")
        with pytest.raises(ValueError):
            session.step(tool_results=result)
    assert len(transport[1]) == 1


@pytest.mark.parametrize(
    "payload",
    [
        response(actual_model="different-model"),
        response(tools=True, arguments='{"key":"A","key":"B"}'),
        response(tools=True, arguments='{"key":"A","reasoning":"PRIVATE_REASONING_CANARY"}'),
        response(finish="length"),
    ],
)
def test_semantic_failure_keeps_physical_cost_and_blocks_resend(tmp_path, transport, payload):
    transport[0].payloads = [payload]
    session = ResearchChatSession(client(), system_prefix="rules", tools=TOOLS)
    with budget(tmp_path) as store:
        with pytest.raises(LLMTransportAttemptError):
            session.step(user_context="test")
        assert store.get_quick_scan_budget_status("research-test")["requests"] == 1
        with pytest.raises(RuntimeError):
            session.step(user_context="retry")
    assert len(transport[1]) == 1


def test_missing_cache_stays_unknown_and_cannot_dispatch_again(tmp_path, transport):
    transport[0].payloads = [response(cache=False)]
    session = ResearchChatSession(client(), system_prefix="rules", tools=TOOLS)
    with budget(tmp_path) as store:
        answer = session.step(user_context="test")
        assert answer["usage_observation"]["cached_input_tokens"] is None
        assert answer["receipt"].get("usage") is None
        assert store.get_quick_scan_budget_status("research-test")["unreconciled_attempts"] == 1
        with pytest.raises(RuntimeError):
            session.step(user_context="again")
    assert len(transport[1]) == 1


def test_timeout_is_single_send_sanitized_and_not_retried(tmp_path, transport):
    transport[0].payloads = [requests.Timeout("SECRET_KEY_CANARY PRIVATE_REASONING_CANARY")]
    session = ResearchChatSession(client(), system_prefix="rules", tools=TOOLS)
    with budget(tmp_path) as store:
        with pytest.raises(Exception) as error:
            session.step(user_context="test")
        assert "CANARY" not in str(error.value)
        assert store.get_quick_scan_budget_status("research-test")["unreconciled_attempts"] == 1
        with pytest.raises(RuntimeError):
            session.step(user_context="again")
    assert len(transport[1]) == 1


def test_budget_cap_is_enforced_before_next_http(tmp_path, transport):
    transport[0].payloads = [response(), response()]
    session = ResearchChatSession(client(), system_prefix="rules", tools=TOOLS)
    with budget(tmp_path, cap=1):
        session.step(user_context="one")
        with pytest.raises(RuntimeError):
            session.step(user_context="two")
    assert len(transport[1]) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://api.deepseek.com/chat/completions",
        "https://api.deepseek.com.evil.example/chat/completions",
        "https://api.deepseek.com/chat/completions?secret=1",
        "https://token-plan-cn.xiaomimimo.com/v1/chat/completions",
    ],
)
def test_unqualified_endpoint_rejected(url):
    c = client()
    c.base_url = url
    with pytest.raises(ValueError):
        ResearchChatSession(c, system_prefix="rules", tools=TOOLS)


def test_declared_output_is_reserved_before_http(tmp_path, transport):
    session = ResearchChatSession(
        client(),
        system_prefix="rules",
        tools=TOOLS,
        max_output_tokens=1024,
        session_output_tokens=1000,
    )
    with budget(tmp_path):
        with pytest.raises(RuntimeError):
            session.step(user_context="test")
    assert not transport[1]


def test_later_client_mutation_cannot_change_endpoint_or_model(tmp_path, transport):
    c = client()
    session = ResearchChatSession(c, system_prefix="rules", tools=TOOLS)
    c.base_url = "https://evil.example/steal"
    c.model = "another-model"
    transport[0].payloads = [response()]
    with budget(tmp_path):
        session.step(user_context="test")
    assert transport[0].post.call_args.args == ("https://api.deepseek.com/chat/completions",)
    assert transport[1][0]["json"]["model"] == "deepseek-flash"


@pytest.mark.parametrize(
    "change",
    [
        {"prompt_tokens_details": {"cached_tokens": 0}},
        {"prompt_cache_miss_tokens": 0},
        {"total_tokens": 999},
        {"completion_tokens_details": {"reasoning_tokens": True}},
    ],
)
def test_incoherent_usage_does_not_settle_or_reopen_session(tmp_path, transport, change):
    payload = response()
    payload["usage"].update(change)
    transport[0].payloads = [payload]
    session = ResearchChatSession(client(), system_prefix="rules", tools=TOOLS)
    with budget(tmp_path) as store:
        answer = session.step(user_context="test")
        assert "usage" not in answer["receipt"]
        assert store.get_quick_scan_budget_status("research-test")["unreconciled_attempts"] == 1
        with pytest.raises(RuntimeError):
            session.step(user_context="test")
    assert len(transport[1]) == 1


def test_concurrent_reentry_does_not_send(transport):
    session = ResearchChatSession(client(), system_prefix="rules", tools=TOOLS)
    session._lock.acquire()
    try:
        with pytest.raises(RuntimeError):
            session.step(user_context="test")
    finally:
        session._lock.release()
    assert not transport[1]


def test_nonobject_http_json_keeps_response_and_freezes_session(tmp_path, transport):
    transport[0].payloads = [[]]
    session = ResearchChatSession(client(), system_prefix="rules", tools=TOOLS)
    with budget(tmp_path) as store:
        with pytest.raises(LLMTransportAttemptError):
            session.step(user_context="test")
        assert store.get_quick_scan_budget_status("research-test")["requests"] == 1
        with pytest.raises(RuntimeError):
            session.step(user_context="test")
    assert len(transport[1]) == 1


def test_tool_schema_cannot_trigger_hidden_remote_fetch(transport):
    tools = copy.deepcopy(TOOLS)
    tools[0]["function"]["parameters"]["properties"]["key"] = {
        "$ref": "https://evil.example/schema"
    }
    with pytest.raises(ValueError, match="remote_schema"):
        ResearchChatSession(client(), system_prefix="rules", tools=tools)
    assert not transport[1]


def test_explicit_independent_session_continues_after_sealed_old_unknown(tmp_path, transport):
    transport[0].payloads = [response(tools=True), response()]
    session = ResearchChatSession(client(), system_prefix="rules", tools=TOOLS)
    task = {"task_id": "later-task", "task_sha256": "c" * 64}
    with budget(tmp_path, sealed_prior_unknown=True, independent_task=task) as store:
        with sqlite3.connect(store.path) as conn:
            before = conn.execute(
                "SELECT * FROM quick_scan_budget_attempt WHERE budget_attempt_id='DISPATCH_prior_unknown'"
            ).fetchone()
        session.step(user_context="read first")
        result = session.step(tool_results={"call_1": '{"value":42}'})
        assert result["content"] == '{"answer":42}'
        status = store.get_quick_scan_budget_status("research-test", include_isolation=True)
        assert status["requests"] == 3 and status["spent_micros"] == 2000
        assert status["reserved_micros"] == 500000
        assert status["unreconciled_attempts"] == 1 and status["in_flight"] == 1
        assert status["blocking_unreconciled_attempts"] == 0
        with sqlite3.connect(store.path) as conn:
            assert (
                before
                == conn.execute(
                    "SELECT * FROM quick_scan_budget_attempt WHERE budget_attempt_id='DISPATCH_prior_unknown'"
                ).fetchone()
            )
    assert len(transport[1]) == 2


def test_ordinary_research_binding_cannot_ignore_sealed_old_unknown(tmp_path, transport):
    transport[0].payloads = [response()]
    session = ResearchChatSession(client(), system_prefix="rules", tools=TOOLS)
    with budget(tmp_path, sealed_prior_unknown=True) as store:
        with pytest.raises(RuntimeError, match="budget_reconciliation_required"):
            session.step(user_context="ordinary binding")
        assert store.get_quick_scan_budget_status("research-test")["requests"] == 1
    assert not transport[1]


def test_independent_session_new_unknown_still_blocks_resend(tmp_path, transport):
    transport[0].payloads = [response(), requests.ConnectionError("synthetic connection failure")]
    session = ResearchChatSession(client(), system_prefix="rules", tools=TOOLS)
    task = {"task_id": "later-task", "task_sha256": "c" * 64}
    with budget(tmp_path, sealed_prior_unknown=True, independent_task=task) as store:
        session.step(user_context="first success")
        with pytest.raises(QuickScanWorkUncertainError):
            session.step(user_context="next becomes unknown")
        status = store.get_quick_scan_budget_status("research-test", include_isolation=True)
        assert status["requests"] == 3 and status["spent_micros"] == 1000
        assert status["reserved_micros"] == 1000000
        assert (
            status["unreconciled_attempts"] == 2 and status["blocking_unreconciled_attempts"] == 1
        )
        with pytest.raises(RuntimeError, match="inspection_or_new_admission"):
            session.step(user_context="never resend unknown")
    assert len(transport[1]) == 2
