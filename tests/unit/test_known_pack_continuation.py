"""Known-member continuation: actual engine/SDK/SQLite, synthetic isolated data."""

import copy
import json
import time
from contextlib import nullcontext
from unittest.mock import Mock

import pytest

from src.core.models import Answer, Question
from src.core.qa_engine import QAEngine
from src.providers.base_llm_provider import standard_answer_transport
from src.providers.llm_provider import LLMProvider, QuestionPackSearchResult
from src.runners.llm_runner import QuickScanWorkLifecycle
from src.utils import quick_scan_work_transport as transport
from src.utils.quick_scan_owner_refresh import OwnerRefreshSession, routing_fingerprints
from src.utils.quick_scan_work_store import QuickScanWorkStore
from tests.unit.test_c06_question_pack_objects import body
from tests.unit.test_qa_question_pack import Provider, engine, q, result
from tests.unit.test_quick_scan_question_pack_transport import (
    MANIFEST,
    QIDS,
    _owner_receipt,
    _policy,
    _v2_context,
)


def test_optin_assertion_runs_after_finalizers_and_continues_only_future_pack():
    e, p, lc = engine()
    original = p.search_question_pack

    def send(questions, **options):
        p.reply = (
            QuestionPackSearchResult(
                (result(questions[0]),),
                (),
                (questions[1].question_id,),
                {"question_pack": {"status": "partial"}},
            )
            if len(questions) == 2
            else None
        )
        return original(questions, **options)

    p.search_question_pack = send
    calls = []

    def prove(handles, failures):
        assert lc.failed == [("CORE_02", "invalid_question")]
        assert [x[0] for x in lc.saved] == ["CORE_01"]
        calls.append(failures)
        return True

    lc.assert_question_pack_terminal = prove
    batch = e.process_questions([q(1), q(2), q(3)], question_pack_size=2)
    assert len(p.calls) == 2
    assert calls == [{"CORE_02": "invalid_question"}]
    assert [x.answer.status for x in batch.results] == ["scored", "error", "scored"]
    assert batch.results[1].answer.score is None


def fixture(tmp_path, monkeypatch, mode):
    from src.providers import llm_client

    monkeypatch.chdir(tmp_path)
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    context = _v2_context()
    questions = [
        Question(
            text=next(x for x in MANIFEST["questions"] if x["id"] == qid)["prompt"], question_id=qid
        )
        for qid in QIDS
    ]
    identity = dict(
        entity_id="ENT_CONTEXT_FIXTURE",
        identity_revision=1,
        source_binding_version=1,
        identity_state="verified",
        source_binding_ref="BND_SYNTHETIC",
        source_binding_refs=["BND_SYNTHETIC"],
        identity_snapshot_sha256=context["identity_snapshot_sha256"],
    )
    scopes = {qid: {"scope": "entity", "scope_id": identity["entity_id"]} for qid in QIDS}
    owner = OwnerRefreshSession(
        Mock(assert_current=Mock()),
        {},
        dict(
            fields=[
                dict(
                    field_id=qid,
                    decision="dispatch_new_work",
                    generation=1,
                    request_identity_key="synthetic-" + qid,
                )
                for qid in QIDS
            ],
            subject_key="synthetic-subject",
            perimeter_sha256="d" * 64,
            decision_id="route_" + "a" * 64,
            anchor_version=1,
            manifest_raw_sha256=context["manifest_file_sha256"],
            provider="mimo",
            model="mimo-v2.6-flash",
            information_cutoff="2026-09-22",
        ),
        scopes,
        {},
        identity,
        store,
        observation_context=context,
    )
    monkeypatch.setattr(
        owner, "register_subject_dispatch", lambda request: _owner_receipt(request, time.time())
    )
    policy = _policy()
    policy["routes"][0]["provider_config_ref"] = "mimo"
    if mode == "other_inflight":
        policy["dispatch"]["max_in_flight_total"] = 3
        policy["quota_groups"][0]["max_in_flight"] = 3
        policy["routes"][0]["max_in_flight"] = 3
        store.reserve_budget_attempt(
            policy,
            budget_attempt_id="unrelated-company-physical",
            route_id="pack-route",
            provider="mimo",
            model_requested="mimo-v2.6-flash",
            quota_group="pack-quota",
        )
    lc = QuickScanWorkLifecycle(
        store,
        entity_id=identity["entity_id"],
        run_id="RUN_CONTINUE",
        scan_id="SCAN_CONTINUE",
        identity=identity,
        transport_managed=True,
        owner_refresh_session=owner,
        observation_context=context,
        scope_by_question=scopes,
        routing_fingerprint_by_question=routing_fingerprints(owner),
        route_id="pack-route",
        provider_name="mimo",
        model_requested="mimo-v2.6-flash",
        budget_policy=policy,
        budget_route=policy["routes"][0],
        c06_authority={"schema_version": "2.0.0"},
    )
    # Actual pre-registered pending future work, claimed only by original before_question.
    import hashlib

    future = questions[-1]
    store.create_or_attach(
        **identity,
        question_id=future.question_id,
        generation=1,
        scope="entity",
        scope_id=identity["entity_id"],
        question_fingerprint=hashlib.sha256(future.text.strip().encode()).hexdigest(),
        routing_fingerprint=routing_fingerprints(owner)[future.question_id],
        run_id="RUN_CONTINUE",
        scan_id="SCAN_CONTINUE",
        owner_refresh_binding=owner.binding(future.question_id),
    )
    config = tmp_path / "apis.json"
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
                        "format_repair_budget": 0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    provider = LLMProvider(
        api_key="synthetic-only",
        provider_name="mimo",
        config_file=str(config),
        require_search=True,
        company_name="Synthetic Co",
        entity_id=identity["entity_id"],
    )
    session = Mock()

    def post(*args, **kwargs):
        first = session.post.call_count == 1
        if mode == "unknown" and first:
            from requests.exceptions import ReadTimeout

            raise ReadTimeout("synthetic unknown outcome")
        selected = (
            QIDS[:1] if first and mode == "parsed_missing" else (QIDS[:2] if first else QIDS[2:])
        )
        answers = []
        for qid in selected:
            supplied = body(qid, scored=True)
            if first and (
                (qid == QIDS[1] and mode not in {"answer_generator", "whole_rejected"})
                or mode == "all_invalid"
            ):
                supplied.pop("missing_fields" if mode == "missing_fields" else "coverage")
            answers.append(
                dict(
                    entity_id=identity["entity_id"],
                    company_name="Synthetic Co",
                    question_id=qid,
                    status="scored",
                    score=7,
                    description=supplied,
                    information_as_of="2026-09-22",
                )
            )
        response = Mock(
            status_code=200,
            headers={"x-request-id": "synthetic-request-" + str(session.post.call_count)},
        )
        response.json.return_value = {
            "id": "synthetic-response-" + str(session.post.call_count),
            "model": "mimo-v2.6-flash",
            "object": "chat.completion",
            "usage": {
                "prompt_tokens": 200,
                "completion_tokens": 120,
                "web_search_usage": {"tool_usage": 1},
            },
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": (
                            "not a JSON answer"
                            if mode == "whole_rejected"
                            else json.dumps({"answers": answers})
                        ),
                        "annotations": [
                            {"type": "url_citation", "url": "https://example.org/synthetic"}
                        ],
                    },
                }
            ],
        }
        return response

    session.post.side_effect = post
    monkeypatch.setattr(llm_client.http_client_manager, "get_sync_session", lambda: session)
    if mode == "finalizer_error":
        monkeypatch.setattr(
            lc,
            "after_question_failed",
            Mock(side_effect=RuntimeError("synthetic DB finalizer failure")),
        )
    if mode == "no_hook":
        monkeypatch.setattr(lc, "assert_question_pack_terminal", None, raising=False)
    if mode in {"proof_DB_error", "conflicting_receipt"}:
        import src.utils.quick_scan_question_pack_journal as journal

        original = journal._read

        def read(connection, pack_id):
            row = connection.execute("SELECT 1 FROM work_item WHERE status='failed'").fetchone()
            if row and mode == "proof_DB_error":
                raise RuntimeError("synthetic DB read failure")
            result = original(connection, pack_id)
            if row and mode == "conflicting_receipt":
                result = {**result, "receipt_sha256": "0" * 64}
            return result

        monkeypatch.setattr(journal, "_read", read)
    return store, provider, lc, questions, session, policy


@pytest.mark.parametrize(
    "mode",
    [
        "coverage",
        "missing_fields",
        "all_invalid",
        "other_inflight",
        "unknown",
        "finalizer_error",
        "no_hook",
        "proof_DB_error",
        "conflicting_receipt",
        "whole_rejected",
        "parsed_missing",
        "answer_generator",
    ],
)
def test_actual_sdk_sqlite_known_failure_continuation_and_failclosed(tmp_path, monkeypatch, mode):
    store, provider, lc, questions, session, policy = fixture(tmp_path, monkeypatch, mode)
    generator = None
    if mode == "answer_generator":
        from src.services.answer_generator import AnswerGenerator

        generator = AnswerGenerator()
        original = generator.generate_answer
        generator.generate_answer = lambda question, results: (
            Answer(text="Synthetic generator rejection", score=None, status="error")
            if question.question_id == QIDS[1]
            else original(question, results)
        )
    with standard_answer_transport(True), transport.bind_quick_scan_budget(
        store,
        policy,
        cost_resolver=lambda _: {
            "actual_cost": 0.03,
            "pricing_ref": "fixture-price",
            "source_ref": "synthetic-rate",
        },
    ):
        batch = QAEngine(
            provider, answer_generator=generator, progress_reporter=Mock(), work_item_lifecycle=lc
        ).process_questions(questions, question_pack_size=2)
    expected = (
        2
        if mode
        in {
            "coverage",
            "missing_fields",
            "all_invalid",
            "other_inflight",
            "parsed_missing",
            "answer_generator",
        }
        else 1
    )
    assert session.post.call_count == expected
    assert (
        len(batch.results) == 3
        and batch.results[1].answer.status == "error"
        and batch.results[1].answer.score is None
    )
    rows = store.find_work_items(entity_id="ENT_CONTEXT_FIXTURE", question_ids=QIDS)
    items = {qid: store.get_item(rows[qid][0]["work_item_id"]) for qid in QIDS}
    assert items[QIDS[2]]["status"] == ("result_ready" if expected == 2 else "pending")
    assert len(store.list_attempts(items[QIDS[2]]["work_item_id"])) == (1 if expected == 2 else 0)
    assert store.get_answer_checkpoint(items[QIDS[1]]["work_item_id"]) is None
    if mode != "unknown" and mode != "finalizer_error":
        assert items[QIDS[1]]["status"] == "failed"
    if mode not in {"unknown", "all_invalid", "whole_rejected"}:
        assert store.get_answer_checkpoint(items[QIDS[0]]["work_item_id"]) is not None
    budget = store.get_quick_scan_budget_status("pack-transport")
    assert budget["requests"] == expected + (1 if mode == "other_inflight" else 0)
    if expected == 2:
        assert (
            budget["spent_micros"] == 60000
            and budget["in_flight"] == (1 if mode == "other_inflight" else 0)
            and budget["unreconciled_attempts"] == 0
        )
