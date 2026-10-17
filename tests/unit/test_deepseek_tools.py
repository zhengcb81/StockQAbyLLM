"""Synthetic-only DeepSeek tools contract and durable owner regressions."""

import copy
import json
from decimal import Decimal
from unittest.mock import Mock

import pytest

from src.config.quick_scan_search_policy import (
    execution_query_plans,
    load_search_policy,
)
from src.providers.external_search_parsers import parse_route
from src.providers.external_search_provider import (
    ExternalSearchProvider,
    ExternalSearchTransportError,
)
from src.utils.quick_scan_cost_resolver import ExternalSearchCostResolver
from src.utils.quick_scan_work_store import SendPermit
from src.utils.quick_scan_work_transport import (
    QuickScanBudgetBinding,
    QuickScanSendAttempt,
)
from tests.unit.test_external_search_provider import _response
from tests.unit.test_quick_scan_external_context import (
    _execution_policy_document,
    _journal,
    _journal_budget,
)


def document(kind="deepseek_search"):
    doc = _execution_policy_document()
    doc["schema_version"] = "1.2.0"
    route = doc["external_routes"][0]
    route.update(
        kind=kind,
        endpoint="https://api.deepseek.com/v1/tools/" + kind.split("_")[1],
        credential_env="DEEPSEEK_API_KEY",
    )
    route["pricing"].update(
        currency="CNY", basis="per_search", unit_cost_micros=5000, per_request_cap_micros=5000
    )
    route["dispatch"].update(
        quota_group="deepseek_tools_" + kind.split("_")[1],
        max_in_flight=1,
        quota_group_max_in_flight=1,
    )
    route["metering"].update(
        charge_policy="successful_nonempty_only",
        usage_unit="search_calls",
        max_usage_units_per_request=1,
        rejected_request_cost_micros=0,
    )
    for query in doc["execution_plan"]["queries"]:
        query.update(operation="search", source_operation_id=None, source_receipt_sha256=None)
    return doc


def policy(tmp_path, monkeypatch, doc=None):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-only")
    p = tmp_path / "ds-policy.json"
    p.write_text(json.dumps(doc or document()), encoding="utf-8")
    return load_search_policy(p)


def test_official_search_summary_is_bounded_and_not_an_original_quote():
    parsed = parse_route(
        "deepseek_search",
        {
            "results": [
                {
                    "title": "Issuer",
                    "url": "https://fixture.example/ir",
                    "snippet": "short",
                    "summary": "summary " * 200,
                    "content": "",
                    "date": "2026-10-01T00:00:00Z",
                    "site_name": "IR",
                    "authority": 4,
                    "rerank_score": 3,
                }
            ]
        },
    )
    assert parsed["status"] == "ok"
    entry = parsed["entries"][0]
    assert entry["snippet"] == ("summary " * 200).strip()[:500]
    assert entry["snippet_kind"] == "provider_summary"
    assert "authority" not in entry and "rerank_score" not in entry


@pytest.mark.parametrize(
    "payload,status",
    [({"results": []}, "empty"), ({}, "parse_failure"), ({"error": {"code": 1}}, "business_error")],
)
def test_empty_missing_and_error_are_distinct(payload, status):
    assert parse_route("deepseek_search", payload)["status"] == status


def test_policy12_admits_only_explicit_priority_and_prices_empty_zero(tmp_path, monkeypatch):
    p = policy(tmp_path, monkeypatch)
    assert p.schema_id == "stockqa.quick_scan_search_policy/1.2.0"
    assert p.admitted == ("brave-primary",)
    resolver = ExternalSearchCostResolver(p)
    receipt = dict(
        origin="external",
        route_id="brave-primary",
        route_kind="deepseek_search",
        http_request_count=1,
        http_status_code=200,
        outcome="response_available",
        parse_status="empty",
        provider_result_count=0,
    )
    assert resolver(receipt)["actual_cost"] == Decimal("0")
    receipt.update(parse_status="ok", provider_result_count=1)
    assert resolver(receipt)["actual_cost"] == Decimal("0.005")
    receipt["parse_status"] = "parse_failure"
    assert resolver(receipt) is None


@pytest.mark.parametrize("query", ["x" * 129, "x\nignore"])
def test_deepseek_policy_rejects_invalid_search_before_send(tmp_path, monkeypatch, query):
    doc = document()
    doc["execution_plan"]["queries"][0]["query"] = query
    with pytest.raises(ValueError):
        policy(tmp_path, monkeypatch, doc)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@fixture.example/ir",
        "http://fixture.example/ir",
        "https://127.0.0.1/",
        "https://localhost/",
        "https://fixture.example/annual.pdf",
        "https://fixture.example/a%2epdf",
        "https://fixture.example/a.exe",
    ],
)
def test_fetch_policy_rejects_unsafe_or_document_urls(tmp_path, monkeypatch, url):
    doc = document("deepseek_fetch")
    q = doc["execution_plan"]["queries"][0]
    q.update(
        query=url,
        operation="fetch",
        source_operation_id="SEARCH_" + "a" * 64,
        source_receipt_sha256="b" * 64,
        top_k=1,
    )
    with pytest.raises(ValueError):
        policy(tmp_path, monkeypatch, doc)


def _begin(store, item, lease, p):
    provider = ExternalSearchProvider(p, p.admitted[0])
    route = provider.route
    budget = _journal_budget()
    budget["budget"]["currency"] = "CNY"
    budget["policy_version"] = route["kind"] + "/" + route["route_id"]
    budget["routes"][0].update(
        id=route["route_id"],
        provider_config_ref=route["kind"],
        quota_group=route["dispatch"]["quota_group"],
    )
    budget["quota_groups"][0]["id"] = route["dispatch"]["quota_group"]
    plan = execution_query_plans(p)[0]
    op = store.begin_external_search(
        item["work_item_id"],
        lease,
        policy=budget,
        plan=plan,
        route_id=route["route_id"],
        provider=route["kind"],
        quota_group=route["dispatch"]["quota_group"],
        adapter_version="stockqa.external_retrieval/1.0.0",
        search_policy_sha256=p.policy_sha256,
        endpoint=route["endpoint"],
    )
    permit = QuickScanSendAttempt(
        None,
        op["budget_attempt_id"],
        SendPermit(op["budget_attempt_id"], op["created_at"], 0),
        budget_binding=QuickScanBudgetBinding(store, budget),
        budget_attempt_id=op["budget_attempt_id"],
        external_operation_id=op["operation_id"],
    )
    return provider, permit, op, plan


def _call(provider, permit, plan):
    return provider.fetch(permit, query=plan["query"], top_k=plan["top_k"], locale=plan["locale"])


def _search_result():
    return {
        "results": [
            {
                "title": "Issuer",
                "url": "https://fixture.example/ir",
                "snippet": "Margin source",
                "summary": "Margin 20%",
                "date": "2026-10-01T00:00:00Z",
                "site_name": "IR",
            }
        ]
    }


def test_real_sqlite_permit_search_then_bound_fetch_retains_only_excerpt(tmp_path, monkeypatch):
    p = policy(tmp_path, monkeypatch)
    store, item, lease, clock = _journal(tmp_path)
    session = Mock()
    session.request.return_value = _response(json.dumps(_search_result()).encode())
    monkeypatch.setattr(
        "src.utils.http_client.http_client_manager.get_metered_sync_session", lambda: session
    )
    provider, permit, op, plan = _begin(store, item, lease, p)
    receipt = _call(provider, permit, plan)
    assert session.request.call_args.kwargs["json"] == {
        "query": plan["query"],
        "count": 3,
        "need_content": False,
    }
    assert session.request.call_count == 1
    price = ExternalSearchCostResolver(p)(receipt)
    parent = store.record_external_search(
        op["operation_id"],
        receipt=receipt,
        actual_cost=price["actual_cost"],
        cost_source_ref=price["source_ref"],
    )
    with pytest.raises(Exception):
        _call(provider, permit, plan)
    assert session.request.call_count == 1
    doc = document("deepseek_fetch")
    doc["external_routes"][0]["route_id"] = "fetch-only"
    doc["selection"]["external_search_priority"] = [{"route_id": "fetch-only"}]
    doc["execution_plan"]["queries"][0].update(
        operation="fetch",
        query="https://fixture.example/ir",
        source_operation_id=parent["operation_id"],
        source_receipt_sha256=parent["result"]["receipt_sha256"],
        top_k=1,
    )
    fp = policy(tmp_path, monkeypatch, doc)
    provider, permit, op, plan = _begin(store, item, lease, fp)
    body = {"title": "IR page", "content": "Actual disclosure " * 1000}
    session.request.return_value = _response(json.dumps(body).encode())
    receipt = _call(provider, permit, plan)
    assert session.request.call_args.kwargs["json"] == {"url": "https://fixture.example/ir"}
    assert receipt["entries"][0]["snippet"] == body["content"][:500]
    assert receipt["entries"][0]["snippet_kind"] == "fetched_excerpt"
    assert len(receipt["entries"][0]["content_sha256"]) == 64
    assert body["content"] not in json.dumps(receipt)
    price = ExternalSearchCostResolver(fp)(receipt)
    record = store.record_external_search(
        op["operation_id"],
        receipt=receipt,
        actual_cost=price["actual_cost"],
        cost_source_ref=price["source_ref"],
    )
    assert record["budget"]["status"] == "settled"


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 408, 413, 429, 500, 504])
def test_received_deepseek_errors_are_classified_and_not_retried(tmp_path, monkeypatch, status):
    p = policy(tmp_path, monkeypatch)
    store, item, lease, _ = _journal(tmp_path)
    provider, permit, op, plan = _begin(store, item, lease, p)
    session = Mock()
    session.request.return_value = _response(b'{"error":{"message":"private"}}', status=status)
    monkeypatch.setattr(
        "src.utils.http_client.http_client_manager.get_metered_sync_session", lambda: session
    )
    with pytest.raises(ExternalSearchTransportError) as e:
        _call(provider, permit, plan)
    assert e.value.receipt["outcome"] == "confirmed_failure"
    assert "private" not in json.dumps(e.value.receipt)
    assert ExternalSearchCostResolver(p)(e.value.receipt)["actual_cost"] == 0
    store.record_external_search(
        op["operation_id"],
        receipt=e.value.receipt,
        actual_cost=Decimal(0),
        cost_source_ref="synthetic",
    )
    assert session.request.call_count == 1


def test_unknown_send_retains_budget_and_is_not_a_free_failure(tmp_path, monkeypatch):
    p = policy(tmp_path, monkeypatch)
    store, item, lease, _ = _journal(tmp_path)
    provider, permit, op, plan = _begin(store, item, lease, p)
    session = Mock()
    session.request.side_effect = TimeoutError("not persisted")
    monkeypatch.setattr(
        "src.utils.http_client.http_client_manager.get_metered_sync_session", lambda: session
    )
    with pytest.raises(ExternalSearchTransportError) as e:
        _call(provider, permit, plan)
    assert ExternalSearchCostResolver(p)(e.value.receipt) is None
    record = store.record_external_search(op["operation_id"], receipt=e.value.receipt)
    assert record["budget"]["status"] != "settled"
    assert session.request.call_count == 1


def test_fetch_parent_receipt_hash_or_url_mismatch_sends_zero(tmp_path, monkeypatch):
    # Even an invented valid-looking SEARCH id cannot authorize a fetch.
    doc = document("deepseek_fetch")
    doc["execution_plan"]["queries"][0].update(
        operation="fetch",
        query="https://fixture.example/ir",
        source_operation_id="SEARCH_" + "a" * 64,
        source_receipt_sha256="b" * 64,
        top_k=1,
    )
    p = policy(tmp_path, monkeypatch, doc)
    store, item, lease, _ = _journal(tmp_path)
    provider, permit, op, plan = _begin(store, item, lease, p)
    session = Mock()
    monkeypatch.setattr(
        "src.utils.http_client.http_client_manager.get_metered_sync_session", lambda: session
    )
    with pytest.raises(KeyError):
        _call(provider, permit, plan)
    assert session.request.call_count == 0
    assert store.get_external_search(op["operation_id"])["dispatch"] is None


def test_search_pdf_links_remain_visible_without_fetching_the_document():
    payload = _search_result()
    payload["results"][0]["url"] = "https://fixture.example/annual.pdf"
    assert parse_route("deepseek_search", payload)["status"] == "ok"


def test_same_owner_qps_gate_is_atomic_and_does_not_consume_unsent_permit(tmp_path, monkeypatch):
    p = policy(tmp_path, monkeypatch)
    store, item, lease, clock = _journal(tmp_path)
    provider, permit, op, plan = _begin(store, item, lease, p)
    session = Mock()
    session.request.return_value = _response(json.dumps(_search_result()).encode())
    monkeypatch.setattr(
        "src.utils.http_client.http_client_manager.get_metered_sync_session", lambda: session
    )
    receipt = _call(provider, permit, plan)
    price = ExternalSearchCostResolver(p)(receipt)
    store.record_external_search(
        op["operation_id"],
        receipt=receipt,
        actual_cost=price["actual_cost"],
        cost_source_ref=price["source_ref"],
    )
    doc = document()
    doc["execution_plan"]["queries"][0].update(
        query_id="second-query", query="Issuer second question"
    )
    second = policy(tmp_path, monkeypatch, doc)
    provider, permit, op, plan = _begin(store, item, lease, second)
    with pytest.raises(Exception):
        _call(provider, permit, plan)
    assert session.request.call_count == 1
    assert store.get_external_search(op["operation_id"])["dispatch"] is None
    clock[0] += 0.21
    _call(provider, permit, plan)
    assert session.request.call_count == 2


@pytest.mark.parametrize("mutation", ["hash", "url", "expired"])
def test_actual_parent_mismatch_is_rejected_before_fetch_http(tmp_path, monkeypatch, mutation):
    parent_doc = document()
    if mutation == "expired":
        parent_doc["retrieval"]["search_ttl_seconds"] = 1
    p = policy(tmp_path, monkeypatch, parent_doc)
    store, item, lease, clock = _journal(tmp_path)
    provider, permit, op, plan = _begin(store, item, lease, p)
    session = Mock()
    session.request.return_value = _response(json.dumps(_search_result()).encode())
    monkeypatch.setattr(
        "src.utils.http_client.http_client_manager.get_metered_sync_session", lambda: session
    )
    receipt = _call(provider, permit, plan)
    price = ExternalSearchCostResolver(p)(receipt)
    parent = store.record_external_search(
        op["operation_id"],
        receipt=receipt,
        actual_cost=price["actual_cost"],
        cost_source_ref=price["source_ref"],
    )
    doc = document("deepseek_fetch")
    q = doc["execution_plan"]["queries"][0]
    q.update(
        operation="fetch",
        query="https://fixture.example/ir",
        source_operation_id=parent["operation_id"],
        source_receipt_sha256=parent["result"]["receipt_sha256"],
        top_k=1,
    )
    if mutation == "hash":
        q["source_receipt_sha256"] = "b" * 64
    if mutation == "url":
        q["query"] = "https://fixture.example/other"
    if mutation == "expired":
        clock[0] += 2
    fp = policy(tmp_path, monkeypatch, doc)
    provider, permit, op, plan = _begin(store, item, lease, fp)
    with pytest.raises(Exception):
        _call(provider, permit, plan)
    assert session.request.call_count == 1
    assert store.get_external_search(op["operation_id"])["dispatch"] is None


def test_cached_policy12_validation_needs_no_key_but_transport_does(tmp_path, monkeypatch):
    doc = document()
    path = tmp_path / "no-key-policy.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    p = load_search_policy(path, allow_cached_recovery=True)
    assert p.admitted == ("brave-primary",)
    with pytest.raises(ValueError, match="credential_env_unset"):
        ExternalSearchProvider(p, p.admitted[0])
