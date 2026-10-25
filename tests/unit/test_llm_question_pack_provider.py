"""Synthetic provider contract tests; not real identity, financial gold or live API evidence."""

import hashlib
import json
from dataclasses import FrozenInstanceError
from unittest.mock import Mock

import pytest

from src.core.models import Question
from src.providers.llm_client import LLMSearchResponse, LLMTransportAttemptError
from src.providers.llm_provider import LLMProvider
from src.utils import quick_scan_work_transport as transport


def _body(qid, *, status="scored", score=8):
    return {
        "question_id": qid,
        "response_kind": "score",
        "status": status,
        "score": score,
        "summary": "Synthetic contract fixture",
        "information_as_of": None,
        "period_start": None,
        "period_end": None,
        "basis": None,
        "trend": None,
        "confidence": None,
        "metrics": [],
        "items": [],
        "evidence": [],
        "counterevidence": [],
        "watch_triggers": [],
        "missing_fields": [],
        "coverage": None,
    }


def _answer(qid, *, status="scored", score=8):
    return {
        "question_id": qid,
        "entity_id": "ENT_SYNTHETIC",
        "company_name": "Synthetic Co",
        "status": status,
        "score": score,
        "information_as_of": None,
        "description": json.dumps(_body(qid, status=status, score=score)),
    }


def _response(answers, *, verified=True, raw=None):
    receipt = {
        "attempt_id": "physical-http",
        "provider": "mimo",
        "request_id": "actual-request",
        "response_id": "actual-response",
        "actual_model": "mimo-v2.6-pro",
        "search_status": "executed" if verified else "unverified",
        "search_receipt_id": "actual-response",
        "source_urls": ["https://example.org/source"],
        "usage": {"output_tokens": 1200, "reasoning_output_tokens": 800},
    }
    metadata = {
        **receipt,
        "work_transport": {"work_attempt_id": "physical-budget", "final_receipt": dict(receipt)},
        "question_pack_transport": {
            "members": [{"question_id": "CORE_01", "work_attempt_id": "forged-first-only"}]
        },
    }
    return LLMSearchResponse(
        json.dumps({"answers": answers}) if raw is None else raw,
        "actual-request",
        "actual-response",
        "mimo-v2.6-pro",
        ("https://example.org/source",),
        metadata,
    )


@pytest.fixture
def pack_fixture(tmp_path, monkeypatch):
    config = tmp_path / "synthetic-models.json"
    config.write_text(
        json.dumps(
            {
                "default_provider": "mimo",
                "providers": {
                    "mimo": {
                        "enabled": True,
                        "api_key": "",
                        "model": "mimo-v2.6-pro",
                        "base_url": "https://api.xiaomimimo.com/v1",
                        "max_retries": 3,
                        "format_repair_budget": 1,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    provider = LLMProvider(
        api_key="synthetic-key",
        provider_name="mimo",
        config_file=str(config),
        require_search=True,
        entity_id="ENT_SYNTHETIC",
        company_name="Synthetic Co",
    )
    questions = [
        Question(
            'Exact question one "quoted".',
            question_id="CORE_01",
            metadata={"anchors": {"1": "weak", "10": "strong"}, "scope": "entity"},
        ),
        Question("Exact question two.", question_id="CORE_02"),
        Question("Exact question three.", question_id="CORE_03"),
    ]
    asserted = Mock()
    projector = Mock(
        return_value={
            "pack_id": "PK_SYNTHETIC",
            "budget_attempt_id": "physical-budget",
            "execution_attempt_id": "physical-http",
            "members": [
                {
                    "question_id": q.question_id,
                    "work_item_id": f"work-{index}",
                    "work_attempt_id": f"logical-{index}",
                }
                for index, q in enumerate(questions)
            ],
        }
    )
    monkeypatch.setattr(transport, "assert_question_pack_binding", asserted, raising=False)
    monkeypatch.setattr(transport, "project_question_pack_transport", projector, raising=False)
    provider.client.send_search_request = Mock(
        return_value=_response([_answer(q.question_id) for q in questions])
    )
    provider.client.send_request = Mock()
    provider._repair_invalid_answer = Mock()
    provider.retry_strategy.get_wait_time = Mock(return_value=0)
    return provider, questions, asserted, projector


def test_question_pack_one_call_exact_prompt_and_trusted_logical_projection(pack_fixture):
    provider, questions, asserted, projector = pack_fixture
    reply = provider.search_question_pack(questions)
    assert len(reply.results) == 3
    assert reply.missing_question_ids == reply.invalid_question_ids == ()
    provider.client.send_search_request.assert_called_once()
    call = provider.client.send_search_request.call_args
    assert call.kwargs == {"thinking_enabled": True, "max_output_tokens": 8192, "json_mode": True}
    prompt = call.args[0]
    assert prompt.count("Target company name: Synthetic Co") == 1
    assert prompt.count("Target stable entity_id: ENT_SYNTHETIC") == 1
    assert '"answers"' in prompt
    assert "Questions as JSON data:" in prompt
    encoded_questions = prompt.split("Questions as JSON data:\n", 1)[1].split(
        "\n\nRequirements:", 1
    )[0]
    items = json.loads(encoded_questions)
    assert [item["question_id"] for item in items] == [q.question_id for q in questions]
    assert [item["text"] for item in items] == [q.text for q in questions]
    assert items[0]["anchors"] == questions[0].metadata["anchors"]
    assert "FULL standard answer" in prompt
    asserted.assert_called_once_with(tuple(q.question_id for q in questions), "ENT_SYNTHETIC")
    projector.assert_called_once()
    for index, result in enumerate(reply.results):
        assert result.metadata["question_id"] == questions[index].question_id
        assert (
            result.metadata["execution"]["work_transport"]["work_attempt_id"] == f"logical-{index}"
        )
        assert (
            result.metadata["execution"]["work_transport"]["final_receipt"]["attempt_id"]
            == "physical-http"
        )
        assert result.metadata["execution"]["usage"]["output_tokens"] == 1200
        assert result.metadata["request_id"] == "actual-request"
        assert result.score == 8
    assert "forged-first-only" not in str(
        [r.metadata["execution"]["work_transport"] for r in reply.results]
    )
    provider._repair_invalid_answer.assert_not_called()
    provider.client.send_request.assert_not_called()
    with pytest.raises(FrozenInstanceError):
        reply.results = ()


def test_question_pack_partial_missing_invalid_never_synthesizes_or_retries(pack_fixture):
    provider, questions, _asserted, _projector = pack_fixture
    invalid = _answer("CORE_02", status="unknown", score=5)
    provider.client.send_search_request.return_value = _response([invalid, _answer("CORE_01")])
    reply = provider.search_question_pack(questions)
    assert [r.metadata["question_id"] for r in reply.results] == ["CORE_01"]
    assert reply.invalid_question_ids == ("CORE_02",)
    assert reply.missing_question_ids == ("CORE_03",)
    provider.client.send_search_request.assert_called_once()
    provider._repair_invalid_answer.assert_not_called()


@pytest.mark.parametrize(
    "case", ["duplicate", "extra", "wrong_entity", "wrong_company", "prose", "extra_root"]
)
def test_question_pack_whole_rejection_is_not_repaired(pack_fixture, case):
    provider, questions, _asserted, _projector = pack_fixture
    answers = [_answer("CORE_01"), _answer("CORE_02"), _answer("CORE_03")]
    raw = None
    if case == "duplicate":
        answers[1] = _answer("CORE_01")
    elif case == "extra":
        answers[1] = _answer("EXTRA_01")
    elif case == "wrong_entity":
        answers[1]["entity_id"] = "ENT_OTHER"
    elif case == "wrong_company":
        answers[1]["company_name"] = "Other Co"
    elif case == "prose":
        raw = "lead-in" + json.dumps({"answers": answers})
    elif case == "extra_root":
        raw = json.dumps({"answers": answers, "accepted_ids": ["CORE_01"]})
    provider.client.send_search_request.return_value = _response(answers, raw=raw)
    reply = provider.search_question_pack(questions)
    assert reply.results == ()
    assert reply.invalid_question_ids == tuple(q.question_id for q in questions)
    assert reply.missing_question_ids == ()
    assert reply.execution_metadata["question_pack"]["answer_status"] == "unknown"
    provider.client.send_search_request.assert_called_once()
    provider._repair_invalid_answer.assert_not_called()


def test_question_pack_unverified_search_never_accepts_scored_answer(pack_fixture):
    provider, questions, _asserted, _projector = pack_fixture
    provider.client.send_search_request.return_value = _response(
        [_answer(q.question_id) for q in questions], verified=False
    )
    reply = provider.search_question_pack(questions)
    assert reply.results == ()
    assert reply.execution_metadata["question_pack"]["answer_status"] == "unknown"
    assert reply.execution_metadata["search_status"] == "unverified"
    provider.client.send_search_request.assert_called_once()


def test_question_pack_valid_unknown_remains_null(pack_fixture):
    provider, questions, _asserted, _projector = pack_fixture
    provider.client.send_search_request.return_value = _response(
        [_answer(q.question_id, status="unknown", score=None) for q in questions]
    )
    reply = provider.search_question_pack(questions)
    assert len(reply.results) == 3
    assert all(r.status == "unknown" and r.score is None for r in reply.results)


@pytest.mark.parametrize(
    "change",
    [
        "require_search",
        "missing_entity",
        "missing_company",
        "missing_id",
        "duplicate",
        "lower_id",
        "too_many",
        "empty",
        "tuple",
        "not_question",
    ],
)
def test_question_pack_bad_inputs_are_rejected_before_dispatch(pack_fixture, change):
    provider, questions, asserted, projector = pack_fixture
    if change == "require_search":
        provider.require_search = False
    elif change == "missing_entity":
        provider.entity_id = None
    elif change == "missing_company":
        provider.company_name = ""
    elif change == "missing_id":
        questions[0].question_id = None
    elif change == "duplicate":
        questions[1].question_id = "CORE_01"
    elif change == "lower_id":
        questions[0].question_id = "core_01"
    elif change == "too_many":
        questions = [Question("q", question_id=f"CORE_{i}") for i in range(31)]
    elif change == "empty":
        questions = []
    elif change == "tuple":
        questions = tuple(questions)
    elif change == "not_question":
        questions = ["q"]
    with pytest.raises(ValueError):
        provider.search_question_pack(questions)
    provider.client.send_search_request.assert_not_called()
    asserted.assert_not_called()
    projector.assert_not_called()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"thinking_enabled": 1},
        {"thinking_enabled": "true"},
        {"thinking_enabled": None},
        {"max_output_tokens": True},
        {"max_output_tokens": 0},
        {"max_output_tokens": 32769},
    ],
)
def test_question_pack_generation_validation_before_dispatch(pack_fixture, kwargs):
    provider, questions, asserted, _projector = pack_fixture
    with pytest.raises(ValueError):
        provider.search_question_pack(questions, **kwargs)
    asserted.assert_not_called()
    provider.client.send_search_request.assert_not_called()


def test_question_pack_missing_binding_and_tampered_owner_fail_closed(pack_fixture):
    provider, questions, asserted, projector = pack_fixture
    asserted.side_effect = transport.QuickScanWorkPersistenceError("no active pack binding")
    with pytest.raises(transport.QuickScanWorkPersistenceError):
        provider.search_question_pack(questions)
    provider.client.send_search_request.assert_not_called()
    asserted.side_effect = None
    projector.side_effect = transport.QuickScanWorkPersistenceError("owner differs")
    with pytest.raises(transport.QuickScanWorkPersistenceError):
        provider.search_question_pack(questions)
    provider.client.send_search_request.assert_called_once()
    provider._repair_invalid_answer.assert_not_called()


def test_question_pack_http_failure_retains_receipt_and_never_retries(pack_fixture):
    provider, questions, _asserted, _projector = pack_fixture
    receipt = {"attempt_id": "actual-failed-http", "provider": "mimo", "http_status_code": 429}
    provider.client.send_search_request.side_effect = LLMTransportAttemptError("HTTPError", receipt)
    reply = provider.search_question_pack(questions)
    assert reply.results == ()
    assert reply.execution_metadata["attempts"] == [receipt]
    assert reply.execution_metadata["failure_type"] == "HTTPError"
    assert reply.execution_metadata["question_pack"]["answer_status"] == "unknown"
    provider.client.send_search_request.assert_called_once()
    provider._repair_invalid_answer.assert_not_called()


def test_question_pack_uncertain_send_is_propagated_without_retry(pack_fixture):
    provider, questions, _asserted, _projector = pack_fixture
    provider.client.send_search_request.side_effect = transport.QuickScanWorkUncertainError(
        "unknown HTTP outcome"
    )
    with pytest.raises(transport.QuickScanWorkUncertainError):
        provider.search_question_pack(questions)
    provider.client.send_search_request.assert_called_once()


def test_question_pack_external_scope_is_rejected_before_dispatch(pack_fixture, monkeypatch):
    provider, questions, asserted, _projector = pack_fixture
    monkeypatch.setattr(
        "src.providers.llm_provider.external_answer_search_mode", lambda: "external_context_only"
    )
    with pytest.raises(transport.QuickScanWorkPersistenceError):
        provider.search_question_pack(questions)
    provider.client.send_search_request.assert_not_called()
    asserted.assert_not_called()


def test_question_pack_explicit_off_and_tokens_are_forwarded(pack_fixture):
    provider, questions, _asserted, _projector = pack_fixture
    provider.search_question_pack(questions, thinking_enabled=False, max_output_tokens=16384)
    assert provider.client.send_search_request.call_args.kwargs == {
        "thinking_enabled": False,
        "max_output_tokens": 16384,
        "json_mode": True,
    }


def test_single_generation_controls_and_repair_share_configuration(tmp_path, monkeypatch):
    config = tmp_path / "single-synthetic-models.json"
    config.write_text(
        json.dumps(
            {
                "default_provider": "mimo",
                "providers": {
                    "mimo": {
                        "enabled": True,
                        "model": "mimo-v2.6-pro",
                        "base_url": "https://api.xiaomimimo.com/v1",
                        "max_retries": 1,
                        "format_repair_budget": 1,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    provider = LLMProvider(
        api_key="synthetic-key",
        provider_name="mimo",
        config_file=str(config),
        require_search=True,
        entity_id="ENT_SYNTHETIC",
        company_name="Synthetic Co",
        thinking_enabled=True,
        max_output_tokens=8192,
    )
    bad = _response([], raw="malformed")
    good = _response([], raw=json.dumps(_answer("CORE_01")))
    provider.client.send_search_request = Mock(side_effect=[bad, good])
    results = provider.search_question(Question("Exact question", question_id="CORE_01"))
    assert len(results) == 1 and results[0].score == 8
    assert provider.client.send_search_request.call_count == 2
    assert all(
        call.kwargs == {"thinking_enabled": True, "max_output_tokens": 8192}
        for call in provider.client.send_search_request.call_args_list
    )


def test_pack_json_mode_uses_original_resolved_sdk_capability_not_config_reference(pack_fixture):
    provider, questions, _asserted, _projector = pack_fixture
    assert provider.client.supports_search_json_mode is True
    # Leave provider/config reference as mimo, but resolve the actual client
    # through another supported native endpoint. The pack must not guess.
    provider.client.provider_name = "openai"
    provider.client.base_url = "https://api.openai.com/v1/responses"
    provider.client.model = "gpt-4.1-mini"
    assert provider.client.supports_web_search is True
    assert provider.client.supports_search_json_mode is False
    provider.search_question_pack(questions)
    assert "json_mode" not in provider.client.send_search_request.call_args.kwargs
    provider.client.send_search_request.assert_called_once()
