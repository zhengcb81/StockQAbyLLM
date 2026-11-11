"""Actual sent short context is an answer source, never a search receipt."""

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from src.utils.quick_scan_research_intent import bind_intent
from src.utils.quick_scan_work_store import QuickScanWorkStore


def test_provided_context_binding_is_optional_and_does_not_repurpose_members(tmp_path):
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    with bind_intent(store, "a" * 64, "b" * 64, ["WORK_original"], provided_context=None):
        pass
    with store._connect() as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(quick_scan_research_intent)")
        }
    assert "provided_context_json" in columns


from contextlib import closing
from datetime import datetime, timezone
from unittest.mock import Mock

from src.providers.llm_client import LLMClient
from src.utils.quick_scan_provided_context import canonical, prepare_provided_context
from src.utils.quick_scan_work_transport import project_quick_scan_answer_sources
from tests.unit.test_llm_client import _b01_thinking_response
from tests.unit.test_quick_scan_observation_context import content
from tests.unit.test_quick_scan_question_pack_transport import _bindings, _v2_context


@pytest.fixture
def provided(tmp_path, monkeypatch):
    from src.providers import llm_client
    from src.utils.http_client import http_client_manager

    monkeypatch.setattr(llm_client, "_utc_now", lambda: "2027-01-15T08:00:00Z")
    store = QuickScanWorkStore(tmp_path / "sources.sqlite", clock=lambda: 1800000000.0)
    context = _v2_context()
    qid = "IQS_01"
    row = context["questions"][qid]
    meta = row["metadata"]
    binding = dict(
        protocol="stockqa.owner_refresh_binding/2.0.0",
        subject_key=row["subject_binding"]["subject_ref"]["analysis_subject_id"] + "@1",
        perimeter_sha256="d" * 64,
        decision_id="route_" + "e" * 64,
        anchor_version=1,
        manifest_raw_sha256=context["manifest_file_sha256"],
        provider="mimo",
        model="mimo-v2.6-flash",
        information_cutoff=meta["information_cutoff"],
        request_identity_key="original-request",
        target_generation=1,
        question_id=qid,
        scope="entity",
        scope_id=meta["entity_id"],
        entity_id=meta["entity_id"],
        identity_revision=1,
        subject_context_schema=context["schema"],
    )
    item = store.create_or_attach(
        entity_id=meta["entity_id"],
        question_id=qid,
        generation=1,
        scope="entity",
        scope_id=meta["entity_id"],
        identity_revision=1,
        source_binding_version=1,
        identity_state="verified",
        source_binding_ref="BND_PACK",
        source_binding_refs=["BND_PACK"],
        identity_snapshot_sha256=context["identity_snapshot_sha256"],
        question_fingerprint=row["work_prompt_sha256"],
        routing_fingerprint=hashlib.sha256(
            canonical(
                dict(
                    subject_key=binding["subject_key"],
                    request_identity_key=binding["request_identity_key"],
                )
            ).encode()
        ).hexdigest(),
        run_id="RUN_SYNTHETIC_PACK",
        scan_id="SCAN_SYNTHETIC_PACK",
        owner_refresh_binding=binding,
    )
    wid = item["work_item_id"]
    lease = store.claim(wid, lease_seconds=300)
    store.freeze_observation_context_before_send(wid, lease, context)
    subject = row["subject_binding"]["subject_ref"]
    company_ref = dict(path="native-derived/company.json", bytes=10, sha256="a" * 64)
    record = dict(
        evidence_id="provided-annual",
        source_url="https://issuer.example.invalid/annual",
        published_at="2026-09-01",
        period_start="2026-01-01",
        period_end="2026-06-30",
        excerpt="Synthetic same-period original facts.",
        issuer_ref=company_ref,
    )
    record["excerpt_sha256"] = hashlib.sha256(record["excerpt"].encode()).hexdigest()
    board = {"records": [record]}
    raw = canonical(board).encode()
    task = SimpleNamespace(
        raw={
            "protocol": "iqs.company-scan-task/1.0.0",
            "information_cutoff": meta["information_cutoff"],
            "evidence_epoch_ref": dict(
                path="native-derived/evidence-board.json",
                bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
            ),
        },
        evidence_board_doc=board,
        company_ref=company_ref,
    )
    dynamic = dict(
        company_ref=company_ref,
        information_cutoff=meta["information_cutoff"],
        requested_question_ids=[qid],
        evidence=[copy.deepcopy(record)],
    )
    sent = []

    def post(*args, **kwargs):
        sent.append(copy.deepcopy(kwargs["json"]))
        return _b01_thinking_response(
            "mimo_chat_completions", "mimo-v2.6-flash", final='{"answers":[]}'
        )

    wire = Mock()
    wire.post.side_effect = post
    monkeypatch.setattr(http_client_manager, "get_sync_session", lambda: wire)
    client = LLMClient(
        "SYNTHETIC", "mimo-v2.6-flash", "https://api.xiaomimimo.com/v1", provider_name="mimo"
    )
    return SimpleNamespace(
        store=store,
        work_id=wid,
        lease=lease,
        context=context,
        task=task,
        dynamic=dynamic,
        subject=subject,
        client=client,
        sent=sent,
    )


def send(f, *, omitted=False):
    prepared = prepare_provided_context(
        f.task, f.dynamic, store=f.store, work_item_ids=[f.work_id], subject=f.subject
    )
    prompt = canonical({**f.dynamic, "evidence": []} if omitted else f.dynamic)
    with (
        _bindings((f.store, ((f.work_id, f.lease),), [1800000000.0])),
        bind_intent(f.store, "a" * 64, "b" * 64, [f.work_id], provided_context=prepared),
    ):
        return f.client.send_search_request(
            prompt,
            "Frozen original rules",
            thinking_enabled=True,
            max_output_tokens=2048,
            research_final_json=True,
        )


def _checkpoint_provided_only_citation(provided):
    f = provided
    result = send(f)
    execution = result.execution_metadata
    projected = project_quick_scan_answer_sources(
        execution, work_store=f.store, work_item_id=f.work_id
    )
    assert (
        projected["search_source_urls"]
        == list(execution["source_urls"])
        == ["https://example.org/source"]
    )
    assert projected["provided_context_sources"][0]["origin"] == "provided_context"
    assert projected["source_urls"] == [
        "https://example.org/source",
        "https://issuer.example.invalid/annual",
    ]
    attempt = f.store.list_attempts(f.work_id)[0]["attempt_id"]
    receipt = f.store.get_attempt_response(attempt)["receipt"]
    body = content()
    body["question_id"] = "IQS_01"
    body["evidence"][0]["url"] = "https://issuer.example.invalid/annual"
    normalized = dict(
        entity_id=f.context["questions"]["IQS_01"]["metadata"]["entity_id"],
        question_id="IQS_01",
        status="scored",
        score=8,
        description=body["summary"],
    )
    cp = f.store.save_answer_checkpoint(
        f.work_id,
        f.lease,
        attempt,
        answer=normalized,
        execution_receipt=receipt,
        observation_context=f.context,
        standard_answer=body,
    )
    assert cp["payload"]["provenance"]["source_urls"] == receipt["source_urls"]
    assert (
        cp["payload"]["provenance"]["provided_context_sources"]
        == projected["provided_context_sources"]
    )
    reopened = QuickScanWorkStore(f.store.path)
    assert reopened.get_answer_checkpoint(f.work_id) == cp
    assert reopened.get_attempt_response(attempt)["receipt"] == receipt and len(f.sent) == 1
    from src.utils.quick_scan_observation_context import (
        bind_context_to_work_item,
        build_observation,
    )

    bound = bind_context_to_work_item(
        f.context, work_item=reopened.get_item(f.work_id), question_id="IQS_01"
    )
    observation = build_observation(
        cp["payload"],
        context=bound,
        standard_answer=body,
        started_at="2027-01-15T08:00:00Z",
        answer_source_urls=projected["source_urls"],
    )
    assert observation["answer"]["evidence"][0]["url"] == "https://issuer.example.invalid/annual"
    assert observation["execution"]["search_receipt_id"] == receipt["search_receipt_id"]


def test_actual_sent_provided_only_citation_checkpoints_and_reopens_without_changing_search_receipt(
    provided,
):
    _checkpoint_provided_only_citation(provided)


@pytest.mark.parametrize("fault", ["unsent", "issuer", "excerpt", "future", "board"])
def test_unbound_context_sources_never_reach_a_paid_send(provided, fault):
    f = provided
    if fault == "issuer":
        f.dynamic["evidence"][0]["issuer_ref"] = {"foreign": True}
    elif fault == "excerpt":
        f.dynamic["evidence"][0]["excerpt"] = "changed"
    elif fault == "future":
        for doc in [f.dynamic, f.task.evidence_board_doc]:
            doc["evidence" if "evidence" in doc else "records"][0]["published_at"] = "2099-01-01"
        raw = canonical(f.task.evidence_board_doc).encode()
        f.task.raw["evidence_epoch_ref"].update(
            bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()
        )
    elif fault == "board":
        f.task.raw["evidence_epoch_ref"]["sha256"] = "f" * 64
    with pytest.raises(ValueError):
        send(f, omitted=fault == "unsent")
    assert not f.sent
    with closing(f.store._connect()) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM quick_scan_budget_attempt").fetchone()[0] == 0
        )


def test_provided_summary_tampering_and_foreign_member_are_not_source_authority(provided):
    import sqlite3

    f = provided
    result = send(f)
    execution = result.execution_metadata
    with pytest.raises(ValueError):
        project_quick_scan_answer_sources(
            execution, work_store=f.store, work_item_id="WORK_foreign"
        )
    with sqlite3.connect(f.store.path) as c:
        c.execute("DROP TRIGGER quick_scan_research_intent_no_update")
        raw = c.execute("SELECT provided_context_json FROM quick_scan_research_intent").fetchone()[
            0
        ]
        data = json.loads(raw)
        data["records"][0]["source_url"] = "https://foreign.example.invalid/annual"
        from src.utils.quick_scan_research_intent import _encode

        c.execute("UPDATE quick_scan_research_intent SET provided_context_json=?", (_encode(data),))
    with pytest.raises(ValueError, match="binding_corrupt"):
        project_quick_scan_answer_sources(execution, work_store=f.store, work_item_id=f.work_id)
    assert len(f.sent) == 1


def test_no_actual_search_cannot_borrow_provided_context_as_search(provided, monkeypatch):
    f = provided
    from src.utils.http_client import http_client_manager

    def post(*args, **kwargs):
        f.sent.append(kwargs["json"])
        value = _b01_thinking_response(
            "mimo_chat_completions", "mimo-v2.6-flash", final='{"answers":[]}'
        )
        payload = value.json.return_value
        payload["choices"][0]["message"]["annotations"] = []
        value.content = json.dumps(payload).encode()
        return value

    wire = Mock()
    wire.post.side_effect = post
    monkeypatch.setattr(http_client_manager, "get_sync_session", lambda: wire)
    result = send(f)
    with pytest.raises(ValueError, match="original_executed_search"):
        project_quick_scan_answer_sources(
            result.execution_metadata, work_store=f.store, work_item_id=f.work_id
        )
    assert len(f.sent) == 1 and f.store.get_answer_checkpoint(f.work_id) is None


def test_checkpoint_cannot_self_report_a_different_provided_summary(provided):
    import sqlite3

    f = provided
    result = send(f)
    attempt = f.store.list_attempts(f.work_id)[0]["attempt_id"]
    receipt = f.store.get_attempt_response(attempt)["receipt"]
    body = content()
    body["question_id"] = "IQS_01"
    body["evidence"][0]["url"] = "https://issuer.example.invalid/annual"
    answer = dict(
        entity_id=f.subject["entity_id"],
        question_id="IQS_01",
        status="scored",
        score=8,
        description=body["summary"],
    )
    cp = f.store.save_answer_checkpoint(
        f.work_id,
        f.lease,
        attempt,
        answer=answer,
        execution_receipt=receipt,
        observation_context=f.context,
        standard_answer=body,
    )
    with sqlite3.connect(f.store.path) as connection:
        connection.execute("DROP TRIGGER answer_checkpoint_no_update")
        payload = copy.deepcopy(cp["payload"])
        payload["provenance"]["provided_context_sources"][0][
            "source_url"
        ] = "https://foreign.invalid/report"
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        connection.execute(
            "UPDATE answer_checkpoint SET payload_json=?,payload_sha256=?",
            (raw, hashlib.sha256(raw.encode()).hexdigest()),
        )
    with pytest.raises(ValueError, match="summary differs"):
        f.store.get_answer_checkpoint(f.work_id)
    assert len(f.sent) == 1


def test_v25_migration_keeps_old_intent_and_receipt_bytes(provided):
    import sqlite3

    f = provided
    # An ordinary old intent has no provided data and retains its exact hash.
    with (
        _bindings((f.store, ((f.work_id, f.lease),), [1800000000.0])),
        bind_intent(f.store, "a" * 64, "c" * 64, [f.work_id]),
    ):
        result = f.client.send_search_request(
            "Original old input",
            "Frozen rules",
            thinking_enabled=True,
            max_output_tokens=2048,
            research_final_json=True,
        )
    with sqlite3.connect(f.store.path) as connection:
        before = connection.execute(
            "SELECT task_sha256,intent_sha256,native_attempt_id,request_payload_sha256,work_item_ids_json,binding_sha256 FROM quick_scan_research_intent"
        ).fetchall()
        responses = connection.execute("SELECT * FROM quick_scan_attempt_response").fetchall()
        connection.execute(
            "ALTER TABLE quick_scan_research_intent DROP COLUMN provided_context_json"
        )
        connection.execute("DROP TABLE quick_scan_research_visible_output_recovery")
        connection.execute("PRAGMA user_version=25")
    from src.utils.quick_scan_refresh_inspect import ReadOnlyWorkView

    view = ReadOnlyWorkView(f.store.path)
    assert view.get_item(f.work_id)["question_id"] == "IQS_01"
    upgraded = QuickScanWorkStore(f.store.path)
    with closing(upgraded._connect()) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 27
        assert [
            tuple(r)
            for r in connection.execute(
                "SELECT task_sha256,intent_sha256,native_attempt_id,request_payload_sha256,work_item_ids_json,binding_sha256 FROM quick_scan_research_intent"
            )
        ] == before
        assert [
            tuple(r) for r in connection.execute("SELECT * FROM quick_scan_attempt_response")
        ] == responses
        assert (
            connection.execute(
                "SELECT provided_context_json FROM quick_scan_research_intent"
            ).fetchone()[0]
            is None
        )
    assert len(f.sent) == 1


@pytest.mark.parametrize("kind", ["messages", "wrong_role", "mismatch", "duplicate", "wrapper"])
def test_original_message_carrier_matches_only_exact_user_dynamic_context(provided, kind):
    from src.utils.quick_scan_provided_context import assert_sent_context

    f = provided
    prepared = prepare_provided_context(
        f.task, f.dynamic, store=f.store, work_item_ids=[f.work_id], subject=f.subject
    )
    user = {"role": "user", "content": "Original directive\n" + canonical(f.dynamic)}
    messages = [{"role": "system", "content": "Frozen original rules"}, user]
    if kind == "wrong_role":
        user["role"] = "assistant"
    elif kind == "mismatch":
        user["content"] = canonical({**f.dynamic, "evidence": []})
    elif kind == "duplicate":
        messages.append(copy.deepcopy(user))
    prompt = json.dumps({"messages": messages} if kind == "wrapper" else messages)
    if kind == "messages":
        assert_sent_context(prepared, prompt)
    else:
        with pytest.raises(ValueError, match="not_actually_sent"):
            assert_sent_context(prepared, prompt)
    assert not f.sent


def test_original_research_sdk_messages_carrier_binds_provided_context(provided, monkeypatch):
    from src.providers import llm_research_client
    from src.providers.llm_research_client import ResearchChatSession
    from src.utils.http_client import http_client_manager
    from tests.unit.test_llm_research_client import TOOLS

    f = provided
    monkeypatch.setattr(llm_research_client, "utc_now", lambda: "2027-01-15T08:00:00Z")
    f.client.base_url = "https://api.xiaomimimo.com/v1/chat/completions"
    posts = []

    def post(*args, **kwargs):
        posts.append(kwargs["json"])
        return _b01_thinking_response(
            "mimo_chat_completions", "mimo-v2.6-flash", final='{"answers":[]}'
        )

    wire = Mock()
    wire.post.side_effect = post
    monkeypatch.setattr(http_client_manager, "get_metered_sync_session", lambda: wire)
    prepared = prepare_provided_context(
        f.task, f.dynamic, store=f.store, work_item_ids=[f.work_id], subject=f.subject
    )
    with (
        _bindings((f.store, ((f.work_id, f.lease),), [1800000000.0])),
        bind_intent(f.store, "a" * 64, "d" * 64, [f.work_id], provided_context=prepared),
    ):
        result = ResearchChatSession(f.client, system_prefix="Original rules", tools=TOOLS).step(
            user_context="Original directive\n" + canonical(f.dynamic)
        )
    assert len(posts) == 1
    assert posts[0]["messages"][-1]["role"] == "user"
    with closing(f.store._connect()) as connection:
        context = json.loads(
            connection.execute(
                "SELECT provided_context_json FROM quick_scan_research_intent"
            ).fetchone()[0]
        )
    assert context["records"][0]["source_url"] == "https://issuer.example.invalid/annual"
    assert result["receipt"]["research_intent_binding_sha256"]


@pytest.mark.parametrize("field", ["period_end", "published_at"])
def test_future_reported_period_is_not_a_future_publication(provided, field):
    f = provided
    for records in (f.task.evidence_board_doc["records"], f.dynamic["evidence"]):
        records[0][field] = "2030-12-31"
    raw = canonical(f.task.evidence_board_doc).encode()
    f.task.raw["evidence_epoch_ref"].update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    if field == "published_at":
        with pytest.raises(ValueError, match="future_date"):
            send(f)
        assert not f.sent
    else:
        result = send(f)
        projected = project_quick_scan_answer_sources(
            result.execution_metadata, work_store=f.store, work_item_id=f.work_id
        )
        assert projected["provided_context_sources"][0]["period_end"] == "2030-12-31"
        assert len(f.sent) == 1


def _sealed_provided_input(f, tmp_path):
    """Drive the original SDK/SQLite checkpoint and original complete adapter."""
    from pathlib import Path

    from src.utils.quick_scan_c06_authority import load_c06_authority
    from src.utils.quick_scan_result_outbox import canonical_sha256
    from tests.unit.test_quick_scan_question_pack_transport import MANIFEST

    _checkpoint_provided_only_citation(f)
    meta = f.context["questions"]["IQS_01"]["metadata"]
    item = f.store.get_item(f.work_id)
    identity = {
        key: item[key]
        for key in (
            "identity_revision",
            "source_binding_version",
            "identity_state",
            "source_binding_ref",
            "identity_snapshot_sha256",
        )
    }
    identity["source_binding_refs"] = ["BND_PACK"]
    question = next(q for q in MANIFEST["questions"] if q["id"] == "IQS_01")
    # Map the actual frozen run labels via the original attach API, never relabel.
    attached = f.store.create_or_attach(
        entity_id=item["entity_id"],
        question_id="IQS_01",
        generation=1,
        scope="entity",
        scope_id=item["entity_id"],
        **identity,
        question_fingerprint=item["question_fingerprint"],
        routing_fingerprint=item["routing_fingerprint"],
        run_id=meta["run_id"],
        scan_id=meta["scan_id"],
    )
    assert attached["work_item_id"] == f.work_id
    doc = json.loads(
        (
            Path(__file__).parents[1] / "fixtures" / "quick_scan_c06_authority_v2_fixture.json"
        ).read_text(encoding="utf8")
    )
    doc.update(
        schema_version="3.0.0",
        observation_context=f.context,
        observation_context_sha256=canonical_sha256(f.context),
    )
    path = tmp_path / "authority-v3.json"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf8")
    from src.utils.quick_scan_question_manifest import load_question_manifest

    manifest = load_question_manifest(
        Path(__file__).parents[1] / "fixtures" / "quick_scan_c06_manifest_v2_fixture.json"
    )
    authority = load_c06_authority(
        path, manifest=manifest, identity_snapshot_sha256=f.context["identity_snapshot_sha256"]
    )
    return authority, identity, question


def test_provided_only_source_seals_exports_reopens_and_hydrates_without_resend(provided, tmp_path):
    from src.core.models import Question
    from src.core.qa_engine import QAEngine
    from src.runners.llm_runner import QuickScanWorkLifecycle
    from src.utils.quick_scan_delivery_seal import seal_result_delivery
    from src.utils.quick_scan_result_outbox import validate_checkpoint_binding

    f = provided
    authority, identity, question = _sealed_provided_input(f, tmp_path)
    attempt = f.store.list_attempts(f.work_id)[0]["attempt_id"]
    original_receipt = f.store.get_attempt_response(attempt)["receipt"]
    original_cp = f.store.get_answer_checkpoint(f.work_id)
    result = seal_result_delivery(f.store, f.work_id, authority=authority)
    assert result["action"] == "sealed"
    delivery = f.store.get_result_delivery(f.work_id)
    assert delivery["state"] == "ready"
    assert (
        delivery["package"]["items"][0]["observation"]["answer"]["evidence"][0]["url"]
        == "https://issuer.example.invalid/annual"
    )
    # Pure validation stays strict without a source set verified by the store.
    with pytest.raises(ValueError, match="evidence URL is not bound"):
        validate_checkpoint_binding(delivery["package"], original_cp)
    trusted_urls = ("https://issuer.example.invalid/annual",)
    validate_checkpoint_binding(
        delivery["package"], original_cp, verified_provided_source_urls=trusted_urls
    )
    bad_package = copy.deepcopy(delivery["package"])
    bad_package["items"][0]["observation"]["answer"]["evidence"][0][
        "url"
    ] = "https://arbitrary.invalid/answer"
    with pytest.raises(ValueError, match="evidence URL is not bound"):
        validate_checkpoint_binding(
            bad_package, original_cp, verified_provided_source_urls=trusted_urls
        )
    with pytest.raises(ValueError, match="provided answer source projection mismatch"):
        validate_checkpoint_binding(
            delivery["package"],
            original_cp,
            verified_provided_source_urls=("https://arbitrary.invalid/answer",),
        )
    forged_cp = copy.deepcopy(original_cp)
    forged_cp["payload"]["provenance"]["provided_context_sources"][0][
        "source_url"
    ] = "https://arbitrary.invalid/answer"
    with pytest.raises(ValueError, match="evidence URL is not bound"):
        validate_checkpoint_binding(bad_package, forged_cp)
    # The original supersede path must use the same verified projection, too.
    assert (
        f.store.supersede_result_delivery(f.work_id, delivery["package"])["package_bytes"]
        == delivery["package_bytes"]
    )
    reopened = QuickScanWorkStore(f.store.path)
    assert reopened.get_result_delivery(f.work_id)["package_bytes"] == delivery["package_bytes"]
    assert reopened.get_answer_checkpoint(f.work_id) == original_cp
    assert reopened.get_attempt_response(attempt)["receipt"] == original_receipt
    meta = f.context["questions"]["IQS_01"]["metadata"]
    lifecycle = QuickScanWorkLifecycle(
        reopened,
        entity_id=meta["entity_id"],
        identity=identity,
        run_id=meta["run_id"],
        scan_id=meta["scan_id"],
        routing_fingerprint=reopened.get_item(f.work_id)["routing_fingerprint"],
    )
    no_send = Mock()
    no_send.get_provider_name.return_value = "synthetic-read-only"
    no_send.search.side_effect = AssertionError("hydrate must not dispatch")
    batch = QAEngine(no_send, work_item_lifecycle=lifecycle).hydrate_questions(
        [Question(text=question["prompt"], question_id="IQS_01")]
    )
    assert batch.results[0].answer.score == 8
    assert batch.results[0].answer.status == "scored"
    assert len(f.sent) == 1


def test_original_approved_owner_restores_provided_only_checkpoint_without_new_send(
    provided, tmp_path
):
    from pathlib import Path

    from src.core.models import Question
    from src.core.qa_engine import QAEngine
    from src.runners.llm_runner import QuickScanWorkLifecycle
    from src.utils.quick_scan_delivery_seal import seal_result_delivery
    from src.utils.quick_scan_question_manifest import load_question_manifest
    from src.utils.quick_scan_refresh_execution import (
        ApprovedRefreshSession,
        registered_scope,
    )

    f = provided
    authority, identity, question_data = _sealed_provided_input(f, tmp_path)
    assert seal_result_delivery(f.store, f.work_id, authority=authority)["action"] == "sealed"
    work = f.store.get_item(f.work_id)
    binding = f.store.get_owner_refresh_binding(f.work_id)["binding"]
    qid = "IQS_01"
    question = Question(question_data["prompt"], question_id=qid)
    identity = {**identity, "entity_id": work["entity_id"]}
    spec = dict(
        **identity,
        question_id=qid,
        generation=work["generation"],
        scope=work["scope"],
        scope_id=work["scope_id"],
        question_fingerprint=work["question_fingerprint"],
        routing_fingerprint=work["routing_fingerprint"],
        owner_refresh_binding=binding,
    )
    manifest = load_question_manifest(
        Path(__file__).parents[1] / "fixtures" / "quick_scan_c06_manifest_v2_fixture.json"
    )
    bundle = dict(
        identity=identity, manifest=manifest, questions={qid: question}, authority=authority
    )
    item = dict(
        question_id=qid,
        field_id=f.context["questions"][qid]["metadata"]["field_id"],
        subject_ref=f.subject,
        generation=work["generation"],
        decision="resume_existing_work",
        reuse_observation_id=None,
        existing_work_status="result_ready",
    )
    confirmation = "rc2_" + "f" * 64
    record = dict(
        inputs=dict(
            action=dict(confirmation_id=confirmation), specifications=[spec], references=[]
        ),
        receipt=dict(
            items=[
                {
                    k: work[k]
                    for k in (
                        "work_item_id",
                        "entity_id",
                        "question_id",
                        "generation",
                        "scope",
                        "scope_id",
                    )
                }
            ]
        ),
    )
    proof = dict(
        confirmation=dict(confirmation_id=confirmation),
        request=dict(
            protocol="iqs.refresh_request/1.1.0",
            source_query=dict(payload=dict(information_cutoff=binding["information_cutoff"])),
        ),
        preview=dict(
            items=[item],
            provider=binding["provider"],
            model=binding["model"],
            scope_sha256="a" * 64,
        ),
        owner_policy=dict(provider=binding["provider"], model=binding["model"]),
    )
    session = ApprovedRefreshSession.__new__(ApprovedRefreshSession)
    session.plan, session.proof, session.record = copy.deepcopy(binding), proof, record
    session.bundle, session.bundles = bundle, {}
    session.input_paths, session.input_raw = {}, {}
    session._policy_check, session.deferred = None, {}
    session.fields = {qid: dict(decision="resume_existing_work")}
    session.entries = registered_scope(
        [item], bundle, record, information_cutoff=binding["information_cutoff"]
    )
    session.native = SimpleNamespace(
        plan=copy.deepcopy(session.plan),
        current=dict(
            anchor_version=binding["anchor_version"], expected_decision_id=binding["decision_id"]
        ),
        check=Mock(),
    )
    session.service = SimpleNamespace(
        store=f.store,
        _inputs_unchanged=Mock(),
        _record=Mock(return_value=copy.deepcopy(record)),
        bridge=SimpleNamespace(inspect=Mock(return_value=copy.deepcopy(proof))),
    )
    before = f.store.path.read_bytes()
    original = f.store.get_answer_checkpoint(f.work_id)
    restored = session.restore_registered_checkpoint(question, store=f.store)
    assert restored["payload"] == original["payload"]
    lifecycle = QuickScanWorkLifecycle(
        f.store,
        entity_id=work["entity_id"],
        identity=identity,
        run_id="SYNTHETIC_OUTER_RUN",
        scan_id="SYNTHETIC_OUTER_SCAN",
        transport_managed=True,
        owner_refresh_session=session,
    )
    provider = Mock()
    provider.get_provider_name.return_value = "hydrate-only"
    provider.search.side_effect = AssertionError("no new search")
    batch = QAEngine(provider, work_item_lifecycle=lifecycle).hydrate_questions([question])
    assert batch.results[0].answer.score == 8
    assert batch.results[0].metadata["answer_checkpoint"]["attempt_id"] == original["attempt_id"]
    assert f.store.path.read_bytes() == before and len(f.sent) == 1
