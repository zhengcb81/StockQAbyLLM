"""Quick scanning judges businesses without turning prompts into diligence checklists."""

import json

import pytest

from src.core.models import Question
from src.providers.base_llm_provider import (
    analysis_subject_scope,
    standard_answer_transport,
)
from src.providers.llm_provider import LLMProvider

ISSUER = "ENT_11111111-1111-4111-8111-111111111111"
SUBJECT = {
    "scope_kind": "standalone_issuer",
    "primary_issuer_id": ISSUER,
    "analysis_subject_id": "ASJ_22222222-2222-4222-8222-222222222222",
}


def provider():
    value = LLMProvider.__new__(LLMProvider)
    value.company_name = "Synthetic issuer"
    value.entity_id = ISSUER
    return value


@pytest.mark.parametrize("packed", [False, True])
def test_live_standard_prompts_treat_checklists_as_leads_and_only_flag_material_unknowns(packed):
    value = provider()
    question = Question(text="Frozen question: assess full-cost economics.", question_id="Q1")
    original = question.text
    with analysis_subject_scope(SUBJECT), standard_answer_transport(True):
        prompt = (
            value._build_prompt("", question_pack=[question])
            if packed
            else value._build_prompt(question.text, question_id="Q1")
        )
    assert "LIGHTWEIGHT QUICK SCAN" in prompt
    assert "model knowledge and available public web information" in prompt
    assert "research leads, not mandatory acquisition checklists" in prompt
    assert "missing_fields may be []" in prompt
    assert "only judgment-critical unknowns" in prompt
    assert "Do not require a filing download" in prompt
    assert "do not list every unverified detail" in prompt
    assert "never manufacture financial inputs or citations" in prompt
    assert "return insufficient_evidence with score=null for the affected question" not in prompt
    assert question.text == original and original in prompt
    if packed:
        encoded = prompt.split("Questions as JSON data:\n", 1)[1].split("\n\nRequirements:", 1)[0]
        assert json.loads(encoded) == [{"question_id": "Q1", "text": original}]


@pytest.mark.parametrize("packed", [False, True])
def test_new_scan_guidance_does_not_leak_into_ordinary_or_later_prompts(packed):
    value = provider()
    question = Question(text="An ordinary original question", question_id="Q1")

    def render():
        return (
            value._build_prompt("", question_pack=[question])
            if packed
            else value._build_prompt(question.text, question_id="Q1")
        )

    before = render()
    assert "LIGHTWEIGHT QUICK SCAN" not in before
    with standard_answer_transport(True):
        assert "LIGHTWEIGHT QUICK SCAN" in render()
    assert render() == before


def test_legal_issuer_scope_keeps_numeric_perimeters_but_does_not_demand_parent_accounts():
    with analysis_subject_scope(SUBJECT):
        prompt = provider()._build_prompt("Judge this business", question_id="Q1")
    assert "ISSUER_LEGAL_ONLY" in prompt
    assert "Do not attribute" in prompt
    assert "Do not require parent-only accounts" in prompt
