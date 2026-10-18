import json
import sqlite3

import pytest

from src.providers.llm_client import LLMTransportAttemptError
from src.providers.llm_research_client import ResearchChatSession
from src.utils.quick_scan_work_store import _sanitized_receipt
from src.utils.quick_scan_work_transport import (
    QuickScanSendAttempt,
    QuickScanWorkUncertainError,
)
from tests.unit.test_llm_research_client import (
    TOOLS,
    budget,
    client,
    deny_network,
    response,
    transport,
)


@pytest.mark.parametrize(
    "kind,code",
    [
        ("length", "research_incomplete_output"),
        ("role", "research_role_invalid"),
        ("schema", "research_tool_arguments_schema_invalid"),
    ],
)
@pytest.mark.parametrize("uncertain", [False, True])
def test_original_native_receipt_retains_code_without_output(
    tmp_path, transport, monkeypatch, kind, code, uncertain
):
    payload = response(tools=True, arguments="{}" if kind == "schema" else '{"key":"A"}')
    if kind == "length":
        payload["choices"][0]["finish_reason"] = "length"
    if kind == "role":
        payload["choices"][0]["message"]["role"] = "user"
    transport[0].payloads = [payload]
    if uncertain:
        original = QuickScanSendAttempt.record_response

        def fail_after_native_commit(self, **kwargs):
            original(self, **kwargs)
            raise QuickScanWorkUncertainError(self.attempt_id, kwargs["receipt"])

        monkeypatch.setattr(QuickScanSendAttempt, "record_response", fail_after_native_commit)
    session = ResearchChatSession(client(), system_prefix="rules", tools=TOOLS)
    with budget(tmp_path) as store:
        with pytest.raises((LLMTransportAttemptError, QuickScanWorkUncertainError)) as caught:
            session.step(user_context="strict diagnostic durability")
        assert caught.value.attempt_receipt.get("research_validation_code") == code
        with sqlite3.connect(store.path) as conn:
            receipts = [
                json.loads(row[0])
                for row in conn.execute("SELECT receipt_json FROM quick_scan_attempt_response")
            ]
            dump = "\n".join(conn.iterdump())
        assert len(receipts) == 1 and receipts[0].get("research_validation_code") == code
        assert "PRIVATE_REASONING_CANARY" not in dump and "SECRET_KEY_CANARY" not in dump
        assert store.get_quick_scan_budget_status("research-test")["spent_micros"] == 1000
        with pytest.raises(RuntimeError):
            session.step(user_context="never resend")
    assert len(transport[1]) == 1


@pytest.mark.parametrize(
    "value", ["RAW_PROVIDER_CANARY", "research_incomplete_output CANARY", None, True, [], {}, 123]
)
def test_sanitizer_rejects_untrusted_diagnostic_values(value):
    assert "research_validation_code" not in _sanitized_receipt({"research_validation_code": value})


def test_success_and_legacy_receipts_unchanged(tmp_path, transport):
    from src.utils.quick_scan_work_store import quick_scan_receipt_sha256

    receipt = {"attempt_id": "old", "http_status_code": 200}
    assert _sanitized_receipt(receipt) == receipt
    assert quick_scan_receipt_sha256(receipt) == quick_scan_receipt_sha256(
        {**receipt, "research_validation_code": "UNTRUSTED_CANARY"}
    )
    transport[0].payloads = [response()]
    with budget(tmp_path):
        result = ResearchChatSession(client(), system_prefix="rules", tools=TOOLS).step(
            user_context="valid response"
        )
    assert "research_validation_code" not in result["receipt"]


@pytest.mark.parametrize("missing", ["prompt_cache_hit_tokens", "completion_tokens_details"])
def test_missing_usage_remains_unpriced(tmp_path, transport, missing):
    payload = response(finish="length")
    payload["usage"].pop(missing)
    transport[0].payloads = [payload]
    session = ResearchChatSession(client(), system_prefix="rules", tools=TOOLS)
    with budget(tmp_path) as store:
        with pytest.raises(LLMTransportAttemptError) as caught:
            session.step(user_context="missing usage")
        assert (
            caught.value.attempt_receipt["research_validation_code"] == "research_incomplete_output"
        )
        assert "usage" not in caught.value.attempt_receipt
        status = store.get_quick_scan_budget_status("research-test")
        assert status["unreconciled_attempts"] == 1 and status["reserved_micros"] > 0
        with pytest.raises(RuntimeError):
            session.step(user_context="no resend")
    assert len(transport[1]) == 1
