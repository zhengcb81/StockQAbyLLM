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
