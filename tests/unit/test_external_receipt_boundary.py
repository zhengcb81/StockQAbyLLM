"""Regression for a paid response reaching the actual durable receipt validator."""

import unittest

from src.providers import external_search_parsers as parser
from src.utils.quick_scan_external_journal import _receipt


class ReceiptBoundary(unittest.TestCase):
    def test_mixed_realistic_response_survives_validator(self):
        rows = [
            {
                "url": "http://example.com/a",
                "title": "HTTP candidate",
                "description": "Unusable link",
            },
            {
                "url": "https://example.com/b",
                "title": "Good candidate",
                "description": "Useful text",
                "page_age": "x" * 101,
            },
            {
                "url": "https://user:pass@example.com/c",
                "title": "Credentials",
                "description": "Unusable link",
            },
            {"url": "https://[broken/a", "title": "Malformed", "description": "Unusable link"},
        ]
        value = parser.parse_brave({"web": {"results": rows}})
        self.assertEqual(len(value["entries"]), 1)
        self.assertIsNone(value["entries"][0]["published_at"])
        context = {
            "route": {"adapter_version": "test", "route_id": "brave-test", "provider": "brave"},
            "plan": {"top_k": 5},
        }
        receipt = {
            "origin": "external",
            "adapter_version": "test",
            "route_id": "brave-test",
            "route_kind": "brave",
            "attempt_id": "DISPATCH_test",
            "http_request_count": 1,
            "http_status_code": 200,
            "outcome": "response_available",
            "parse_status": "ok",
            "provider_result_count": 4,
            "response_body_sha256": "a" * 64,
            "entries": value["entries"],
        }
        self.assertEqual(_receipt(receipt, context, "DISPATCH_test")["entries"], value["entries"])

    def test_date_is_not_truncated_into_a_fabricated_date(self):
        for date in ("x" * 101, "2024-01-01\nmalformed", "   "):
            with self.subTest(date=repr(date)):
                entry = parser._candidate(
                    title="t", url="https://example.com/a", snippet="s", published_at=date
                )
                self.assertIsNone(entry["published_at"])

    def test_url_contract(self):
        for url in (
            "http://example.com",
            "https://user@example.com",
            "https://[bad",
            "https://example.com:bad",
            "https://example.com/\n",
            "https://example.com/" + "a" * 2048,
        ):
            with self.subTest(url=url[:50]):
                self.assertIsNone(parser._candidate(title="t", url=url, snippet="s"))

    def test_legal_metadata_preserved(self):
        entry = parser._candidate(
            title=" t ", url="https://example.com/a", snippet=" s ", published_at="2024-07-27"
        )
        self.assertEqual(entry["published_at"], "2024-07-27")
        self.assertEqual(entry["snippet"], "s")

    def test_nonempty_deepseek_response_keeps_billable_count_after_filter(self):
        value = parser.parse_deepseek_search(
            {
                "results": [
                    {"url": "http://example.com/a", "title": "t", "summary": "s"},
                    {"url": "https://user@example.com/b", "title": "t", "summary": "s"},
                ]
            }
        )
        self.assertEqual(value["entries"], [])
        self.assertEqual(value["provider_result_count"], 2)


import json
from decimal import Decimal
from unittest.mock import Mock

import pytest

from tests.unit.test_external_search_provider import _permit, _provider, _response


@pytest.mark.parametrize("all_unsafe", [False, True])
def test_one_paid_response_always_reaches_durable_journal(tmp_path, monkeypatch, all_unsafe):
    from src.utils.http_client import http_client_manager

    provider = _provider(tmp_path, monkeypatch, "brave")
    permit = _permit(tmp_path, provider)
    rows = [
        {"title": "HTTP candidate", "url": "http://example.com/a", "description": "not retained"}
    ]
    if not all_unsafe:
        rows.append(
            {
                "title": "Good",
                "url": "https://example.com/b",
                "description": "retained",
                "page_age": "x" * 101,
            }
        )
    session = Mock()
    session.request.return_value = _response(json.dumps({"web": {"results": rows}}).encode())
    monkeypatch.setattr(http_client_manager, "get_metered_sync_session", lambda *a, **k: session)
    receipt = provider.fetch(permit, query="Fixture", top_k=2, locale="en-US")
    store = permit.budget_binding.store
    record = store.record_external_search(
        permit.external_operation_id,
        receipt=receipt,
        actual_cost=Decimal("0.01"),
        cost_source_ref="synthetic-regression-price",
    )
    assert record["budget"]["status"] == "settled"
    assert record["budget"]["in_flight"] == 0
    assert record["result"]["receipt"]["response_body_sha256"] == receipt["response_body_sha256"]
    assert len(record["result"]["receipt"]["entries"]) == (0 if all_unsafe else 1)
    assert record["result"]["receipt"]["provider_result_count"] == len(rows)
    assert session.request.call_count == 1
