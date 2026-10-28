"""Synthetic prompt-only period semantics; no clients, accounts or network."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.core.models import Question
from src.providers import llm_provider as module
from src.providers.base_llm_provider import BaseLLMProvider, standard_answer_transport
from src.providers.llm_client import SearchCapabilityUnavailable
from src.providers.llm_provider import LLMProvider
from src.utils import quick_scan_work_transport as transport


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network forbidden in prompt-only tests")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("requests.sessions.Session.request", forbidden)


@pytest.fixture
def subject():
    value = LLMProvider.__new__(LLMProvider)
    value.company_name = "Synthetic issuer"
    value.entity_id = "ENT_SYNTHETIC"
    questions = [Question("Assess demand over the next three to five years.", question_id="IQS_19")]
    return value, questions


def test_full_standard_pack_defines_evidence_period_and_forward_metric_window(subject):
    provider, questions = subject
    with standard_answer_transport(True):
        prompt = provider._build_prompt("", question_pack=questions)
    for rule in (
        "Top-level period_start/period_end describe the evidence or financial coverage period",
        "confirmable on or before the information cutoff",
        "not the future forecast window, even when top-level basis=forward",
        "Unknown top-level periods stay null",
        "future forecast window only in the relevant metric's own period_start/period_end with basis=forward",
        "qualitative future windows in summary",
        "Do not invent a numeric metric just to describe a forecast window",
        "Without numeric evidence, keep metrics=[]",
        "information_as_of and every evidence.published_at must be on or before the information cutoff",
    ):
        assert rule in prompt
    assert questions[0].text in prompt
    assert "metrics MUST also be []" in prompt
    assert "score=null" in prompt


@pytest.mark.parametrize("standard", [False, True])
def test_single_prompt_retains_original_base_behavior(subject, standard):
    provider, _ = subject
    with standard_answer_transport(standard):
        assert provider._build_prompt(
            "Frozen single question", question_id="IQS_19"
        ) == BaseLLMProvider._build_prompt(provider, "Frozen single question", question_id="IQS_19")


def test_nonstandard_pack_is_byte_identical_to_base_and_context_restores(subject):
    provider, questions = subject
    with standard_answer_transport(False):
        before = provider._build_prompt("", question_pack=questions)
        assert before == BaseLLMProvider._build_question_pack_prompt(provider, questions)
    with standard_answer_transport(True):
        assert "direct JSON object (NOT a quoted JSON string)" in provider._build_prompt(
            "", question_pack=questions
        )
    with standard_answer_transport(False):
        assert provider._build_prompt("", question_pack=questions) == before
        assert "single-line JSON object string" in before
        assert "Top-level period_start/period_end describe" not in before
        assert "FULL-STANDARD SELF-CHECK" not in before


@pytest.mark.parametrize("standard", [False, True])
def test_real_pack_send_path_uses_mode_specific_generated_prompt(
    subject, monkeypatch, tmp_path, standard
):
    provider, questions = subject
    provider.require_search = True
    provider.api_key = "synthetic-not-a-real-key"
    provider.config_manager = SimpleNamespace(config_file=tmp_path / "synthetic.json")
    send = Mock(side_effect=SearchCapabilityUnavailable("synthetic offline stop"))
    provider.client = SimpleNamespace(
        supports_web_search=True, supports_search_json_mode=True, send_search_request=send
    )
    monkeypatch.setattr(transport, "assert_question_pack_binding", Mock())
    monkeypatch.setattr(module, "external_answer_search_mode", lambda: None)
    monkeypatch.setattr(module, "_final_answer_diagnostic_policy", lambda _: None)
    with standard_answer_transport(standard):
        expected = provider._build_prompt("", question_pack=questions)
        result = provider.search_question_pack(questions)
    send.assert_called_once()
    assert send.call_args.args[0] == expected
    assert ("Top-level period_start/period_end describe" in expected) is standard
    assert result.missing_question_ids == ("IQS_19",)


def test_full_standard_pack_self_check_keeps_every_original_member_complete(subject):
    provider, _ = subject
    questions = [
        Question("Frozen diagnostic question three.", question_id="DUPONT_03"),
        Question("Frozen diagnostic question four.", question_id="DUPONT_04"),
    ]
    with standard_answer_transport(True):
        prompt = provider._build_prompt("", question_pack=questions)
    assert "For every original question_id, emit its own complete standard answer object" in prompt
    assert "Never merge answers or omit a requested question_id" in prompt
    assert (
        "Evidence insufficient for one member still requires that member's independent status=insufficient_evidence answer with score=null"
        in prompt
    )
    assert (
        "Keep every schema-required key, including coverage and missing_fields, even for insufficient_evidence, not_applicable or search_unavailable answers"
        in prompt
    )
    assert '"question_id": "DUPONT_03"' in prompt
    assert '"question_id": "DUPONT_04"' in prompt
    assert "basis=current|trailing|normalized|stress|forward|not_applicable" in prompt


def test_full_standard_pack_self_check_states_schema_length_limits(subject):
    provider, questions = subject
    with standard_answer_transport(True):
        prompt = provider._build_prompt("", question_pack=questions)
    assert "Obey every schema maxLength before emission" in prompt
    assert (
        "standard-1 summary is at most 800 characters and coverage.reason at most 350 characters"
        in prompt
    )
    assert "If the question schema sets a stricter limit, obey the stricter limit" in prompt


def test_full_standard_pack_never_uses_unknown_as_answer_status(subject):
    provider, questions = subject
    with standard_answer_transport(True):
        prompt = provider._build_prompt("", question_pack=questions)
    assert "Use status=not_applicable only when the question is genuinely inapplicable" in prompt
    assert "use status=search_unavailable only when search is actually unavailable" in prompt
    assert (
        "Unknown means missing knowledge, never a permitted answer status: never emit status=unknown"
        in prompt
    )
    assert "unknown/insufficient_evidence answer" not in prompt
    assert "unknown or not_applicable answers" not in prompt
    assert "basis=current|trailing|normalized|stress|forward|not_applicable" in prompt
