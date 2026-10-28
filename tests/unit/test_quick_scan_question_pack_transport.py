"""Native group admission against isolated SQLite and synthetic owner receipts.

No HTTP, provider credentials, real identities or company documents are used.
The frozen v2 fixture supplies a separate original question context per member.
"""

from __future__ import annotations

import copy
import hashlib
import json
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.providers.model_resolution import model_resolution_sha256
from src.utils import quick_scan_work_transport as transport
from src.utils.quick_scan_result_outbox import canonical_sha256
from src.utils.quick_scan_work_store import QuickScanWorkStore, _sanitized_receipt

FIX = Path(__file__).resolve().parents[1] / "fixtures"
BASE = json.loads((FIX / "quick_scan_c06_authority_v2_fixture.json").read_bytes())[
    "observation_context"
]
MANIFEST = json.loads((FIX / "quick_scan_c06_manifest_v2_fixture.json").read_bytes())
QIDS = ("IQS_01", "IQS_02", "IQS_04")
HTTP_ID = "11111111-1111-4111-8111-111111111111"
PROMPT = "Synthetic frozen question pack"
SYSTEM = "Only final structured answers"
PAYLOAD_SHA = hashlib.sha256(b"synthetic actual HTTP payload").hexdigest()


def _v2_context():
    context = copy.deepcopy(BASE)
    context["schema"] = "stockqa.quick_scan_observation_context/2.0.0"
    for qid, row in context["questions"].items():
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
                    else (
                        meta["security_id"] if meta["scope"] == "security" else meta["segment_id"]
                    )
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
    return context


@pytest.fixture
def pack(tmp_path):
    clock = [1800000000.0]
    store = QuickScanWorkStore(tmp_path / "pack.sqlite", clock=lambda: clock[0])
    context = _v2_context()
    members = []
    for qid in QIDS:
        row = context["questions"][qid]
        meta = row["metadata"]
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
        )
        lease = store.claim(item["work_item_id"], lease_seconds=300)
        store.freeze_observation_context_before_send(item["work_item_id"], lease, context)
        members.append((item["work_item_id"], lease))
    return store, tuple(members), clock


def _policy():
    return {
        "configured": True,
        "policy_id": "pack-transport",
        "policy_version": "pack-transport@1",
        "budget": {
            "currency": "USD",
            "max_cost": 20,
            "max_requests": 10,
            "max_cost_per_attempt": 1,
        },
        "cost_policy": {
            "pricing_basis": "verified_rate_card",
            "pricing_ref": "fixture-price",
            "reserve_before_dispatch": True,
            "unknown_actual_cost_action": "retain_reservation_and_pause",
        },
        "dispatch": {"max_in_flight_total": 1},
        "quota_groups": [{"id": "pack-quota", "max_in_flight": 1}],
        "routes": [
            {
                "id": "pack-route",
                "provider_config_ref": "mimo-fixture",
                "model": "mimo-v2.6-flash",
                "quota_group": "pack-quota",
                "max_in_flight": 1,
                "eligible": True,
                "unavailable_reason": None,
            }
        ],
    }


def _bindings(pack, *, registrar=None, with_budget=True):
    store, members, clock = pack
    stack = ExitStack()
    stack.enter_context(transport.bind_quick_scan_question_pack(store, members))
    stack.enter_context(
        transport.bind_quick_scan_route(
            route_id="pack-route",
            provider="mimo-fixture",
            model_requested="mimo-v2.6-flash",
            quota_group="pack-quota",
        )
    )
    if with_budget:
        stack.enter_context(
            transport.bind_quick_scan_budget(
                store,
                _policy(),
                cost_resolver=lambda _: {
                    "actual_cost": 0.03,
                    "pricing_ref": "fixture-price",
                    "source_ref": "synthetic-rate",
                },
            )
        )
    if registrar is None:
        registrar = lambda request: _owner_receipt(request, clock[0])
    stack.enter_context(transport.bind_subject_dispatch_registrar(registrar))
    return stack


def _owner_receipt(request, now):
    value = {
        "protocol": "stockwiki.subject_dispatch_receipt/1.0.0",
        "store_id": "fixture-owner",
        "dispatch_request": copy.deepcopy(request),
        "request_sha256": canonical_sha256(request),
        "registered_at": datetime.fromtimestamp(now, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    }
    value["receipt_id"] = "sdr_" + canonical_sha256(value)
    return value


def _begin(**changes):
    args = {
        "model_requested": "mimo-v2.6-flash",
        "provider_actual": "mimo",
        "execution_attempt_id": HTTP_ID,
        "request_payload_sha256": PAYLOAD_SHA,
    }
    args.update(changes)
    return transport.begin_quick_scan_send(PROMPT, SYSTEM, **args)


def _response(**changes):
    value = {
        "provider": "mimo",
        "requested_model": "mimo-v2.6-flash",
        "actual_model": "mimo-v2.6-flash",
        "model_resolution_sha256": model_resolution_sha256(None),
        "search_protocol": "mimo_chat_completions",
        "search_status": "executed",
        "response_status": "completed",
        "http_status_code": 200,
        "request_id": "req-fixture",
        "response_id": "resp-fixture",
        "response_sha256": "f" * 64,
        "response_json_basis": "strict_http_json",
        "attempt_id": HTTP_ID,
        "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
        "request_payload_sha256": PAYLOAD_SHA,
        "search_receipt_id": "search-fixture",
        "source_urls": ["https://example.org/synthetic"],
        "completed_at": "2027-01-15T08:00:00Z",
        "usage": {
            "schema": "quick-scan-usage-v1",
            "input_tokens": 12,
            "cached_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 9,
            "reasoning_output_tokens": 2,
            "search_tool_calls": 1,
        },
    }
    value.update(changes)
    return value


def _count(store, table):
    with closing(store._connect()) as connection:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _success(handle):
    handle.consume_for_post()
    value = _response()
    handle.record_response(http_status_code=200, request_id="req-fixture", receipt=value)
    value["work_transport"] = {
        "work_attempt_id": handle.attempt_id,
        "final_receipt": _sanitized_receipt(value),
    }
    return value


def test_group_uses_one_budget_and_distinct_original_question_owner_receipts(pack):
    store, members, clock = pack
    requests = []

    def registrar(request):
        requests.append(copy.deepcopy(request))
        assert _count(store, "quick_scan_budget_attempt") == 0
        return _owner_receipt(request, clock[0])

    with _bindings(pack, registrar=registrar):
        transport.assert_question_pack_binding(QIDS, "ENT_CONTEXT_FIXTURE")
        handle = _begin()
        assert handle.attempt_id == handle.budget_attempt_id and handle.permit.lease_epoch == 0
        assert _count(store, "quick_scan_budget_attempt") == 1
        assert [r["question_id"] for r in requests] == list(QIDS)
        assert len({r["work_attempt_id"] for r in requests}) == 3
        assert {r["execution_attempt_id"] for r in requests} == {HTTP_ID}
        assert {r["dispatch_prompt_sha256"] for r in requests} == {
            hashlib.sha256(PROMPT.encode()).hexdigest()
        }
        value = _success(handle)
        projected = transport.project_question_pack_transport(value)
        assert projected == value["question_pack_transport"]
        assert [m["question_id"] for m in projected["members"]] == list(QIDS)
        assert [m["work_item_id"] for m in projected["members"]] == [m[0] for m in members]
        for member in projected["members"]:
            assert store.get_subject_dispatch_receipt(member["work_attempt_id"]) is not None
            assert (
                store.get_attempt_response(member["work_attempt_id"])["receipt"]
                == value["work_transport"]["final_receipt"]
            )
            assert store.get_answer_checkpoint(member["work_item_id"]) is None
        status = store.get_quick_scan_budget_status("pack-transport")
        assert status["requests"] == 1 and status["spent_micros"] == 30000
        with pytest.raises(transport.QuickScanWorkPersistenceError, match="already consumed"):
            handle.consume_for_post()


@pytest.mark.parametrize(
    "qids,entity",
    [
        (("IQS_02", "IQS_01", "IQS_04"), "ENT_CONTEXT_FIXTURE"),
        (("IQS_01", "IQS_02"), "ENT_CONTEXT_FIXTURE"),
        (("IQS_01", "IQS_02", "IQS_99"), "ENT_CONTEXT_FIXTURE"),
        (QIDS, "ENT_FOREIGN"),
    ],
)
def test_pre_http_exact_question_order_count_and_company_identity(pack, qids, entity):
    store, _, _ = pack
    with _bindings(pack):
        with pytest.raises(transport.QuickScanWorkPersistenceError):
            transport.assert_question_pack_binding(qids, entity)
    assert _count(store, "attempt") == 0 and _count(store, "quick_scan_budget_attempt") == 0


def test_pre_http_requires_active_group_binding():
    with pytest.raises(transport.QuickScanWorkPersistenceError):
        transport.assert_question_pack_binding(QIDS, "ENT_CONTEXT_FIXTURE")
    with pytest.raises(transport.QuickScanWorkPersistenceError):
        transport.project_question_pack_transport(
            {"question_pack_transport": {"pack_id": "injected"}}
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"request_payload_sha256": None},
        {"execution_attempt_id": None},
        {"execution_attempt_id": "not-a-uuid"},
        {"provider_actual": None},
    ],
)
def test_group_requires_actual_payload_provider_and_execution_uuid(pack, changes):
    store, _, _ = pack
    with _bindings(pack):
        with pytest.raises(transport.QuickScanWorkPersistenceError):
            _begin(**changes)
    assert _count(store, "attempt") == 0 and _count(store, "quick_scan_budget_attempt") == 0


def test_group_requires_explicit_same_store_budget(pack):
    store, _, _ = pack
    with _bindings(pack, with_budget=False):
        with pytest.raises(transport.QuickScanWorkPersistenceError):
            _begin()
    assert _count(store, "attempt") == 0


@pytest.mark.parametrize("conflict", ["single", "own-reservation", "external", "format-repair"])
def test_group_rejects_conflicting_legacy_or_external_contexts(pack, conflict):
    store, members, _ = pack
    with ExitStack() as stack:
        if conflict == "single":
            stack.enter_context(transport.bind_quick_scan_work(store, *members[0]))
        elif conflict == "format-repair":
            stack.enter_context(transport.bind_quick_scan_format_repair())
        else:
            variable = (
                transport._OWN_RESERVATION
                if conflict == "own-reservation"
                else transport._EXTERNAL_CONTEXT
            )
            token = variable.set(
                True if conflict == "own-reservation" else {"mode": "external_context_only"}
            )
            stack.callback(variable.reset, token)
        with pytest.raises(transport.QuickScanWorkPersistenceError):
            stack.enter_context(transport.bind_quick_scan_question_pack(store, members))
    assert _count(store, "attempt") == 0


def test_group_cannot_be_shadowed_by_single_work_binding(pack):
    store, members, _ = pack
    with transport.bind_quick_scan_question_pack(store, members):
        with pytest.raises(transport.QuickScanWorkPersistenceError):
            with transport.bind_quick_scan_work(store, *members[0]):
                pass


def test_middle_owner_registration_failure_creates_no_paid_intent(pack):
    store, _, clock = pack
    seen = []

    def registrar(request):
        seen.append(request["question_id"])
        if len(seen) == 2:
            raise TimeoutError("synthetic owner refusal")
        return _owner_receipt(request, clock[0])

    with _bindings(pack, registrar=registrar):
        with pytest.raises(transport.QuickScanWorkPersistenceError):
            _begin()
    assert _count(store, "quick_scan_budget_attempt") == 0
    assert len(seen) == 2


def test_rechecks_owner_guard_and_all_member_leases_before_post(pack):
    store, _, clock = pack
    guards = []
    with _bindings(pack), transport.bind_owner_refresh_guard(lambda: guards.append("checked")):
        handle = _begin()
        before = len(guards)
        clock[0] += 301
        with pytest.raises(transport.QuickScanWorkPersistenceError):
            handle.consume_for_post()
        assert len(guards) > before
    assert _count(store, "quick_scan_budget_attempt") == 1


def test_unknown_delivery_keeps_every_member_uncertain_and_never_falls_back(pack):
    store, members, _ = pack
    with _bindings(pack):
        handle = _begin()
        handle.consume_for_post()
        with pytest.raises(transport.QuickScanWorkUncertainError):
            handle.record_failure(
                _response(
                    http_status_code=None,
                    response_status="unknown",
                    response_sha256=None,
                    actual_model=None,
                    response_id=None,
                    search_status="unverified",
                )
            )
    assert all(store.get_item(wid)["status"] == "uncertain" for wid, _ in members)
    assert all(store.get_answer_checkpoint(wid) is None for wid, _ in members)
    assert store.get_quick_scan_budget_status("pack-transport")["requests"] == 1


@pytest.mark.parametrize(
    "status,code,category",
    [
        (401, "invalid_api_key", "authentication_rejected"),
        (404, "missing_model", "model_or_endpoint_unavailable"),
        (429, "insufficient_quota", "quota_exhausted"),
        (429, "rate_limit_exceeded", "rate_limited"),
    ],
)
def test_group_uses_same_confirmed_refusal_classification_as_single(pack, status, code, category):
    store, members, _ = pack
    with _bindings(pack):
        handle = _begin()
        handle.consume_for_post()
        handle.record_failure(
            _response(
                http_status_code=status,
                provider_error_code=code,
                response_status="failed",
                response_sha256=None,
                response_id=None,
                actual_model=None,
                search_status="unverified",
            )
        )
    for wid, _ in members:
        assert store.list_attempts(wid)[0]["failure_category"] == category
    assert store.get_quick_scan_budget_status("pack-transport")["requests"] == 1


def test_late_success_preserves_one_physical_receipt_and_cost_but_no_answers(pack):
    store, members, clock = pack
    with _bindings(pack):
        handle = _begin()
        handle.consume_for_post()
        clock[0] += 301
        with pytest.raises(transport.QuickScanWorkUncertainError):
            handle.record_response(
                http_status_code=200, request_id="req-fixture", receipt=_response()
            )
    assert all(store.get_item(wid)["status"] == "uncertain" for wid, _ in members)
    assert all(store.get_answer_checkpoint(wid) is None for wid, _ in members)
    status = store.get_quick_scan_budget_status("pack-transport")
    assert status["requests"] == 1 and status["spent_micros"] == 30000


@pytest.mark.parametrize(
    "field",
    [
        "pack_id",
        "member_work_id",
        "member_order",
        "payload",
        "execution",
        "model",
        "final_receipt",
    ],
)
def test_projection_rejects_injected_mapping_or_modified_physical_receipt(pack, field):
    with _bindings(pack):
        handle = _begin()
        value = _success(handle)
        injected = copy.deepcopy(value)
        if field == "pack_id":
            injected["question_pack_transport"]["pack_id"] = "PACK_INJECTED"
        elif field == "member_work_id":
            injected["question_pack_transport"]["members"][0]["work_item_id"] = "WORK_OTHER"
        elif field == "member_order":
            injected["question_pack_transport"]["members"].reverse()
        elif field == "payload":
            injected["request_payload_sha256"] = "0" * 64
        elif field == "execution":
            injected["attempt_id"] = "22222222-2222-4222-8222-222222222222"
        elif field == "model":
            injected["actual_model"] = "wrong-model"
        else:
            injected["work_transport"]["final_receipt"]["usage"]["output_tokens"] += 1
        with pytest.raises(transport.QuickScanWorkPersistenceError):
            transport.project_question_pack_transport(injected)


def test_group_member_ids_from_another_store_never_prepare(pack):
    store, members, clock = pack
    other = QuickScanWorkStore(store.path.with_name("other.sqlite"), clock=lambda: clock[0])
    with transport.bind_quick_scan_question_pack(other, members):
        with pytest.raises(transport.QuickScanWorkPersistenceError):
            transport.assert_question_pack_binding(QIDS, "ENT_CONTEXT_FIXTURE")
    assert _count(other, "attempt") == 0


def test_group_other_budget_store_never_prepares_any_member(pack):
    store, _, clock = pack
    other = QuickScanWorkStore(store.path.with_name("budget-other.sqlite"), clock=lambda: clock[0])
    with _bindings(pack, with_budget=False), transport.bind_quick_scan_budget(other, _policy()):
        with pytest.raises(transport.QuickScanWorkPersistenceError):
            _begin()
    assert _count(store, "attempt") == 0 and _count(other, "quick_scan_budget_attempt") == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"request_payload_sha256": "0" * 64},
        {"attempt_id": "22222222-2222-4222-8222-222222222222"},
        {"prompt_sha256": "0" * 64},
        {"requested_model": "wrong-model"},
        {"actual_model": "wrong-model"},
        {"provider": "minimax"},
    ],
)
def test_wrong_original_receipt_retains_sanitized_exception_evidence_not_fake_settlement(
    pack, changes
):
    store, members, clock = pack
    with _bindings(pack):
        handle = _begin()
        handle.consume_for_post()
        value = _response(
            raw_response="never retain a body",
            reasoning_content="never retain thinking",
            **changes,
        )
        with pytest.raises(transport.QuickScanWorkPersistenceError) as failed:
            handle.record_response(http_status_code=200, request_id="req-fixture", receipt=value)
        assert failed.value.attempt_receipt == _sanitized_receipt(value)
    status = store.get_quick_scan_budget_status("pack-transport")
    assert status["requests"] == 1 and status["spent_micros"] == 0
    assert status["reserved_micros"] == 1000000
    assert all(store.get_answer_checkpoint(wid) is None for wid, _ in members)
    clock[0] += 301
    for wid, _ in members:
        assert store.recover_expired(wid) == "uncertain"


def test_original_native_search_events_are_bound_and_store_only_short_metadata(pack):
    store, _, _ = pack
    with _bindings(pack):
        handle = _begin()
        handle.consume_for_post()
        value = _response(
            web_search_calls=[
                {
                    "id": "search-fixture",
                    "status": "completed",
                    "action_type": "search",
                    "source_urls": ["https://example.org/synthetic"],
                }
            ],
            raw_response="never retain a body",
            reasoning_content="never retain thinking",
        )
        handle.record_response(http_status_code=200, request_id="req-fixture", receipt=value)
        value["work_transport"] = {
            "work_attempt_id": handle.attempt_id,
            "final_receipt": _sanitized_receipt(value),
        }
        projected = transport.project_question_pack_transport(value)
        persisted = store.get_question_pack(projected["pack_id"])
        assert persisted["execution_receipt"]["web_search_calls"] == value["web_search_calls"]
        assert (
            "raw_response" not in persisted["execution_receipt"]
            and "reasoning_content" not in persisted["execution_receipt"]
        )
        changed = copy.deepcopy(value)
        changed["web_search_calls"][0]["source_urls"] = ["https://example.org/foreign"]
        with pytest.raises(transport.QuickScanWorkPersistenceError):
            transport.project_question_pack_transport(changed)


@pytest.mark.parametrize("returned_count", [1, 3])
@pytest.mark.parametrize("tamper_group", [False, True])
def test_ordered_cascade_preserves_group_receipt_for_second_owner_projection(
    pack, returned_count, tamper_group
):
    """The actual engine projects the group again after provider/cascade annotation."""
    from types import SimpleNamespace

    from src.core.models import Question, SearchResult
    from src.providers.llm_provider import QuestionPackSearchResult
    from src.utils.llm_integration import OrderedSearchProviderCascade

    group_before = {}
    member_ids = []

    def native_pack(questions, **kwargs):
        physical = _success(_begin())
        group_before.update(copy.deepcopy(physical))
        original_members = physical["question_pack_transport"]["members"]
        results = []
        for member in original_members[:returned_count]:
            execution = copy.deepcopy(physical)
            execution["work_transport"]["work_attempt_id"] = member["work_attempt_id"]
            member_ids.append(member["work_attempt_id"])
            results.append(
                SearchResult(
                    title="Synthetic",
                    snippet="Synthetic final answer",
                    source="llm_api",
                    status="scored",
                    score=7,
                    metadata={
                        "question_id": member["question_id"],
                        "execution": execution,
                        "attempts": [copy.deepcopy(physical)],
                    },
                )
            )
        physical["question_pack"] = {"status": "complete" if returned_count == 3 else "partial"}
        if tamper_group:
            physical["request_payload_sha256"] = "0" * 64
        group_before.clear()
        group_before.update(copy.deepcopy(physical))
        return QuestionPackSearchResult(
            tuple(results),
            tuple(QIDS[returned_count:]),
            (),
            physical,
        )

    native = SimpleNamespace(
        model="mimo-v2.6-flash",
        client=SimpleNamespace(supports_web_search=True),
        search_question_pack=native_pack,
        get_provider_name=lambda: "mimo-fixture",
    )
    router = OrderedSearchProviderCascade(
        [native],
        [{**_policy()["routes"][0]}],
        policy_version="fixture@hash",
        max_attempts_per_dispatch_round=1,
    )
    with _bindings(pack):
        response = router.search_question_pack([Question(q, question_id=q) for q in QIDS])
        # The raw group identity must survive; each member keeps its distinct owner.
        for field in ("attempt_id", "prompt_sha256", "request_payload_sha256", "work_transport"):
            assert response.execution_metadata[field] == group_before[field]
        assert [
            r.metadata["execution"]["work_transport"]["work_attempt_id"] for r in response.results
        ] == member_ids
        assert response.execution_metadata["work_transport"]["work_attempt_id"] not in member_ids
        if tamper_group:
            # A valid member must never heal an invalid physical group receipt.
            with pytest.raises(transport.QuickScanWorkPersistenceError):
                transport.project_question_pack_transport(response.execution_metadata)
        else:
            assert transport.project_question_pack_transport(response.execution_metadata) == (
                group_before["question_pack_transport"]
            )
        assert response.execution_metadata["dispatch_outcome"]["state"] == "completed"
        assert response.missing_question_ids == tuple(QIDS[returned_count:])
        assert pack[0].get_quick_scan_budget_status("pack-transport")["requests"] == 1
