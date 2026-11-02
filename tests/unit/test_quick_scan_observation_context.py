"""Synthetic fixtures from the actual IQS compiler, never real identity goldens."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from src.utils import quick_scan_observation_context as api
from src.utils.quick_scan_c06_authority import AuthorityUnavailable, load_c06_authority
from src.utils.quick_scan_question_manifest import load_question_manifest
from src.utils.quick_scan_result_outbox import canonical_sha256

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
AUTHORITY = json.loads(
    (FIXTURES / "quick_scan_c06_authority_v2_fixture.json").read_text(encoding="utf-8")
)
CONTEXT = AUTHORITY["observation_context"]
MANIFEST = load_question_manifest(FIXTURES / "quick_scan_c06_manifest_v2_fixture.json")
QID = MANIFEST["questions"][0]["id"]


def bound(**changes):
    args: dict[str, Any] = dict(
        manifest=MANIFEST,
        identity_snapshot_sha256="b" * 64,
        entity_id="ENT_CONTEXT_FIXTURE",
        question_id=QID,
        scope="entity",
        scope_id="ENT_CONTEXT_FIXTURE",
    )
    args.update(changes)
    return api.bind_question_context(copy.deepcopy(CONTEXT), **args)


def content():
    return dict(
        question_id=QID,
        response_kind="score",
        status="scored",
        score=8,
        summary="Synthetic supported answer; not a real company conclusion.",
        information_as_of="2026-09-01",
        period_start="2026-01-01",
        period_end="2026-06-30",
        basis="current",
        trend="stable",
        confidence="medium",
        metrics=[],
        items=[],
        evidence=[
            {
                "id": "e1",
                "title": "Synthetic test source",
                "url": "https://example.invalid/company",
                "published_at": "2026-09-01",
                "claim": "Synthetic test claim only.",
            }
        ],
        counterevidence="Synthetic missing detail.",
        watch_triggers=["Synthetic trigger"],
        missing_fields=["customer concentration"],
        coverage={
            "status": "partial",
            "reason": "Synthetic fixture has incomplete company coverage.",
        },
    )


def normalized(body):
    return dict(
        entity_id="ENT_CONTEXT_FIXTURE",
        question_id=QID,
        status=body["status"],
        score=body["score"],
        description=body["summary"],
    )


def validate(body):
    return api.validate_standard_answer(
        body,
        metadata=CONTEXT["questions"][QID]["metadata"],
        normalized_answer=normalized(content()),
        source_urls=["https://example.invalid/company"],
    )


def checkpoint():
    return {
        "checkpoint_schema": "quick-scan-answer",
        "checkpoint_schema_version": 1,
        "work_item_id": "WORK_fixture",
        "attempt_id": "ATTEMPT_fixture",
        "work": {
            "entity_id": "ENT_CONTEXT_FIXTURE",
            "question_id": QID,
            "scope": "entity",
            "scope_id": "ENT_CONTEXT_FIXTURE",
            "identity_snapshot_sha256": "b" * 64,
            "question_fingerprint": CONTEXT["questions"][QID]["work_prompt_sha256"],
        },
        "answer": normalized(content()),
        "provenance": {
            "actual_provider": "openai",
            "actual_model": "fixture-backup-model",
            "model_requested": "fixture-backup-model",
            "request_id": "req-fixture",
            "provider_attempt_id": "provider-attempt-fixture",
            "search_status": "executed",
            "search_receipt_id": "search-fixture",
            "provider_prompt_sha256": "c" * 64,
            "response_completed_at": "2026-09-22T09:01:00Z",
            "source_urls": ["https://example.invalid/company"],
        },
    }


def test_actual_iqs_context_is_accepted_and_detached():
    saved = api.validate_context_document(
        CONTEXT, expected_sha256=AUTHORITY["observation_context_sha256"]
    )
    assert saved == CONTEXT
    saved["questions"][QID]["metadata"]["cohort"]["industries"].append("different")
    assert saved != CONTEXT


@pytest.mark.parametrize("kind", ["hash", "version", "field", "schema", "review", "date"])
def test_context_tampering_or_self_approval_is_refused(kind):
    bad = copy.deepcopy(CONTEXT)
    if kind == "hash":
        expected = "0" * 64
    else:
        if kind == "version":
            bad["schema"] = "stockqa.quick_scan_observation_context/9.0.0"
        elif kind == "field":
            bad["unknown_field"] = "discarding this would be unsafe"
        elif kind == "schema":
            bad["answer_schema_sha256"] = "not-a-digest"
        elif kind == "review":
            bad["questions"][QID]["metadata"]["evidence_review_status"] = "reviewed"
        elif kind == "date":
            bad["questions"][QID]["metadata"]["information_cutoff"] = "not-a-date"
        expected = canonical_sha256(bad)
    with pytest.raises(ValueError):
        api.validate_context_document(bad, expected_sha256=expected)


def test_context_binds_exact_manifest_identity_and_scope():
    context = bound()
    assert context["metadata"] == CONTEXT["questions"][QID]["metadata"]
    assert context["identity_snapshot_sha256"] == "b" * 64
    assert context["work_prompt_sha256"] == CONTEXT["questions"][QID]["work_prompt_sha256"]


@pytest.mark.parametrize(
    "changes",
    [
        dict(identity_snapshot_sha256="d" * 64),
        dict(entity_id="ENT_OTHER"),
        dict(question_id="NO_SUCH_QUESTION"),
        dict(scope="security"),
        dict(scope_id="ENT_OTHER"),
    ],
)
def test_binding_changes_are_not_silently_accepted(changes):
    with pytest.raises(ValueError):
        bound(**changes)


@pytest.mark.parametrize(
    "key",
    [
        "manifest_sha256",
        "prompt",
        "semantic_sha256",
        "definition_sha256",
        "rubric_version",
    ],
)
def test_changed_manifest_cannot_rebind_context(key):
    bad = copy.deepcopy(MANIFEST)
    if key == "manifest_sha256":
        bad[key] = "e" * 64
    else:
        bad["questions"][0][key] = "changed"
    with pytest.raises(ValueError):
        bound(manifest=bad)


def test_actual_full_answer_is_preserved_without_generated_defaults():
    body = content()
    assert validate(body) == body
    assert validate(body) is not body


@pytest.mark.parametrize(
    "kind",
    [
        "missing_confidence",
        "missing_basis",
        "unknown",
        "wrong_score",
        "wrong_question",
        "unbound_url",
        "duplicate_evidence",
        "future_source",
        "future_information",
        "reversed_period",
        "metric_without_evidence",
        "document",
    ],
)
def test_incomplete_or_conflicting_standard_answer_is_refused(kind):
    body = content()
    if kind == "missing_confidence":
        body.pop("confidence")
    elif kind == "missing_basis":
        body.pop("basis")
    elif kind == "unknown":
        body.update(status="unknown", score=None)
    elif kind == "wrong_score":
        body["score"] = 9
    elif kind == "wrong_question":
        body["question_id"] = "OTHER"
    elif kind == "unbound_url":
        body["evidence"][0]["url"] = "https://example.invalid/unsearched"
    elif kind == "duplicate_evidence":
        conflicting = copy.deepcopy(body["evidence"][0])
        conflicting["claim"] = "A different claim under the same evidence id."
        body["evidence"].append(conflicting)
    elif kind == "future_source":
        body["evidence"][0]["published_at"] = "2026-09-23"
    elif kind == "future_information":
        body["information_as_of"] = "2026-09-23"
    elif kind == "reversed_period":
        body["period_start"] = "2026-07-01"
    elif kind == "metric_without_evidence":
        body["metrics"] = [
            {
                "metric_id": "custom.fixture",
                "value": 12,
                "unit": "percent",
                "currency": None,
                "unit_detail": None,
                "period_start": None,
                "period_end": None,
                "basis": "current",
                "definition": "Synthetic metric.",
                "evidence_ids": [],
            }
        ]
    elif kind == "document":
        body["raw_html"] = "<html>not a lightweight answer</html>"
    with pytest.raises(ValueError):
        validate(body)


def test_complete_observation_has_stable_content_id_and_original_timestamps():
    original = api.build_observation(
        checkpoint(),
        context=bound(),
        standard_answer=content(),
        started_at="2026-09-22T09:00:00Z",
    )
    again = api.build_observation(
        checkpoint(),
        context=bound(),
        standard_answer=content(),
        started_at="2026-09-22T09:00:00Z",
    )
    assert original == again
    assert original["observed_at"] == original["execution"]["answered_at"]
    assert original["execution"]["started_at"] == "2026-09-22T09:00:00Z"
    assert original["execution"]["model_resolved"] == "fixture-backup-model"
    assert original["information_cutoff"] == "2026-09-22"
    assert original["answer"] == content()
    body = {key: value for key, value in original.items() if key != "observation_id"}
    assert original["observation_id"] == "obs_" + canonical_sha256(body)


def test_different_actual_attempt_changes_observation_id():
    first = api.build_observation(
        checkpoint(),
        context=bound(),
        standard_answer=content(),
        started_at="2026-09-22T09:00:00Z",
    )
    new = checkpoint()
    new["attempt_id"] = "ATTEMPT_second"
    new["provenance"]["provider_attempt_id"] = "provider-attempt-second"
    new["provenance"]["request_id"] = "req-second"
    second = api.build_observation(
        new,
        context=bound(),
        standard_answer=content(),
        started_at="2026-09-22T09:00:00Z",
    )
    assert first["observation_id"] != second["observation_id"]


def test_started_after_answer_cannot_be_invented():
    with pytest.raises(ValueError):
        api.build_observation(
            checkpoint(),
            context=bound(),
            standard_answer=content(),
            started_at="2026-09-22T09:02:00Z",
        )


@pytest.mark.parametrize("status", ["insufficient_evidence", "not_applicable"])
def test_nonscored_states_remain_null_without_fallback(status):
    body = content()
    body.update(status=status, score=None, evidence=[], metrics=[])
    saved = checkpoint()
    saved["answer"] = normalized(body)
    observation = api.build_observation(
        saved, context=bound(), standard_answer=body, started_at="2026-09-22T09:00:00Z"
    )
    assert observation["answer"]["status"] == status
    assert observation["answer"]["score"] is None


def test_low_score_is_not_upgraded_or_dropped():
    body = content()
    body.update(score=1, confidence="low")
    saved = checkpoint()
    saved["answer"] = normalized(body)
    observation = api.build_observation(
        saved, context=bound(), standard_answer=body, started_at="2026-09-22T09:00:00Z"
    )
    assert observation["answer"]["score"] == 1
    assert observation["answer"]["confidence"] == "low"


def test_full_standard_body_above_legacy_description_limit_is_not_truncated():
    body = content()
    base = body["evidence"][0]
    body["evidence"] = [
        {**base, "id": "e" + str(index), "claim": "synthetic " + "x" * 380} for index in range(18)
    ]
    assert len(json.dumps(body)) > 5000
    result = validate(body)
    assert result == body
    assert len(result["evidence"]) == 18


def metric_body():
    registry = json.loads(
        (FIXTURES.parents[1] / "src/config/quick_scan_metric_registry.json").read_text(
            encoding="utf-8"
        )
    )
    metric_id, definition = next(iter(registry["metrics"].items()))
    body = content()
    body["metrics"] = [
        {
            "metric_id": metric_id,
            "value": 12,
            "unit": definition["unit"],
            "currency": "USD" if definition["unit"] in {"currency", "currency_per_share"} else None,
            "unit_detail": "synthetic custom unit" if definition["unit"] == "other" else None,
            "period_start": None,
            "period_end": None,
            "basis": "current",
            "definition": "Synthetic registered metric.",
            "evidence_ids": ["e1"],
        }
    ]
    return body


def test_registered_metric_retains_declared_unit_and_evidence():
    body = metric_body()
    assert validate(body) == body


@pytest.mark.parametrize("kind", ["wrong_unit", "duplicate_metric", "nan"])
def test_metric_semantic_errors_are_refused(kind):
    body = metric_body()
    metric = body["metrics"][0]
    if kind == "wrong_unit":
        metric["unit"] = "ratio" if metric["unit"] != "ratio" else "percent"
    elif kind == "duplicate_metric":
        body["metrics"].append(dict(metric, value=metric["value"] + 1))
    elif kind == "nan":
        metric["value"] = float("nan")
    with pytest.raises(ValueError):
        validate(body)


def test_different_actual_model_changes_observation_id():
    first = api.build_observation(
        checkpoint(),
        context=bound(),
        standard_answer=content(),
        started_at="2026-09-22T09:00:00Z",
    )
    other = checkpoint()
    other["provenance"].update(
        model_requested="fixture-other-model", actual_model="fixture-other-model"
    )
    second = api.build_observation(
        other,
        context=bound(),
        standard_answer=content(),
        started_at="2026-09-22T09:00:00Z",
    )
    assert first["observation_id"] != second["observation_id"]


_DETACHED = {
    "execution": {
        "provider": "openai",
        "model_requested": "fixture-backup-model",
        "model_resolved": "fixture-backup-model",
        "request_id": "req-fixture",
        "attempt_id": "provider-attempt-fixture",
        "search_status": "executed",
        "search_receipt_id": "search-fixture",
        "prompt_sha256": "c" * 64,
        "answered_at": "2026-09-22T09:01:00Z",
        "started_at": "2026-09-22T09:00:00Z",
        "model_revision": None,
    },
    "observed_at": "2026-09-22T09:01:00Z",
    "observation_id": "obs_" + "a" * 64,
}


def _detached(field):
    return content() if field == "answer" else copy.deepcopy(_DETACHED[field])


def test_embedded_answer_is_refused_at_the_real_consumption_entry(tmp_path):
    """The frozen context carries METADATA only — re-signing the digest never
    legitimises a smuggled answer, execution or any other detached field."""
    for field in ("answer", "execution", "observed_at", "observation_id"):
        injected = _detached(field)
        metadata_bound = copy.deepcopy(CONTEXT)
        metadata_bound["questions"][QID]["metadata"][field] = injected
        with pytest.raises(ValueError, match="frozen standard schema|detached"):
            api.validate_context_document(
                metadata_bound, expected_sha256=canonical_sha256(metadata_bound)
            )
        question_bound = copy.deepcopy(CONTEXT)
        question_bound["questions"][QID][field] = injected
        with pytest.raises(ValueError):
            api.validate_context_document(
                question_bound, expected_sha256=canonical_sha256(question_bound)
            )
        raised = copy.deepcopy(CONTEXT)
        raised[field] = injected
        with pytest.raises(ValueError):
            api.validate_context_document(raised, expected_sha256=canonical_sha256(raised))

    # a pure-metadata v2 authority is still the accepted input
    clean = tmp_path / "authority-clean.json"
    clean.write_text(json.dumps(AUTHORITY, ensure_ascii=False), encoding="utf-8")
    loaded = load_c06_authority(clean)
    assert loaded["schema_version"] == "2.0.0"
    assert loaded["observation_context"] == CONTEXT

    # ... but the same authority with an answer welded onto its top level is not
    smuggled = copy.deepcopy(AUTHORITY)
    smuggled["answer"] = content()
    path = tmp_path / "authority-embedded.json"
    path.write_text(json.dumps(smuggled, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(AuthorityUnavailable):
        load_c06_authority(path)


# --- QA-C06-02 remediation group 3: strict JSON at the body entry -----------


def test_valid_standard_body_still_parses_to_the_same_object():
    raw = json.dumps(content())
    assert api.parse_standard_answer(raw) == content()
    assert api.parse_standard_answer(raw) is not content()


def test_duplicate_score_in_standard_body_is_refused():
    raw = json.dumps(content()).replace('"score": 8', '"score": 1, "score": 8', 1)
    with pytest.raises(ValueError, match="strict"):
        api.parse_standard_answer(raw)


def test_nested_duplicate_key_in_standard_body_is_refused():
    raw = json.dumps(content()).replace(
        '"claim": "Synthetic test claim only."',
        '"claim": "first", "claim": "second"',
        1,
    )
    with pytest.raises(ValueError, match="strict"):
        api.parse_standard_answer(raw)


def test_non_finite_number_in_standard_body_is_refused():
    raw = json.dumps(content()).replace('"score": 8', '"score": NaN', 1)
    with pytest.raises(ValueError, match="strict"):
        api.parse_standard_answer(raw)


def test_plain_prose_and_identity_free_json_keep_their_legacy_handling():
    assert api.parse_standard_answer("plain prose answer") is None
    assert api.parse_standard_answer('{"note": "not an answer"}') is None
    assert api.parse_standard_answer("{see the attached note}") is None


def test_strict_json_loads_rejects_duplicates_and_non_finite_at_every_level():
    from src.utils.quick_scan_result_outbox import strict_json_loads

    with pytest.raises(ValueError):
        strict_json_loads('{"a": 1, "a": 2}')
    with pytest.raises(ValueError):
        strict_json_loads('{"outer": {"inner": 1, "inner": 2}}')
    with pytest.raises(ValueError):
        strict_json_loads('{"score": NaN}')
    with pytest.raises(ValueError):
        strict_json_loads('{"score": Infinity}')
    assert strict_json_loads('{"a": [1, {"b": 2}]}') == {"a": [1, {"b": 2}]}
    with pytest.raises(json.JSONDecodeError):
        strict_json_loads("{truncated")


# --- QA-C06-02 remediation group 1: bind shares the manifest rule set -------


@pytest.mark.parametrize(
    "changes",
    [
        {"question_definition_sha256": "0" * 64},
        {"template_version": "99.0.0"},
        {"entity_id": "ENT_SOMEONE_ELSE"},
        {
            "cohort": {
                "company_type": "seed",
                "industries": ["chips"],
                "stage": "seed",
                "subtype": None,
            }
        },
    ],
)
def test_bind_question_context_uses_the_same_manifest_rule_set(changes):
    """The dispatcher binding and the authority loader must never diverge:
    metadata that the loader refuses can never bind here either."""
    tampered = copy.deepcopy(CONTEXT)
    tampered["questions"][QID]["metadata"].update(changes)
    with pytest.raises(ValueError):
        api.bind_question_context(
            tampered,
            manifest=MANIFEST,
            identity_snapshot_sha256=CONTEXT["identity_snapshot_sha256"],
            entity_id="ENT_CONTEXT_FIXTURE",
            question_id=QID,
            scope="entity",
            scope_id="ENT_CONTEXT_FIXTURE",
        )


# Exact article URLs returned in the 2026-11-01 CATL native-search receipt.
# Offline URL-identity fixtures, not proof of financial claims.
_ARTICLE_SINA = "https://finance.sina.com.cn/wm/2026-10-21/doc-inivyaau7964703.shtml"
_ARTICLE_EASTMONEY = "https://caifuhao.eastmoney.com/news/20261027113534047352800"


@pytest.mark.parametrize(
    "article,tracked",
    [
        (
            _ARTICLE_SINA,
            _ARTICLE_SINA + "?cre=tianyi&mod=pchp&loc=32&r=0&rfunc=37&tj=cxvertical_pc_hp&tr=12",
        ),
        (
            _ARTICLE_EASTMONEY,
            _ARTICLE_EASTMONEY
            + "?from=guba&name=5a6B5b635pe25Luj5ZCn&gubaurl=aHR0cDovL2d1YmFwb3N0LmVhc3Rtb25leS5jb20vbGlzdC8sMzAwNzUwLmh0bWw%3D",
        ),
    ],
)
def test_native_article_tracking_accepts_original_final_body_without_rewriting_urls(
    article, tracked
):
    body = content()
    body["evidence"][0]["url"] = article
    before = copy.deepcopy(body)
    result = api.validate_standard_answer(
        body,
        metadata=CONTEXT["questions"][QID]["metadata"],
        normalized_answer=normalized(body),
        source_urls=[tracked],
    )
    assert result == before and body == before
    assert api._matching_evidence_source_url(article, [tracked]) == tracked


@pytest.mark.parametrize(
    "article,tracked",
    [
        (_ARTICLE_SINA, _ARTICLE_SINA + "?tr=12&%63re=tianyi&loc=32&mod=pchp"),
        (_ARTICLE_EASTMONEY, _ARTICLE_EASTMONEY + "?gubaurl=a%2Fb%3D&name=a+b&from=guba"),
    ],
)
def test_known_article_tracking_order_and_encoding_do_not_change_resource_identity(
    article, tracked
):
    assert api._evidence_url_identity(article) == api._evidence_url_identity(tracked)


@pytest.mark.parametrize("key", ["stockid", "ctrl", "year", "id", "ticker", "page", "start", "end"])
def test_known_article_content_selectors_and_repeated_values_are_not_removed(key):
    first = _ARTICLE_SINA + "?" + key + "=300750&cre=tianyi"
    second = _ARTICLE_SINA + "?" + key + "=002841&cre=tianyi"
    with pytest.raises(ValueError):
        api._matching_evidence_source_url(first, [second])
    with pytest.raises(ValueError):
        api._matching_evidence_source_url(_ARTICLE_SINA, [first])
    repeated = _ARTICLE_SINA + "?" + key + "=300750&" + key + "=002841"
    with pytest.raises(ValueError):
        api._matching_evidence_source_url(first, [repeated])


@pytest.mark.parametrize(
    "article,query",
    [
        ("https://example.invalid/wm/2026-10-21/doc-inivyaau7964703.shtml", "cre=tianyi"),
        ("https://finance.sina.com.cn/corp/go.php", "cre=tianyi"),
        ("https://finance.sina.com.cn/wm/2026-10-21/index.shtml", "cre=tianyi"),
        ("https://finance.sina.com.cn/wm/2026-10-21/doc-inivyaau7964703.shtml/extra", "cre=tianyi"),
        ("https://caifuhao.eastmoney.com/news/search", "from=guba"),
        (_ARTICLE_SINA, "from=guba"),
        (_ARTICLE_EASTMONEY, "cre=tianyi"),
        (_ARTICLE_SINA, "unknown_tracking=1"),
    ],
)
def test_tracking_whitelist_does_not_apply_to_other_hosts_paths_or_query_keys(article, query):
    with pytest.raises(ValueError):
        api._matching_evidence_source_url(article, [article + "?" + query])


@pytest.mark.parametrize(
    "other",
    [
        "https://finance.sina.com.cn/wm/2026-10-21/doc-othercompany.shtml?cre=tianyi",
        "https://finance.sina.com.cn.evil.invalid/wm/2026-10-21/doc-inivyaau7964703.shtml?cre=tianyi",
        "https://caifuhao.eastmoney.com/news/20261027113534047352801?from=guba",
        "https://finance.sina.com.cn@evil.invalid/wm/2026-10-21/doc-inivyaau7964703.shtml?cre=tianyi",
        "https://user:password@finance.sina.com.cn/wm/2026-10-21/doc-inivyaau7964703.shtml?cre=tianyi",
        _ARTICLE_SINA + "?token=secret&cre=tianyi",
    ],
)
def test_article_tracking_does_not_bind_other_resources_or_credentials(other):
    with pytest.raises(ValueError):
        api._matching_evidence_source_url(_ARTICLE_SINA, [other])


@pytest.mark.parametrize("stock_path", ["sz300750.html", "sh600519.html", "bj430047.html"])
def test_exact_quote_navigation_true_matches_without_rewriting_original_answer(stock_path):
    article = "http://quote.eastmoney.com/" + stock_path
    receipt = article + "?jump_to_web=true"
    body = content()
    body["evidence"][0]["url"] = article
    before = copy.deepcopy(body)
    result = api.validate_standard_answer(
        body,
        metadata=CONTEXT["questions"][QID]["metadata"],
        normalized_answer=normalized(body),
        source_urls=[receipt],
    )
    assert result == before and body == before
    assert api._matching_evidence_source_url(article, [receipt]) == receipt


@pytest.mark.parametrize(
    "other",
    [
        "http://quote.eastmoney.com/sz002841.html?jump_to_web=true",
        "http://quote.eastmoney.com/sh300750.html?jump_to_web=true",
        "http://quote.eastmoney.com/sz300750.html?jump_to_web=true&code=002841",
        "http://quote.eastmoney.com/sz300750.html?jump_to_web=true&year=2025",
        "http://quote.eastmoney.com/sz300750.html?jump_to_web=true&unknown=1",
        "http://quote.eastmoney.com/sz300750.html?jump_to_web=false",
        "http://quote.eastmoney.com/sz300750.html?jump_to_web=unknown",
        "http://quote.eastmoney.com.evil.invalid/sz300750.html?jump_to_web=true",
    ],
)
def test_quote_navigation_does_not_hide_security_or_content_changes(other):
    with pytest.raises(ValueError):
        api._matching_evidence_source_url("http://quote.eastmoney.com/sz300750.html", [other])


@pytest.mark.parametrize(
    "url",
    [
        "http://example.invalid/sz300750.html",
        "http://quote.eastmoney.com/sz300750.html/extra",
        "http://quote.eastmoney.com/sz300750.htm",
    ],
)
def test_quote_navigation_is_not_ignored_outside_exact_stock_path(url):
    with pytest.raises(ValueError):
        api._matching_evidence_source_url(url, [url + "?jump_to_web=true"])


def period_metric_body():
    body = metric_body()
    body["metrics"][0].update(period_start="2024-01-01", period_end="2024-12-31")
    return body


def test_metric_multiple_years_reach_actual_standard_checkpoint_validator():
    body = period_metric_body()
    body["metrics"].append(
        dict(body["metrics"][0], period_start="2025-01-01", period_end="2025-12-31")
    )
    assert validate(body) == body


@pytest.mark.parametrize(
    "change",
    [
        {"value": 99, "definition": "Another segment must not be hidden in prose"},
        {"period_start": None},
        {"period_start": None, "period_end": None},
        {"period_start": "2025-01-01", "period_end": "2025-12-31", "unit": "count"},
        {"period_start": "2025-01-01", "period_end": "2025-12-31", "evidence_ids": ["foreign"]},
    ],
)
def test_metric_coordinates_do_not_weaken_duplicate_units_or_source_checks(change):
    body = period_metric_body()
    body["metrics"].append(dict(body["metrics"][0], **change))
    with pytest.raises(ValueError):
        validate(body)


def test_metric_distinct_basis_and_currency_are_preserved():
    body = period_metric_body()
    metric = body["metrics"][0]
    # Use a canonical currency metric so currency is an economic coordinate.
    metric.update(metric_id="financial.fcf", unit="currency", currency="USD")
    body["metrics"].extend([dict(metric, basis="normalized"), dict(metric, currency="CNY")])
    assert validate(body) == body


def test_unregistered_metric_labels_are_preserved_without_guessed_mapping():
    body = content()
    body["metrics"] = [
        dict(
            metric_id="financial.fcf_2024",
            value=100,
            unit="currency",
            currency="CNY",
            unit_detail=None,
            period_start="2024-01-01",
            period_end="2024-12-31",
            basis="trailing",
            definition="Synthetic cash flow definition, not a company fact",
            evidence_ids=["e1"],
        )
    ]
    before = copy.deepcopy(body)
    result = validate(body)
    assert result == before and body == before
    assert result["metrics"][0]["metric_id"] == "financial.fcf_2024"


def test_standard_body_uses_the_same_visible_json_format_compatibility():
    body = content()
    raw = json.dumps(body)
    repeated = raw.replace(
        '"information_as_of": "2026-09-01"',
        '"information_as_of": "2026-09-01", "information_as_of": "2026-09-01"',
    )
    assert api.parse_standard_answer("```json\n" + repeated + "\n```") == body


def test_exact_repeated_metric_and_evidence_rows_retain_actual_body():
    body = metric_body()
    body["metrics"].append(copy.deepcopy(body["metrics"][0]))
    body["evidence"].append(copy.deepcopy(body["evidence"][0]))
    before = copy.deepcopy(body)
    assert validate(body) == before and body == before


def test_same_evidence_id_with_different_content_is_still_ambiguous():
    body = content()
    body["evidence"].append(dict(body["evidence"][0], claim="Different unaligned claim"))
    with pytest.raises(ValueError, match="evidence"):
        validate(body)


@pytest.mark.parametrize("length", [900, 5000, 6000])
def test_extended_visible_explanation_preserves_raw_body_and_schema_context(length):
    body = content()
    body.update(
        summary="A" * length,
        confidence_rationale={"text": "Partial source coverage", "limitations": ["Synthetic only"]},
    )
    before = copy.deepcopy(body)
    original_schema = copy.deepcopy(api._resources()[1])
    result = api.validate_standard_answer(
        body,
        metadata=CONTEXT["questions"][QID]["metadata"],
        normalized_answer=normalized(body),
        source_urls=["https://example.invalid/company"],
    )
    assert result == before and body == before
    assert api._resources()[1] == original_schema
    assert api.split_standard_answer_transport(json.dumps(body))[0] == body["summary"]


@pytest.mark.parametrize("fault", ["private", "entity", "unit"])
def test_visible_explanation_extensions_never_hide_semantic_conflicts(fault):
    body = content()
    body["confidence_rationale"] = (
        {"thinking_content": "SYNTHETIC_PRIVATE"} if fault == "private" else "Business rationale"
    )
    if fault == "entity":
        body["entity_id"] = "ANOTHER_ISSUER"
    elif fault == "unit":
        body["metrics"] = [
            dict(
                metric_id="financial.net_margin",
                value=10,
                unit="currency",
                currency="USD",
                unit_detail="USD",
                period_start=None,
                period_end=None,
                basis="current",
                definition="Conflicting canonical unit",
                evidence_ids=["e1"],
            )
        ]
    with pytest.raises(ValueError):
        api.validate_standard_answer(
            body,
            metadata=CONTEXT["questions"][QID]["metadata"],
            normalized_answer=normalized(body),
            source_urls=["https://example.invalid/company"],
        )


def test_current_additive_answer_resources_are_provenance_not_a_static_hash_allowlist():
    iqs = Path(__file__).resolve().parents[3] / "invest-quick-scan"
    context = copy.deepcopy(CONTEXT)
    answer_schema = json.loads(
        (iqs / "schemas/answer-content.schema.json").read_text(encoding="utf8")
    )
    metrics = json.loads((iqs / "questions/metric-registry.json").read_text(encoding="utf8"))
    metrics["metrics"]["new.unrelated_metric"] = {"unit": "ratio"}
    context.update(
        answer_schema_sha256=canonical_sha256(answer_schema),
        metric_registry_sha256=canonical_sha256(metrics),
    )
    before = copy.deepcopy(context)
    assert (
        api.validate_context_document(context, expected_sha256=canonical_sha256(context)) == before
    )
    api.check_context_matches_manifest(context, MANIFEST)
    assert context == before
    bad = copy.deepcopy(context)
    bad["questions"][QID]["metadata"]["entity_id"] = "WRONG_ISSUER"
    with pytest.raises(ValueError):
        api.check_context_matches_manifest(bad, MANIFEST)


def test_new_answer_format_does_not_change_existing_observation_profiles(monkeypatch):
    """A new format is additive; existing public files remain the frozen v1."""
    api._resources.cache_clear()
    old_answer, old_metrics = copy.deepcopy(api._resources("1.1.0")[1:3])
    new_answer = copy.deepcopy(old_answer)
    new_answer["required"].append("new_format_field")
    new_metrics = copy.deepcopy(old_metrics)
    metric_id = next(iter(new_metrics["metrics"]))
    new_metrics["metrics"][metric_id]["unit"] = "other"
    original_read = Path.read_text

    def read_resource(path, *args, **kwargs):
        if path.name == "answer-format-v2.json":
            return json.dumps(new_answer)
        if path.name == "metric-format-v2.json":
            return json.dumps(new_metrics)
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_resource)
    monkeypatch.setitem(
        api._ANSWER_FORMATS, "2.0.0", ("answer-format-v2.json", "metric-format-v2.json")
    )
    monkeypatch.setitem(
        api._OBSERVATION_FORMATS, "1.3.0", ("quick_scan_observation.v1.2.schema.json", "2.0.0")
    )
    try:
        for version in ("1.1.0", "1.2.0"):
            assert api._resources(version)[1:3] == (old_answer, old_metrics)
            metadata = copy.deepcopy(CONTEXT["questions"][QID]["metadata"])
            metadata["schema_version"] = version
            assert (
                api.validate_standard_answer(
                    content(),
                    metadata=metadata,
                    normalized_answer=normalized(content()),
                    source_urls=["https://example.invalid/company"],
                )
                == content()
            )
        metadata["schema_version"] = "1.3.0"
        with pytest.raises(ValueError, match="frozen standard schema"):
            api.validate_standard_answer(
                content(),
                metadata=metadata,
                normalized_answer=normalized(content()),
                source_urls=["https://example.invalid/company"],
            )
        assert api._resources("1.3.0")[1:3] == (new_answer, new_metrics)
    finally:
        api._resources.cache_clear()
