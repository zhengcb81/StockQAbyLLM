"""Opt-in controller validation never relaxes the native transport envelope."""

import copy
import json

import pytest

from src.providers.llm_client import LLMTransportAttemptError
from src.providers.llm_research_client import ResearchChatSession
from tests.unit.test_llm_research_client import (
    TOOLS,
    budget,
    client,
    deny_network,
    response,
    transport,
)


def answer_tools():
    tools = copy.deepcopy(TOOLS)
    tools[0]["function"]["name"] = "submit_answers"
    return tools


def answer_response(arguments="{}"):
    payload = response(tools=True, arguments=arguments)
    payload["choices"][0]["message"]["tool_calls"][0]["function"]["name"] = "submit_answers"
    return payload


def test_strict_schema_failure_retains_receipt_and_value_free_diagnostic(tmp_path, transport):
    transport[0].payloads = [
        answer_response('{"key":{"SECRET_ARGUMENT_CANARY":"PRIVATE_REASONING_CANARY"}}')
    ]
    session = ResearchChatSession(client(), system_prefix="rules", tools=answer_tools())
    with budget(tmp_path) as store:
        with pytest.raises(LLMTransportAttemptError) as caught:
            session.step(user_context="strict validation")
        error = caught.value
        assert error.attempt_receipt["http_status_code"] == 200
        assert error.attempt_receipt["usage"]["output_tokens"] == 20
        assert error.validation_diagnostic == {
            "code": "research_tool_arguments_schema_invalid",
            "validator": "type",
            "field_path": "key",
        }
        assert store.get_quick_scan_budget_status("research-test")["spent_micros"] == 1000
        assert "CANARY" not in str(error) + json.dumps(error.validation_diagnostic) + json.dumps(
            error.attempt_receipt
        )
        with pytest.raises(RuntimeError):
            session.step(user_context="no resend")
    assert len(transport[1]) == 1


def test_controller_defers_only_submit_answers_and_keeps_full_wire_schema(tmp_path, transport):
    original = answer_tools()
    transport[0].payloads = [answer_response(), response()]
    session = ResearchChatSession(
        client(), system_prefix="rules", tools=original, tool_argument_validation="controller"
    )
    with budget(tmp_path):
        result = session.step(user_context="controller will validate")
        assert result["tool_calls"][0]["arguments"] == {}
        assert result["tool_argument_validation"] == "controller"
        session.step(tool_results={"call_1": '{"status":"rejected","code":"schema_required"}'})
    assert transport[1][0]["json"]["tools"] == original
    assert (
        transport[1][1]["json"]["messages"][-1]["content"]
        == '{"status":"rejected","code":"schema_required"}'
    )
    assert len(transport[1]) == 2


def test_controller_cannot_disable_other_tool_schema(tmp_path, transport):
    transport[0].payloads = [response(tools=True, arguments="{}")]
    session = ResearchChatSession(
        client(),
        system_prefix="rules",
        tools=answer_tools() + TOOLS,
        tool_argument_validation="controller",
    )
    with budget(tmp_path):
        with pytest.raises(LLMTransportAttemptError) as caught:
            session.step(user_context="lookup remains strict")
    assert caught.value.validation_diagnostic["validator"] == "required"


@pytest.mark.parametrize("mode", ["disabled", True, None])
def test_validation_mode_is_explicit_and_closed(mode):
    with pytest.raises(ValueError, match="research_tool_argument_validation_invalid"):
        ResearchChatSession(
            client(), system_prefix="rules", tools=answer_tools(), tool_argument_validation=mode
        )


def test_controller_mode_requires_non_executing_submit_tool():
    with pytest.raises(ValueError, match="research_controller_submit_answers_required"):
        ResearchChatSession(
            client(), system_prefix="rules", tools=TOOLS, tool_argument_validation="controller"
        )


@pytest.mark.parametrize("arguments", ["[]", "null", '{"key":"A","key":"B"}', '{"key":NaN}'])
def test_controller_keeps_strict_json_object_transport(tmp_path, transport, arguments):
    transport[0].payloads = [answer_response(arguments)]
    session = ResearchChatSession(
        client(), system_prefix="rules", tools=answer_tools(), tool_argument_validation="controller"
    )
    with budget(tmp_path):
        with pytest.raises(LLMTransportAttemptError):
            session.step(user_context="invalid transport JSON")
    assert len(transport[1]) == 1


@pytest.mark.parametrize("kind", ["name", "id", "type", "role", "finish"])
def test_controller_keeps_tool_and_response_identity(tmp_path, transport, kind):
    payload = answer_response()
    message = payload["choices"][0]["message"]
    call = message["tool_calls"][0]
    if kind == "name":
        call["function"]["name"] = "execute_order"
    elif kind == "id":
        call["id"] = ""
    elif kind == "type":
        call["type"] = "other"
    elif kind == "role":
        message["role"] = "user"
    else:
        payload["choices"][0]["finish_reason"] = "length"
    transport[0].payloads = [payload]
    session = ResearchChatSession(
        client(), system_prefix="rules", tools=answer_tools(), tool_argument_validation="controller"
    )
    with budget(tmp_path):
        with pytest.raises(LLMTransportAttemptError) as caught:
            session.step(user_context="envelope invalid")
    assert caught.value.attempt_receipt["http_status_code"] == 200
    assert caught.value.validation_diagnostic["code"].startswith("research_")


def test_schema_diagnostic_does_not_copy_unrecognized_argument_keys(tmp_path, transport):
    transport[0].payloads = [
        answer_response('{"key":"A","SECRET_ARGUMENT_CANARY":"PRIVATE_REASONING_CANARY"}')
    ]
    session = ResearchChatSession(client(), system_prefix="rules", tools=answer_tools())
    with budget(tmp_path):
        with pytest.raises(LLMTransportAttemptError) as caught:
            session.step(user_context="bad extra property")
    diagnostic = caught.value.validation_diagnostic
    assert diagnostic["validator"] == "additionalProperties"
    assert diagnostic["field_path"] == ""
    assert "CANARY" not in json.dumps(diagnostic) + str(caught.value)
