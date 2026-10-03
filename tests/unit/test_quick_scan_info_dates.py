"""Decision-7a: structured information dates and per-source title/date capture.

Covers the two G1 findings that motivated 决定7a:
* F1 — quick-scan answers must carry a structured ``information_as_of``
  (collected from the model under an explicit prompt rule, validated to a
  real YYYY-MM-DD date, never fabricated and never hardcoded to null).
* F2 — search receipts must capture each cited source's title and publication
  date alongside its URL (``source_urls`` stays byte-compatible).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from src.core.models import (
    Answer,
    QABatchResult,
    QAResult,
    Question,
    _information_as_of_from_metadata,
    _published_date_from_receipt,
)
from src.providers.base_llm_provider import BaseLLMProvider
from src.providers.llm_client import (
    _extract_source_urls,
    _extract_sources,
    _parse_mimo_search_response,
    _parse_search_response,
)
from src.providers.llm_response_parser import LLMResponseParser, information_date


class _ResponseStub:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _quick_scan_payload() -> dict:
    return {
        "id": "resp_1",
        "model": "MiniMax-M3",
        "status": "completed",
        "output": [
            {
                "type": "web_search_call",
                "id": "ws_1",
                "status": "completed",
                "action": {
                    "type": "search",
                    "sources": [
                        {
                            "type": "url",
                            "url": "https://example.com/annual",
                            "title": "Annual report",
                            "published_date": "2026-03-31",
                        },
                        {"type": "url", "url": "https://example.com/untitled"},
                        {"type": "url", "url": "ftp://ignored/nope"},
                        "not-a-dict",
                    ],
                },
            },
            {
                "type": "message",
                "status": "completed",
                "content": [{"type": "output_text", "text": "scored answer after search"}],
            },
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def _batch(metadata: dict) -> QABatchResult:
    result = QAResult(
        question=Question(text="质量如何？", question_id="IQS_05"),
        answer=Answer(text="结构化描述", score=7, status="scored", metadata=metadata),
    )
    batch = QABatchResult()
    batch.add_result(result)
    return batch


def _envelope(metadata: dict) -> dict:
    return _batch(metadata).to_quick_scan_dict(
        entity_id="ENT_TEST",
        company_name="测试公司",
        provider_name="minimax",
        requested_model="MiniMax-M3",
    )


def test_prompt_requests_structured_information_as_of() -> None:
    """决定7a F1: the prompt must ASK for the date, else the model never returns it."""
    stub = SimpleNamespace(company_name="测试公司", entity_id="ENT_TEST")
    prompt = BaseLLMProvider._build_prompt(stub, "质量如何？", "IQS_05")
    assert '"information_as_of"' in prompt
    assert "YYYY-MM-DD" in prompt
    assert "not today's date" in prompt
    assert "never guess" in prompt
    # the added field must not break the JSON template shape
    assert prompt.rstrip().endswith("}")
    assert prompt.count('"description"') == 1


def test_information_date_accepts_only_real_dates() -> None:
    assert information_date("2026-06-30") == "2026-06-30"
    assert information_date(" 2026-06-30 ") == "2026-06-30"
    assert information_date("2026/06/30") is None
    assert information_date("2026-13-45") is None
    assert information_date("not-a-date") is None
    assert information_date(None) is None
    assert information_date("") is None
    assert information_date(20260630) is None


def test_parser_extracts_and_validates_information_as_of() -> None:
    parser = LLMResponseParser()

    valid = parser.parse_structured_response(
        json.dumps(
            {
                "status": "scored",
                "score": 7,
                "description": "依据充分",
                "information_as_of": "2026-06-30",
            }
        )
    )
    assert valid is not None
    assert valid.information_as_of == "2026-06-30"
    assert valid.score == 7

    malformed = parser.parse_structured_response(
        json.dumps(
            {
                "status": "scored",
                "score": 7,
                "description": "日期写错但答案仍有效",
                "information_as_of": "last week",
            }
        )
    )
    assert malformed is not None, "a malformed date must not destroy the answer"
    assert malformed.information_as_of is None, "and must never become a fabricated date"

    absent = parser.parse_structured_response(
        json.dumps({"status": "scored", "score": 6, "description": "无日期字段"})
    )
    assert absent is not None
    assert absent.information_as_of is None


def test_envelope_populates_information_dates_from_metadata() -> None:
    """决定7a F1 root cause: the envelope used to hardcode both fields to null."""
    envelope = _envelope(
        {
            "source_urls": ["https://example.com/annual"],
            "information_as_of": "2026-06-30",
            "execution": {
                "web_search_calls": [
                    {
                        "id": "ws_1",
                        "source_urls": ["https://example.com/annual"],
                        "sources": [
                            {
                                "url": "https://example.com/annual",
                                "title": "Annual report",
                                "published_date": "2026-03-31",
                            }
                        ],
                    }
                ]
            },
        }
    )
    answer = envelope["answers"]["IQS_05"]
    assert answer["information_as_of"] == "2026-06-30"
    assert answer["published_date"] == "2026-03-31"
    assert answer["source_urls"] == ["https://example.com/annual"]
    assert "information_as_of" in answer
    assert "published_date" in answer


def test_envelope_stays_null_when_dates_are_absent_or_ambiguous() -> None:
    """No fabrication: nothing in, nothing out; two conflicting dates stay null."""
    empty = _envelope({"source_urls": ["https://example.com/a"]})["answers"]["IQS_05"]
    assert empty["information_as_of"] is None
    assert empty["published_date"] is None

    ambiguous = _envelope(
        {
            "execution": {
                "web_search_calls": [
                    {"sources": [{"url": "https://a", "published_date": "2026-01-01"}]},
                    {"sources": [{"url": "https://b", "published_date": "2026-02-02"}]},
                ]
            }
        }
    )["answers"]["IQS_05"]
    assert ambiguous["published_date"] is None, "two dates are ambiguous, not a guess"
    assert ambiguous["information_as_of"] is None

    bad_metadata = _envelope({"information_as_of": "someday"})
    assert (
        bad_metadata["answers"]["IQS_05"]["information_as_of"] is None
    ), "a non-date never reaches the envelope"


def test_extract_sources_keeps_title_and_date_beside_url() -> None:
    """决定7a F2: per-source {url,title,published_date}; http(s) only."""
    sources = _extract_sources(
        [
            {
                "url": "https://example.com/a",
                "title": "标题A",
                "published_date": "2026-03-31",
            },
            {"url": "https://example.com/b", "title": "  ", "date": "2026-04-01"},
            {"url": "https://example.com/a", "title": "重复"},
            {"url": "ftp://bad/x", "title": "not http"},
            {"url": "not a url"},
            "plain string",
            None,
        ]
    )
    assert [s["url"] for s in sources] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert sources[0]["title"] == "标题A"
    assert sources[0]["published_date"] == "2026-03-31"
    assert sources[1]["title"] is None, "blank title stays null, not invented"
    assert sources[1]["published_date"] == "2026-04-01"
    assert _extract_sources("not a list") == []
    # the legacy url-only extractor is unchanged for existing receipts
    assert _extract_source_urls([{"url": "https://example.com/a"}]) == ["https://example.com/a"]


def test_responses_receipt_carries_sources_and_source_urls_unchanged() -> None:
    """决定7a F2: web_search_calls[i].sources is additive; source_urls byte-compatible."""
    parsed = _parse_search_response(
        _ResponseStub(_quick_scan_payload()),
        provider="minimax",
        requested_model="MiniMax-M3",
    )
    metadata = parsed.execution_metadata
    call = metadata["web_search_calls"][0]
    assert call["sources"] == [
        {
            "url": "https://example.com/annual",
            "title": "Annual report",
            "published_date": "2026-03-31",
        },
        {"url": "https://example.com/untitled", "title": None, "published_date": None},
    ]
    assert call["source_urls"] == [
        "https://example.com/annual",
        "https://example.com/untitled",
    ]
    assert metadata["source_urls"] == call["source_urls"]
    assert all(isinstance(u, str) for u in call["source_urls"])
    assert call["id"] and call["action_type"] == "search"


def test_mimo_receipt_carries_citation_titles() -> None:
    """The chat-completions path captures url_citation titles too."""
    payload = {
        "id": "resp_mimo",
        "model": "mimo",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": "answer text",
                    "annotations": [
                        {
                            "type": "url_citation",
                            "url": "https://example.com/issuer",
                            "title": "Issuer source",
                            "published_date": "2026-05-05",
                        },
                        {"type": "url_citation", "url": "ftp://ignored"},
                    ],
                },
            }
        ],
        "usage": {},
    }
    parsed = _parse_mimo_search_response(_ResponseStub(payload), requested_model="mimo")
    metadata = parsed.execution_metadata
    call = metadata["web_search_calls"][0]
    assert call["evidence_basis"] == "url_citation_annotations"
    assert call["sources"] == [
        {
            "url": "https://example.com/issuer",
            "title": "Issuer source",
            "published_date": "2026-05-05",
        }
    ]
    assert call["source_urls"] == ["https://example.com/issuer"]
    assert metadata["source_urls"] == ["https://example.com/issuer"]


def test_serialization_whitelist_keeps_the_new_fields() -> None:
    from src.core.models import serialize_answer_for_exchange

    serialized = serialize_answer_for_exchange(
        "IQS_05",
        {
            "status": "scored",
            "score": 7,
            "description": "d",
            "source_urls": ["https://example.com"],
            "published_date": "2026-03-31",
            "information_as_of": "2026-06-30",
            "check_level": "unverified_model_output",
        },
    )
    assert serialized["information_as_of"] == "2026-06-30"
    assert serialized["published_date"] == "2026-03-31"
    assert serialized["source_urls"] == ["https://example.com"]


def test_helper_units_match_documented_rules() -> None:
    assert _information_as_of_from_metadata({"information_as_of": "2026-01-31"}) == "2026-01-31"
    assert _information_as_of_from_metadata({"information_as_of": "bad"}) is None
    assert _information_as_of_from_metadata({}) is None
    assert _published_date_from_receipt({"execution": {"web_search_calls": []}}) is None
    assert _published_date_from_receipt({"information_as_of": "2026-01-01"}) is None


def test_envelope_drops_impossible_and_prose_dates() -> None:
    """Review hardening: calendar-invalid / prose dates never reach the envelope."""
    impossible = _envelope({"information_as_of": "2026-13-45"})["answers"]["IQS_05"]
    assert impossible["information_as_of"] is None
    prose = _envelope(
        {
            "information_as_of": "March 2026",
            "execution": {
                "web_search_calls": [
                    {"sources": [{"url": "https://a", "published_date": "March 2026"}]}
                ]
            },
        }
    )["answers"]["IQS_05"]
    assert prose["information_as_of"] is None
    assert prose["published_date"] is None, "prose dates are not aggregated"
    real = _envelope(
        {
            "information_as_of": "2026-03-31",
            "execution": {
                "web_search_calls": [
                    {"sources": [{"url": "https://a", "published_date": "2026-03-31"}]}
                ]
            },
        }
    )["answers"]["IQS_05"]
    assert real["information_as_of"] == "2026-03-31"
    assert real["published_date"] == "2026-03-31"


def test_format_repair_prompt_lists_information_as_of() -> None:
    """Review info: a repaired answer must be able to keep the date field."""
    stub = SimpleNamespace(company_name="测试公司", entity_id="ENT_TEST")
    repair = BaseLLMProvider._build_format_repair_prompt(
        stub, "原任务", '{"broken":', "IQS_05", "ENT_TEST", "测试公司"
    )
    assert "information_as_of" in repair
    assert "YYYY-MM-DD" in repair
