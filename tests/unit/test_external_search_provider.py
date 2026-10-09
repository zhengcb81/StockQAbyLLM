"""One HTTP boundary for external retrieval, with no hidden paid retry."""

import json
from unittest.mock import Mock

import pytest

from src.config.quick_scan_search_policy import load_search_policy
from src.providers.external_search_parsers import parse_zai_rest
from src.utils.quick_scan_work_store import SendPermit
from src.utils.quick_scan_work_transport import (
    QuickScanBudgetBinding,
    QuickScanSendAttempt,
)
from tests.unit.test_qa_net01_search_boundary import _policy_document
from tests.unit.test_quick_scan_external_context import (
    _journal,
    _journal_budget,
    _journal_plan,
)

ENDPOINTS = {
    "brave": "https://api.search.brave.com/res/v1/web/search",
    "tavily": "https://api.tavily.com/search",
    "zai_rest": "https://api.z.ai/api/paas/v4/web_search",
}


def _provider(tmp_path, monkeypatch, kind, *, endpoint=None):
    from src.providers.external_search_provider import ExternalSearchProvider

    document = _policy_document()
    document["external_routes"][0].update(
        kind=kind, endpoint=endpoint or ENDPOINTS[kind], credential_env="IQS_SYNTHETIC_SEARCH_TOKEN"
    )
    monkeypatch.setenv("IQS_SYNTHETIC_SEARCH_TOKEN", "synthetic-test-only")
    path = tmp_path / "search-policy.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return ExternalSearchProvider(load_search_policy(path), "brave-primary")


def _permit(tmp_path, provider, *, query="Fixture", top_k=2):
    # Real SQLite/Q09 boundary; only the HTTP response is a double.
    store, item, lease, _ = _journal(tmp_path)
    budget = _journal_budget()
    budget["routes"][0]["provider_config_ref"] = provider.route["kind"]
    plan = _journal_plan()
    plan.update(query=query, top_k=top_k)
    operation = store.begin_external_search(
        item["work_item_id"],
        lease,
        policy=budget,
        plan=plan,
        route_id=provider.route["route_id"],
        provider=provider.route["kind"],
        quota_group="search",
        adapter_version="stockqa.external_retrieval/1.0.0",
        search_policy_sha256=provider.policy_sha256,
        endpoint=provider.route["endpoint"],
    )
    handle = QuickScanSendAttempt(
        None,
        operation["budget_attempt_id"],
        SendPermit(operation["budget_attempt_id"], operation["created_at"], 0),
        budget_binding=QuickScanBudgetBinding(store, budget),
        budget_attempt_id=operation["budget_attempt_id"],
    )
    # This already permits the test-first API without requiring a missing
    # constructor argument; implementation will make it an explicit field.
    handle.external_operation_id = operation["operation_id"]
    return handle


def _response(body, *, status=200, request_id="real-header-id"):
    response = Mock()
    response.status_code = status
    response.headers = {} if request_id is None else {"x-request-id": request_id}
    response.iter_content.return_value = [body]
    return response


@pytest.mark.parametrize("kind", list(ENDPOINTS))
def test_one_metered_request_keeps_external_origin_and_never_adds_native_events(
    tmp_path, monkeypatch, kind
):
    provider = _provider(tmp_path, monkeypatch, kind)
    body = {
        "brave": {
            "web": {
                "results": [
                    {
                        "title": "Fixture Corp",
                        "url": "https://fixture.example/ir",
                        "description": "Published margin",
                        "page_age": "2026-10-01",
                    }
                ]
            }
        },
        "tavily": {
            "results": [
                {
                    "title": "Fixture Corp",
                    "url": "https://fixture.example/ir",
                    "content": "Published margin",
                    "published_date": "2026-10-01",
                }
            ]
        },
        "zai_rest": {
            "request_id": "zai-body-id",
            "search_result": [
                {
                    "title": "Fixture Corp",
                    "link": "https://fixture.example/ir",
                    "content": "Published margin",
                    "publish_date": "2026-10-01",
                }
            ],
        },
    }[kind]
    session = Mock()
    session.request.return_value = _response(json.dumps(body).encode())
    monkeypatch.setattr(
        "src.utils.http_client.http_client_manager.get_metered_sync_session",
        lambda: session,
        raising=False,
    )
    result = provider.fetch(
        _permit(tmp_path, provider, query="Fixture Corp margins", top_k=3),
        query="Fixture Corp margins",
        top_k=3,
        locale="en-US",
    )
    assert session.request.call_count == 1
    kwargs = session.request.call_args.kwargs
    assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
    assert result["origin"] == "external" and result["parse_status"] == "ok"
    assert result["request_id"] == "real-header-id"
    assert result["http_request_count"] == 1 and len(result["response_body_sha256"]) == 64
    assert "web_search_calls" not in result and "actual_model" not in result
    assert result["entries"][0]["url"] == "https://fixture.example/ir"
    assert "raw_content" not in result and "response_body" not in result


def test_official_zai_search_result_shape_is_not_misclassified_as_parse_failure():
    parsed = parse_zai_rest(
        {
            "request_id": "zai-id",
            "search_result": [
                {
                    "title": "Fixture",
                    "link": "https://fixture.example",
                    "content": "Short evidence",
                    "publish_date": "2026-10-01",
                }
            ],
        }
    )
    assert parsed["status"] == "ok" and len(parsed["entries"]) == 1


def test_retrieval_host_mismatch_is_rejected_before_transport(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="endpoint"):
        _provider(tmp_path, monkeypatch, "brave", endpoint="https://api.tavily.com/search")


def test_sent_timeout_is_one_unknown_operation_without_hidden_retry(tmp_path, monkeypatch):
    from src.providers.external_search_provider import ExternalSearchTransportError

    provider = _provider(tmp_path, monkeypatch, "tavily")
    session = Mock()
    session.request.side_effect = TimeoutError("private transport detail")
    monkeypatch.setattr(
        "src.utils.http_client.http_client_manager.get_metered_sync_session",
        lambda: session,
        raising=False,
    )
    with pytest.raises(ExternalSearchTransportError) as caught:
        provider.fetch(_permit(tmp_path, provider), query="Fixture", top_k=2, locale="en-US")
    assert session.request.call_count == 1
    assert caught.value.receipt["outcome"] == "unknown"
    assert "private transport detail" not in str(caught.value)


def test_missing_server_request_id_stays_missing(tmp_path, monkeypatch):
    provider = _provider(tmp_path, monkeypatch, "brave")
    session = Mock()
    session.request.return_value = _response(b'{"web":{"results":[]}}', request_id=None)
    monkeypatch.setattr(
        "src.utils.http_client.http_client_manager.get_metered_sync_session",
        lambda: session,
        raising=False,
    )
    result = provider.fetch(_permit(tmp_path, provider), query="Fixture", top_k=2, locale="en-US")
    assert result["request_id"] is None and result["parse_status"] == "empty"


def test_no_durable_permit_means_no_http(tmp_path, monkeypatch):
    provider = _provider(tmp_path, monkeypatch, "brave")
    session = Mock()
    monkeypatch.setattr(
        "src.utils.http_client.http_client_manager.get_metered_sync_session",
        lambda: session,
        raising=False,
    )
    with pytest.raises(ValueError, match="permit"):
        provider.fetch(None, query="Fixture", top_k=2, locale="en-US")
    session.request.assert_not_called()


def test_metered_get_has_no_adapter_retry_or_cross_host_redirect():
    from src.utils.http_client import http_client_manager

    session = http_client_manager.get_metered_sync_session()
    assert session.get_adapter("https://api.search.brave.com").max_retries.total == 0
    assert session.trust_env is False
    http_client_manager.close_all()


def test_wrong_query_is_refused_before_the_real_journal_dispatch(tmp_path, monkeypatch):
    provider = _provider(tmp_path, monkeypatch, "brave")
    permit = _permit(tmp_path, provider)
    session = Mock()
    session.request.return_value = _response(b'{"web":{"results":[]}}')
    monkeypatch.setattr(
        "src.utils.http_client.http_client_manager.get_metered_sync_session", lambda: session
    )
    with pytest.raises(ValueError, match="binding"):
        provider.fetch(permit, query="Unapproved other issuer", top_k=2, locale="en-US")
    session.request.assert_not_called()
    assert (
        permit.budget_binding.store.get_external_search(permit.external_operation_id)["dispatch"]
        is None
    )


def test_mutated_route_is_refused_before_any_credential_or_http(tmp_path, monkeypatch):
    provider = _provider(tmp_path, monkeypatch, "brave")
    permit = _permit(tmp_path, provider)
    provider.route["endpoint"] = "https://another.example/search"
    session = Mock()
    session.request.return_value = _response(b'{"web":{"results":[]}}')
    monkeypatch.setattr(
        "src.utils.http_client.http_client_manager.get_metered_sync_session", lambda: session
    )
    with pytest.raises(ValueError, match="frozen"):
        provider.fetch(permit, query="Fixture", top_k=2, locale="en-US")
    session.request.assert_not_called()


def test_fresh_handle_cannot_repeat_an_already_consumed_durable_dispatch(tmp_path, monkeypatch):
    provider = _provider(tmp_path, monkeypatch, "brave")
    permit = _permit(tmp_path, provider)
    session = Mock()
    session.request.return_value = _response(b'{"web":{"results":[]}}')
    monkeypatch.setattr(
        "src.utils.http_client.http_client_manager.get_metered_sync_session", lambda: session
    )
    provider.fetch(permit, query="Fixture", top_k=2, locale="en-US")
    replay = QuickScanSendAttempt(
        None,
        permit.attempt_id,
        permit.permit,
        budget_binding=permit.budget_binding,
        budget_attempt_id=permit.budget_attempt_id,
    )
    replay.external_operation_id = permit.external_operation_id
    with pytest.raises(RuntimeError, match="persist"):
        provider.fetch(replay, query="Fixture", top_k=2, locale="en-US")
    assert session.request.call_count == 1


def test_a_native_budget_only_permit_without_external_intent_never_sends(tmp_path, monkeypatch):
    provider = _provider(tmp_path, monkeypatch, "brave")
    permit = _permit(tmp_path, provider)
    permit.external_operation_id = None
    session = Mock()
    session.request.return_value = _response(b'{"web":{"results":[]}}')
    monkeypatch.setattr(
        "src.utils.http_client.http_client_manager.get_metered_sync_session", lambda: session
    )
    with pytest.raises(ValueError, match="permit"):
        provider.fetch(permit, query="Fixture", top_k=2, locale="en-US")
    session.request.assert_not_called()


def _execution_provider(tmp_path, monkeypatch, kind="tavily"):
    from src.providers.external_search_provider import ExternalSearchProvider
    from tests.unit.test_quick_scan_external_context import _execution_policy

    def configure(document):
        route = document["external_routes"][0]
        route.update(kind=kind, endpoint=ENDPOINTS[kind])
        if kind == "tavily":
            route["pricing"].update(basis="per_search", per_request_cap_micros=6000)
            route["metering"].update(
                charge_policy="provider_usage", usage_unit="credits", max_usage_units_per_request=2
            )

    policy = _execution_policy(tmp_path, monkeypatch, configure)
    return policy, ExternalSearchProvider(policy, "brave-primary")


def _execution_permit(tmp_path, policy, *, mutate=None):
    from src.config.quick_scan_search_policy import execution_query_plans
    from src.utils.quick_scan_external_context import (
        build_shared_external_budget_policy,
    )
    from tests.unit.test_quick_scan_budget import _policy

    store, item, lease, _ = _journal(tmp_path)
    budget = build_shared_external_budget_policy(_policy(), policy)
    plan = execution_query_plans(policy)[0]
    if mutate:
        mutate(plan)
    operation = store.begin_external_search(
        item["work_item_id"],
        lease,
        policy=budget,
        plan=plan,
        route_id="brave-primary",
        provider=policy.routes[0]["kind"],
        quota_group="search-brave",
        adapter_version="stockqa.external_retrieval/1.0.0",
        search_policy_sha256=policy.policy_sha256,
        endpoint=policy.routes[0]["endpoint"],
    )
    handle = QuickScanSendAttempt(
        None,
        operation["budget_attempt_id"],
        SendPermit(operation["budget_attempt_id"], operation["created_at"], 0),
        budget_binding=QuickScanBudgetBinding(store, budget),
        budget_attempt_id=operation["budget_attempt_id"],
        external_operation_id=operation["operation_id"],
    )
    return handle, plan


def test_tavily_actual_usage_and_body_request_id_reach_the_real_journal(tmp_path, monkeypatch):
    from src.utils.quick_scan_cost_resolver import ExternalSearchCostResolver

    policy, provider = _execution_provider(tmp_path, monkeypatch)
    permit, plan = _execution_permit(tmp_path, policy)
    body = {
        "request_id": "actual-vendor-id",
        "usage": {"credits": 2},
        "results": [
            {
                "title": "Fixture",
                "url": "https://fixture.example/ir",
                "content": "Margin",
                "published_date": "2026-10-01",
            }
        ],
    }
    session = Mock()
    session.request.return_value = _response(json.dumps(body).encode(), request_id=None)
    monkeypatch.setattr(
        "src.utils.http_client.http_client_manager.get_metered_sync_session", lambda: session
    )
    receipt = provider.fetch(
        permit, query=plan["query"], top_k=plan["top_k"], locale=plan["locale"]
    )
    assert session.request.call_args.kwargs["json"]["include_usage"] is True
    assert receipt["request_id"] is None and receipt["provider_request_id"] == "actual-vendor-id"
    assert receipt["usage"] == {
        "schema": "stockqa.external_search_usage/1.0.0",
        "unit": "credits",
        "count": 2,
    }
    price = ExternalSearchCostResolver(policy)(receipt)
    saved = permit.budget_binding.store.record_external_search(
        permit.external_operation_id,
        receipt=receipt,
        actual_cost=price["actual_cost"],
        cost_source_ref=price["source_ref"],
    )
    assert saved["budget"]["actual_cost_micros"] == 6000
    assert saved["result"]["receipt"]["provider_request_id"] == "actual-vendor-id"
    assert session.request.call_count == 1


@pytest.mark.parametrize("credits", [None, True, "2", -1])
def test_missing_or_invalid_tavily_usage_retains_reservation_without_guessing(
    tmp_path, monkeypatch, credits
):
    from src.utils.quick_scan_cost_resolver import ExternalSearchCostResolver

    policy, provider = _execution_provider(tmp_path, monkeypatch)
    permit, plan = _execution_permit(tmp_path, policy)
    body = {"results": []}
    if credits is not None:
        body["usage"] = {"credits": credits}
    session = Mock()
    session.request.return_value = _response(json.dumps(body).encode())
    monkeypatch.setattr(
        "src.utils.http_client.http_client_manager.get_metered_sync_session", lambda: session
    )
    receipt = provider.fetch(
        permit, query=plan["query"], top_k=plan["top_k"], locale=plan["locale"]
    )
    assert receipt["usage_status"] == ("missing" if credits is None else "invalid")
    assert receipt.get("usage") is None and ExternalSearchCostResolver(policy)(receipt) is None
    saved = permit.budget_binding.store.record_external_search(
        permit.external_operation_id, receipt=receipt
    )
    assert (
        saved["budget"]["status"] == "response_unpriced"
        and saved["budget"]["actual_cost_micros"] is None
    )
    assert (
        permit.budget_binding.store.get_quick_scan_budget_status(
            permit.budget_binding.policy["policy_id"]
        )["reserved_micros"]
        == 6_000_000
    )
    assert session.request.call_count == 1


def test_a_self_consistent_journal_query_outside_the_frozen_policy_sends_zero_http(
    tmp_path, monkeypatch
):
    policy, provider = _execution_provider(tmp_path, monkeypatch, "brave")
    permit, plan = _execution_permit(
        tmp_path, policy, mutate=lambda p: p.update(query="Another unapproved query")
    )
    session = Mock()
    monkeypatch.setattr(
        "src.utils.http_client.http_client_manager.get_metered_sync_session", lambda: session
    )
    with pytest.raises(ValueError, match="frozen.*plan"):
        provider.fetch(permit, query=plan["query"], top_k=plan["top_k"], locale=plan["locale"])
    assert (
        permit.budget_binding.store.get_external_search(permit.external_operation_id)["dispatch"]
        is None
    )
    session.request.assert_not_called()


def test_actual_usage_over_the_verified_bound_is_charged_but_not_reusable(tmp_path, monkeypatch):
    from src.utils.quick_scan_cost_resolver import ExternalSearchCostResolver
    from src.utils.quick_scan_work_store import BudgetAdmissionError, Lease

    policy, provider = _execution_provider(tmp_path, monkeypatch)
    permit, plan = _execution_permit(tmp_path, policy)
    session = Mock()
    session.request.return_value = _response(
        b'{"usage":{"credits":3},"results":[{"url":"https://fixture.example/ir","title":"Fixture","content":"Margin","published_date":"2026-10-01"}]}'
    )
    monkeypatch.setattr(
        "src.utils.http_client.http_client_manager.get_metered_sync_session", lambda: session
    )
    receipt = provider.fetch(
        permit, query=plan["query"], top_k=plan["top_k"], locale=plan["locale"]
    )
    assert receipt["usage_status"] == "exceeds_verified_bound"
    price = ExternalSearchCostResolver(policy)(receipt)
    assert str(price["actual_cost"]) == "0.009000"
    saved = permit.budget_binding.store.record_external_search(
        permit.external_operation_id,
        receipt=receipt,
        actual_cost=price["actual_cost"],
        cost_source_ref=price["source_ref"],
    )
    assert saved["budget"]["actual_cost_micros"] == 9000
    with pytest.raises(BudgetAdmissionError, match="external_pricing_bound_exceeded"):
        permit.budget_binding.store.begin_external_search(
            saved["work_item_id"],
            Lease(saved["lease_token"], saved["lease_epoch"]),
            policy=permit.budget_binding.policy,
            plan=plan,
            route_id="brave-primary",
            provider="tavily",
            quota_group="search-brave",
            adapter_version="stockqa.external_retrieval/1.0.0",
            search_policy_sha256=policy.policy_sha256,
            endpoint=provider.route["endpoint"],
        )
    assert session.request.call_count == 1
