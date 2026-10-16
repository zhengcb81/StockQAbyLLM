"""Tool continuation keeps new scheduling context separate from actual receipts."""

import json

import pytest

from src.providers.llm_research_client import ResearchChatSession
from tests.unit.test_llm_research_client import (
    TOOLS,
    budget,
)
from tests.unit.test_llm_research_client import client as make_client
from tests.unit.test_llm_research_client import (
    deny_network,
    response,
    transport,
)


def test_tool_turn_contains_new_pack_after_all_actual_tool_results(tmp_path, transport):
    wire, sent = transport
    wire.payloads = [response(tools=True), response()]
    client = make_client()
    session = ResearchChatSession(client, system_prefix="Synthetic continuation test", tools=TOOLS)
    with budget(tmp_path):
        first = session.step(user_context="Requested question IDs: IQS_01,IQS_02,IQS_04")
        second = session.step(
            tool_results={"call_1": '{"committed":["IQS_01","IQS_02","IQS_04"]}'},
            continuation_context="Requested question IDs: IQS_05,IQS_06,IQS_07",
        )
    messages = sent[1]["json"]["messages"]
    assert [m["role"] for m in messages[-2:]] == ["tool", "user"]
    assert messages[-2]["tool_call_id"] == "call_1"
    assert messages[-1]["content"] == "Requested question IDs: IQS_05,IQS_06,IQS_07"
    assert messages[-3]["reasoning_content"] == "PRIVATE_REASONING_CANARY"
    assert "PRIVATE_REASONING_CANARY" not in json.dumps([first, second])


@pytest.mark.parametrize("bad", [{}, "", 5])
def test_continuation_context_rejects_invalid_value_before_any_new_post(tmp_path, transport, bad):
    wire, sent = transport
    wire.payloads = [response(tools=True)]
    client = make_client()
    session = ResearchChatSession(client, system_prefix="Synthetic", tools=TOOLS)
    with budget(tmp_path):
        session.step(user_context="first pack")
        with pytest.raises(ValueError):
            session.step(tool_results={"call_1": "{}"}, continuation_context=bad)
    assert len(sent) == 1


@pytest.mark.parametrize("bad", ["not a pending turn", "x" * 60001], ids=["no-pending", "oversize"])
def test_continuation_requires_actual_pending_turn(tmp_path, transport, bad):
    wire, sent = transport
    session = ResearchChatSession(make_client(), system_prefix="Synthetic", tools=TOOLS)
    with budget(tmp_path):
        with pytest.raises(ValueError):
            session.step(user_context="first", continuation_context=bad)
    assert sent == []
