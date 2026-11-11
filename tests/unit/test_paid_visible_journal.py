"""One original durable visible journal for ordinary and shared native scans."""

import json
from unittest.mock import Mock

import pytest

from src.providers.llm_client import LLMClient
from src.utils.quick_scan_research_output import public_final_output
from src.utils.quick_scan_work_store import QuickScanWorkStore
from tests.unit.test_llm_client import _b01_thinking_response
from tests.unit.test_quick_scan_question_pack_transport import _bindings, pack


@pytest.mark.parametrize("research", [False, True])
def test_ordinary_and_shared_final_use_same_journal_without_diagnostics(
    pack, monkeypatch, research
):
    store, members, clock = pack
    monkeypatch.setattr("src.providers.llm_client._utc_now", lambda: "2027-01-15T08:00:00Z")
    body = (
        '{"answers":[{"question_id":"IQS_01","score":8,"description":"Synthetic public answer"}]}'
    )
    response = _b01_thinking_response("mimo_chat_completions", "mimo-v2.6-flash", final=body)
    wire = Mock()
    wire.post.return_value = response
    monkeypatch.setattr(
        "src.providers.llm_client.http_client_manager.get_sync_session", lambda: wire
    )
    client = LLMClient(
        "SECRET_KEY_CANARY",
        "mimo-v2.6-flash",
        "https://api.xiaomimimo.com/v1/chat/completions",
        provider_name="mimo",
    )
    with _bindings(pack):
        result = client.send_search_request("synthetic", research_final_json=research)
    # A new process/store needs only original SQLite, no optional capture directory.
    reopened = QuickScanWorkStore(store.path)
    physical = result.execution_metadata["work_transport"]["work_attempt_id"]
    saved = reopened.get_research_visible_output(physical)
    assert saved["visible_output"]["content"] == body
    assert saved == QuickScanWorkStore(store.path).get_research_visible_output(physical)
    assert "reasoning_content" not in json.dumps(saved)
    assert wire.post.call_count == 1


@pytest.mark.parametrize(
    "content",
    [
        '{"authorization":"Board authorized a buyback", "score":8}',
        '```json\n{"authorization":"Board authorized a buyback", "score":8}\n```',
        "An ordinary final response which did not follow the JSON prompt.",
    ],
)
def test_business_text_retains_independent_of_semantic_format(content):
    assert public_final_output(content)["content"] == content


@pytest.mark.parametrize(
    "content",
    [
        '{"reasoning_content":"private"}',
        '```json\n{"reasoning_content":"private"}\n```',
        '<think>private</think>{"score":8}',
        '{"answer":"SECRET_KEY_CANARY"}',
    ],
)
def test_private_or_credential_output_never_enters_journal(content):
    assert public_final_output(content, credential="SECRET_KEY_CANARY") is None


def test_visible_answer_schema_projection_relaxes_display_limits_without_mutation():
    from src.utils.quick_scan_observation_context import _visible_answer_schema

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string", "maxLength": 80},
            "question_id": {"type": "string", "maxLength": 40},
            "evidence": {
                "type": "array",
                "maxItems": 12,
                "items": {"type": "object"},
            },
        },
        "required": ["summary", "question_id", "evidence"],
    }
    frozen = json.loads(json.dumps(schema))
    projected = _visible_answer_schema(schema)
    assert schema == frozen
    assert projected["additionalProperties"] is True
    assert "maxLength" not in projected["properties"]["summary"]
    assert projected["properties"]["question_id"]["maxLength"] == 40
    assert "maxItems" not in projected["properties"]["evidence"]
    assert projected["required"] == ["summary", "question_id", "evidence"]


@pytest.mark.parametrize(
    "basis,information_future", [("forward", False), ("current", False), ("forward", True)]
)
def test_forecast_period_is_distinct_from_disclosure_cutoff(basis, information_future):
    from tests.unit.test_quick_scan_observation_context import content, validate

    body = content()
    body.update(basis=basis, period_start="2027-01-01", period_end="2027-12-31")
    if information_future:
        body["information_as_of"] = "2027-01-01"
    if basis == "forward" and not information_future:
        validate(body)
    else:
        with pytest.raises(ValueError, match="after cutoff"):
            validate(body)


@pytest.mark.parametrize("target", ["answer", "metric"])
@pytest.mark.parametrize("basis", ["current", "forward"])
@pytest.mark.parametrize(
    "start,end", [("2027-01-01", None), (None, "2027-12-31"), ("2027-12-31", "2027-01-01")]
)
def test_period_endpoints_are_checked_independently(target, basis, start, end):
    from tests.unit.test_quick_scan_observation_context import content, validate

    body = content()
    value = body
    if target == "metric":
        from tests.unit.test_quick_scan_observation_context import metric_body

        body["metrics"] = metric_body()["metrics"]
        value = body["metrics"][0]
    value.update(basis=basis, period_start=start, period_end=end)
    if basis == "forward" and not (start and end and start > end):
        validate(body)
    else:
        with pytest.raises(ValueError):
            validate(body)
