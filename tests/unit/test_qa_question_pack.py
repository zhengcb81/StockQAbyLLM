"""Synthetic orchestration/SQLite tests; no financial or real-owner acceptance."""

import copy
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from src.core.models import Answer, QAResult, Question, SearchResult
from src.core.qa_engine import QAEngine
from src.providers.llm_provider import QuestionPackSearchResult
from src.runners.llm_runner import QuickScanWorkLifecycle
from src.utils.quick_scan_work_transport import QuickScanWorkUncertainError


def q(n, **meta):
    return Question(text=f"Synthetic question {n}", question_id=f"CORE_{n:02}", metadata=meta)


def result(question):
    return SearchResult(
        title="Synthetic",
        snippet="Synthetic answer",
        source="llm",
        score=8,
        status="scored",
        metadata={
            "question_id": question.question_id,
            "parsed_score": 8,
            "execution": {
                "work_transport": {
                    "work_attempt_id": "logical-" + question.question_id,
                    "final_receipt": {"attempt_id": "physical-http"},
                }
            },
        },
    )


class Lifecycle:
    def __init__(self):
        self.events = []
        self.references = {}
        self.checkpoints = {}
        self.refused = set()
        self.saved = []
        self.failed = []

    def assert_question_pack_ready(self, questions):
        self.events.append(("ready", tuple(x.question_id for x in questions)))

    def question_pack_group_key(self, question):
        return (
            "ENT_TEST",
            question.metadata.get("company", "Synthetic Co"),
            question.metadata.get("scope", "entity"),
            question.metadata.get("scope_id", "ENT_TEST"),
            question.metadata.get("perimeter", "a" * 64),
        )

    def reference_question(self, question):
        self.events.append(("reference", question.question_id))
        return self.references.get(question.question_id)

    def hydrate_question(self, question):
        self.events.append(("hydrate", question.question_id))
        return self.checkpoints.get(question.question_id)

    def before_question(self, question):
        self.events.append(("claim", question.question_id))
        return {
            "claimed": question.question_id not in self.refused,
            "reason": "fixture_refusal",
            "work_item_id": question.question_id,
            "lease": "synthetic",
        }

    @contextmanager
    def dispatch_question_pack_context(self, handles):
        self.events.append(("dispatch", tuple(h["work_item_id"] for h in handles)))
        yield

    def after_question(self, handle, res):
        self.saved.append((handle["work_item_id"], res))

    def after_question_failed(self, handle, error):
        self.failed.append((handle["work_item_id"], error))


class Provider:
    require_search = True

    def __init__(self):
        self.calls = []
        self.reply = None
        self.error = None

    def get_provider_name(self):
        return "synthetic"

    def search_question_pack(self, questions, **options):
        self.calls.append((tuple(x.question_id for x in questions), options))
        if self.error:
            raise self.error
        return self.reply or QuestionPackSearchResult(
            tuple(result(x) for x in questions), (), (), {"actual": "physical"}
        )

    def search_question(self, question):
        return [result(question)]


def engine():
    p = Provider()
    lc = Lifecycle()
    return QAEngine(p, progress_reporter=Mock(), work_item_lifecycle=lc), p, lc


@pytest.mark.parametrize(
    "options",
    [
        {"question_pack_size": True},
        {"question_pack_size": 0},
        {"question_pack_size": 31},
        {"question_pack_size": 2.0},
        {"thinking_enabled": "true"},
        {"max_output_tokens": True},
        {"max_output_tokens": 0},
        {"max_output_tokens": 32769},
    ],
)
def test_options_fail_before_any_claim_or_send(options):
    e, p, lc = engine()
    with pytest.raises((ValueError, TypeError)):
        e.process_questions([q(1)], **options)
    assert not p.calls and not lc.events


def test_pack_requires_lifecycle_and_search_provider_before_claim():
    e, p, lc = engine()
    e.work_item_lifecycle = None
    with pytest.raises(ValueError):
        e.process_questions([q(1)], question_pack_size=2)
    e.work_item_lifecycle = lc
    p.require_search = False
    with pytest.raises(ValueError):
        e.process_questions([q(1)], question_pack_size=2)
    assert not p.calls and not lc.events


def test_one_request_split_retains_each_logical_transport_and_uses_explicit_defaults():
    e, p, lc = engine()
    batch = e.process_questions([q(1), q(2), q(3)], question_pack_size=3)
    assert len(p.calls) == 1 and p.calls[0][1] == {
        "thinking_enabled": True,
        "max_output_tokens": 8192,
    }
    assert [x.question.question_id for x in batch.results] == [
        "CORE_01",
        "CORE_02",
        "CORE_03",
    ]
    assert [x[0] for x in lc.saved] == ["CORE_01", "CORE_02", "CORE_03"]
    assert (
        batch.results[1].answer.metadata["execution"]["work_transport"]["work_attempt_id"]
        == "logical-CORE_02"
    )
    assert not lc.failed


def test_partial_valid_only_checkpointed_without_automatic_second_call():
    e, p, lc = engine()
    p.reply = QuestionPackSearchResult(
        (result(q(2)),), ("CORE_01",), ("CORE_03",), {"actual": "physical"}
    )
    batch = e.process_questions([q(1), q(2), q(3)], question_pack_size=3)
    assert len(p.calls) == 1 and [x[0] for x in lc.saved] == ["CORE_02"]
    assert [x.answer.status for x in batch.results] == ["error", "scored", "error"]
    assert [x[0] for x in lc.failed] == ["CORE_01", "CORE_03"]
    assert batch.results[0].answer.metadata["execution"] == {"actual": "physical"}


def test_reference_and_hydration_are_before_claim_and_interrupt_consecutive_pack():
    e, p, lc = engine()
    lc.references["CORE_02"] = QAResult(q(2), Answer("Original owner", 8, status="scored"))
    lc.checkpoints["CORE_04"] = {
        "payload": {
            "answer": {
                "description": "Original checkpoint",
                "score": 8,
                "status": "scored",
            },
            "provenance": {"response_completed_at": "2026-09-01T00:00:00Z"},
        }
    }
    batch = e.process_questions([q(1), q(2), q(3), q(4), q(5)], question_pack_size=3)
    assert [x[0] for x in p.calls] == [("CORE_01",), ("CORE_03",), ("CORE_05",)]
    assert [x for x in lc.events if x[0] == "claim"] == [
        ("claim", "CORE_01"),
        ("claim", "CORE_03"),
        ("claim", "CORE_05"),
    ]
    assert batch.results[3].answer.created_at.isoformat().startswith("2026-09-01")
    assert batch.results[3].metadata["answer_checkpoint"]["hydrated"]


def test_claim_refusal_is_not_sent_and_splits_pack():
    e, p, lc = engine()
    lc.refused.add("CORE_02")
    batch = e.process_questions([q(1), q(2), q(3)], question_pack_size=3)
    assert [x[0] for x in p.calls] == [("CORE_01",)]
    assert batch.results[1].metadata["work_claim_refused"]
    assert batch.results[2].metadata["question_pack_not_dispatched"]


def test_scope_perimeter_company_and_security_do_not_mix():
    e, p, lc = engine()
    questions = [
        q(1),
        q(2, perimeter="b" * 64),
        q(3, scope="segment", scope_id="SEG_1"),
        q(4, scope="security", scope_id="SEC_1"),
        q(5, scope="security", scope_id="SEC_1"),
        q(6, company="Other synthetic company"),
    ]
    e.process_questions(questions, question_pack_size=30)
    assert [x[0] for x in p.calls] == [(x.question_id,) for x in questions]


def test_capacity_chunks_preserve_original_order():
    e, p, lc = engine()
    e.process_questions([q(i) for i in range(1, 8)], question_pack_size=3)
    assert [x[0] for x in p.calls] == [
        ("CORE_01", "CORE_02", "CORE_03"),
        ("CORE_04", "CORE_05", "CORE_06"),
        ("CORE_07",),
    ]


def test_uncertain_exception_keeps_original_physical_receipt_for_every_member():
    e, p, lc = engine()
    p.error = QuickScanWorkUncertainError(
        "physical-unknown",
        attempt_receipt={
            "provider": "mimo",
            "requested_model": "mimo-v2.6-pro",
            "request_id": "original-request",
        },
    )
    batch = e.process_questions([q(1), q(2)], question_pack_size=2)
    assert len(p.calls) == 1 and not lc.saved and len(lc.failed) == 2
    for item in batch.results:
        assert item.answer.status == "error" and item.answer.score is None
        assert item.answer.metadata["execution"]["attempt_id"] == "physical-unknown"
        assert item.answer.metadata["execution"]["request_id"] == "original-request"


@pytest.mark.parametrize("mode", ["duplicate", "foreign", "missing_diagnostics"])
def test_provider_contract_violation_fails_whole_group_without_saving(mode):
    e, p, lc = engine()
    results = (
        (result(q(1)), result(q(1)))
        if mode == "duplicate"
        else ((result(q(9)),) if mode == "foreign" else (result(q(1)),))
    )
    p.reply = QuestionPackSearchResult(results, (), (), {})
    batch = e.process_questions([q(1), q(2)], question_pack_size=2)
    assert (
        not lc.saved
        and len(lc.failed) == 2
        and all(x.answer.status == "error" for x in batch.results)
    )


def test_explicit_control_experiment_options_reach_pack_provider():
    e, p, lc = engine()
    e.process_questions(
        [q(1), q(2)],
        question_pack_size=2,
        thinking_enabled=False,
        max_output_tokens=9000,
    )
    assert p.calls[0][1] == {"thinking_enabled": False, "max_output_tokens": 9000}


def test_default_single_does_not_call_pack_or_new_hooks():
    e, p, lc = engine()
    lc.dispatch_context = lambda _: __import__("contextlib").nullcontext()
    e.process_questions([q(1), q(2)])
    assert not p.calls and not any(x[0] == "ready" for x in lc.events)


def test_real_lifecycle_requires_owner_and_transport_before_any_store_rows(tmp_path):
    from src.utils.quick_scan_work_store import QuickScanWorkStore

    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    lc = QuickScanWorkLifecycle(
        store, entity_id="ENT_TEST", run_id="RUN_TEST", scan_id="SCAN_TEST", identity={}
    )
    with pytest.raises(ValueError):
        lc.assert_question_pack_ready([q(1)])
    lc._transport_managed = True
    with pytest.raises(ValueError):
        lc.assert_question_pack_ready([q(1)])
    assert not store.find_work_items(entity_id="ENT_TEST", question_ids=["CORE_01"])["CORE_01"]


@pytest.mark.parametrize(
    "partial",
    [
        False,
        True,
        "partial_pending",
        "invalid_pending",
        "rejected_pending",
        "uncertain_pending",
        "unverified_pending",
    ],
)
def test_actual_lifecycle_client_group_store_and_checkpoints_only_valid_members(
    tmp_path, monkeypatch, partial
):
    """Actual execution code, mock vendor HTTP/owner transport; no real owner acceptance."""
    import hashlib
    import json
    import time

    from src.providers import llm_client
    from src.providers.llm_provider import LLMProvider
    from src.utils import quick_scan_work_transport as transport
    from src.utils.quick_scan_owner_refresh import (
        OwnerRefreshSession,
        routing_fingerprints,
    )
    from src.utils.quick_scan_work_store import QuickScanWorkStore
    from tests.unit.test_quick_scan_observation_context import content
    from tests.unit.test_quick_scan_question_pack_transport import (
        MANIFEST,
        _owner_receipt,
        _policy,
        _v2_context,
    )

    monkeypatch.chdir(tmp_path)
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    context = _v2_context()
    has_pending = isinstance(partial, str)
    qids = ["IQS_01", "IQS_02"] + (["IQS_04"] if has_pending else [])
    questions = [
        Question(
            text=next(x for x in MANIFEST["questions"] if x["id"] == qid)["prompt"],
            question_id=qid,
        )
        for qid in qids
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
    scopes = {qid: {"scope": "entity", "scope_id": identity["entity_id"]} for qid in qids}
    owner = OwnerRefreshSession(
        Mock(assert_current=Mock()),
        {},
        dict(
            fields=[
                dict(
                    field_id=qid,
                    decision="dispatch_new_work",
                    generation=2 if qid == "IQS_02" else 1,
                    request_identity_key="synthetic-" + qid,
                )
                for qid in qids
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
    expected_routing = routing_fingerprints(owner)
    expected_generation = {qid: owner.fields[qid]["generation"] for qid in qids}
    assert len({expected_routing[qid] for qid in qids[:2]}) == 2
    assert [expected_generation[qid] for qid in qids[:2]] == [1, 2]
    owner_requests = []

    def register(request):
        owner_requests.append(copy.deepcopy(request))
        return _owner_receipt(request, time.time())

    monkeypatch.setattr(owner, "register_subject_dispatch", register)
    lc = QuickScanWorkLifecycle(
        store,
        entity_id=identity["entity_id"],
        run_id="RUN_ENTRY_TEST",
        scan_id="SCAN_ENTRY_TEST",
        identity=identity,
        transport_managed=True,
        owner_refresh_session=owner,
        observation_context=context,
        scope_by_question=scopes,
        routing_fingerprint_by_question=expected_routing,
        generation_by_question=expected_generation,
        route_id="pack-route",
        provider_name="mimo",
        model_requested="mimo-v2.6-flash",
        budget_policy=_policy(),
        budget_route={**_policy()["routes"][0], "provider_config_ref": "mimo"},
    )
    if has_pending:
        # Model the real admission ledger: the next question is registered
        # pending before this engine runs, not invented by a skipped claim.
        future = questions[-1]
        registered = store.create_or_attach(
            **identity,
            question_id=future.question_id,
            generation=expected_generation[future.question_id],
            scope="entity",
            scope_id=identity["entity_id"],
            question_fingerprint=hashlib.sha256(future.text.strip().encode("utf-8")).hexdigest(),
            routing_fingerprint=expected_routing[future.question_id],
            run_id="RUN_ENTRY_TEST",
            scan_id="SCAN_ENTRY_TEST",
            owner_refresh_binding=owner.binding(future.question_id),
        )
        assert registered["status"] == "pending"
    config = tmp_path / "llm_apis.json"
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
    answers = []
    for question in (
        questions[:2] if partial == "invalid_pending" else (questions[:1] if partial else questions)
    ):
        body = content()
        body["question_id"] = question.question_id
        body["evidence"][0]["url"] = "https://example.org/source"
        answers.append(
            dict(
                entity_id=identity["entity_id"],
                company_name="Synthetic Co",
                question_id=question.question_id,
                status="scored",
                score=8,
                description=json.dumps(body),
                information_as_of="2026-09-01",
            )
        )
    if partial == "invalid_pending":
        answers[1]["score"] = 11
    response = Mock()
    response.status_code = 200
    response.headers = {"x-request-id": "original-entry-request"}
    response.json.return_value = {
        "id": "entry-response",
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
                        "rejected non-JSON final answer"
                        if partial == "rejected_pending"
                        else json.dumps({"answers": answers})
                    ),
                    "reasoning_content": "private synthetic reasoning must never persist",
                    "annotations": [{"type": "url_citation", "url": "https://example.org/source"}],
                },
            }
        ],
    }
    session = Mock()

    def post(*args, **kwargs):
        assert transport._WORK_BINDING.get() is None
        assert transport._QUESTION_PACK_BINDING.get() is not None
        assert kwargs["json"]["thinking"] == {"type": "enabled"}
        assert kwargs["json"]["max_completion_tokens"] == 8192
        if partial == "uncertain_pending":
            from requests.exceptions import ReadTimeout

            raise ReadTimeout("synthetic response outcome unknown")
        if partial == "unverified_pending":
            response.json.return_value["usage"]["web_search_usage"]["tool_usage"] = 0
            response.json.return_value["choices"][0]["message"]["annotations"] = []
        return response

    session.post.side_effect = post
    monkeypatch.setattr(llm_client.http_client_manager, "get_sync_session", lambda: session)
    observed_errors = []
    for obj, name in [
        (lc, "dispatch_question_pack_context"),
        (provider, "search_question_pack"),
    ]:
        original = getattr(obj, name)

        def trace(*args, _original=original, **kwargs):
            try:
                return _original(*args, **kwargs)
            except Exception as error:
                observed_errors.append((type(error).__name__, str(error), repr(error.__cause__)))
                raise

        monkeypatch.setattr(obj, name, trace)
    policy = _policy()
    policy["routes"][0]["provider_config_ref"] = "mimo"
    with transport.bind_quick_scan_budget(
        store,
        policy,
        cost_resolver=lambda _: {
            "actual_cost": 0.03,
            "pricing_ref": "fixture-price",
            "source_ref": "synthetic-rate",
        },
    ):
        batch = QAEngine(
            provider, progress_reporter=Mock(), work_item_lifecycle=lc
        ).process_questions(questions, question_pack_size=2)
    assert session.post.call_count == 1, observed_errors
    assert [r["question_id"] for r in owner_requests] == qids[:2]
    assert len({r["execution_attempt_id"] for r in owner_requests}) == 1
    assert len({r["work_attempt_id"] for r in owner_requests}) == 2
    rows = store.find_work_items(entity_id=identity["entity_id"], question_ids=qids)
    persisted = {qid: store.get_item(rows[qid][0]["work_item_id"]) for qid in qids}
    assert {qid: persisted[qid]["routing_fingerprint"] for qid in qids} == expected_routing
    assert {qid: persisted[qid]["generation"] for qid in qids} == expected_generation
    checkpoints = [store.get_answer_checkpoint(rows[qid][0]["work_item_id"]) for qid in qids]
    if partial in {"uncertain_pending", "unverified_pending"}:
        assert checkpoints[:2] == [None, None]
        assert [r.answer.status for r in batch.results[:2]] == ["error", "error"]
        if partial == "uncertain_pending":
            assert all(persisted[qid]["status"] == "uncertain" for qid in qids[:2])
            assert all(
                batch.results[index].answer.metadata["execution"]["dispatch_outcome"]["state"]
                == "uncertain"
                for index in range(2)
            )
        else:
            assert all(
                persisted[qid]["status"] not in {"failed", "result_ready"} for qid in qids[:2]
            )
            assert all(
                batch.results[index].metadata["question_pack_error"] == "search_unverified"
                for index in range(2)
            )
    elif partial == "rejected_pending":
        assert checkpoints[:2] == [None, None]
        assert all(persisted[qid]["status"] == "failed" for qid in qids[:2])
        assert [r.answer.status for r in batch.results[:2]] == ["error", "error"]
    else:
        assert checkpoints[0] is not None, [r.answer.text for r in batch.results]
        assert (checkpoints[1] is None) == bool(partial)
        assert persisted[qids[0]]["status"] == "result_ready"
        assert persisted[qids[1]]["status"] == ("failed" if partial else "result_ready")
        assert [r.answer.status for r in batch.results[:2]] == (
            ["scored", "error"] if partial else ["scored", "scored"]
        )
    if has_pending:
        assert persisted[qids[2]]["status"] == "pending" and checkpoints[2] is None
        assert store.list_attempts(persisted[qids[2]]["work_item_id"]) == []
        assert batch.results[2].metadata["question_pack_not_dispatched"]
    assert store.get_quick_scan_budget_status("pack-transport")["requests"] == 1
    assert "private synthetic reasoning" not in repr(checkpoints)
    if checkpoints[0] is not None:
        assert (
            checkpoints[0]["attempt_id"]
            == batch.results[0].answer.metadata["execution"]["work_transport"]["work_attempt_id"]
        )
    assert transport._QUESTION_PACK_BINDING.get() is None
    public = batch.to_quick_scan_dict(
        entity_id=identity["entity_id"],
        company_name="Synthetic Co",
        provider_name="mimo",
        requested_model="mimo-v2.6-flash",
        work_store=store,
    )
    for index, item in enumerate(batch.results[:2]):
        if item.answer.status != "error":
            continue
        receipt = public["execution_receipts"][item.question.question_id]
        answer = public["answers"][item.question.question_id]
        if partial == "uncertain_pending":
            assert receipt["search_status"] == "unverified"
        else:
            expected_search = "unverified" if partial == "unverified_pending" else "executed"
            assert receipt["search_status"] == expected_search
            assert receipt["actual_model"] == "mimo-v2.6-flash"
            assert receipt["response_id"] == "entry-response"
            assert receipt["request_id"] == "original-entry-request"
            assert receipt["response_status"] == "completed"
            assert receipt["http_status_code"] == 200
        assert answer["status"] == "error" and answer["score"] is None
        assert answer["source_urls"] == [] and receipt["source_urls"] == []
        assert answer["check_level"] == "unverified_model_output"
        assert checkpoints[index] is None
    if has_pending:
        pending_receipt = public["execution_receipts"][qids[2]]
        assert pending_receipt["search_status"] == "not_attempted"
        assert pending_receipt["response_id"] is None
        assert pending_receipt["web_search_calls"] == []
    if checkpoints[0] is not None:
        from types import SimpleNamespace

        # Engine recovery seam: load the ACTUAL persisted checkpoint through
        # its public getter. Original owner readiness/group checks still run;
        # no claim/after-hook/transport may run. This is not a fabricated
        # production owner approval or a replacement owner-session adapter.
        def forbidden_recovery_action(*args, **kwargs):
            raise AssertionError("Checkpoint recovery attempted dispatch or state change")

        def read_original_checkpoint(question):
            assert question.question_id == qids[0]
            return store.get_answer_checkpoint(persisted[qids[0]]["work_item_id"])

        recovery = SimpleNamespace(
            assert_question_pack_ready=lc.assert_question_pack_ready,
            question_pack_group_key=lc.question_pack_group_key,
            reference_question=lc.reference_question,
            hydrate_question=read_original_checkpoint,
            before_question=forbidden_recovery_action,
            after_question=forbidden_recovery_action,
            after_question_failed=forbidden_recovery_action,
            dispatch_question_pack_context=forbidden_recovery_action,
        )
        checkpoint_ledger = (tmp_path / "work.sqlite").read_bytes()
        recovered = QAEngine(
            provider, progress_reporter=Mock(), work_item_lifecycle=recovery
        ).process_questions(questions[:1], question_pack_size=2)
        assert recovered.results[0].answer.source == "answer_checkpoint"
        assert (
            recovered.results[0].answer.score == 8
            and recovered.results[0].metadata["answer_checkpoint"]["hydrated"]
        )
        assert (
            session.post.call_count == 1
            and (tmp_path / "work.sqlite").read_bytes() == checkpoint_ledger
        )
    # Original ready/missing/uncertain outcomes cannot become a fresh request
    # on re-entry; the checkpoint and unstarted pending member stay intact.
    before_checkpoints = copy.deepcopy(checkpoints)
    before_items = {qid: store.get_item(rows[qid][0]["work_item_id"]) for qid in qids}
    before_attempts = {qid: store.list_attempts(rows[qid][0]["work_item_id"]) for qid in qids}
    with transport.bind_quick_scan_budget(
        store,
        policy,
        cost_resolver=lambda _: {
            "actual_cost": 0.03,
            "pricing_ref": "fixture-price",
            "source_ref": "synthetic-rate",
        },
    ):
        QAEngine(provider, progress_reporter=Mock(), work_item_lifecycle=lc).process_questions(
            questions, question_pack_size=2
        )
    assert session.post.call_count == 1, observed_errors
    assert [
        store.get_answer_checkpoint(rows[qid][0]["work_item_id"]) for qid in qids
    ] == before_checkpoints
    # The legacy create_or_attach INSERT OR IGNORE may advance SQLite's
    # header transaction counter. Compare original business records instead;
    # byte-readonly checkpoint hydration is proven independently above.
    assert {qid: store.get_item(rows[qid][0]["work_item_id"]) for qid in qids} == before_items
    assert {
        qid: store.list_attempts(rows[qid][0]["work_item_id"]) for qid in qids
    } == before_attempts
    if has_pending:
        assert store.get_item(persisted[qids[2]]["work_item_id"])["status"] == "pending"
        assert store.list_attempts(persisted[qids[2]]["work_item_id"]) == []


def test_composed_approved_owner_session_keeps_native_context_and_outer_admission_guard(
    tmp_path,
):
    """Synthetic object graph probes actual wrapper protocol, not a consent gold."""
    from types import SimpleNamespace

    from src.utils.quick_scan_owner_refresh import OwnerRefreshSession
    from src.utils.quick_scan_refresh_execution import ApprovedRefreshSession
    from src.utils.quick_scan_work_store import QuickScanWorkStore
    from tests.unit.test_quick_scan_question_pack_transport import MANIFEST, _v2_context

    context = _v2_context()
    store = QuickScanWorkStore(tmp_path / "wrapper.sqlite")
    qid = "IQS_01"
    question = Question(
        text=next(x for x in MANIFEST["questions"] if x["id"] == qid)["prompt"],
        question_id=qid,
    )
    scopes = {qid: {"scope": "entity", "scope_id": "ENT_CONTEXT_FIXTURE"}}
    native = OwnerRefreshSession(
        Mock(assert_current=Mock()),
        {},
        dict(
            fields=[{"field_id": qid, "decision": "dispatch_new_work"}],
            perimeter_sha256="d" * 64,
        ),
        scopes,
        {},
        {},
        store,
        observation_context=context,
    )
    # Only the shape-level wrapper is synthesized. The real prepare_execution
    # authority/consent construction is deliberately not claimed by this test.
    approved = object.__new__(ApprovedRefreshSession)
    approved.native = native
    approved.service = SimpleNamespace(store=store)
    approved.plan = native.plan
    approved.fields = native.fields
    approved.scope_bindings = scopes
    approved.check = Mock()
    lc = QuickScanWorkLifecycle(
        store,
        entity_id="ENT_CONTEXT_FIXTURE",
        run_id="RUN_WRAPPER",
        scan_id="SCAN_WRAPPER",
        identity={},
        transport_managed=True,
        owner_refresh_session=approved,
        observation_context=context,
        scope_by_question=scopes,
    )
    lc.assert_question_pack_ready([question])
    approved.check.assert_called_once()
    approved.check = Mock(side_effect=ValueError("synthetic admission revoked"))
    with pytest.raises(ValueError, match="admission revoked"):
        lc.assert_question_pack_ready([question])
    approved.check = Mock()
    approved.service.store = QuickScanWorkStore(tmp_path / "foreign-wrapper.sqlite")
    with pytest.raises(ValueError):
        lc.assert_question_pack_ready([question])
    approved.check.assert_not_called()


def test_explicit_member_rejection_carries_typed_reason_but_storage_error_does_not():
    e, p, lc = engine()
    e.answer_generator.generate_answer = lambda *a: Answer(
        "Synthetic rejected body", None, status="error"
    )
    e.process_questions([q(1)], question_pack_size=3)
    assert lc.failed == [("CORE_01", "answer_generator_rejected_member")] and not lc.saved
    e, p, lc = engine()

    def storage_failure(*a):
        raise RuntimeError("Synthetic storage error")

    lc.after_question = storage_failure
    e.process_questions([q(1)], question_pack_size=3)
    assert lc.failed == [("CORE_01", "RuntimeError")]


@pytest.mark.parametrize(
    "kind", ["missing", "invalid", "unverified", "truncated", "transport", "contract"]
)
def test_unsaved_pack_stops_later_claims_without_losing_pending_questions(kind):
    e, p, lc = engine()
    if kind == "missing":
        p.reply = QuestionPackSearchResult((result(q(1)), result(q(3))), ("CORE_02",), (), {})
    elif kind == "invalid":
        p.reply = QuestionPackSearchResult((result(q(2)),), (), ("CORE_01", "CORE_03"), {})
    elif kind in {"unverified", "truncated"}:
        p.reply = QuestionPackSearchResult(
            (),
            ("CORE_01", "CORE_02", "CORE_03"),
            (),
            {
                "question_pack": {"status": "search_unverified"},
                "search_status": "unverified",
                "response_status": "incomplete" if kind == "truncated" else "completed",
            },
        )
    elif kind == "transport":
        p.error = QuickScanWorkUncertainError(
            "physical-uncertain", {"request_id": "original-request"}
        )
    else:
        p.reply = QuestionPackSearchResult((result(q(99)),), (), (), {})
    batch = e.process_questions([q(i) for i in range(1, 9)], question_pack_size=3)
    assert [call[0] for call in p.calls] == [("CORE_01", "CORE_02", "CORE_03")]
    assert [event[1] for event in lc.events if event[0] == "claim"] == [
        "CORE_01",
        "CORE_02",
        "CORE_03",
    ]
    assert [item.question.question_id for item in batch.results] == [
        f"CORE_{i:02}" for i in range(1, 9)
    ]
    for item in batch.results[3:]:
        assert item.answer.status == "error" and item.answer.score is None
        assert item.metadata["question_pack_not_dispatched"] is True
        assert not item.metadata.get("answer_checkpoint")
        assert item.answer.metadata.get("execution") is None
    if kind in {"unverified", "truncated"}:
        assert [reason for _, reason in lc.failed] == ["search_unverified"] * 3
    if kind == "transport":
        for item in batch.results[:3]:
            assert item.answer.metadata["execution"]["attempt_id"] == "physical-uncertain"
            assert item.answer.metadata["execution"]["dispatch_outcome"]["state"] == "uncertain"


def test_group_boundary_flush_failure_stops_before_claiming_new_scope():
    e, p, lc = engine()
    p.reply = QuestionPackSearchResult((), ("CORE_01",), (), {})
    batch = e.process_questions(
        [q(1), q(2, scope="security", scope_id="SEC_TEST"), q(3)], question_pack_size=3
    )
    assert [event for event in lc.events if event[0] == "claim"] == [("claim", "CORE_01")]
    assert len(p.calls) == 1
    assert all(item.metadata["question_pack_not_dispatched"] for item in batch.results[1:])


def test_failed_pack_keeps_existing_reference_and_checkpoint_recovery_never_sends_pending():
    e, p, lc = engine()
    lc.checkpoints["CORE_01"] = {
        "payload": {
            "answer": {"description": "Stored original", "score": 7, "status": "scored"},
            "provenance": {"response_completed_at": "2026-09-01T00:00:00Z"},
        }
    }
    lc.refused.add("CORE_02")
    batch = e.process_questions([q(1), q(2), q(3), q(4)], question_pack_size=3)
    assert not p.calls and not lc.saved and not lc.failed
    assert (
        batch.results[0].answer.score == 7
        and batch.results[0].metadata["answer_checkpoint"]["hydrated"]
    )
    assert [event for event in lc.events if event[0] == "claim"] == [("claim", "CORE_02")]
    assert all(item.metadata["question_pack_not_dispatched"] for item in batch.results[2:])


def test_failure_hook_exception_preserves_all_received_members_and_stops_further_dispatch():
    e, p, lc = engine()
    p.reply = QuestionPackSearchResult((result(q(2)),), ("CORE_01",), ("CORE_03",), {})

    def hook_failed(*args):
        raise RuntimeError("private finalization details must not leak")

    lc.after_question_failed = hook_failed
    batch = e.process_questions([q(i) for i in range(1, 6)], question_pack_size=3)
    assert len(p.calls) == 1 and [item[0] for item in lc.saved] == ["CORE_02"]
    assert [item.answer.status for item in batch.results] == [
        "error",
        "scored",
        "error",
        "error",
        "error",
    ]
    assert "private finalization" not in repr(batch)


def test_search_or_transport_stage_is_not_reclassified_as_deterministic_missing():
    for stage in ("search_unverified", "transport_failed", "unavailable"):
        e, p, lc = engine()
        p.reply = QuestionPackSearchResult(
            (), ("CORE_01", "CORE_02"), (), {"question_pack": {"status": stage}}
        )
        batch = e.process_questions([q(1), q(2), q(3)], question_pack_size=2)
        assert len(p.calls) == 1 and not lc.saved
        assert lc.failed == [("CORE_01", stage), ("CORE_02", stage)]
        assert batch.results[2].metadata["question_pack_not_dispatched"]


@pytest.mark.parametrize("with_fake_proof", [False, True])
def test_failed_member_cannot_promote_arbitrary_nested_executed_claim(with_fake_proof):
    e, p, lc = engine()
    execution = {
        "search_status": "executed",
        "actual_model": "forged-model",
        "response_id": "forged-response",
        "request_id": "forged-request",
        "source_urls": ["https://example.org/not-an-owner-proof"],
    }
    if with_fake_proof:
        execution["question_pack_transport"] = {"pack_id": "forged-pack"}
        execution["work_transport"] = {"final_receipt": dict(execution)}
    p.reply = QuestionPackSearchResult((), ("CORE_01", "CORE_02"), (), execution)
    batch = e.process_questions([q(1), q(2), q(3)], question_pack_size=2)
    public = batch.to_quick_scan_dict(
        entity_id="ENT_TEST",
        company_name="Synthetic Co",
        provider_name="mimo",
        requested_model="mimo-v2.6-flash",
    )
    failed = public["execution_receipts"]["CORE_01"]
    assert failed["search_status"] == "not_attempted"
    assert failed["actual_model"] is None and failed["response_id"] is None
    assert public["answers"]["CORE_01"]["source_urls"] == []
    assert all(x.answer.status == "error" and x.answer.score is None for x in batch.results)
    assert len(p.calls) == 1 and not lc.saved
    assert public["execution_receipts"]["CORE_03"]["search_status"] == "not_attempted"


@pytest.mark.parametrize("search_status", ["unverified", "unavailable", "not_attempted"])
def test_failed_pack_keeps_negative_search_state_without_promoting_unverified_identity(
    search_status,
):
    e, p, lc = engine()
    execution = {
        "search_status": search_status,
        "actual_model": "not-owner-verified",
        "response_id": "not-owner-verified",
        "source_urls": ["https://example.org/not-answer-evidence"],
    }
    p.reply = QuestionPackSearchResult((), ("CORE_01", "CORE_02"), (), execution)
    batch = e.process_questions([q(1), q(2)], question_pack_size=2)
    public = batch.to_quick_scan_dict(
        entity_id="ENT_TEST",
        company_name="Synthetic Co",
        provider_name="mimo",
        requested_model="mimo-v2.6-flash",
    )
    for qid in ["CORE_01", "CORE_02"]:
        receipt = public["execution_receipts"][qid]
        assert receipt["search_status"] == search_status
        assert receipt["actual_model"] is None and receipt["response_id"] is None
        assert receipt["source_urls"] == []
        assert public["answers"][qid]["score"] is None
    assert not lc.saved and len(p.calls) == 1
