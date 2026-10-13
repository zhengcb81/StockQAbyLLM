"""Synthetic durable-record assembly, not vendor/owner/financial gold."""

import copy

import pytest
import test_c06_authority as iqs_fixture

from src.utils import quick_scan_observation_context as api
from src.utils.quick_scan_work_store import quick_scan_receipt_sha256
from tests.unit.test_quick_scan_observation_context import content


@pytest.fixture(scope="module")
def exported():
    iqs_fixture.C06AuthorityTests.setUpClass()
    fixture = iqs_fixture.C06AuthorityTests()
    authority = fixture.create(
        contract_versions={**fixture.versions, "observation_schema": "1.2.0"},
        observation_schema_version="1.2.0",
    )
    manifest = copy.deepcopy(fixture.manifest)
    manifest["manifest_sha256"] = "a" * 64
    return authority, manifest


def pair(exported):
    authority, manifest = exported
    question = next(q for q in manifest["questions"] if q["scope"] == "entity")
    ctx = api.bind_question_context(
        authority["observation_context"],
        manifest=manifest,
        identity_snapshot_sha256="b" * 64,
        entity_id=manifest["profile"]["entity_id"],
        question_id=question["id"],
        scope="entity",
        scope_id=manifest["profile"]["entity_id"],
    )
    body = content()
    body["question_id"] = question["id"]
    receipt = dict(
        provider="mimo",
        requested_model="mimo-v2.6-flash",
        actual_model="mimo-v2.6-flash",
        request_id=None,
        response_id="SYNTHETIC_completion-A",
        attempt_id="SYNTHETIC_physical-A",
        request_payload_sha256="e" * 64,
        response_sha256="d" * 64,
        prompt_sha256="c" * 64,
        completed_at="2026-10-12T10:01:00Z",
        response_status="completed",
        http_status_code=200,
        search_status="executed",
        search_receipt_id="SYNTHETIC_native-search-A",
        source_urls=["https://example.invalid/company"],
        response_json_basis="strict_http_json",
    )
    digest = quick_scan_receipt_sha256(receipt)
    response = dict(
        attempt_id="SYNTHETIC_logical-A",
        provider="mimo",
        model_requested="mimo-v2.6-flash",
        model_resolved="mimo-v2.6-flash",
        response_id=receipt["response_id"],
        provider_attempt_id=receipt["attempt_id"],
        response_sha256=receipt["response_sha256"],
        receipt_sha256=digest,
        receipt=receipt,
    )
    checkpoint = dict(
        checkpoint_schema="quick-scan-answer",
        checkpoint_schema_version=1,
        attempt_id=response["attempt_id"],
        work_item_id="SYNTHETIC_work-A",
        work=dict(
            entity_id=manifest["profile"]["entity_id"],
            question_id=question["id"],
            scope="entity",
            scope_id=manifest["profile"]["entity_id"],
            identity_snapshot_sha256="b" * 64,
            question_fingerprint=ctx["work_prompt_sha256"],
        ),
        answer=dict(
            entity_id=manifest["profile"]["entity_id"],
            question_id=question["id"],
            status="scored",
            score=8,
            description=body["summary"],
        ),
        provenance=dict(
            actual_provider=receipt["provider"],
            model_requested=receipt["requested_model"],
            actual_model=receipt["actual_model"],
            request_id=None,
            response_id=receipt["response_id"],
            provider_attempt_id=receipt["attempt_id"],
            provider_prompt_sha256=receipt["prompt_sha256"],
            response_sha256=receipt["response_sha256"],
            receipt_sha256=digest,
            response_completed_at=receipt["completed_at"],
            search_status="executed",
            search_receipt_id=receipt["search_receipt_id"],
            source_urls=receipt["source_urls"],
            model_resolution_binding="durable-response-v1",
        ),
    )
    return checkpoint, ctx, body, response


def build(parts):
    checkpoint, ctx, body, response = parts
    return api.build_observation(
        checkpoint,
        context=ctx,
        standard_answer=body,
        started_at="2026-10-12T10:00:00Z",
        durable_response=response,
    )


def test_missing_http_header_is_preserved_as_null_with_separate_response_and_physical_id(
    exported,
):
    parts = pair(exported)
    record = build(parts)
    execution = record["execution"]
    assert record["schema_version"] == "1.2.0"
    assert execution["request_id"] is None
    assert execution["response_id"] == "SYNTHETIC_completion-A"
    assert execution["attempt_id"] == "SYNTHETIC_physical-A"
    assert execution["attempt_id"] != parts[0]["attempt_id"]
    assert execution["request_payload_sha256"] == "e" * 64
    assert execution["response_sha256"] == "d" * 64
    assert execution["receipt_sha256"] == parts[3]["receipt_sha256"]
    assert record == build(parts)


def test_actual_nonempty_header_is_preserved(exported):
    checkpoint, ctx, body, response = pair(exported)
    response["receipt"]["request_id"] = "SYNTHETIC_header-A"
    checkpoint["provenance"]["request_id"] = "SYNTHETIC_header-A"
    digest = quick_scan_receipt_sha256(response["receipt"])
    response["receipt_sha256"] = digest
    checkpoint["provenance"]["receipt_sha256"] = digest
    assert (
        build((checkpoint, ctx, body, response))["execution"]["request_id"] == "SYNTHETIC_header-A"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "no_record",
        "wrong_logical_attempt",
        "wrong_receipt_hash",
        "wrong_response_hash",
        "wrong_response_id",
        "wrong_physical_id",
        "wrong_model",
        "missing_payload_hash",
        "wrong_completion_time",
        "missing_header_capture",
    ],
)
def test_mixed_or_missing_durable_record_never_becomes_publishable(exported, mutation):
    checkpoint, ctx, body, response = pair(exported)
    if mutation == "no_record":
        response = None
    elif mutation == "wrong_logical_attempt":
        response["attempt_id"] = "SYNTHETIC_other_logical"
    elif mutation == "wrong_receipt_hash":
        response["receipt_sha256"] = "f" * 64
    elif mutation == "wrong_response_hash":
        response["response_sha256"] = "f" * 64
    elif mutation == "wrong_response_id":
        checkpoint["provenance"]["response_id"] = "SYNTHETIC_other_completion"
    elif mutation == "wrong_physical_id":
        checkpoint["provenance"]["provider_attempt_id"] = "SYNTHETIC_other_physical"
    elif mutation == "wrong_model":
        checkpoint["provenance"]["actual_model"] = "SYNTHETIC_other_model"
    elif mutation == "wrong_completion_time":
        checkpoint["provenance"]["response_completed_at"] = "2026-10-12T10:02:00Z"
    else:
        response["receipt"].pop(
            "request_payload_sha256" if mutation == "missing_payload_hash" else "request_id"
        )
        digest = quick_scan_receipt_sha256(response["receipt"])
        response["receipt_sha256"] = digest
        checkpoint["provenance"]["receipt_sha256"] = digest
    with pytest.raises(ValueError):
        build((checkpoint, ctx, body, response))


def test_old_observation_shape_does_not_gain_new_receipt_fields():
    from tests.unit.test_quick_scan_observation_context import bound, checkpoint

    old = api.build_observation(
        checkpoint(),
        context=bound(),
        standard_answer=content(),
        started_at="2026-09-22T09:00:00Z",
    )
    assert old["schema_version"] == "1.1.0"
    assert not {
        "response_id",
        "request_payload_sha256",
        "response_sha256",
        "receipt_sha256",
    } & set(old["execution"])
