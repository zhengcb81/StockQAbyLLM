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
        manifest_raw_sha256="c" * 64,
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
        routing_fingerprint="e" * 64,
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
    with _bindings((f.store, ((f.work_id, f.lease),), [1800000000.0])), bind_intent(
        f.store, "a" * 64, "b" * 64, [f.work_id], provided_context=prepared
    ):
        return f.client.send_search_request(
            prompt,
            "Frozen original rules",
            thinking_enabled=True,
            max_output_tokens=2048,
            research_final_json=True,
        )


def test_actual_sent_provided_only_citation_checkpoints_and_reopens_without_changing_search_receipt(
    provided,
):
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
    with _bindings((f.store, ((f.work_id, f.lease),), [1800000000.0])), bind_intent(
        f.store, "a" * 64, "c" * 64, [f.work_id]
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
        connection.execute("PRAGMA user_version=25")
    from src.utils.quick_scan_refresh_inspect import ReadOnlyWorkView

    view = ReadOnlyWorkView(f.store.path)
    assert view.get_item(f.work_id)["question_id"] == "IQS_01"
    upgraded = QuickScanWorkStore(f.store.path)
    with closing(upgraded._connect()) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 26
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
    from src.providers.llm_research_client import ResearchChatSession
    from src.utils.http_client import http_client_manager
    from tests.unit.test_llm_research_client import TOOLS

    f = provided
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
    with _bindings((f.store, ((f.work_id, f.lease),), [1800000000.0])), bind_intent(
        f.store, "a" * 64, "d" * 64, [f.work_id], provided_context=prepared
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
