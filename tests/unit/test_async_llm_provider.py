"""Async repair provenance and durable-error propagation with isolated HTTP."""

import asyncio
import hashlib
import json
import subprocess
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from src.core.models import Question
from src.providers.async_llm_provider import AsyncLLMProvider
from src.providers.llm_client import LLMTransportAttemptError
from src.utils.quick_scan_work_store import (
    QuickScanWorkStore,
    quick_scan_receipt_sha256,
)
from src.utils.quick_scan_work_transport import (
    QuickScanSendAttempt,
    QuickScanWorkPersistenceError,
    QuickScanWorkUncertainError,
    bind_quick_scan_route,
    bind_quick_scan_work,
)
from tests.unit.test_execution_feedback_intake import PYTHON, _offline_env


def _provider(tmp_path, *, alias=False):
    resolution = {
        "schema_version": "1.0.0",
        "aliases": (
            [
                {
                    "provider": "openai",
                    "protocol": "responses",
                    "requested_model": "model-a",
                    "resolved_model": "model-b",
                }
            ]
            if alias
            else []
        ),
    }
    path = tmp_path / "synthetic-llm.json"
    path.write_text(
        json.dumps(
            {
                "default_provider": "openai",
                "providers": {
                    "openai": {
                        "enabled": True,
                        "api_key": "synthetic-key",
                        "model": "model-a",
                        "base_url": "https://api.openai.com/v1/responses",
                        "max_retries": 2,
                        "format_repair_budget": 1,
                    }
                },
                "quick_scan_model_resolution": resolution,
            }
        ),
        encoding="utf-8",
    )
    provider = AsyncLLMProvider(
        provider_name="openai",
        api_key="synthetic-key",
        config_file=str(path),
        require_search=True,
        entity_id="ENT_FIXTURE_CO",
        company_name="Fixture Co",
    )
    provider.retry_strategy.get_wait_time = lambda _retry: 0
    return provider, resolution


def test_batch_failure_is_readable_under_one_parent_attempt_without_resending(
    tmp_path, monkeypatch
):
    original = tmp_path / "original"
    inbox = tmp_path / "inbox"
    monkeypatch.setenv("RESEARCH_FEEDBACK_DIR", str(inbox))
    monkeypatch.setenv("RESEARCH_FEEDBACK_RUN_ID", "original-batch")
    monkeypatch.setenv("RESEARCH_FEEDBACK_ATTEMPT_ID", "original-attempt")
    provider, _ = _provider(tmp_path)
    private = "private-question-sentinel"
    method = AsyncMock(side_effect=TypeError("private-error-sentinel"))
    monkeypatch.setattr(provider.client, "send_search_request_async", method)

    assert asyncio.run(provider.batch_search_async([private, "other-private-question"])) == [[], []]
    assert method.await_count == 2

    original.mkdir()
    env = _offline_env(original)
    cold = json.loads(
        subprocess.check_output(
            [
                *PYTHON,
                "-m",
                "src.utils.quick_scan_feedback_owner",
                "--observation-dir",
                str(inbox),
                "runtime-history",
                "--run-id",
                "original-batch",
                "--attempt-id",
                "original-attempt",
            ],
            cwd=original,
            env=env,
            timeout=30,
        )
    )
    failures = cold["failures"]
    assert len(failures) == 2
    assert {failure["diagnostic"]["root_type"] for failure in failures} == {"TypeError"}
    assert {failure["cost_state"] for failure in failures} == {"unknown"}
    assert all(failure["diagnostic"]["chain"][0]["frames"] for failure in failures)
    references = [failure["diagnostic"]["references"] for failure in failures]
    assert {ref["question_id"] for ref in references} == {
        "query-sha256-" + hashlib.sha256(query.encode()).hexdigest()
        for query in (private, "other-private-question")
    }
    assert len({ref["invocation_id"] for ref in references}) == 1
    assert private not in json.dumps(cold) and "private-error-sentinel" not in json.dumps(cold)
    assert method.await_count == 2


def test_batch_keeps_original_transport_causes_when_returns_error_results(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    monkeypatch.setenv("RESEARCH_FEEDBACK_DIR", str(inbox))
    monkeypatch.setenv("RESEARCH_FEEDBACK_RUN_ID", "timeout-batch")
    monkeypatch.setenv("RESEARCH_FEEDBACK_ATTEMPT_ID", "timeout-attempt")
    provider, _ = _provider(tmp_path)
    provider.max_retries = 1
    questions = {
        "private-transport-question-alpha": "request-fixture-alpha",
        "private-transport-question-beta": "request-fixture-beta",
    }

    async def timeout(prompt):
        (request_id,) = [request for question, request in questions.items() if question in prompt]
        try:
            raise TimeoutError("private-transport-error")
        except TimeoutError as cause:
            raise LLMTransportAttemptError(
                "TimeoutError",
                {"request_id": request_id, "attempt_id": "provider-" + request_id},
            ) from cause

    transport = AsyncMock(side_effect=timeout)
    monkeypatch.setattr(provider.client, "send_search_request_async", transport)
    results = asyncio.run(provider.batch_search_async(list(questions)))
    assert [result[0].status for result in results] == ["error", "error"]
    assert [result[0].metadata["attempts"][0]["request_id"] for result in results] == list(
        questions.values()
    )
    assert transport.await_count == 2

    cold = tmp_path / "fresh-reader"
    cold.mkdir()
    env = _offline_env(cold)
    route = [
        *PYTHON,
        "-m",
        "src.utils.quick_scan_feedback_owner",
        "--observation-dir",
        str(inbox),
        "runtime-history",
        "--run-id",
        "timeout-batch",
        "--attempt-id",
        "timeout-attempt",
    ]
    first, second = (
        subprocess.check_output(route, cwd=cold, env=env, timeout=30) for _ in range(2)
    )
    assert first == second and transport.await_count == 2
    failures = json.loads(first)["failures"]
    expected = {
        "query-sha256-" + hashlib.sha256(question.encode()).hexdigest(): request
        for question, request in questions.items()
    }
    assert len(failures) == len(expected)
    invocations = set()
    for failure in failures:
        diagnostic = failure["diagnostic"]
        assert diagnostic["root_type"] == "TimeoutError"
        assert [node["type"] for node in diagnostic["chain"]] == [
            "LLMTransportAttemptError",
            "TimeoutError",
        ]
        assert diagnostic["chain"][-1]["frames"]
        refs = diagnostic["references"]
        assert refs["request_id"] == expected.pop(refs["question_id"])
        assert refs["provider_attempt_id"] == "provider-" + refs["request_id"]
        assert refs["attempt_id"] == "timeout-attempt"
        invocations.add(refs["invocation_id"])
        assert failure["cost_state"] == "unknown"
    assert not expected and len(invocations) == 1
    assert b"private-transport-" not in first and b"async_provider_returned_error" not in first


def _claimed(tmp_path):
    store = QuickScanWorkStore(tmp_path / "async-repair.sqlite")
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
        question_fingerprint=hashlib.sha256(b"question").hexdigest(),
        routing_fingerprint=hashlib.sha256(b"routes").hexdigest(),
        run_id="run-async-repair",
        scan_id="scan-async-repair",
    )
    lease = store.claim(item["work_item_id"], lease_seconds=30)
    assert lease is not None
    return store, item["work_item_id"], lease


def _response(response_id, *, valid=False, model="model-a"):
    text = "malformed answer"
    if valid:
        text = json.dumps(
            {
                "question_id": "CORE_01",
                "entity_id": "ENT_FIXTURE_CO",
                "company_name": "Fixture Co",
                "status": "scored",
                "score": 8,
                "description": "Synthetic repaired answer",
                "actual_model": "forged-in-answer",
            }
        )
    response = Mock()
    response.status_code = 200
    response.headers = {"x-request-id": f"request-{response_id}"}
    response.json.return_value = {
        "id": response_id,
        "status": "completed",
        "model": model,
        "output": [
            {
                "type": "web_search_call",
                "id": f"search-{response_id}",
                "status": "completed",
                "action": {
                    "type": "search",
                    "sources": [{"type": "url", "url": "https://example.org/source"}],
                },
            },
            {
                "type": "message",
                "status": "completed",
                "content": [{"type": "output_text", "text": text}],
            },
        ],
    }
    return response


def _install_http(monkeypatch, responses):
    session = Mock()
    session.post = AsyncMock(side_effect=responses)
    monkeypatch.setattr(
        "src.providers.llm_client.http_client_manager.get_async_client",
        AsyncMock(return_value=session),
    )
    return session


@pytest.mark.parametrize("alias", [False, True])
def test_async_format_repair_records_two_attempts_and_only_final_http_identity(
    tmp_path, monkeypatch, alias
):
    provider, resolution = _provider(tmp_path, alias=alias)
    store, work_id, lease = _claimed(tmp_path)
    resolved = "model-b" if alias else "model-a"
    session = _install_http(
        monkeypatch, [_response("initial"), _response("repaired", valid=True, model=resolved)]
    )
    route = {"route_id": "openai-route", "provider": "openai", "model_requested": "model-a"}
    if alias:
        route["model_resolution"] = resolution
    with bind_quick_scan_work(store, work_id, lease), bind_quick_scan_route(**route):
        results = asyncio.run(
            provider.search_question_async(Question("Synthetic question", question_id="CORE_01"))
        )
    assert results[0].status == "scored"
    assert results[0].score == 8
    assert session.post.await_count == 2
    attempts = store.list_attempts(work_id)
    assert len(attempts) == 2
    assert [attempt["phase"] for attempt in attempts] == [
        "response_available",
        "response_available",
    ]
    assert attempts[0]["attempt_id"] != attempts[1]["attempt_id"]
    execution = results[0].metadata["execution"]
    final = execution["work_transport"]["final_receipt"]
    assert execution["format_repair"]["status"] == "repaired"
    assert execution["work_transport"]["work_attempt_id"] == attempts[1]["attempt_id"]
    assert final["response_id"] == "repaired"
    assert final["actual_model"] == resolved
    assert attempts[1]["receipt_sha256"] == quick_scan_receipt_sha256(final)
    assert attempts[0]["receipt_sha256"] != attempts[1]["receipt_sha256"]
    assert [call.kwargs["json"]["model"] for call in session.post.await_args_list] == [
        "model-a",
        "model-a",
    ]


def test_async_repair_uncertain_outcome_propagates_without_third_post(tmp_path, monkeypatch):
    provider, _ = _provider(tmp_path)
    store, work_id, lease = _claimed(tmp_path)
    session = _install_http(
        monkeypatch, [_response("initial"), httpx.ReadTimeout("synthetic uncertain repair")]
    )
    with bind_quick_scan_work(store, work_id, lease), bind_quick_scan_route(
        route_id="openai-route", provider="openai", model_requested="model-a"
    ):
        with pytest.raises(QuickScanWorkUncertainError):
            asyncio.run(
                provider.search_question_async(
                    Question("Synthetic question", question_id="CORE_01")
                )
            )
    assert session.post.await_count == 2
    attempts = store.list_attempts(work_id)
    assert len(attempts) == 2
    assert attempts[0]["phase"] == "response_available"
    assert attempts[1]["phase"] == "uncertain"


def test_async_repair_persistence_error_propagates_without_retry_or_success(tmp_path, monkeypatch):
    provider, _ = _provider(tmp_path)
    store, work_id, lease = _claimed(tmp_path)
    session = _install_http(monkeypatch, [_response("initial"), _response("repaired", valid=True)])
    original = QuickScanSendAttempt.record_response
    written = []

    def record_response(attempt, **kwargs):
        written.append(attempt.attempt_id)
        if len(written) == 2:
            raise QuickScanWorkPersistenceError("synthetic repair receipt write failed")
        return original(attempt, **kwargs)

    monkeypatch.setattr(QuickScanSendAttempt, "record_response", record_response)
    with bind_quick_scan_work(store, work_id, lease), bind_quick_scan_route(
        route_id="openai-route", provider="openai", model_requested="model-a"
    ):
        with pytest.raises(QuickScanWorkPersistenceError, match="synthetic repair"):
            asyncio.run(
                provider.search_question_async(
                    Question("Synthetic question", question_id="CORE_01")
                )
            )
    assert session.post.await_count == 2
    assert len(set(written)) == 2
    assert len(store.list_attempts(work_id)) == 2
