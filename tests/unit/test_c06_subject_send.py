"""Actual producer admission, synthetic frozen context, zero live HTTP."""

import copy
import hashlib
import json
import runpy
import sqlite3
from contextlib import ExitStack
from pathlib import Path

import pytest

dc = pytest.importorskip(
    "subject_dispatch_contract", reason="C06 component tests require the IQS pure-contract runtime"
)
from src.utils import quick_scan_observation_context as ctx
from src.utils import quick_scan_work_transport as transport
from src.utils.quick_scan_result_outbox import canonical_sha256
from src.utils.quick_scan_work_store import QuickScanWorkStore, WorkConflictError

FIX = Path(__file__).resolve().parents[1] / "fixtures"
AUTH = json.loads((FIX / "quick_scan_c06_authority_v2_fixture.json").read_bytes())
BASE = AUTH["observation_context"]
QID = next(iter(BASE["questions"]))
MANIFEST = json.loads((FIX / "quick_scan_c06_manifest_v2_fixture.json").read_bytes())


def v2():
    value = copy.deepcopy(BASE)
    value["schema"] = "stockqa.quick_scan_observation_context/2.0.0"
    for qid, row in value["questions"].items():
        meta = row["metadata"]
        row["subject_binding"] = {
            "protocol": "stockqa.subject_context/1.0.0",
            "identity_revision": 1,
            "subject_ref": {
                "entity_id": meta["entity_id"],
                "primary_issuer_id": meta["entity_id"],
                "analysis_subject_id": "ASJ_11111111-1111-4111-8111-111111111111",
                "analysis_subject_revision": 1,
                "perimeter_sha256": "d" * 64,
                "scope": meta["scope"],
                "scope_id": (
                    meta["entity_id"]
                    if meta["scope"] == "entity"
                    else meta["security_id"] if meta["scope"] == "security" else meta["segment_id"]
                ),
                "security_id": meta["security_id"],
                "segment_id": meta["segment_id"],
                "listing_id": (
                    "LST_11111111-1111-4111-8111-111111111111"
                    if meta["scope"] == "security"
                    else None
                ),
            },
            "question_contract": copy.deepcopy(
                next(q for q in MANIFEST["questions"] if q["id"] == qid)
            ),
        }
    return value


def work(tmp_path, *, context=None):
    clock = [1800000000.0]
    store = QuickScanWorkStore(tmp_path / "work.sqlite", clock=lambda: clock[0])
    row = BASE["questions"][QID]
    meta = row["metadata"]
    item = store.create_or_attach(
        entity_id=meta["entity_id"],
        question_id=QID,
        generation=1,
        scope=meta["scope"],
        scope_id=meta["entity_id"],
        identity_revision=1,
        source_binding_version=1,
        identity_state="verified",
        source_binding_ref="BND_FIXTURE",
        source_binding_refs=["BND_FIXTURE"],
        identity_snapshot_sha256=BASE["identity_snapshot_sha256"],
        question_fingerprint=row["work_prompt_sha256"],
        routing_fingerprint="e" * 64,
        run_id="RUN_SYNTHETIC",
        scan_id="SCAN_SYNTHETIC",
    )
    lease = store.claim(item["work_item_id"], lease_seconds=300)
    if context is not None:
        store.freeze_observation_context_before_send(item["work_item_id"], lease, context)
    return store, item, lease, clock


def receipt(request):
    pre = {
        "protocol": "stockwiki.subject_dispatch_receipt/1.0.0",
        "store_id": "qsobs_fixture",
        "dispatch_request": copy.deepcopy(request),
        "request_sha256": dc.digest(request),
        "registered_at": "2027-01-15T08:00:00Z",
    }  # exactly the synthetic QA clock
    pre["receipt_id"] = "sdr_" + dc.digest(pre)
    return pre


def test_context2_is_versioned_and_never_adds_subject_to_original_metadata():
    value = v2()
    assert ctx.validate_context_document(value, expected_sha256=canonical_sha256(value)) == value
    assert ctx.validate_context_document(BASE, expected_sha256=canonical_sha256(BASE)) == BASE
    assert "analysis_subject" not in value["questions"][QID]["metadata"]


@pytest.mark.parametrize(
    "field,new",
    [
        ("entity_id", "ENT_FOREIGN"),
        ("scope_id", "ENT_FOREIGN"),
        ("security_id", "SEC_FOREIGN"),
        ("scope", "security"),
    ],
)
def test_subject_binding_cannot_disagree_with_frozen_metadata(field, new):
    value = v2()
    value["questions"][QID]["subject_binding"]["subject_ref"][field] = new
    with pytest.raises(ValueError):
        ctx.validate_context_document(value, expected_sha256=canonical_sha256(value))


def test_context_is_frozen_before_answer_and_restart_cannot_rebind(tmp_path):
    value = v2()
    store, item, lease, clock = work(tmp_path, context=value)
    assert store.get_answer_checkpoint(item["work_item_id"]) is None
    again = QuickScanWorkStore(store.path, clock=lambda: clock[0])
    assert again.get_observation_context(item["work_item_id"])["context"] == value
    changed = copy.deepcopy(value)
    changed["questions"][QID]["subject_binding"]["subject_ref"]["analysis_subject_revision"] = 2
    with pytest.raises(WorkConflictError):
        again.freeze_observation_context_before_send(item["work_item_id"], lease, changed)


def bindings(store, item, lease, registrar):
    stack = ExitStack()
    stack.enter_context(transport.bind_quick_scan_work(store, item["work_item_id"], lease))
    stack.enter_context(
        transport.bind_quick_scan_route(
            route_id="preferred", provider="user-config-ref", model_requested="mimo-v2.6-pro"
        )
    )
    if registrar is not None:
        stack.enter_context(transport.bind_subject_dispatch_registrar(registrar))
    return stack


def begin():
    return transport.begin_quick_scan_send(
        "actual prompt",
        "actual system",
        model_requested="mimo-v2.6-pro",
        provider_actual="mimo",
        execution_attempt_id="11111111-1111-4111-8111-111111111111",
    )


def test_before_send_records_actual_provider_uuid_and_distinct_local_work_attempt(tmp_path):
    store, item, lease, clock = work(tmp_path, context=v2())
    events = []

    def register(request):
        assert store.list_attempts(item["work_item_id"])[0]["phase"] == "prepared"
        assert store.get_answer_checkpoint(item["work_item_id"]) is None
        events.append(copy.deepcopy(request))
        return receipt(request)

    with bindings(store, item, lease, register):
        handle = begin()
        saved = store.get_subject_dispatch_receipt(handle.attempt_id)
        assert saved["receipt"]["dispatch_request"] == events[0]
        assert events[0]["work_attempt_id"] == handle.attempt_id
        assert events[0]["execution_attempt_id"] == "11111111-1111-4111-8111-111111111111"
        assert events[0]["provider"] == "mimo" and events[0]["route_provider"] == "user-config-ref"
        assert (
            saved["receipt"]["dispatch_request"]["dispatch_work_prompt_sha256"]
            == hashlib.sha256(b"actual system\0actual prompt").hexdigest()
        )
        assert (
            saved["receipt"]["dispatch_request"]["dispatch_prompt_sha256"]
            == hashlib.sha256(b"actual prompt").hexdigest()
        )
        handle.consume_for_post()
        with pytest.raises(transport.QuickScanWorkPersistenceError):
            handle.consume_for_post()
    reopened = QuickScanWorkStore(store.path, clock=lambda: clock[0])
    assert reopened.get_subject_dispatch_receipt(handle.attempt_id) == saved


@pytest.mark.parametrize("failure", ["missing", "timeout", "wrong-subject", "future"])
def test_failed_registration_never_marks_paid_send_intent(tmp_path, failure):
    store, item, lease, clock = work(tmp_path, context=v2())

    def register(request):
        if failure == "timeout":
            raise TimeoutError("synthetic owner timeout")
        pre = receipt(request)
        if failure == "wrong-subject":
            pre["dispatch_request"]["subject_ref"]["analysis_subject_revision"] = 2
            pre["request_sha256"] = dc.digest(pre["dispatch_request"])
        if failure == "future":
            pre["registered_at"] = "2035-01-01T00:00:00Z"
        pre["receipt_id"] = "sdr_" + dc.digest({k: v for k, v in pre.items() if k != "receipt_id"})
        return pre

    with bindings(store, item, lease, None if failure == "missing" else register):
        with pytest.raises(transport.QuickScanWorkPersistenceError):
            begin()
    assert [x["phase"] for x in store.list_attempts(item["work_item_id"])] == ["prepared"]
    with sqlite3.connect(store.path) as con:
        assert con.execute("SELECT COUNT(*) FROM quick_scan_budget_attempt").fetchone()[0] == 0


def test_direct_mark_cannot_bypass_missing_context2_receipt(tmp_path):
    store, item, lease, clock = work(tmp_path, context=v2())
    attempt = store.prepare_attempt(
        item["work_item_id"],
        lease,
        route_id="preferred",
        provider="user-config-ref",
        model_requested="mimo-v2.6-pro",
        request_cache_key="REQ_" + "a" * 64,
        prompt_sha256="a" * 64,
    )
    with pytest.raises(WorkConflictError, match="subject_dispatch"):
        store.mark_send_intent(item["work_item_id"], lease, attempt["attempt_id"])
    assert store.list_attempts(item["work_item_id"])[0]["phase"] == "prepared"


def test_old_contextless_transport_still_has_no_owner_registration_requirement(tmp_path):
    store, item, lease, clock = work(tmp_path)
    with transport.bind_quick_scan_work(
        store, item["work_item_id"], lease
    ), transport.bind_quick_scan_route(
        route_id="legacy", provider="mimo", model_requested="mimo-v2.6-pro"
    ):
        assert (
            transport.begin_quick_scan_send("prompt", "system", model_requested="mimo-v2.6-pro")
            is not None
        )


def test_context2_cannot_be_added_after_an_existing_send_intent(tmp_path):
    store, item, lease, clock = work(tmp_path)
    with transport.bind_quick_scan_work(
        store, item["work_item_id"], lease
    ), transport.bind_quick_scan_route(
        route_id="legacy", provider="mimo", model_requested="mimo-v2.6-pro"
    ):
        transport.begin_quick_scan_send("prompt", "system", model_requested="mimo-v2.6-pro")
    with pytest.raises(WorkConflictError, match="before"):
        store.freeze_observation_context_before_send(item["work_item_id"], lease, v2())


def test_authority_v3_loads_context2_but_v2_cannot_silently_gain_it(tmp_path):
    from src.utils.quick_scan_c06_authority import (
        AuthorityUnavailable,
        load_c06_authority,
    )
    from src.utils.quick_scan_question_manifest import load_question_manifest

    manifest = load_question_manifest(FIX / "quick_scan_c06_manifest_v2_fixture.json")
    authority = copy.deepcopy(AUTH)
    authority.update(
        schema_version="3.0.0",
        observation_context=v2(),
        observation_context_sha256=canonical_sha256(v2()),
    )
    path = tmp_path / "authority3.json"
    path.write_text(json.dumps(authority), encoding="utf-8")
    loaded = load_c06_authority(
        path, manifest=manifest, identity_snapshot_sha256=BASE["identity_snapshot_sha256"]
    )
    assert loaded["schema_version"] == "3.0.0"
    authority["schema_version"] = "2.0.0"
    path.write_text(json.dumps(authority), encoding="utf-8")
    with pytest.raises(AuthorityUnavailable):
        load_c06_authority(path, manifest=manifest)


def test_schema14_migration_preserves_original_work_and_never_backfills_receipts(tmp_path):
    from src.utils.quick_scan_work_store import SCHEMA_VERSION
    from tests.c06_legacy_fixture import remove_c06_additions

    store, item, lease, clock = work(tmp_path)
    with sqlite3.connect(store.path) as con:
        remove_c06_additions(con, after_version=14)
        con.execute("PRAGMA user_version=14")
    migrated = QuickScanWorkStore(store.path, clock=lambda: clock[0])
    assert migrated.get_item(item["work_item_id"]) == store.get_item(item["work_item_id"])
    with sqlite3.connect(store.path) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert (
            con.execute("SELECT COUNT(*) FROM quick_scan_subject_send_receipt").fetchone()[0] == 0
        )


@pytest.mark.parametrize("async_mode", [False, True])
def test_actual_llm_client_generates_and_preserves_execution_uuid_before_registration(
    tmp_path, monkeypatch, async_mode
):
    import asyncio
    from unittest.mock import AsyncMock, Mock

    from src.providers import llm_client
    from tests.unit.test_quick_scan_work_transport import _http_response

    store, item, lease, clock = work(tmp_path, context=v2())
    monkeypatch.setattr(llm_client, "_utc_now", lambda: "2027-01-15T08:00:00Z")
    requests = []

    def register(request):
        requests.append(copy.deepcopy(request))
        return receipt(request)

    response = _http_response(200)
    session = Mock()

    def post(*args, **kwargs):
        attempt = store.list_attempts(item["work_item_id"])[0]
        assert attempt["phase"] == "send_intent"
        saved = store.get_subject_dispatch_receipt(attempt["attempt_id"])
        assert saved["receipt"]["dispatch_request"] == requests[0]
        return response

    if async_mode:

        async def apost(*args, **kwargs):
            return post(*args, **kwargs)

        session.post = AsyncMock(side_effect=apost)
        monkeypatch.setattr(
            llm_client.http_client_manager, "get_async_client", AsyncMock(return_value=session)
        )
    else:
        session.post = Mock(side_effect=post)
        monkeypatch.setattr(llm_client.http_client_manager, "get_sync_session", lambda: session)
    client_type = llm_client.AsyncLLMClient if async_mode else llm_client.LLMClient
    client = client_type(
        api_key="synthetic-only",
        model="fixture-model",
        base_url="https://api.openai.com/v1/responses",
        provider_name="openai",
    )
    with transport.bind_quick_scan_work(
        store, item["work_item_id"], lease
    ), transport.bind_quick_scan_route(
        route_id="preferred", provider="user-config-ref", model_requested="fixture-model"
    ), transport.bind_subject_dispatch_registrar(
        register
    ):
        result = (
            asyncio.run(client.send_search_request_async("actual prompt", "actual system"))
            if async_mode
            else client.send_search_request("actual prompt", "actual system")
        )
    assert session.post.call_count == 1
    assert (
        requests[0]["provider"] == "openai" and requests[0]["route_provider"] == "user-config-ref"
    )
    assert result.execution_metadata["attempt_id"] == requests[0]["execution_attempt_id"]
    assert requests[0]["work_attempt_id"] != requests[0]["execution_attempt_id"]


def test_persisted_subject_receipt_is_sql_immutable(tmp_path):
    store, item, lease, clock = work(tmp_path, context=v2())
    with bindings(store, item, lease, receipt):
        handle = begin()
    with sqlite3.connect(store.path) as con:
        for sql in (
            "DELETE FROM quick_scan_subject_send_receipt",
            "UPDATE quick_scan_subject_send_receipt SET receipt_json=receipt_json",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                con.execute(sql)


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("failure", ["timeout", "invalid-json", "auth", "quota"])
def test_actual_bound_client_keeps_terminal_failure_and_known_configuration(
    tmp_path, monkeypatch, async_mode, failure
):
    import asyncio
    from unittest.mock import AsyncMock, Mock

    import httpx
    import requests

    from src.providers import llm_client
    from tests.unit.test_quick_scan_work_transport import _http_response

    store, item, lease, clock = work(tmp_path, context=v2())
    monkeypatch.setattr(llm_client, "_utc_now", lambda: "2027-01-15T08:00:00Z")
    registered = []

    def register(request):
        registered.append(copy.deepcopy(request))
        return receipt(request)

    session = Mock()
    response = _http_response(
        401 if failure == "auth" else 429 if failure == "quota" else 200,
        code="insufficient_quota" if failure == "quota" else None,
    )
    if failure == "invalid-json":
        response.json.side_effect = ValueError("synthetic malformed JSON")

    def post(*args, **kwargs):
        attempt = store.list_attempts(item["work_item_id"])[0]
        assert (
            store.get_subject_dispatch_receipt(attempt["attempt_id"])["receipt"]["dispatch_request"]
            == registered[0]
        )
        if failure == "timeout":
            error_type = httpx.ReadTimeout if async_mode else requests.Timeout
            raise error_type("synthetic timeout after send")
        return response

    if async_mode:

        async def apost(*args, **kwargs):
            return post(*args, **kwargs)

        session.post = AsyncMock(side_effect=apost)
        monkeypatch.setattr(
            llm_client.http_client_manager, "get_async_client", AsyncMock(return_value=session)
        )
    else:
        session.post = Mock(side_effect=post)
        monkeypatch.setattr(llm_client.http_client_manager, "get_sync_session", lambda: session)
    client_type = llm_client.AsyncLLMClient if async_mode else llm_client.LLMClient
    client = client_type(
        api_key="synthetic-only",
        model="fixture-model",
        base_url="https://api.openai.com/v1/responses",
        provider_name="openai",
    )
    with transport.bind_quick_scan_work(
        store, item["work_item_id"], lease
    ), transport.bind_quick_scan_route(
        route_id="preferred", provider="user-config-ref", model_requested="fixture-model"
    ), transport.bind_subject_dispatch_registrar(
        register
    ):
        expected_error = (
            transport.QuickScanWorkUncertainError
            if failure in {"timeout", "invalid-json"}
            else llm_client.LLMTransportAttemptError
        )
        with pytest.raises(expected_error) as caught:
            if async_mode:
                asyncio.run(client.send_search_request_async("actual prompt", "actual system"))
            else:
                client.send_search_request("actual prompt", "actual system")
    assert session.post.call_count == 1
    actual = caught.value.attempt_receipt
    assert actual["attempt_id"] == registered[0]["execution_attempt_id"]
    assert actual["requested_model"] == "fixture-model" and actual["search_protocol"] == "responses"
    assert actual["search_status"] == "unverified" and "usage" not in actual
    attempt = store.list_attempts(item["work_item_id"])[0]
    assert attempt["phase"] in {"confirmed_failure", "uncertain"}
    if failure in {"timeout", "invalid-json"}:
        assert (
            attempt["phase"] == "uncertain"
            and store.get_item(item["work_item_id"])["status"] == "uncertain"
        )
        assert actual["actual_model"] is None and actual["response_id"] is None
        assert "response_sha256" not in actual
    else:
        assert attempt["phase"] == "confirmed_failure"
    reopened = QuickScanWorkStore(store.path, clock=lambda: clock[0])
    assert reopened.list_attempts(item["work_item_id"])[0]["phase"] == attempt["phase"]
    assert reopened.get_subject_dispatch_receipt(attempt["attempt_id"]) is not None


def test_context2_does_not_change_original_observation_id_hash_or_content():
    old = runpy.run_path(str(Path(__file__).with_name("test_quick_scan_observation_context.py")))
    checkpoint = old["checkpoint"]()
    selected_qid = old["QID"]  # manifest order need not equal JSON object key order
    checkpoint["work"]["identity_revision"] = 1
    bound = ctx.bind_context_to_work_item(
        v2(), work_item=checkpoint["work"], question_id=selected_qid
    )
    plain = ctx.bind_context_to_work_item(
        BASE, work_item=checkpoint["work"], question_id=selected_qid
    )
    before = ctx.build_observation(
        checkpoint,
        context=plain,
        standard_answer=old["content"](),
        started_at="2026-09-22T09:00:00Z",
    )
    after = ctx.build_observation(
        checkpoint,
        context=bound,
        standard_answer=old["content"](),
        started_at="2026-09-22T09:00:00Z",
    )
    assert before == after
    assert canonical_sha256(before) == canonical_sha256(after)
    assert "analysis_subject" not in after and "subject_binding" not in after
    checkpoint["work"].pop("identity_revision")
    with pytest.raises(ValueError, match="identity revision"):
        ctx.bind_context_to_work_item(v2(), work_item=checkpoint["work"], question_id=selected_qid)
