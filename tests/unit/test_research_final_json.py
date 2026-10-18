"""Native SDK wire mode; original metering and guards, scripted wire only."""

import pytest

from src.providers.llm_research_client import (
    ResearchChatSession,
    ResearchResponseValidationError,
)
from tests.unit.test_llm_research_client import (
    TOOLS,
    budget,
    client,
    deny_network,
    response,
    transport,
)


def test_explicit_final_json_omits_function_controls_but_keeps_metering(tmp_path, transport):
    wire, sent = transport
    wire.payloads = [response()]
    with budget(tmp_path) as store:
        result = ResearchChatSession(
            client(),
            system_prefix="Return strict answers JSON.",
            tools=[],
            response_channel="final_json",
        ).step(user_context="synthetic")
    assert (
        len(sent) == 1 and "tools" not in sent[0]["json"] and "tool_choice" not in sent[0]["json"]
    )
    assert sent[0]["json"]["thinking"] == {"type": "enabled"}
    assert result["content"] == '{"answer":42}' and result["tool_calls"] == []
    assert store.get_quick_scan_budget_status("research-test")["requests"] == 1


@pytest.mark.parametrize("tools,channel", [([], "tools"), (TOOLS, "final_json"), ([], "unknown")])
def test_mode_mismatch_does_not_silently_change_old_default(tools, channel):
    with pytest.raises(ValueError):
        ResearchChatSession(client(), system_prefix="stable", tools=tools, response_channel=channel)


def test_undeclared_provider_tool_in_final_json_cannot_become_carrier(tmp_path, transport):
    wire, sent = transport
    wire.payloads = [response(tools=True)]
    with budget(tmp_path):
        with pytest.raises(ResearchResponseValidationError):
            ResearchChatSession(
                client(), system_prefix="strict JSON", tools=[], response_channel="final_json"
            ).step(user_context="synthetic")
    assert len(sent) == 1 and "tools" not in sent[0]["json"]
