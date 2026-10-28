"""Explicit C06 object codec using original SQLite/receipt/standard APIs.

All answers, subjects, URLs, receipts and costs below are synthetic fixtures.
They establish software behavior only; no real company golden is fabricated.
"""

import copy
import hashlib
import json
from unittest.mock import Mock

import pytest

from src.core.models import Question
from src.providers.base_llm_provider import (
    standard_answer_transport,
    standard_answer_transport_enabled,
)
from src.providers.llm_client import LLMSearchResponse
from src.providers.llm_provider import LLMProvider
from src.providers.llm_response_parser import LLMResponseParser
from src.services.answer_generator import AnswerGenerator
from src.utils import quick_scan_work_transport as transport
from src.utils.quick_scan_work_store import _sanitized_receipt
from tests.unit.test_quick_scan_question_pack_transport import (
    HTTP_ID,
    QIDS,
    SYSTEM,
    _bindings,
    _response,
    pack,
)


def body(qid, *, scored=False):
    return {
        "question_id": qid,
        "response_kind": "score",
        "status": "scored" if scored else "insufficient_evidence",
        "score": 7 if scored else None,
        "summary": "Synthetic standard answer; no company claim.",
        "information_as_of": "2026-09-22" if scored else None,
        "period_start": None,
        "period_end": None,
        "basis": "current" if scored else "not_applicable",
        "trend": "stable" if scored else "uncertain",
        "confidence": "low",
        "metrics": [],
        "items": [],
        "evidence": (
            [
                {
                    "id": "E1",
                    "title": "Synthetic source",
                    "url": "https://example.org/synthetic",
                    "published_at": "2026-09-21",
                    "claim": "Synthetic scoped evidence.",
                }
            ]
            if scored
            else []
        ),
        "counterevidence": "No verified synthetic counterevidence.",
        "watch_triggers": [],
        "missing_fields": [] if scored else ["verified evidence"],
        "coverage": {
            "status": "partial" if scored else "unknown",
            "reason": "Synthetic bounded coverage.",
        },
    }


def member(qid, *, as_object=True, scored=False):
    supplied = body(qid, scored=scored)
    return {
        "question_id": qid,
        "entity_id": "ENT_CONTEXT_FIXTURE",
        "company_name": "Synthetic fixture company",
        "status": supplied["status"],
        "score": supplied["score"],
        "information_as_of": supplied["information_as_of"],
        "description": supplied if as_object else json.dumps(supplied, ensure_ascii=False),
    }


@pytest.mark.parametrize("as_object", [True, False])
def test_explicit_codec_preserves_supplied_body_and_unknown_null_without_mutating_input(as_object):
    item = member("IQS_01", as_object=as_object)
    original = copy.deepcopy(item)
    result = LLMResponseParser().parse_question_pack_response(
        json.dumps({"answers": [item]}),
        ("IQS_01",),
        expected_entity_id=item["entity_id"],
        expected_company_name=item["company_name"],
        standard_answer_objects=True,
    )
    assert result.invalid_question_ids == () and result.answers[0].score is None
    assert json.loads(result.answers[0].description) == body("IQS_01")
    assert item == original


def test_generic_codec_does_not_accept_model_selected_objects_or_truthy_mode():
    item = member("IQS_01")
    item["standard_answer_objects"] = True
    parsed = LLMResponseParser().parse_question_pack_response(
        json.dumps({"answers": [item]}), ("IQS_01",)
    )
    assert parsed.invalid_question_ids == ("IQS_01",)
    with pytest.raises(ValueError, match="explicit bool"):
        LLMResponseParser().parse_question_pack_response(
            json.dumps({"answers": [item]}), ("IQS_01",), standard_answer_objects=1
        )


@pytest.mark.parametrize("fault", ["score", "status", "id", "duplicate", "nonfinite"])
def test_object_codec_keeps_original_ambiguity_and_inner_outer_conflict_rejection(fault):
    item = member("IQS_01")
    items = [item]
    if fault == "score":
        item["score"] = 8
        item["status"] = "scored"
    if fault == "status":
        item["status"] = "not_applicable"
    if fault == "id":
        item["description"]["question_id"] = "IQS_02"
    if fault == "duplicate":
        items.append(copy.deepcopy(item))
    if fault == "nonfinite":
        item["description"]["metrics"] = [float("nan")]
    parsed = LLMResponseParser().parse_question_pack_response(
        json.dumps({"answers": items}), ("IQS_01",), standard_answer_objects=True
    )
    assert parsed is None or parsed.invalid_question_ids == ("IQS_01",)


def provider_for(tmp_path, answers, *, source_urls=None):
    config = tmp_path / "models.json"
    config.write_text(
        json.dumps(
            {
                "default_provider": "mimo",
                "providers": {
                    "mimo": {
                        "enabled": True,
                        "api_key": "",
                        "model": "mimo-v2.6-flash",
                        "base_url": "https://api.xiaomimimo.com/v1",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    provider = LLMProvider(
        api_key="synthetic-not-sent",
        provider_name="mimo",
        config_file=str(config),
        require_search=True,
        entity_id="ENT_CONTEXT_FIXTURE",
        company_name="Synthetic fixture company",
    )
    questions = [Question("Synthetic question " + qid, question_id=qid) for qid in QIDS]

    def send(prompt, **controls):
        payload_sha = hashlib.sha256(
            json.dumps({"prompt": prompt, "controls": controls}, sort_keys=True).encode()
        ).hexdigest()
        handle = transport.begin_quick_scan_send(
            prompt,
            SYSTEM,
            model_requested="mimo-v2.6-flash",
            provider_actual="mimo",
            execution_attempt_id=HTTP_ID,
            request_payload_sha256=payload_sha,
        )
        handle.consume_for_post()
        receipt = _response(
            prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
            request_payload_sha256=payload_sha,
        )
        if source_urls is not None:
            receipt["source_urls"] = list(source_urls)
        handle.record_response(http_status_code=200, request_id="req-fixture", receipt=receipt)
        receipt["work_transport"] = {
            "work_attempt_id": handle.attempt_id,
            "final_receipt": _sanitized_receipt(receipt),
        }
        return LLMSearchResponse(
            json.dumps({"answers": answers}),
            receipt["request_id"],
            receipt["response_id"],
            receipt["actual_model"],
            tuple(receipt["source_urls"]),
            receipt,
        )

    provider.client.send_search_request = Mock(side_effect=send)
    provider.client.send_request = Mock(side_effect=AssertionError("no extra model call"))
    provider._repair_invalid_answer = Mock(side_effect=AssertionError("no automatic repair"))
    return provider, questions


@pytest.mark.parametrize("as_object", [True, False])
def test_original_provider_generator_checkpoint_standard_roundtrip_and_one_physical_fee(
    pack, tmp_path, as_object
):
    store, members, _ = pack
    answers = [
        member(qid, as_object=as_object, scored=index == 0) for index, qid in enumerate(QIDS)
    ]
    original = copy.deepcopy(answers)
    provider, questions = provider_for(tmp_path, answers)
    with _bindings(pack), standard_answer_transport(True):
        result = provider.search_question_pack(questions)
        assert result.invalid_question_ids == result.missing_question_ids == ()
        assert len(result.results) == 3
        assert (
            result.execution_metadata["question_pack_standard_diagnostics"]["status"] == "complete"
        )
        for index, search in enumerate(result.results):
            answer = AnswerGenerator().generate_answer(questions[index], [search])
            supplied = json.loads(answer.text)
            assert supplied == body(QIDS[index], scored=index == 0)
            work_id, lease = members[index]
            context = store.get_observation_context(work_id)["context"]
            execution = search.metadata["execution"]
            attempt_id = execution["question_pack_member"]["work_attempt_id"]
            normalized = {
                "entity_id": "ENT_CONTEXT_FIXTURE",
                "question_id": QIDS[index],
                "status": supplied["status"],
                "score": supplied["score"],
                "description": supplied["summary"],
            }
            checkpoint = store.save_answer_checkpoint(
                work_id,
                lease,
                attempt_id,
                answer=normalized,
                execution_receipt=execution,
                observation_context=context,
                standard_answer=supplied,
            )
            assert checkpoint["payload"]["answer"]["score"] == (7 if index == 0 else None)
            assert store.get_standard_answer(work_id) is not None
    assert not standard_answer_transport_enabled() and answers == original
    assert provider.client.send_search_request.call_count == 1
    prompt = provider.client.send_search_request.call_args.args[0]
    assert "direct JSON object (NOT a quoted JSON string)" in prompt
    assert store.get_quick_scan_budget_status("pack-transport")["requests"] == 1
    assert store.get_quick_scan_budget_status("pack-transport")["spent_micros"] == 30000


@pytest.mark.parametrize(
    "fault", ["prose", "coverage", "unknown_field", "evidence_url", "future_date", "metrics"]
)
def test_complete_profile_rejects_incomplete_member_before_result_and_preserves_valid_siblings(
    pack, tmp_path, fault
):
    answers = [member(qid) for qid in QIDS]
    bad = answers[1]["description"]
    if fault == "prose":
        answers[1]["description"] = "Plain generic prose is not a complete C06 answer."
    if fault == "coverage":
        bad.pop("coverage")
    if fault == "unknown_field":
        bad["invented_authority"] = True
    if fault == "evidence_url":
        bad["evidence"] = [
            {
                "id": "E2",
                "title": "Synthetic",
                "url": "https://foreign.invalid/unretrieved",
                "published_at": None,
                "claim": "Not physically retrieved.",
            }
        ]
    if fault == "future_date":
        bad["information_as_of"] = "2099-01-01"
    if fault == "metrics":
        bad["metrics"] = [{"id": "not-a-complete-metric"}]
    provider, questions = provider_for(tmp_path, answers)
    with _bindings(pack), standard_answer_transport(True):
        result = provider.search_question_pack(questions)
        assert result.invalid_question_ids == (QIDS[1],)
        assert [row.metadata["question_id"] for row in result.results] == [QIDS[0], QIDS[2]]
        assert result.execution_metadata["question_pack"]["status"] == "partial"
        assert result.execution_metadata["question_pack_standard_diagnostics"]["rejections"] == [
            {"question_id": QIDS[1], "code": "full_standard_answer_invalid"}
        ]
    assert provider.client.send_search_request.call_count == 1
    assert all(pack[0].get_answer_checkpoint(wid) is None for wid, _ in pack[1])


def test_standard_validator_requires_original_active_receipt_binding(pack, tmp_path):
    provider, questions = provider_for(tmp_path, [member(qid) for qid in QIDS])
    with _bindings(pack), standard_answer_transport(True):
        result = provider.search_question_pack(questions)
        execution = result.execution_metadata
        row = {
            "question_id": QIDS[0],
            "entity_id": "ENT_CONTEXT_FIXTURE",
            "status": "insufficient_evidence",
            "score": None,
            "description": json.dumps(body(QIDS[0])),
        }
        assert transport.validate_question_pack_standard_answers(execution, [row]) == {}
        tampered = copy.deepcopy(execution)
        tampered["source_urls"] = ["https://foreign.invalid/unretrieved"]
        with pytest.raises(transport.QuickScanWorkPersistenceError):
            transport.validate_question_pack_standard_answers(tampered, [row])
    with pytest.raises(transport.QuickScanWorkPersistenceError):
        transport.validate_question_pack_standard_answers(execution, [row])


@pytest.mark.parametrize("as_object", [True, False])
@pytest.mark.parametrize("claimed_date", ["2099-01-01", "not-a-date", "2026-09-21"])
def test_c06_rejects_raw_or_nested_date_conflicts_without_rewriting_unknown(
    pack, tmp_path, as_object, claimed_date
):
    answers = [member(qid, as_object=as_object) for qid in QIDS]
    answers[1]["information_as_of"] = claimed_date
    provider, questions = provider_for(tmp_path, answers)
    with _bindings(pack), standard_answer_transport(True):
        result = provider.search_question_pack(questions)
        assert result.invalid_question_ids == (QIDS[1],)
        assert [row.metadata["question_id"] for row in result.results] == [QIDS[0], QIDS[2]]
        assert (
            result.execution_metadata["question_pack_standard_diagnostics"]["status"] == "partial"
        )
    assert provider.client.send_search_request.call_count == 1


def test_no_parsed_members_cannot_claim_full_standard_complete(pack, tmp_path):
    answers = [member(qid) for qid in QIDS]
    for answer in answers:
        answer["score"] = 8
    provider, questions = provider_for(tmp_path, answers)
    with _bindings(pack), standard_answer_transport(True):
        result = provider.search_question_pack(questions)
        assert result.results == () and result.invalid_question_ids == QIDS
        diagnostic = result.execution_metadata["question_pack_standard_diagnostics"]
        assert diagnostic["status"] == "rejected"
        assert {row["question_id"] for row in diagnostic["rejections"]} == set(QIDS)
    assert provider.client.send_search_request.call_count == 1


@pytest.mark.parametrize("as_object", [True, False])
def test_canonical_native_source_alias_preserves_raw_standard_and_projects_exact_provenance(
    pack, tmp_path, as_object
):
    """Real SDK/SQLite fixtures; all content and sources synthetic, no paid HTTP."""
    from src.utils.quick_scan_observation_context import (
        bind_context_to_work_item,
        build_observation,
    )
    from src.utils.quick_scan_result_outbox import validate_checkpoint_binding

    raw_url = "https://example.org/report?stamp"
    receipt_url = raw_url + "="
    answers = [
        member(qid, as_object=as_object, scored=index == 0) for index, qid in enumerate(QIDS)
    ]
    supplied = body(QIDS[0], scored=True)
    supplied["evidence"][0]["url"] = raw_url
    answers[0]["description"] = supplied if as_object else json.dumps(supplied)
    provider, questions = provider_for(tmp_path, answers, source_urls=[receipt_url])
    with _bindings(pack), standard_answer_transport(True):
        result = provider.search_question_pack(questions)
        assert result.invalid_question_ids == result.missing_question_ids == ()
        answer = AnswerGenerator().generate_answer(questions[0], [result.results[0]])
        assert json.loads(answer.text) == supplied
        store, members, _ = pack
        wid, lease = members[0]
        item = store.get_item(wid)
        context = store.get_observation_context(wid)["context"]
        execution = result.results[0].metadata["execution"]
        checkpoint = store.save_answer_checkpoint(
            wid,
            lease,
            execution["question_pack_member"]["work_attempt_id"],
            answer={
                "entity_id": item["entity_id"],
                "question_id": QIDS[0],
                "status": "scored",
                "score": 7,
                "description": supplied["summary"],
            },
            execution_receipt=execution,
            observation_context=context,
            standard_answer=supplied,
        )
        bound = bind_context_to_work_item(context, work_item=item, question_id=QIDS[0])
        observation = build_observation(
            checkpoint["payload"],
            context=bound,
            standard_answer=supplied,
            started_at="2026-09-22T00:00:00Z",
        )
        assert observation["answer"]["evidence"][0]["url"] == receipt_url
        assert supplied["evidence"][0]["url"] == raw_url
        validate_checkpoint_binding({"items": [{"observation": observation}]}, checkpoint)
        assert store.get_standard_answer(wid)["answer"] == supplied
    assert provider.client.send_search_request.call_count == 1
    assert store.get_quick_scan_budget_status("pack-transport")["requests"] == 1
