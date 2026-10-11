"""Behavioral tests for one shared ordered router, never financial goldens."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.core.models import Question, SearchResult
from src.providers.llm_provider import QuestionPackSearchResult
from src.utils.llm_integration import OrderedSearchProviderCascade
from src.utils.quick_scan_work_transport import (
    QuickScanBudgetDeferredError,
    QuickScanWorkUncertainError,
)


def questions():
    return [Question("Moat?", question_id="IQS_05"), Question("Cash?", question_id="IQS_06")]


def pack(*, status="complete", ids=("IQS_05", "IQS_06"), code=200, failure=None):
    receipt = {
        "attempt_id": "physical-" + str(code),
        "provider": "mimo",
        "http_status_code": code,
        "search_status": "executed",
        "response_id": "actual-response",
        "actual_model": "actual-model",
    }
    if failure:
        receipt["failure_type"] = failure
    if code == 429:
        receipt["provider_error_code"] = "quota_exceeded"
    metadata = {**receipt, "attempts": [receipt], "question_pack": {"status": status}}
    results = tuple(
        SearchResult(
            title=qid,
            snippet="Observed answer",
            status="scored",
            source="llm_api",
            score=7,
            metadata={
                "question_id": qid,
                "search_status": "executed",
                "execution": dict(metadata),
                "attempts": [receipt],
            },
        )
        for qid in ids
    )
    missing = tuple(qid for qid in ("IQS_05", "IQS_06") if qid not in ids)
    invalid = ("IQS_05", "IQS_06") if status == "rejected" else ()
    return QuestionPackSearchResult(results, missing, invalid, metadata)


def provider(name, result, *, capability=True):
    return SimpleNamespace(
        model=name + "-model",
        client=SimpleNamespace(supports_web_search=capability),
        search_question_pack=Mock(return_value=result),
        search_question=Mock(side_effect=AssertionError("no single dispatch")),
        get_provider_name=lambda: name,
    )


def cascade(first, second):
    return OrderedSearchProviderCascade(
        [first, second],
        [
            {
                "id": name,
                "provider_config_ref": name,
                "model": name + "-model",
                "quota_group": name + "-quota",
                "eligible": True,
            }
            for name in ("first", "second")
        ],
        policy_version="pack-fixture@hash",
        max_attempts_per_dispatch_round=2,
    )


def test_confirmed_quota_refusal_uses_next_route_and_keeps_physical_trace():
    first = provider("first", pack(status="transport_failed", ids=(), code=429))
    second = provider("second", pack(ids=("IQS_05",), status="partial"))
    inputs = questions()
    response = cascade(first, second).search_question_pack(
        inputs, thinking_enabled=True, max_output_tokens=4096
    )
    assert [r.metadata["question_id"] for r in response.results] == ["IQS_05"]
    assert response.missing_question_ids == ("IQS_06",)
    assert [r["http_status_code"] for r in response.results[0].metadata["attempts"]] == [429, 200]
    assert response.execution_metadata["dispatch_outcome"]["state"] == "completed"
    assert response.execution_metadata["route_trace"][-1]["route_id"] == "second"
    second.search_question_pack.assert_called_once_with(
        inputs, thinking_enabled=True, max_output_tokens=4096
    )
    first.search_question.assert_not_called()
    second.search_question.assert_not_called()


@pytest.mark.parametrize("status", ["rejected", "search_unverified", "partial"])
def test_known_response_without_valid_members_does_not_retry(status):
    first = provider("first", pack(status=status, ids=()))
    second = provider("second", pack())
    response = cascade(first, second).search_question_pack(questions())
    assert response.results == ()
    assert response.invalid_question_ids == (("IQS_05", "IQS_06") if status == "rejected" else ())
    second.search_question_pack.assert_not_called()
    assert (
        response.execution_metadata["route_trace"][-1]["decision"] == "stopped_non_fallback_error"
    )


def test_partial_member_and_low_score_stop_without_quality_fallback():
    returned = pack(ids=("IQS_05",), status="partial")
    returned.results[0].score = 1
    first, second = provider("first", returned), provider("second", pack())
    response = cascade(first, second).search_question_pack(questions())
    assert response.results[0].score == 1
    assert response.missing_question_ids == ("IQS_06",)
    second.search_question_pack.assert_not_called()


def test_budget_deferred_is_diagnostic_without_synthetic_financial_answers():
    first, second = provider("first", pack()), provider("second", pack())
    first.search_question_pack.side_effect = QuickScanBudgetDeferredError("capacity_full")
    response = cascade(first, second).search_question_pack(questions())
    assert response.results == ()
    assert response.missing_question_ids == ("IQS_05", "IQS_06")
    assert response.execution_metadata["dispatch_outcome"]["state"] == "budget_deferred"
    second.search_question_pack.assert_not_called()


def test_uncertain_transport_raises_without_route_change():
    first, second = provider("first", pack()), provider("second", pack())
    first.search_question_pack.side_effect = QuickScanWorkUncertainError("one")
    with pytest.raises(QuickScanWorkUncertainError):
        cascade(first, second).search_question_pack(questions())
    second.search_question_pack.assert_not_called()


def test_missing_pack_capability_skips_route_without_single_dispatch():
    first, second = provider("first", pack()), provider("second", pack())
    first.search_question_pack = None
    response = cascade(first, second).search_question_pack(questions())
    assert len(response.results) == 2
    assert (
        response.execution_metadata["route_trace"][0]["decision"] == "skipped_missing_question_pack"
    )
    first.search_question.assert_not_called()


@pytest.mark.parametrize(
    "value", [[], ["text"], [Question("No ID")], [Question("One", question_id="IQS_05")] * 2]
)
def test_invalid_pack_rejected_before_any_route(value):
    first, second = provider("first", pack()), provider("second", pack())
    with pytest.raises(ValueError):
        cascade(first, second).search_question_pack(value)
    first.search_question_pack.assert_not_called()
    second.search_question_pack.assert_not_called()


@pytest.mark.parametrize("value", [None, 1, "yes"])
def test_thinking_must_be_explicit_bool(value):
    first, second = provider("first", pack()), provider("second", pack())
    with pytest.raises(ValueError):
        cascade(first, second).search_question_pack(questions(), thinking_enabled=value)
    first.search_question_pack.assert_not_called()


def test_single_path_preserved_without_pack_options():
    first, second = provider("first", pack()), provider("second", pack())
    first.search_question.side_effect = None
    first.search_question.return_value = list(pack(ids=("IQS_05",)).results)
    question = questions()[0]
    result = cascade(first, second).search_question(question)
    assert result[0].score == 7
    first.search_question.assert_called_once_with(question)
    first.search_question_pack.assert_not_called()
