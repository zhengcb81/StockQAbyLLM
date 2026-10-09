"""Durable quick-scan send boundary tests using isolated SQLite and HTTP fixtures."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from unittest.mock import Mock

import pytest
import requests

from src.core.models import Question, SearchResult
from src.providers.llm_client import LLMClient, LLMTransportAttemptError
from src.providers.llm_provider import LLMProvider
from src.providers.llm_response_parser import LLMResponseParser
from src.utils.llm_integration import OrderedSearchProviderCascade
from src.utils.quick_scan_work_store import (
    BudgetAdmissionError,
    QuickScanWorkStore,
    WorkConflictError,
)
from src.utils.quick_scan_work_transport import (
    QuickScanSendAttempt,
    QuickScanWorkPersistenceError,
    QuickScanWorkUncertainError,
    bind_quick_scan_budget,
    bind_quick_scan_route,
    bind_quick_scan_work,
)


def _create_claimed_item(tmp_path, *, clock=None):
    store = (
        QuickScanWorkStore(tmp_path / "quick_scan_work.sqlite", clock=clock)
        if clock
        else (QuickScanWorkStore(tmp_path / "quick_scan_work.sqlite"))
    )
    item = store.create_or_attach(
        entity_id="ENT_FIXTURE_CO",
        question_id="CORE_01",
        generation=1,
        scope="entity",
        scope_id="ENT_FIXTURE_CO",
        identity_revision=1,
        source_binding_version=1,
        identity_state="verified",
        source_binding_ref="BND_FIXTURE",
        source_binding_refs=["BND_FIXTURE"],
        identity_snapshot_sha256=hashlib.sha256(b"identity").hexdigest(),
        question_fingerprint=hashlib.sha256(b"question-v1").hexdigest(),
        routing_fingerprint=hashlib.sha256(b"routes-v1").hexdigest(),
        run_id="run-fixture",
        scan_id="scan-fixture",
    )
    lease = store.claim(item["work_item_id"], lease_seconds=30)
    assert lease is not None
    return store, item["work_item_id"], lease


def _http_response(status_code, *, code=None):
    response = Mock()
    response.status_code = status_code
    response.headers = {"x-request-id": f"req-{status_code}"}
    if status_code == 200:
        response.json.return_value = {
            "id": "resp-complete",
            "status": "completed",
            "model": "fixture-model",
            "output": [
                {
                    "type": "web_search_call",
                    "id": "search-1",
                    "status": "completed",
                    "action": {
                        "type": "search",
                        "sources": [{"type": "url", "url": "https://example.org/source"}],
                    },
                },
                {
                    "type": "message",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": (
                                '{"question_id":"CORE_01","entity_id":"ENT_FIXTURE_CO",'
                                '"company_name":"Fixture Co","score":8,"description":"fixture"}'
                            ),
                        }
                    ],
                },
            ],
        }
    else:
        response.json.return_value = {"error": {"code": code or "invalid_api_key"}}
        response.raise_for_status.side_effect = requests.HTTPError(f"fixture HTTP {status_code}")
    return response


class _PostingProvider:
    def __init__(self, route_name, model):
        self.route_name = route_name
        self.model = model
        self.client = LLMClient(
            api_key="fixture-key",
            model=model,
            base_url="https://api.openai.com/v1/responses",
            provider_name="openai",
        )

    def get_provider_name(self):
        return self.route_name

    def search_question(self, question):
        try:
            response = self.client.send_search_request(question.text)
        except LLMTransportAttemptError as error:
            return [
                SearchResult(
                    title="fixture failure",
                    snippet="fixture provider refusal",
                    source="llm_api",
                    status="error",
                    metadata={
                        "attempts": [error.attempt_receipt],
                        "execution": {"search_status": "unverified"},
                    },
                )
            ]
        return [
            SearchResult(
                title="fixture success",
                snippet=response.content or "fixture response received",
                source="llm_api",
                score=8,
                status="scored",
                metadata={
                    "attempts": response.execution_metadata.get("attempts", []),
                    "execution": response.execution_metadata,
                    "search_status": response.execution_metadata.get("search_status"),
                },
            )
        ]


def _cascade(primary, backup, *, quota_group=None):
    routes = [
        {
            "id": "primary-route",
            "provider_config_ref": "primary",
            "model": "fixture-model",
            "eligible": True,
            "quota_group": quota_group,
        },
        {
            "id": "backup-route",
            "provider_config_ref": "backup",
            "model": "fixture-model",
            "eligible": True,
            "quota_group": quota_group,
        },
    ]
    return OrderedSearchProviderCascade(
        providers=[primary, backup],
        routes=routes,
        policy_version="policy-fixture-v1",
        max_attempts_per_dispatch_round=2,
    )


def _budget_policy():
    return {
        "configured": True,
        "policy_id": "late-receipt-budget",
        "policy_version": "late-receipt-budget@v1",
        "budget": {
            "currency": "USD",
            "max_cost": 20,
            "max_requests": 10,
            "max_cost_per_attempt": 2,
        },
        "cost_policy": {
            "pricing_basis": "verified_rate_card",
            "pricing_ref": "fixture-price-v1",
            "reserve_before_dispatch": True,
            "unknown_actual_cost_action": "retain_reservation_and_pause",
        },
        "dispatch": {"max_in_flight_total": 2},
        "quota_groups": [{"id": "A", "max_in_flight": 2}],
        "routes": [
            {
                "id": "primary-route",
                "provider_config_ref": "primary",
                "model": "fixture-model",
                "quota_group": "A",
                "max_in_flight": 1,
                "eligible": True,
                "unavailable_reason": None,
            }
        ],
    }


def _raising_cost_resolver(_receipt):
    raise RuntimeError("fixture provider usage lookup failed")


def test_send_intent_is_committed_before_post_and_success_receipt_is_durable(tmp_path, monkeypatch):
    store, work_id, lease = _create_claimed_item(tmp_path)
    response = _http_response(200)
    session = Mock()

    def post(*args, **kwargs):
        assert store.list_attempts(work_id)[0]["phase"] == "send_intent"
        return response

    session.post.side_effect = post
    monkeypatch.setattr(
        "src.providers.llm_client.http_client_manager.get_sync_session",
        lambda: session,
    )
    provider = _PostingProvider("primary", "fixture-model")
    cascade = _cascade(provider, _PostingProvider("backup", "fixture-model"))

    with bind_quick_scan_work(store, work_id, lease):
        result = cascade.search_question(
            Question("Describe durable advantage", question_id="CORE_01")
        )

    assert result[0].score == 8
    assert session.post.call_count == 1
    attempt = store.list_attempts(work_id)[0]
    assert attempt["phase"] == "response_available"
    assert attempt["http_status_code"] == 200
    assert attempt["request_id"] == "req-200"
    assert "answer" not in attempt
    assert "prompt" not in attempt

    parsed = LLMResponseParser().parse_structured_response(
        result[0].snippet,
        expected_question_id="CORE_01",
        expected_entity_id="ENT_FIXTURE_CO",
        expected_company_name="Fixture Co",
        strict_json_only=True,
    )
    assert parsed is not None
    checkpoint = store.save_answer_checkpoint(
        work_id,
        lease,
        attempt["attempt_id"],
        answer={
            "entity_id": parsed.entity_id,
            "question_id": parsed.question_id,
            "status": parsed.status,
            "score": parsed.score,
            "description": parsed.description,
        },
        execution_receipt=result[0].metadata["execution"],
    )
    assert checkpoint["payload"]["answer"]["score"] == 8
    assert checkpoint["attempt_id"] == attempt["attempt_id"]
    assert checkpoint["payload"]["provenance"]["provider_attempt_id"] != attempt["attempt_id"]
    assert store.get_item(work_id)["status"] == "result_ready"


@pytest.mark.parametrize(
    "status_code,error_code,expected_category",
    [
        (401, "invalid_api_key", "authentication_rejected"),
        (403, "invalid_api_key", "authentication_rejected"),
        (404, "model_not_found", "model_or_endpoint_unavailable"),
        (429, "insufficient_quota", "quota_exhausted"),
    ],
)
def test_explicit_provider_refusal_is_recorded_before_fallback(
    tmp_path, monkeypatch, status_code, error_code, expected_category
):
    store, work_id, lease = _create_claimed_item(tmp_path)
    session = Mock()
    session.post.side_effect = [
        _http_response(status_code, code=error_code),
        _http_response(200),
    ]
    monkeypatch.setattr(
        "src.providers.llm_client.http_client_manager.get_sync_session",
        lambda: session,
    )
    cascade = _cascade(
        _PostingProvider("primary", "fixture-model"),
        _PostingProvider("backup", "fixture-model"),
    )

    with bind_quick_scan_work(store, work_id, lease):
        result = cascade.search_question(
            Question("Describe durable advantage", question_id="CORE_01")
        )

    attempts = store.list_attempts(work_id)
    assert result[0].score == 8
    assert session.post.call_count == 2
    assert [item["phase"] for item in attempts] == ["confirmed_failure", "response_available"]
    assert attempts[0]["failure_category"] == expected_category


def test_unknown_http_outcome_marks_work_uncertain_and_blocks_fallback(tmp_path, monkeypatch):
    store, work_id, lease = _create_claimed_item(tmp_path)
    session = Mock()
    session.post.side_effect = requests.Timeout("simulated timeout")
    monkeypatch.setattr(
        "src.providers.llm_client.http_client_manager.get_sync_session",
        lambda: session,
    )
    cascade = _cascade(
        _PostingProvider("primary", "fixture-model"),
        _PostingProvider("backup", "fixture-model"),
    )

    with bind_quick_scan_work(store, work_id, lease):
        with pytest.raises(QuickScanWorkUncertainError):
            cascade.search_question(Question("Describe durable advantage", question_id="CORE_01"))

    assert session.post.call_count == 1
    assert store.get_item(work_id)["status"] == "uncertain"
    assert [item["phase"] for item in store.list_attempts(work_id)] == ["uncertain"]


def test_local_ledger_failure_happens_before_http_post(tmp_path, monkeypatch):
    store, work_id, lease = _create_claimed_item(tmp_path)
    session = Mock()
    monkeypatch.setattr(
        "src.providers.llm_client.http_client_manager.get_sync_session",
        lambda: session,
    )

    def fail_before_send(*args, **kwargs):
        raise sqlite3.OperationalError("fixture ledger unavailable")

    monkeypatch.setattr(store, "mark_send_intent", fail_before_send)
    provider = _PostingProvider("primary", "fixture-model")
    cascade = _cascade(provider, _PostingProvider("backup", "fixture-model"))

    with bind_quick_scan_work(store, work_id, lease):
        with pytest.raises(QuickScanWorkPersistenceError):
            cascade.search_question(Question("Describe durable advantage", question_id="CORE_01"))

    session.post.assert_not_called()


def test_stale_lease_fails_before_http_post(tmp_path, monkeypatch):
    now = [1_800_000_000.0]
    store, work_id, lease = _create_claimed_item(tmp_path, clock=lambda: now[0])
    now[0] += 31
    session = Mock()
    monkeypatch.setattr(
        "src.providers.llm_client.http_client_manager.get_sync_session",
        lambda: session,
    )
    cascade = _cascade(
        _PostingProvider("primary", "fixture-model"),
        _PostingProvider("backup", "fixture-model"),
    )

    with bind_quick_scan_work(store, work_id, lease):
        with pytest.raises(QuickScanWorkPersistenceError):
            cascade.search_question(Question("Describe durable advantage", question_id="CORE_01"))

    session.post.assert_not_called()
    assert store.list_attempts(work_id) == []


@pytest.mark.parametrize("status_code", [500, 502, 503])
def test_server_error_is_uncertain_and_never_falls_back(tmp_path, monkeypatch, status_code):
    store, work_id, lease = _create_claimed_item(tmp_path)
    session = Mock()
    session.post.side_effect = requests.HTTPError(f"fixture HTTP {status_code}")
    monkeypatch.setattr(
        "src.providers.llm_client.http_client_manager.get_sync_session",
        lambda: session,
    )
    cascade = _cascade(
        _PostingProvider("primary", "fixture-model"),
        _PostingProvider("backup", "fixture-model"),
    )

    # Attach an explicit response object so the transport receipt carries the 5xx status.
    response = _http_response(status_code, code="server_error")
    session.post.side_effect = None
    session.post.return_value = response

    with bind_quick_scan_work(store, work_id, lease):
        with pytest.raises(QuickScanWorkUncertainError):
            cascade.search_question(Question("Describe durable advantage", question_id="CORE_01"))

    assert session.post.call_count == 1
    assert store.get_item(work_id)["status"] == "uncertain"
    assert store.list_attempts(work_id)[0]["phase"] == "uncertain"


def test_failure_to_persist_response_receipt_never_dispatches_backup(tmp_path, monkeypatch):
    store, work_id, lease = _create_claimed_item(tmp_path)
    session = Mock()
    session.post.return_value = _http_response(200)
    monkeypatch.setattr(
        "src.providers.llm_client.http_client_manager.get_sync_session",
        lambda: session,
    )
    original = store.record_attempt_outcome

    def fail_response_receipt(*args, **kwargs):
        if kwargs.get("outcome") == "response_available":
            raise sqlite3.OperationalError("fixture receipt store unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "record_attempt_outcome", fail_response_receipt)
    cascade = _cascade(
        _PostingProvider("primary", "fixture-model"),
        _PostingProvider("backup", "fixture-model"),
    )

    with bind_quick_scan_work(store, work_id, lease):
        with pytest.raises(QuickScanWorkPersistenceError):
            cascade.search_question(Question("Describe durable advantage", question_id="CORE_01"))

    assert session.post.call_count == 1
    assert store.list_attempts(work_id)[0]["phase"] == "send_intent"


@pytest.mark.parametrize(
    "status_code,error_code",
    [
        (200, None),
        (401, "invalid_api_key"),
        (429, "insufficient_quota"),
        (503, "server_error"),
    ],
)
def test_late_response_receipt_is_retained_but_never_accepted_or_failed_over(
    tmp_path, monkeypatch, status_code, error_code
):
    now = [1_800_000_000.0]
    store, work_id, lease = _create_claimed_item(tmp_path, clock=lambda: now[0])
    session = Mock()

    def post(*args, **kwargs):
        now[0] += 31
        assert store.recover_expired(work_id) == "uncertain"
        return _http_response(status_code, code=error_code)

    session.post.side_effect = post
    monkeypatch.setattr(
        "src.providers.llm_client.http_client_manager.get_sync_session",
        lambda: session,
    )
    cascade = _cascade(
        _PostingProvider("primary", "fixture-model"),
        _PostingProvider("backup", "fixture-model"),
        quota_group="A",
    )
    policy = _budget_policy()

    with bind_quick_scan_work(store, work_id, lease):
        with bind_quick_scan_budget(store, policy):
            with pytest.raises(QuickScanWorkUncertainError):
                cascade.search_question(
                    Question("Describe durable advantage", question_id="CORE_01")
                )

    attempts = store.list_attempts(work_id)
    assert session.post.call_count == 1
    assert store.get_item(work_id)["status"] == "uncertain"
    assert attempts[0]["phase"] == "send_intent"
    assert attempts[0]["receipt_sha256"] is None
    assert attempts[0]["late_receipt_sha256"]
    budget = store.get_quick_scan_budget_status(policy["policy_id"])
    assert budget["in_flight"] == 0
    assert budget["reserved_micros"] == 2_000_000
    assert budget["unreconciled_attempts"] == 1
    with pytest.raises(BudgetAdmissionError, match="budget_reconciliation_required"):
        store.reserve_budget_attempt(
            policy,
            budget_attempt_id="DISPATCH_AFTER_LATE_RESPONSE",
            route_id="primary-route",
            provider="primary",
            model_requested="fixture-model",
            quota_group="A",
        )
    if status_code == 200:
        store.note_late_receipt(
            work_id,
            lease,
            attempts[0]["attempt_id"],
            receipt_sha256=attempts[0]["late_receipt_sha256"],
            budget_outcome="response_available",
            http_status_code=200,
        )
        with pytest.raises(WorkConflictError, match="different outcome"):
            store.note_late_receipt(
                work_id,
                lease,
                attempts[0]["attempt_id"],
                receipt_sha256=attempts[0]["late_receipt_sha256"],
                budget_outcome="response_available",
                http_status_code=201,
            )


def test_late_timeout_without_http_response_keeps_budget_slot_uncertain(tmp_path, monkeypatch):
    now = [1_800_000_000.0]
    store, work_id, lease = _create_claimed_item(tmp_path, clock=lambda: now[0])
    session = Mock()

    def post(*args, **kwargs):
        now[0] += 31
        assert store.recover_expired(work_id) == "uncertain"
        raise requests.Timeout("fixture timeout before any response")

    session.post.side_effect = post
    monkeypatch.setattr(
        "src.providers.llm_client.http_client_manager.get_sync_session",
        lambda: session,
    )
    cascade = _cascade(
        _PostingProvider("primary", "fixture-model"),
        _PostingProvider("backup", "fixture-model"),
        quota_group="A",
    )
    policy = _budget_policy()

    with bind_quick_scan_work(store, work_id, lease):
        with bind_quick_scan_budget(store, policy):
            with pytest.raises(QuickScanWorkUncertainError):
                cascade.search_question(
                    Question("Describe durable advantage", question_id="CORE_01")
                )

    assert session.post.call_count == 1
    assert store.get_item(work_id)["status"] == "uncertain"
    budget = store.get_quick_scan_budget_status(policy["policy_id"])
    assert budget["in_flight"] == 1
    assert budget["reserved_micros"] == 2_000_000
    assert budget["unreconciled_attempts"] == 1


@pytest.mark.parametrize(
    "cost_resolver",
    [
        pytest.param(_raising_cost_resolver, id="resolver-raises"),
        pytest.param(
            lambda _receipt: {
                "actual_cost": float("nan"),
                "pricing_ref": "fixture-price-v1",
                "source_ref": "fixture-usage",
            },
            id="invalid-measurement",
        ),
    ],
)
def test_cost_resolver_failure_after_http_success_keeps_reserve_and_releases_slot(
    tmp_path, monkeypatch, cost_resolver
):
    store, work_id, lease = _create_claimed_item(tmp_path)
    session = Mock()
    session.post.return_value = _http_response(200)
    monkeypatch.setattr(
        "src.providers.llm_client.http_client_manager.get_sync_session",
        lambda: session,
    )
    cascade = _cascade(
        _PostingProvider("primary", "fixture-model"),
        _PostingProvider("backup", "fixture-model"),
        quota_group="A",
    )
    policy = _budget_policy()

    with bind_quick_scan_work(store, work_id, lease):
        with bind_quick_scan_budget(store, policy, cost_resolver=cost_resolver):
            results = cascade.search_question(
                Question("Describe durable advantage", question_id="CORE_01")
            )

    assert results[0].score == 8
    assert session.post.call_count == 1
    assert store.list_attempts(work_id)[0]["phase"] == "response_available"
    budget = store.get_quick_scan_budget_status(policy["policy_id"])
    assert budget["spent_micros"] == 0
    assert budget["reserved_micros"] == 2_000_000
    assert budget["in_flight"] == 0
    assert budget["unreconciled_attempts"] == 1
    with pytest.raises(BudgetAdmissionError, match="budget_reconciliation_required"):
        store.reserve_budget_attempt(
            policy,
            budget_attempt_id="DISPATCH_AFTER_COST_RESOLVER_FAILURE",
            route_id="primary-route",
            provider="primary",
            model_requested="fixture-model",
            quota_group="A",
        )


def test_explicit_format_repair_uses_second_durable_attempt_on_same_route(tmp_path, monkeypatch):
    store, work_id, lease = _create_claimed_item(tmp_path)
    config_file = tmp_path / "llm_apis.json"
    config_file.write_text(
        json.dumps(
            {
                "default_provider": "openai",
                "providers": {
                    "openai": {
                        "enabled": True,
                        "api_key": "",
                        "model": "fixture-model",
                        "base_url": "https://api.openai.com/v1/responses",
                        "max_retries": 1,
                        "format_repair_budget": 1,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    malformed = _http_response(200)
    malformed.json.return_value["output"][1]["content"][0]["text"] = "not-json"
    repaired = _http_response(200)
    repaired.json.return_value["output"][1]["content"][0]["text"] = (
        '{"question_id":"CORE_01","entity_id":"ENT_FIXTURE_CO",'
        '"company_name":"Fixture Co","status":"scored","score":8,'
        '"description":"validated fixture answer"}'
    )
    session = Mock()
    session.post.side_effect = [malformed, repaired]
    monkeypatch.setattr(
        "src.providers.llm_client.http_client_manager.get_sync_session",
        lambda: session,
    )
    provider = LLMProvider(
        provider_name="openai",
        api_key="fixture-key",
        model="fixture-model",
        config_file=str(config_file),
        require_search=True,
        company_name="Fixture Co",
        entity_id="ENT_FIXTURE_CO",
    )
    cascade = _cascade(provider, _PostingProvider("backup", "fixture-model"))

    with bind_quick_scan_work(store, work_id, lease):
        results = cascade.search_question(
            Question("Describe durable advantage", question_id="CORE_01")
        )

    attempts = store.list_attempts(work_id)
    assert results[0].score == 8
    assert results[0].status == "scored"
    assert session.post.call_count == 2
    assert [item["phase"] for item in attempts] == [
        "response_available",
        "response_available",
    ]
    assert len({item["route_id"] for item in attempts}) == 1
    assert len({item["provider"] for item in attempts}) == 1
    assert len({item["model_requested"] for item in attempts}) == 1
    assert attempts[0]["prompt_sha256"] != attempts[1]["prompt_sha256"]
    assert all("answer" not in item and "prompt" not in item for item in attempts)


def test_send_handle_rejects_duplicate_consumption(tmp_path):
    store, work_id, lease = _create_claimed_item(tmp_path)
    with bind_quick_scan_work(store, work_id, lease), bind_quick_scan_route(
        route_id="primary-route", provider="primary", model_requested="fixture-model"
    ):
        from src.utils.quick_scan_work_transport import begin_quick_scan_send

        handle = begin_quick_scan_send("prompt", "system")
    assert isinstance(handle, QuickScanSendAttempt)
    handle.consume_for_post()
    with pytest.raises(QuickScanWorkPersistenceError, match="already consumed"):
        handle.consume_for_post()


def test_provider_route_without_work_binding_preserves_legacy_behavior(monkeypatch):
    session = Mock()
    session.post.return_value = _http_response(200)
    monkeypatch.setattr(
        "src.providers.llm_client.http_client_manager.get_sync_session",
        lambda: session,
    )
    cascade = _cascade(
        _PostingProvider("primary", "fixture-model"),
        _PostingProvider("backup", "fixture-model"),
    )

    result = cascade.search_question(Question("Legacy question", question_id="CORE_01"))

    assert result[0].score == 8
    session.post.assert_called_once()


def _q10_alias_policy(actual="fixture-resolved"):
    return {
        "schema_version": "1.0.0",
        "aliases": [
            {
                "provider": "openai",
                "protocol": "responses",
                "requested_model": "fixture-model",
                "resolved_model": actual,
            }
        ],
    }


@pytest.mark.parametrize("work_bound", [False, True])
def test_q10_route_model_mismatch_precedes_budget_reservation_and_post(
    tmp_path, monkeypatch, work_bound
):
    from contextlib import nullcontext

    store, work_id, lease = _create_claimed_item(tmp_path)
    session = Mock()
    monkeypatch.setattr(
        "src.providers.llm_client.http_client_manager.get_sync_session", lambda: session
    )
    client = LLMClient("fixture-key", "wrong-request", "https://api.openai.com/v1/responses")
    policy = _budget_policy()
    with bind_quick_scan_work(store, work_id, lease) if work_bound else nullcontext():
        with bind_quick_scan_budget(store, policy), bind_quick_scan_route(
            route_id="primary-route",
            provider="primary",
            model_requested="fixture-model",
            quota_group="A",
            model_resolution=_q10_alias_policy(),
        ):
            with pytest.raises(QuickScanWorkPersistenceError, match="requested model"):
                client.send_search_request("synthetic prompt")
    session.post.assert_not_called()
    assert store.list_attempts(work_id) == []
    budget = store.get_quick_scan_budget_status(policy["policy_id"])
    assert budget["requests"] == 0
    assert budget["reserved_micros"] == 0


@pytest.mark.parametrize("work_bound", [False, True])
@pytest.mark.parametrize("status", [200, 401])
def test_q10_http_source_is_durable_with_or_without_work_and_without_usage(
    tmp_path, monkeypatch, work_bound, status
):
    from contextlib import nullcontext

    store, work_id, lease = _create_claimed_item(tmp_path)
    response = _http_response(status)
    response.json.return_value.update({"id": "synthetic-http-id", "model": "fixture-resolved"})
    session = Mock()
    session.post.return_value = response
    monkeypatch.setattr(
        "src.providers.llm_client.http_client_manager.get_sync_session", lambda: session
    )
    client = LLMClient("fixture-key", "fixture-model", "https://api.openai.com/v1/responses")
    policy = _budget_policy()
    resolution = _q10_alias_policy()
    with bind_quick_scan_work(store, work_id, lease) if work_bound else nullcontext():
        with bind_quick_scan_budget(store, policy), bind_quick_scan_route(
            route_id="primary-route",
            provider="primary",
            model_requested="fixture-model",
            quota_group="A",
            model_resolution=resolution,
        ):
            # Changes to a caller-owned dictionary cannot authorize this dispatch anew.
            resolution["aliases"].clear()
            if status == 200:
                result = client.send_search_request("synthetic prompt")
                assert result.search_verified is True
            else:
                with pytest.raises(LLMTransportAttemptError):
                    client.send_search_request("synthetic prompt")
    with sqlite3.connect(store.path) as connection:
        (attempt_id,) = connection.execute(
            "SELECT attempt_id FROM quick_scan_attempt_response"
        ).fetchone()
        work_fk, budget_fk = connection.execute(
            "SELECT work_attempt_id,budget_attempt_id FROM quick_scan_attempt_resolution"
        ).fetchone()
    original = store.get_attempt_response(attempt_id)
    assert original["model_requested"] == "fixture-model"
    assert original["model_resolved"] == "fixture-resolved"
    assert original["response_id"] == "synthetic-http-id"
    assert (
        original["response_sha256"]
        == hashlib.sha256(
            json.dumps(
                response.json.return_value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("utf-8")
        ).hexdigest()
    )
    assert original["receipt"]["response_json_basis"] == "parsed_payload"
    assert "usage" not in original["receipt"]
    assert original["model_resolution"]["aliases"]
    assert (work_fk is not None, budget_fk is not None) == (work_bound, not work_bound)
    restarted = QuickScanWorkStore(store.path)
    assert restarted.get_attempt_response(attempt_id) == original
    budget = restarted.get_quick_scan_budget_status(policy["policy_id"])
    assert budget["spent_micros"] == 0
    assert budget["reserved_micros"] == 2_000_000
    assert budget["unreconciled_attempts"] == 1
    assert budget["in_flight"] == 0


@pytest.mark.parametrize("distinct_requested", [False, True])
def test_q10_refused_primary_then_registered_backup_uses_final_attempt(
    tmp_path, monkeypatch, distinct_requested
):
    store, work_id, lease = _create_claimed_item(tmp_path)
    backup_response = _http_response(200)
    backup_response.json.return_value["model"] = "fixture-resolved"
    # A body-generated model has no authority over the HTTP model.
    backup_response.json.return_value["output"][1]["content"][0]["text"] = (
        '{"actual_model":"forged-model","question_id":"CORE_01",'
        '"entity_id":"ENT_FIXTURE_CO","score":8,"description":"synthetic"}'
    )
    session = Mock()
    session.post.side_effect = [_http_response(401), backup_response]
    monkeypatch.setattr(
        "src.providers.llm_client.http_client_manager.get_sync_session", lambda: session
    )
    primary_requested = "synthetic-primary-requested" if distinct_requested else "fixture-model"
    backup_requested = "synthetic-backup-requested" if distinct_requested else "fixture-model"
    cascade = _cascade(
        _PostingProvider("primary", primary_requested), _PostingProvider("backup", backup_requested)
    )
    cascade.routes[0]["model"] = primary_requested
    cascade.routes[1]["model"] = backup_requested
    resolution = _q10_alias_policy()
    resolution["aliases"][0]["requested_model"] = backup_requested
    cascade.routes[1]["model_resolution"] = resolution
    with bind_quick_scan_work(store, work_id, lease):
        result = cascade.search_question(Question("synthetic", question_id="CORE_01"))
    attempts = store.list_attempts(work_id)
    assert [attempt["phase"] for attempt in attempts] == ["confirmed_failure", "response_available"]
    receipt = result[0].metadata["execution"]["work_transport"]["final_receipt"]
    checkpoint = store.save_answer_checkpoint(
        work_id,
        lease,
        attempts[-1]["attempt_id"],
        answer={
            "entity_id": "ENT_FIXTURE_CO",
            "question_id": "CORE_01",
            "status": "scored",
            "score": 8,
            "description": "synthetic",
        },
        execution_receipt=receipt,
    )
    assert checkpoint["payload"]["provenance"]["route_id"] == "backup-route"
    assert checkpoint["payload"]["provenance"]["actual_model"] == "fixture-resolved"
    assert checkpoint["payload"]["provenance"]["model_requested"] == backup_requested
    assert attempts[0]["model_requested"] == primary_requested
    assert [call.kwargs["json"]["model"] for call in session.post.call_args_list] == [
        primary_requested,
        backup_requested,
    ]
    assert session.post.call_count == 2
