"""Pure standard-answer URL provenance checks, using synthetic company data."""

import copy

import pytest

from src.utils.quick_scan_observation_context import (
    build_observation,
    validate_standard_answer,
)
from tests.unit.test_quick_scan_observation_context import (
    CONTEXT,
    QID,
    bound,
    checkpoint,
    content,
    normalized,
)


def validate_url(body, sources):
    return validate_standard_answer(
        body,
        metadata=CONTEXT["questions"][QID]["metadata"],
        normalized_answer=normalized(body),
        source_urls=sources,
    )


@pytest.mark.parametrize(
    "url,source",
    [
        ("https://example.invalid/report?stamp", "https://example.invalid/report?stamp="),
        ("https://example.invalid/report?stamp=", "https://example.invalid/report?stamp"),
        (
            "https://example.invalid/report?q=storage%20battery",
            "https://example.invalid/report?q=storage+battery",
        ),
        ("https://example.invalid/report?q=%41", "https://example.invalid/report?q=A"),
        ("https://example.invalid/report?doc=1#page=2", "https://example.invalid/report?doc=1"),
        ("https://example.invalid/report?doc=1", "https://example.invalid/report?doc=1#page=2"),
        (
            "https://example.invalid/report?doc=1&doc=2",
            "https://example.invalid/report?doc=1&doc=2",
        ),
    ],
)
def test_original_receipt_canonical_identity_preserves_answer_and_sources(url, source):
    body = content()
    body["evidence"][0]["url"] = url
    sources = [source]
    before_body, before_sources = copy.deepcopy(body), copy.deepcopy(sources)
    saved = validate_url(body, sources)
    assert saved == body == before_body
    assert sources == before_sources
    assert saved is not body
    saved["evidence"][0]["url"] = "https://example.invalid/different"
    assert body == before_body


@pytest.mark.parametrize(
    "query",
    [
        "doc=1&token=synthetic",
        "doc=1&ToKeN=synthetic",
        "doc=1&%74oken=synthetic",
        "doc=1&api_key=synthetic",
        "doc=1&AUTHORIZATION=synthetic",
        "doc=1&token=&doc=1",
        "doc=1&token=one&token=two",
    ],
)
@pytest.mark.parametrize("sensitive_side", ["answer", "receipt", "both"])
def test_sensitive_query_is_rejected_before_canonicalization(query, sensitive_side):
    body = content()
    clean = "https://example.invalid/report?doc=1"
    sensitive = "https://example.invalid/report?" + query
    body["evidence"][0]["url"] = sensitive if sensitive_side in {"answer", "both"} else clean
    sources = [sensitive if sensitive_side in {"receipt", "both"} else clean]
    before_body, before_sources = copy.deepcopy(body), copy.deepcopy(sources)
    with pytest.raises(ValueError):
        validate_url(body, sources)
    assert body == before_body and sources == before_sources


@pytest.mark.parametrize(
    "url,source",
    [
        ("https://foreign.invalid/report?doc=1", "https://example.invalid/report?doc=1"),
        ("https://example.invalid/other?doc=1", "https://example.invalid/report?doc=1"),
        ("https://example.invalid/report?doc=2", "https://example.invalid/report?doc=1"),
        (
            "https://example.invalid/report?doc=1&doc=2",
            "https://example.invalid/report?doc=2&doc=1",
        ),
        ("https://example.invalid/report?doc=1&doc=1", "https://example.invalid/report?doc=1"),
        (
            "https://user:synthetic@example.invalid/report?doc=1",
            "https://example.invalid/report?doc=1",
        ),
        (
            "https://example.invalid/report?doc=1",
            "https://user:synthetic@example.invalid/report?doc=1",
        ),
        (
            "https://user:synthetic@example.invalid/report",
            "https://user:synthetic@example.invalid/report",
        ),
        ("file:///report.pdf", "file:///report.pdf"),
        ("https:///report.pdf", "https:///report.pdf"),
    ],
)
def test_foreign_or_unsafe_url_never_becomes_a_bound_source(url, source):
    body = content()
    body["evidence"][0]["url"] = url
    with pytest.raises(ValueError):
        validate_url(body, [source])


def test_empty_sources_cannot_bind_scored_evidence():
    with pytest.raises(ValueError):
        validate_url(content(), [])


def test_unavailable_answer_without_evidence_and_without_sources_remains_valid():
    body = content()
    body.update(status="insufficient_evidence", score=None, evidence=[])
    saved = validate_url(body, [])
    assert saved == body and saved is not body


def test_url_identity_does_not_relax_original_cutoff():
    body = content()
    body["evidence"][0]["url"] = "https://example.invalid/report?stamp"
    body["evidence"][0]["published_at"] = "2099-01-01"
    with pytest.raises(ValueError, match="URL/date"):
        validate_url(body, ["https://example.invalid/report?stamp="])


@pytest.mark.parametrize(
    "raw_url", ["https://example.invalid/report?stamp", "https://example.invalid/report?stamp="]
)
def test_observation_projects_only_derived_evidence_to_exact_receipt_spelling(raw_url):
    body = content()
    body["evidence"][0]["url"] = raw_url
    receipt_url = "https://example.invalid/report?stamp="
    saved = checkpoint()
    saved["provenance"]["source_urls"] = [receipt_url]
    before_body, before_checkpoint = copy.deepcopy(body), copy.deepcopy(saved)
    observation = build_observation(
        saved, context=bound(), standard_answer=body, started_at="2026-09-22T09:00:00Z"
    )
    expected_answer = copy.deepcopy(body)
    expected_answer["evidence"][0]["url"] = receipt_url
    assert observation["answer"] == expected_answer
    assert observation["answer"]["evidence"][0]["url"] in saved["provenance"]["source_urls"]
    assert body == before_body and saved == before_checkpoint
    # Previously exact source spelling follows the same original observation path.
    exact = build_observation(
        saved, context=bound(), standard_answer=expected_answer, started_at="2026-09-22T09:00:00Z"
    )
    assert observation == exact


def test_exact_source_spelling_wins_when_equivalent_receipt_urls_are_present():
    body = content()
    body["evidence"][0]["url"] = "https://example.invalid/report?stamp"
    saved = checkpoint()
    saved["provenance"]["source_urls"] = [
        "https://example.invalid/report?stamp=",
        body["evidence"][0]["url"],
    ]
    observation = build_observation(
        saved, context=bound(), standard_answer=body, started_at="2026-09-22T09:00:00Z"
    )
    assert observation["answer"] == body
