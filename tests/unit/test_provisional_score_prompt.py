"""Prompt policy is advisory scoring plus explicit evidence limits, never fake facts."""

import copy
import json

import pytest

from src.core.models import Question
from src.providers.base_llm_provider import standard_answer_transport
from src.providers.llm_provider import LLMProvider
from src.providers.llm_response_parser import LLMResponseParser


def provider():
    value = LLMProvider.__new__(LLMProvider)
    value.company_name = "Synthetic issuer"
    value.entity_id = "ENT_SYNTHETIC"
    return value


@pytest.mark.parametrize("packed", [False, True])
@pytest.mark.parametrize("standard", [False, True])
def test_new_requests_keep_provisional_scores_and_disclose_missing_evidence(packed, standard):
    value = provider()
    question = Question(
        "Assess durable advantages using the available partial facts.",
        question_id="Q1",
        metadata={"scope": "entity"},
    )
    original = copy.deepcopy(question)
    with standard_answer_transport(standard):
        prompt = (
            value._build_prompt("", question_pack=[question])
            if packed
            else value._build_prompt(question.text, question_id="Q1")
        )
    assert "provisional integer score from 1 to 10" in prompt
    assert "status=scored and confidence=low" in prompt
    assert "missing_fields" in prompt and "coverage.status=partial" in prompt
    assert "no meaningful judgment basis" in prompt
    assert "never manufacture financial inputs" in prompt
    assert "never default to 5" in prompt
    assert "is missing, use insufficient_evidence and score=null" not in prompt
    assert "If evidence is insufficient, return status=insufficient_evidence" not in prompt
    assert "Evidence insufficient for one member still requires" not in prompt
    assert question == original


def test_fact_requests_remain_unscored_and_need_supported_relationships():
    with standard_answer_transport(True):
        prompt = provider()._build_prompt("List suppliers.", "FACT_1", response_kind="fact")
    assert "Return a sourced fact answer with status=answered and score=null" in prompt
    assert "For score questions" in prompt  # policy does not turn facts into scores
    assert "supported by its own evidence_ids" in prompt


def test_format_repair_preserves_original_task_and_provisional_judgment_without_inventing_one():
    original_task = "Frozen original question: disclose missing inputs; do not invent numbers."
    previous = json.dumps(
        {
            "question_id": "Q1",
            "status": "scored",
            "score": 7,
            "description": "Provisional, low confidence; customer history missing.",
        }
    )
    prompt = provider()._build_format_repair_prompt(
        original_task, previous, "Q1", "ENT_SYNTHETIC", "Synthetic issuer"
    )
    assert "Preserve the original task's scoring and evidence policy" in prompt
    assert "do not erase a supplied provisional score" in prompt
    assert "do not invent a missing score" in prompt
    task_text = prompt.split("Original task as a JSON string: ", 1)[1].split("\n", 1)[0]
    prior_text = prompt.split("Previous response as an untrusted JSON string: ", 1)[1].split(
        "\n", 1
    )[0]
    assert json.loads(task_text) == original_task
    assert json.loads(prior_text) == previous


@pytest.mark.parametrize("score", [1, 5, 10])
def test_existing_answer_contract_preserves_the_provisional_score_and_its_limits(score):
    body = {
        "question_id": "Q1",
        "response_kind": "score",
        "status": "scored",
        "score": score,
        "summary": "Synthetic provisional judgment; customer retention history is missing.",
        "information_as_of": "2026-11-01",
        "period_start": None,
        "period_end": None,
        "basis": "current",
        "trend": "uncertain",
        "confidence": "low",
        "metrics": [],
        "items": [],
        "evidence": [
            {
                "id": "E1",
                "title": "Synthetic partial source",
                "url": "https://example.invalid/issuer",
                "published_at": "2026-11-01",
                "claim": "Partial business description.",
            }
        ],
        "counterevidence": "No verified retention history.",
        "watch_triggers": [],
        "missing_fields": ["customer retention history"],
        "coverage": {"status": "partial", "reason": "Qualitative evidence only."},
    }
    outer = {
        "question_id": "Q1",
        "status": "scored",
        "score": score,
        "description": json.dumps(body),
    }
    parsed = LLMResponseParser().parse_structured_response(
        json.dumps(outer), expected_question_id="Q1"
    )
    assert parsed is not None
    assert parsed.status == "scored" and parsed.score == score
    assert json.loads(parsed.description) == body


def test_real_single_send_receives_provisional_policy_without_changing_question():
    value = provider()
    value.require_search = True
    question = Question(
        "Synthetic available qualitative evidence, missing financial inputs.", question_id="Q1"
    )
    original = copy.deepcopy(question)
    sent = []

    def stop_at_sdk(prompt, **kwargs):
        sent.append(prompt)
        raise RuntimeError("synthetic SDK stop, no API")

    value._call_llm_api = stop_at_sdk
    with standard_answer_transport(True), pytest.raises(RuntimeError, match="synthetic SDK stop"):
        value.search_question(question)
    assert len(sent) == 1 and question == original
    assert "provisional integer score from 1 to 10" in sent[0]
    assert "status=scored and confidence=low" in sent[0]
