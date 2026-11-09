"""Research-channel qualification and public pure answer transport contracts."""

import copy
import json
from types import SimpleNamespace

import pytest

from src.providers.llm_client import LLMClient
from src.providers.llm_research_client import ResearchChatSession
from src.runners.llm_runner import QuickScanWorkLifecycle
from src.utils.quick_scan_company_context import company_scan_capabilities


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket

    monkeypatch.setattr(
        socket, "create_connection", lambda *a, **k: pytest.fail("network forbidden")
    )
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("network forbidden"))


def test_openai_general_web_search_does_not_claim_research_channel():
    client = LLMClient(
        "SYNTHETIC", "gpt-4.1", "https://api.openai.com/v1/responses", provider_name="openai"
    )
    assert client.supports_web_search is True
    caps = company_scan_capabilities(client)
    assert caps["native_web_search"] is False
    assert caps["question_packs"] is False
    with pytest.raises(ValueError, match="native_search_endpoint_not_qualified"):
        ResearchChatSession(
            client,
            system_prefix="original",
            tools=[],
            response_channel="final_json",
            native_search=True,
        )


@pytest.mark.parametrize(
    "provider,model,url,native,external",
    [
        ("mimo", "mimo-v2.6-pro", "https://api.xiaomimimo.com/v1", True, False),
        ("mimo", "mimo-v2.6-pro", "https://api.xiaomimimo.com/v1/chat/completions", True, True),
        ("mimo", "mimo-v2.6-flash", "https://api.xiaomimimo.com/v1/chat/completions", True, True),
        ("mimo", "mimo-v2.6-pro", "https://token-plan-cn.xiaomimimo.com/v1", True, False),
        ("deepseek", "deepseek-flash", "https://api.deepseek.com/chat/completions", False, True),
        ("deepseek", "deepseek-flash", "https://api.deepseek.com/v1/chat/completions", False, True),
        ("openai", "gpt-4.1", "https://api.openai.com/v1/responses", False, False),
        (
            "unsupported",
            "mimo-v2.6-pro",
            "https://api.xiaomimimo.com/v1/chat/completions",
            False,
            False,
        ),
        ("mimo", "unknown", "https://api.xiaomimimo.com/v1/chat/completions", False, False),
        ("mimo", "mimo-v2.6-pro", "http://api.xiaomimimo.com/v1/chat/completions", False, False),
        (
            "mimo",
            "mimo-v2.6-pro",
            "https://user@api.xiaomimimo.com/v1/chat/completions",
            False,
            False,
        ),
        (
            "mimo",
            "mimo-v2.6-pro",
            "https://api.xiaomimimo.com:444/v1/chat/completions",
            False,
            False,
        ),
        (
            "mimo",
            "mimo-v2.6-pro",
            "https://api.xiaomimimo.com/v1/chat/completions?x=1",
            False,
            False,
        ),
        ("mimo", "mimo-v2.6-pro", "https://api.xiaomimimo.com/v1/unsupported", False, False),
        ("mimo", "mimo-v2.6-pro", "https://[invalid", False, False),
    ],
)
def test_qualification_and_actual_constructor_agree(provider, model, url, native, external):
    from src.providers.llm_research_client import research_channel_capabilities

    client = LLMClient("SYNTHETIC", model, url, provider_name=provider)
    before = vars(client).copy()
    caps = research_channel_capabilities(client)
    assert caps["native_final_json"] is native
    assert caps["external_final_json"] is external
    assert caps["tools"] is external
    for is_native, accepted in [(True, native), (False, external)]:
        scan = company_scan_capabilities(client, native_search=is_native)
        assert scan["question_packs"] is accepted
        assert scan["native_web_search"] is native
        assert scan["max_output_tokens"] == 32768
        kwargs = dict(
            system_prefix="original",
            tools=[],
            response_channel="final_json",
            native_search=is_native,
            max_output_tokens=32768,
        )
        if accepted:
            ResearchChatSession(client, **kwargs)
        else:
            with pytest.raises(ValueError, match="endpoint_not_qualified"):
                ResearchChatSession(client, **kwargs)
    assert vars(client) == before


def test_external_final_json_uses_original_budgeted_chat_transport(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from src.providers import llm_research_client as module
    from tests.unit.test_llm_research_client import budget, client, response

    native = client()
    sent = []

    def post(*args, **kwargs):
        sent.append(copy.deepcopy(kwargs["json"]))
        payload = response()
        reply = Mock(status_code=200, headers={"x-request-id": "synthetic"})
        reply.content = json.dumps(payload).encode()
        reply.json.return_value = payload
        return reply

    monkeypatch.setattr(
        module.http_client_manager, "get_metered_sync_session", lambda: SimpleNamespace(post=post)
    )
    session = ResearchChatSession(
        native,
        system_prefix="original questions",
        tools=[],
        response_channel="final_json",
        max_output_tokens=32768,
    )
    with budget(tmp_path):
        result = session.step(user_context="original external short context")
    assert len(sent) == 1
    assert sent[0]["max_tokens"] == 32768 and sent[0]["thinking"] == {"type": "enabled"}
    assert "tools" not in sent[0] and result["content"] == '{"answer":42}'


@pytest.mark.parametrize("summary", ["  exact original summary  ", "x" * 5000])
def test_public_transport_matches_original_parts_without_truncation_or_mutation(summary):
    from src.utils.quick_scan_observation_context import split_standard_answer_transport

    answer = dict(
        question_id="Q1", response_kind="score", summary=summary, extra={"original": [1, 2]}
    )
    text = json.dumps(answer, ensure_ascii=False)
    before = copy.deepcopy(answer)
    assert split_standard_answer_transport(text) == (summary, answer)
    lifecycle = QuickScanWorkLifecycle.__new__(QuickScanWorkLifecycle)
    lifecycle._observation_context = {}
    assert lifecycle._standard_transport(text) == split_standard_answer_transport(text)
    assert answer == before and json.loads(text) == before
    _, parsed = split_standard_answer_transport(text)
    parsed["extra"]["original"].append(3)
    assert json.loads(text) == before


@pytest.mark.parametrize("text", ["ordinary prose", '{"not_a_standard_answer":1}', "{broken}"])
def test_public_transport_preserves_nonstandard_legacy_text(text):
    from src.utils.quick_scan_observation_context import split_standard_answer_transport

    assert split_standard_answer_transport(text) == (text, None)


@pytest.mark.parametrize("summary", [None, "", "   ", 7, pytest.param("overlong", id="overlong")])
def test_public_transport_refuses_unusable_or_overlong_standard_summary(summary):
    from src.core.models import QUICK_SCAN_ANSWER_DESCRIPTION_MAX_CHARS
    from src.utils.quick_scan_observation_context import split_standard_answer_transport

    if summary == "overlong":
        summary = "x" * (QUICK_SCAN_ANSWER_DESCRIPTION_MAX_CHARS + 1)
    text = json.dumps(dict(question_id="Q1", response_kind="score", summary=summary))
    with pytest.raises(ValueError, match="summary"):
        split_standard_answer_transport(text)
    lifecycle = QuickScanWorkLifecycle.__new__(QuickScanWorkLifecycle)
    lifecycle._observation_context = None
    assert lifecycle._standard_transport(text) == (text, None)


@pytest.mark.parametrize(
    "text",
    [
        '{"question_id":"Q","response_kind":"score","summary":"a","summary":"b"}',
        '{"question_id":"Q","response_kind":"score","summary":"a","x":NaN}',
    ],
)
def test_public_transport_keeps_original_strict_json_refusals(text):
    from src.utils.quick_scan_observation_context import split_standard_answer_transport

    with pytest.raises(ValueError, match="strict JSON"):
        split_standard_answer_transport(text)


def test_public_registered_validation_keeps_immutable_inputs_without_renewing_permission():
    from src.utils.quick_scan_owner_research import OwnerResearchServices

    mapper = OwnerResearchServices.__new__(OwnerResearchServices)
    calls = []

    def original(*, check_current):
        calls.append(check_current)
        raise ValueError("original_registered_inputs_changed")

    mapper._assert_registered_inputs = original
    with pytest.raises(ValueError, match="original_registered_inputs_changed"):
        mapper.validate_registered_inputs()
    assert calls == [False]


@pytest.mark.parametrize(
    "protocol",
    ["iqs.refresh_request/1.5.0", "iqs.refresh_request/1.4.0", "iqs.refresh_request/1.3.0"],
)
@pytest.mark.parametrize("use_port", [False, True], ids=["existing-lifecycle", "task-port"])
def test_public_company_task_validation_retains_original_v15_check_only(
    monkeypatch, protocol, use_port
):
    from src.utils import quick_scan_owner_budget_continuation as original
    from src.utils.quick_scan_company_context import validate_company_scan_task
    from src.utils.quick_scan_owner_refresh import OwnerRefreshSession
    from src.utils.quick_scan_refresh_execution import ApprovedRefreshSession

    store = object()
    owner = ApprovedRefreshSession.__new__(ApprovedRefreshSession)
    owner.native = OwnerRefreshSession.__new__(OwnerRefreshSession)
    owner.native.store = store
    owner.service = SimpleNamespace(store=store)
    owner.proof = {"request": {"protocol": protocol}}
    lifecycle = QuickScanWorkLifecycle(
        store,
        entity_id="ENT_A",
        run_id="original-run",
        scan_id="original-scan",
        identity={},
        transport_managed=True,
        owner_refresh_session=owner,
    )
    source = lifecycle.company_scan_task_port() if use_port else lifecycle
    task = object()
    calls = []

    def validate(candidate_owner, candidate_task, *, search_service):
        calls.append((candidate_owner, candidate_task, search_service))
        raise ValueError("original_v15_check_failure")

    monkeypatch.setattr(original, "assert_research_task", validate)
    if protocol == "iqs.refresh_request/1.5.0":
        with pytest.raises(ValueError, match="original_v15_check_failure"):
            validate_company_scan_task(source, task)
        assert calls == [(owner, task, None)]
    else:
        assert validate_company_scan_task(source, task) is None
        assert calls == []


def test_implemented_tools_capability_keeps_original_16k_limit():
    from src.providers.llm_research_client import research_channel_capabilities
    from tests.unit.test_llm_research_client import TOOLS, client

    native = client()
    caps = research_channel_capabilities(native)
    assert caps["tools"] is True
    assert caps["tools_max_output_tokens"] == 16384
    ResearchChatSession(native, system_prefix="original", tools=TOOLS, max_output_tokens=16384)
    with pytest.raises(ValueError, match="research_limits_invalid"):
        ResearchChatSession(native, system_prefix="original", tools=TOOLS, max_output_tokens=16385)
    assert caps["final_json_max_output_tokens"] == 32768
