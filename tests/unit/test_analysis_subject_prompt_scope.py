"""Owner-selected legal-issuer scope reaches actual prompts, no network/models."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from src.core.models import Question
from src.providers.base_llm_provider import analysis_subject_scope
from src.providers.llm_provider import LLMProvider
from src.runners.llm_runner import _owner_analysis_scope

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


def test_single_prompt_restricts_perimeter_without_changing_question_text():
    value = provider()
    question = "A frozen, unchanged question?"
    legacy = value._build_prompt(question, question_id="IQS_01")
    with analysis_subject_scope(SUBJECT):
        scoped = value._build_prompt(question, question_id="IQS_01")
    assert (
        "ISSUER_LEGAL_ONLY" in scoped
        and "NOT verification of a consolidated reporting perimeter" in scoped
    )
    assert "insufficient_evidence with score=null" in scoped and question in scoped
    assert (
        scoped != legacy
        and hashlib.sha256(scoped.encode()).hexdigest()
        != hashlib.sha256(legacy.encode()).hexdigest()
    )
    assert value._build_prompt(question, question_id="IQS_01") == legacy


def test_pack_uses_same_owner_scope_and_preserves_all_ids():
    value = provider()
    questions = [
        Question(text="Frozen question one", question_id="IQS_01"),
        Question(text="Frozen question two", question_id="IQS_02"),
    ]
    before = [q.text for q in questions]
    with analysis_subject_scope(SUBJECT):
        prompt = value._build_prompt("", question_pack=questions)
    assert "ISSUER_LEGAL_ONLY" in prompt and "IQS_01" in prompt and "IQS_02" in prompt
    assert [q.text for q in questions] == before


def test_original_consolidated_scope_retains_identical_legacy_prompt():
    value = provider()
    legacy = value._build_prompt("Q", question_id="IQS_01")
    with analysis_subject_scope({**SUBJECT, "scope_kind": "consolidated_reporting_group"}):
        assert value._build_prompt("Q", question_id="IQS_01") == legacy


def test_scope_never_leaks_across_threads_or_exception():
    value = provider()
    legacy = value._build_prompt("Q")

    def render(scoped):
        if not scoped:
            return value._build_prompt("Q")
        with analysis_subject_scope(SUBJECT):
            return value._build_prompt("Q")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(render, [True, False] * 10))
    assert all(
        ("ISSUER_LEGAL_ONLY" in item) == (index % 2 == 0) for index, item in enumerate(results)
    )
    with pytest.raises(RuntimeError):
        with analysis_subject_scope(SUBJECT):
            raise RuntimeError("synthetic exit")
    assert value._build_prompt("Q") == legacy


def test_foreign_issuer_scope_rejected_before_any_provider_call():
    value = provider()
    value.entity_id = "ENT_OTHER"
    with analysis_subject_scope(SUBJECT), pytest.raises(ValueError, match="issuer_mismatch"):
        value._build_prompt("Q")


def test_runner_takes_subject_from_original_native_session_not_question_metadata():
    native = SimpleNamespace(current={"snapshot": {"subject_binding": {"subject": SUBJECT}}})
    approved = SimpleNamespace(native=native)
    with _owner_analysis_scope(approved):
        assert "ISSUER_LEGAL_ONLY" in provider()._build_prompt("Q")
    with _owner_analysis_scope(None):
        assert "ISSUER_LEGAL_ONLY" not in provider()._build_prompt("Q")


@pytest.mark.parametrize("packed", [False, True])
def test_prompt_requires_comparable_unique_metrics_and_question_level_evidence(packed):
    """Both actual prompt routes preserve scope/data and explain known gaps."""
    import json

    from src.providers.base_llm_provider import standard_answer_transport

    value = provider()
    question = Question(
        text="Assess product-level full-cost unit economics; preserve this question.",
        question_id="IQS_01",
        metadata={"scope": "entity", "scope_id": ISSUER},
    )
    before = (question.text, dict(question.metadata))
    with analysis_subject_scope(SUBJECT), standard_answer_transport(True):
        prompt = (
            value._build_prompt("", question_pack=[question])
            if packed
            else value._build_prompt(question.text, question_id=question.question_id)
        )
    assert "copy the provided target entity_id, company_name and question_id EXACTLY" in prompt
    assert "character for character" in prompt and "do not add entity/company identity" in prompt
    assert "subject, generation, manifest, provenance" in prompt
    assert "retain information_as_of, period_start, period_end and evidence.published_at" in prompt
    assert "each metric_id MUST occur at most once" in prompt
    assert "even for non-currency units set currency:null" in prompt
    assert "never omit required keys just because their value is null" in prompt
    assert "distinct custom.<segment>.<metric> IDs" in prompt
    assert "Do not repeat business.segment_revenue" in prompt
    assert "group profit or cash flow" in prompt and "full-cost unit economics" in prompt
    assert "insufficient_evidence and score=null" in prompt
    assert "conditional proxy" in prompt and "unknown" in prompt
    assert "accounting perimeter, period, units/currency and source" in prompt
    assert "trend claim against the chronological numeric sequence" in prompt
    assert "dip followed by recovery" in prompt
    assert "information_as_of must cover ALL cited facts and publication dates" in prompt
    assert "search snippets alone do not prove" in prompt
    assert "ISSUER_LEGAL_ONLY" in prompt and ISSUER in prompt
    assert before == (question.text, question.metadata)
    if packed:
        raw = prompt.split("Questions as JSON data:\n", 1)[1].split("\n\nRequirements:", 1)[0]
        assert json.loads(raw) == [
            {"question_id": "IQS_01", "text": question.text, **question.metadata}
        ]
    else:
        assert question.text in prompt
        assert "latest verified cited fact/publication date" in prompt


@pytest.mark.parametrize("omit_currency", [False, True])
def test_original_metric_validator_requires_explicit_nullable_currency(omit_currency):
    """A prompt rule describes the old contract; it must never fill old answers."""
    from tests.unit.test_quick_scan_observation_context import content, validate

    answer = content()
    metric = {
        "metric_id": "custom.synthetic_scope_review",
        "value": None,
        "unit": "percent",
        "currency": None,
        "unit_detail": None,
        "period_start": None,
        "period_end": None,
        "basis": "current",
        "definition": "Synthetic non-currency measure; no company financial value.",
        "evidence_ids": [],
    }
    if omit_currency:
        del metric["currency"]
    answer["metrics"] = [metric]
    if omit_currency:
        with pytest.raises(ValueError, match="frozen standard schema"):
            validate(answer)
        assert "currency" not in answer["metrics"][0], "Never repair the original payload"
    else:
        result = validate(answer)
        assert result["metrics"][0]["currency"] is None
