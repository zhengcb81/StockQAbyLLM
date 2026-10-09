"""Actual public main() with synthetic identity/manifest/pricing and HTTP only.

These are software integration fixtures, never StockWiki owner goldens or
financial correctness evidence. Every file and database stays in tmp_path.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from src.utils.quick_scan_work_store import QuickScanWorkStore

# These tests reuse the existing guarded subprocess harness. Only the actual
# requests HTTP boundary, a synthetic price card and the store's public clock
# dependency are controlled. No runner/coordinator/parser/checkpoint is mocked.
from tests.integration.test_qa_c06_02_subprocess_cli import _GUARD, _child_env
from tests.integration.test_quick_scan_cli import (
    _http_failure,
    _invoke,
    _ordered_policy,
    _response,
    _write_inputs,
)
from tests.unit.test_external_search_provider import _response as search_response
from tests.unit.test_quick_scan_external_context import _execution_policy_document


def _inputs(tmp_path, monkeypatch, *, hybrid=False):
    # Independently freeze inputs before constructing any model answer.
    prompt = "公司的第1项核心优势是否持久？"
    identity = {
        "object_type": "entity",
        "schema_version": "2.2.0",
        "payload": {
            "entity_id": "ENT_SYNTHETIC",
            "identity_revision": 1,
            "identity_state": "verified",
            "listings": [{"source_binding_ref": "BND_SYNTHETIC_ONLY"}],
        },
        "fixture_notice": "synthetic identity, not a StockWiki verified issuer golden",
    }
    identity_path = tmp_path / "identity-synthetic.json"
    identity_path.write_text(json.dumps(identity), encoding="utf-8")
    digest = lambda data: hashlib.sha256(data).hexdigest()
    manifest = {
        "schema_version": "1.0.0",
        "template_version": "synthetic/1",
        "question_count": 1,
        "modules": ["synthetic/core"],
        "module_locks": {},
        "replacements": {},
        "questions": [
            {
                "id": "IQS_05",
                "module_id": "synthetic/core",
                "scope": "entity",
                "prompt": prompt,
                "prompt_sha256": digest(prompt.encode()),
                "semantic_sha256": digest(b"synthetic-semantic"),
                "definition_sha256": digest(b"synthetic-definition"),
            }
        ],
    }
    manifest_path = tmp_path / "manifest-synthetic.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    search = _execution_policy_document()
    search["external_routes"][0]["credential_env"] = "IQS_SYNTHETIC_SEARCH_TOKEN"
    search["external_routes"][0]["endpoint"] = "https://api.search.brave.com/res/v1/web/search"
    plan = search["execution_plan"]
    plan["entity_id"] = "ENT_SYNTHETIC"
    plan["identity_snapshot_sha256"] = digest(identity_path.read_bytes())
    plan["question_manifest_sha256"] = digest(manifest_path.read_bytes())
    for origin in plan["entity_domain_bindings"]:
        origin["identity_snapshot_sha256"] = plan["identity_snapshot_sha256"]
    if hybrid:
        search["selection"].update(
            mode="explicit_hybrid", hybrid_plan_reference="synthetic-hybrid/1"
        )
        plan["answer_search_mode"] = "native_with_external_context"
    search_path = tmp_path / "search-synthetic.json"
    search_path.write_text(json.dumps(search), encoding="utf-8")
    model = _ordered_policy(max_attempts=1)
    model["models"] = [{**model["models"][0], "provider_config_ref": "openai", "model": "gpt-4.1"}]
    model["budget"]["max_cost"] = 25
    providers = {
        "openai": {
            "enabled": True,
            "api_key": "synthetic-only",
            "model": "gpt-4.1",
            "base_url": "https://api.openai.com/v1/responses",
            "max_retries": 1,
        }
    }
    monkeypatch.setenv("IQS_SYNTHETIC_SEARCH_TOKEN", "synthetic-only")
    session = Mock()
    session.request.return_value = search_response(
        json.dumps(
            {
                "web": {
                    "results": [
                        {
                            "title": "Synthetic issuer",
                            "url": "https://fixture.example/ir",
                            "description": "Synthetic margin disclosure",
                            "page_age": "2026-10-01",
                        }
                    ]
                }
            }
        ).encode()
    )
    monkeypatch.setattr(
        "src.utils.http_client.http_client_manager.get_metered_sync_session", lambda: session
    )
    response = _response(
        searched=hybrid,
        content=json.dumps(
            {
                "entity_id": "ENT_SYNTHETIC",
                "company_name": "Fixture Corp",
                "question_id": "IQS_05",
                "status": "scored",
                "score": 6,
                "description": "Synthetic supported answer",
            }
        ),
    )
    response.json.return_value["model"] = "gpt-4.1"
    response.json.return_value["output"][-1]["role"] = "assistant"
    response.content = json.dumps(response.json.return_value).encode()
    response.text = response.content.decode()
    kwargs = dict(
        entity_id="ENT_SYNTHETIC",
        model_policy=model,
        provider_configs=providers,
        responses=[response],
        extra_argv=[
            "--identity-snapshot",
            str(identity_path),
            "--question-manifest",
            str(manifest_path),
            "--search-policy",
            str(search_path),
        ],
    )
    return kwargs, session, search_path


@pytest.mark.parametrize("hybrid", [False, True])
def test_public_external_main_cold_and_warm_keep_receipt_and_avoid_http(
    tmp_path, monkeypatch, hybrid
):
    kwargs, search, _ = _inputs(tmp_path, monkeypatch, hybrid=hybrid)
    code, output, models = _invoke(monkeypatch, tmp_path, **kwargs)
    assert code == 0
    first = json.loads(output.read_text("utf-8"))
    assert first["schema_version"] == "stockqa.quick_scan_result/1.1.0"
    receipt = first["execution_receipts"]["IQS_05"]
    assert receipt["search_status"] == "executed" and "search_binding" in receipt
    assert first["answers"]["IQS_05"]["score"] == 6
    assert search.request.call_count == 1 and models.post.call_count == 1
    proof = receipt["search_binding"]["external_context_use"]
    # Consumer's public temporal contract: persistence/use occurs after the
    # original HTTP completion and no later than the durable answer time.
    assert datetime.fromisoformat(
        receipt["completed_at"].replace("Z", "+00:00")
    ) <= datetime.fromtimestamp(proof["used_at"], timezone.utc)
    assert datetime.fromtimestamp(proof["used_at"], timezone.utc) <= datetime.fromisoformat(
        receipt["answered_at"].replace("Z", "+00:00")
    )
    store = QuickScanWorkStore(tmp_path / "quick_scan_work.sqlite")
    assert store.get_answer_checkpoint(proof["work_item_id"])["checkpoint_schema_version"] == 2
    before = store.get_quick_scan_budget_status("quick-scan-test-policy")
    code, output, models = _invoke(monkeypatch, tmp_path, **kwargs)
    assert code == 0
    second = json.loads(output.read_text("utf-8"))
    assert second["execution_receipts"]["IQS_05"]["answered_at"] == receipt["answered_at"]
    assert second["execution_receipts"]["IQS_05"] == receipt
    assert search.request.call_count == 1 and models.post.call_count == 0
    assert store.get_quick_scan_budget_status("quick-scan-test-policy") == before


def test_public_external_main_wrong_manifest_fails_before_store_or_http(
    tmp_path, monkeypatch, caplog
):
    kwargs, search, path = _inputs(tmp_path, monkeypatch)
    document = json.loads(path.read_text("utf-8"))
    document["execution_plan"]["question_manifest_sha256"] = "e" * 64
    path.write_text(json.dumps(document), encoding="utf-8")
    code, _, models = _invoke(monkeypatch, tmp_path, **kwargs)
    assert code != 0
    assert "external_policy_manifest_mismatch" in caplog.text
    assert search.request.call_count == 0 and models.post.call_count == 0
    assert not (tmp_path / "quick_scan_work.sqlite").exists()


def test_public_external_main_uncertain_search_preserves_reservation_and_stops_model(
    tmp_path, monkeypatch
):
    kwargs, search, _ = _inputs(tmp_path, monkeypatch)
    search.request.side_effect = requests.Timeout("synthetic timeout")
    code, _, models = _invoke(monkeypatch, tmp_path, **kwargs)
    assert code != 0
    assert search.request.call_count == 1 and models.post.call_count == 0
    store = QuickScanWorkStore(tmp_path / "quick_scan_work.sqlite")
    budget = store.get_quick_scan_budget_status("quick-scan-test-policy")
    assert budget["requests"] == 1 and budget["reserved_micros"] > 0


@pytest.mark.parametrize("hybrid", [False, True])
@pytest.mark.parametrize("status", [401, 429])
def test_public_external_main_fallback_reuses_context_and_recovers_all_actual_attempts(
    tmp_path, monkeypatch, hybrid, status
):
    kwargs, search, _ = _inputs(tmp_path, monkeypatch, hybrid=hybrid)
    model = _ordered_policy(max_attempts=2)
    model["models"][0]["model"] = "gpt-4.1"
    model["models"][1]["model"] = "gpt-4.1-mini"
    model["budget"]["max_cost"] = 25
    primary = dict(kwargs["provider_configs"]["openai"])
    backup = {**primary, "model": "gpt-4.1-mini"}
    response = kwargs["responses"][0]
    response.json.return_value["model"] = "gpt-4.1-mini"
    response.content = json.dumps(response.json.return_value).encode()
    response.text = response.content.decode()
    rejection = _http_failure(status, error_code="insufficient_quota" if status == 429 else None)
    rejection.content = json.dumps(rejection.json.return_value).encode()
    rejection.text = rejection.content.decode()
    kwargs.update(
        model_policy=model,
        provider_name="primary",
        provider_configs={"primary": primary, "backup": backup},
        responses=[rejection, response],
    )
    code, output, models = _invoke(monkeypatch, tmp_path, **kwargs)
    assert code == 0
    receipt = json.loads(output.read_text("utf-8"))["execution_receipts"]["IQS_05"]
    assert receipt["requested_model"] == receipt["actual_model"] == "gpt-4.1-mini"
    assert [attempt["http_status_code"] for attempt in receipt["attempts"]] == [status, 200]
    assert [attempt["requested_model"] for attempt in receipt["attempts"]] == [
        "gpt-4.1",
        "gpt-4.1-mini",
    ]
    assert search.request.call_count == 1 and models.post.call_count == 2
    payloads = [call.kwargs["json"] for call in models.post.call_args_list]
    assert payloads[0]["input"] == payloads[1]["input"]
    proof = receipt["search_binding"]["external_context_use"]
    store = QuickScanWorkStore(tmp_path / "quick_scan_work.sqlite")
    attempts = store.list_attempts(proof["work_item_id"])
    assert [attempt["route_id"] for attempt in attempts] == ["primary-route", "backup-route"]
    contexts = [
        store.get_external_context_use(attempt["attempt_id"])["intent"] for attempt in attempts
    ]
    assert contexts[0]["context_sha256"] == contexts[1]["context_sha256"]
    before = store.get_quick_scan_budget_status("quick-scan-test-policy")
    assert before["requests"] == 3 and before["spent_micros"] == 23000
    assert before["reserved_micros"] == 0
    code, output, models = _invoke(monkeypatch, tmp_path, **kwargs)
    assert code == 0 and models.post.call_count == 0 and search.request.call_count == 1
    assert json.loads(output.read_text("utf-8"))["execution_receipts"]["IQS_05"] == receipt
    assert store.get_quick_scan_budget_status("quick-scan-test-policy") == before


@pytest.mark.parametrize("mutation", ["missing_use", "corrupt_use"])
def test_public_external_main_corrupt_saved_evidence_never_reasks_or_overwrites_answer(
    tmp_path, monkeypatch, mutation
):
    kwargs, search, _ = _inputs(tmp_path, monkeypatch)
    code, output, models = _invoke(monkeypatch, tmp_path, **kwargs)
    assert code == 0 and models.post.call_count == 1
    original_output = output.read_bytes()
    receipt = json.loads(original_output)["execution_receipts"]["IQS_05"]
    proof = receipt["search_binding"]["external_context_use"]
    store = QuickScanWorkStore(tmp_path / "quick_scan_work.sqlite")
    before = store.get_quick_scan_budget_status("quick-scan-test-policy")
    # Deliberately corrupt only this disposable SQLite fixture behind its
    # immutable triggers, as a damaged-restore test; never production data.
    with closing(store._connect()) as connection, connection:
        if mutation == "missing_use":
            connection.execute("DROP TRIGGER quick_scan_external_use_no_delete")
            connection.execute(
                "DELETE FROM quick_scan_external_use_intent WHERE attempt_id=?",
                (proof["work_attempt_id"],),
            )
        else:
            connection.execute("DROP TRIGGER quick_scan_external_use_no_update")
            connection.execute(
                "UPDATE quick_scan_external_use_intent SET metadata_sha256=? WHERE attempt_id=?",
                ("e" * 64, proof["work_attempt_id"]),
            )
    code, output, models = _invoke(monkeypatch, tmp_path, **kwargs)
    assert code != 0
    assert models.post.call_count == 0 and search.request.call_count == 1
    assert output.read_bytes() == original_output
    assert store.get_quick_scan_budget_status("quick-scan-test-policy") == before


def test_public_external_main_deepseek_text_endpoint_keeps_thinking_out_of_output_and_restores(
    tmp_path, monkeypatch
):
    kwargs, search, _ = _inputs(tmp_path, monkeypatch)
    kwargs["model_policy"]["models"][0].update(
        provider_config_ref="deepseek", model="deepseek-flash"
    )
    kwargs.update(
        provider_name="deepseek",
        provider_configs={
            "deepseek": {
                "enabled": True,
                "api_key": "synthetic-only",
                "model": "deepseek-flash",
                "base_url": "https://api.deepseek.com/responses",
                "max_retries": 1,
            }
        },
    )
    response = kwargs["responses"][0]
    response.json.return_value["model"] = "deepseek-flash"
    response.json.return_value["output"].insert(
        0,
        {
            "type": "reasoning",
            "content": [{"type": "reasoning_text", "text": "synthetic-thinking-MUST-NOT-LEAK"}],
        },
    )
    response.content = json.dumps(response.json.return_value).encode()
    response.text = response.content.decode()
    code, output, models = _invoke(monkeypatch, tmp_path, **kwargs)
    assert code == 0 and models.post.call_count == 1 and search.request.call_count == 1
    assert models.post.call_args.args[0] == "https://api.deepseek.com/responses"
    payload = models.post.call_args.kwargs["json"]
    assert payload["reasoning"] == {"effort": "high"}
    assert not ({"tools", "tool_choice", "include"} & set(payload))
    first = json.loads(output.read_text("utf-8"))
    receipt = first["execution_receipts"]["IQS_05"]
    assert first["provider"]["name"] == receipt["provider"] == "deepseek"
    assert receipt["actual_model"] == receipt["requested_model"] == "deepseek-flash"
    assert receipt["search_binding"]["native_receipt"]["search_status"] == "unverified"
    assert receipt["search_status"] == "executed" and receipt["web_search_calls"] == []
    assert "synthetic-thinking-MUST-NOT-LEAK" not in output.read_text("utf-8")
    proof = receipt["search_binding"]["external_context_use"]
    store = QuickScanWorkStore(tmp_path / "quick_scan_work.sqlite")
    checkpoint = store.get_answer_checkpoint(proof["work_item_id"])
    assert checkpoint["payload"]["provenance"]["actual_provider"] == "deepseek"
    assert "synthetic-thinking-MUST-NOT-LEAK".encode() not in store.path.read_bytes()
    before = store.get_quick_scan_budget_status("quick-scan-test-policy")
    code, output, models = _invoke(monkeypatch, tmp_path, **kwargs)
    assert code == 0 and models.post.call_count == 0 and search.request.call_count == 1
    assert json.loads(output.read_text("utf-8"))["execution_receipts"]["IQS_05"] == receipt
    assert store.get_quick_scan_budget_status("quick-scan-test-policy") == before


_EXTERNAL_CHILD = r"""
import hashlib
import time
import requests
from src.utils.quick_scan_work_store import QuickScanWorkStore

def _clock():
    return float((OWNED / "store-clock.txt").read_text("utf-8"))

_original_init = QuickScanWorkStore.__init__
def _store_init(self, path, **kwargs):
    kwargs["clock"] = _clock
    _original_init(self, path, **kwargs)
QuickScanWorkStore.__init__ = _store_init

def _pause(point):
    if os.environ.get("IQS_CHILD_PAUSE") != point:
        return
    (OWNED / "child-barrier.json").write_text(
        json.dumps({"point": point, "pid": os.getpid()}), encoding="utf-8")
    # Parent really terminates this process. A timeout raises if that never
    # happens, so a hung fixture cannot silently become a successful run.
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        time.sleep(0.02)
    raise RuntimeError("synthetic parent did not terminate the child")

_original_search_result = QuickScanWorkStore.record_external_search
def _record_search(self, *args, **kwargs):
    result = _original_search_result(self, *args, **kwargs)
    _pause("search_settled")
    return result
QuickScanWorkStore.record_external_search = _record_search

_original_mcp_result = QuickScanWorkStore.record_mcp_stage
def _record_control(self, *args, **kwargs):
    result = _original_mcp_result(self, *args, **kwargs)
    _pause("settled:" + result["stage"])
    return result
QuickScanWorkStore.record_mcp_stage = _record_control

def _sent(kind, *, payload=None):
    entry = {"kind": kind, "pid": os.getpid(), "synthetic_only": True}
    if payload is not None and kind == "model":
        entry.update(model=payload["model"],
            has_context="Synthetic margin disclosure" in payload["input"],
            native_tools="tools" in payload,
            thinking=payload.get("reasoning"),
            input_sha256=hashlib.sha256(payload["input"].encode()).hexdigest())
    _append(OWNED / "external-child-http.jsonl", json.dumps(entry))
    _pause("sent:" + kind)

def _response(payload, *, status=200, headers=None):
    response = requests.Response()
    response.status_code = status
    response.headers.update({"Content-Type": "application/json", "x-request-id": "synthetic-child-request", **(headers or {})})
    response._content = b"" if payload is None else json.dumps(payload).encode()
    response._content_consumed = True
    response.encoding = "utf-8"
    return response

def _source():
    return {"title": "Synthetic issuer", "url": "https://fixture.example/ir",
        "description": "Synthetic margin disclosure", "page_age": "2026-09-01"}

def _search_request(self, method, url, **kwargs):
    if url == "https://api.search.brave.com/res/v1/web/search":
        assert method == "GET"
        _sent("search")
        return _response({"web": {"results": [_source()]}})
    assert url == "https://api.z.ai/api/mcp/web_search_prime/mcp" and method == "POST"
    message = kwargs["json"]
    rpc_method = message["method"]
    _sent(rpc_method)
    if rpc_method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
            "serverInfo": {"name": "synthetic-child", "version": "1"}}
        return _response({"jsonrpc": "2.0", "id": message["id"], "result": result},
            headers={"Mcp-Session-Id": "SYNTHETIC_PRIVATE_SESSION"})
    assert kwargs["headers"]["Mcp-Session-Id"] == "SYNTHETIC_PRIVATE_SESSION"
    assert kwargs["headers"]["MCP-Protocol-Version"] == "2024-11-05"
    if rpc_method == "notifications/initialized":
        assert "id" not in message
        return _response(None, status=202)
    if rpc_method == "tools/list":
        result = {"tools": [{"name": "web_search_prime", "inputSchema": {
            "type": "object", "properties": {"search_query": {"type": "string"}},
            "required": ["search_query"], "additionalProperties": False}}]}
    else:
        assert rpc_method == "tools/call"
        assert message["params"]["name"] == "web_search_prime"
        assert set(message["params"]["arguments"]) == {"search_query"}
        result = {"content": [{"type": "text", "text": json.dumps([{
            "title": "Synthetic issuer", "url": "https://fixture.example/ir",
            "content": "Synthetic margin disclosure", "publish_date": "2026-09-01"}])}]}
    return _response({"jsonrpc": "2.0", "id": message["id"], "result": result})

def _model_post(self, url, **kwargs):
    assert url in {"https://api.openai.com/v1/responses", "https://api.deepseek.com/responses"}
    payload = kwargs["json"]
    _sent("model", payload=payload)
    body = {"entity_id": "ENT_SYNTHETIC", "company_name": "Fixture Corp", "question_id": "IQS_05",
        "status": "scored", "score": 6, "description": "Synthetic supported answer"}
    standard_path = OWNED / "standard-synthetic.json"
    if standard_path.exists():
        standard = json.loads(standard_path.read_text("utf-8"))
        body.update(status=standard["status"], score=standard["score"],
            information_as_of=standard["information_as_of"], description=json.dumps(standard))
    output = [{"type": "reasoning", "content": [{"type": "reasoning_text", "text": "PRIVATE_CHILD_THINKING"}]}]
    if "tools" in payload:
        output.append({"type": "web_search_call", "id": "synthetic-native-call", "status": "completed",
            "action": {"type": "search", "sources": [{"url": "https://fixture.example/ir"}]}})
    output.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": json.dumps(body)}]})
    return _response({"id": "synthetic-child-model", "model": payload["model"], "status": "completed",
        "usage": {"input_tokens": 1000, "output_tokens": 10, "input_tokens_details": {"cached_tokens": 0}}, "output": output})

if os.environ.get("IQS_CHILD_HTTP") == "1":
    requests.Session.request = _search_request
    requests.Session.post = _model_post
"""


def _subprocess_inputs(tmp_path, monkeypatch, *, mcp=False, hybrid=False, deepseek=False):
    kwargs, _, search_path = _inputs(tmp_path, monkeypatch, hybrid=hybrid)
    document = json.loads(search_path.read_text("utf-8"))
    # Keep evidence valid across an expired worker lease; passage of time is a
    # public injected clock dependency, not a rewrite of SQLite owner rows.
    document["retrieval"]["search_ttl_seconds"] = 3600
    if mcp:
        document["external_routes"][0].update(
            kind="zai_mcp_streamable", endpoint="https://api.z.ai/api/mcp/web_search_prime/mcp"
        )
    search_path.write_text(json.dumps(document), encoding="utf-8")
    provider, model = ("deepseek", "deepseek-flash") if deepseek else ("openai", "gpt-4.1")
    if deepseek:
        kwargs["model_policy"]["models"][0].update(provider_config_ref=provider, model=model)
        kwargs["provider_configs"] = {
            provider: {
                "enabled": True,
                "api_key": "synthetic-only",
                "model": model,
                "base_url": "https://api.deepseek.com/responses",
                "max_retries": 1,
            }
        }
    card = {
        "schema_version": "1.0.0",
        "rate_cards": [
            {
                "pricing_ref": "fixture-cli-rate-card-v1",
                "provider": provider,
                "model": model,
                "currency": "USD",
                "source_ref": "operator-synthetic-child-pricing/1",
                "source_checked_at": "2026-10-08",
                "rates": {
                    "input_per_million_tokens": "10",
                    "cached_input_per_million_tokens": "0",
                    "cache_creation_input_per_million_tokens": "0",
                    "output_per_million_tokens": "0",
                    "search_tool_call": "0",
                },
            }
        ],
    }
    questions, _ = _write_inputs(
        tmp_path,
        provider_configs=kwargs["provider_configs"],
        model_policy=kwargs["model_policy"],
        rate_cards=card,
    )
    spend = tmp_path / "spend-synthetic.json"
    spend.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "currency": "USD",
                "hard_cap": 25,
                "pricing_snapshot_ref": "operator-synthetic-child-pricing/1",
                "authorized_at": "2026-10-06T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "logs").mkdir()
    (tmp_path / "temp").mkdir()
    (tmp_path / "guard").mkdir()
    (tmp_path / "guard/sitecustomize.py").write_text(_GUARD + _EXTERNAL_CHILD, encoding="utf-8")
    (tmp_path / "store-clock.txt").write_text(str(time.time()), encoding="utf-8")
    return [
        "--company",
        "Fixture Corp",
        "--entity-id",
        "ENT_SYNTHETIC",
        "--provider",
        provider,
        "--config",
        str(questions),
        "--output",
        str(tmp_path / "result.json"),
        "--require-search",
        "--spend-authorization",
        str(spend),
        *kwargs["extra_argv"],
    ]


def _external_child_env(tmp_path, *, stub, pause=None):
    env = _child_env(tmp_path, stub=False)
    env.update(IQS_CHILD_HTTP="1" if stub else "0", IQS_CHILD_PAUSE=pause or "")
    if stub:
        env["IQS_SYNTHETIC_SEARCH_TOKEN"] = "synthetic-only"
    else:
        env.pop("IQS_SYNTHETIC_SEARCH_TOKEN", None)
    return env


def _external_spawn(tmp_path, argv, *, stub, label):
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            str(Path(__file__).resolve().parents[2] / "main_with_llm.py"),
            *argv,
        ],
        cwd=tmp_path,
        env=_external_child_env(tmp_path, stub=stub),
        capture_output=True,
        timeout=45,
    )
    (tmp_path / (label + "-stdout.log")).write_bytes(result.stdout)
    (tmp_path / (label + "-stderr.log")).write_bytes(result.stderr)
    return result


def _external_kill(tmp_path, argv, point):
    process = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            str(Path(__file__).resolve().parents[2] / "main_with_llm.py"),
            *argv,
        ],
        cwd=tmp_path,
        env=_external_child_env(tmp_path, stub=True, pause=point),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    reached = False
    terminated = False
    try:
        deadline = time.monotonic() + 20
        while process.poll() is None and time.monotonic() < deadline:
            if (tmp_path / "child-barrier.json").exists():
                barrier = json.loads((tmp_path / "child-barrier.json").read_text("utf-8"))
                assert barrier == {"point": point, "pid": process.pid}
                reached = True
                break
            time.sleep(0.02)
    finally:
        if process.poll() is None:
            process.kill()
            terminated = True
        stdout, stderr = process.communicate(timeout=5)
        (tmp_path / "killed-stdout.log").write_bytes(stdout)
        (tmp_path / "killed-stderr.log").write_bytes(stderr)
    assert reached, stdout[-3000:] + stderr[-3000:]
    assert terminated and process.returncode != 0
    (tmp_path / "termination.json").write_text(
        json.dumps(
            {
                "pid": process.pid,
                "point": point,
                "returncode": process.returncode,
                "parent_terminated": terminated,
            }
        ),
        encoding="utf-8",
    )


def _external_sends(tmp_path):
    path = tmp_path / "external-child-http.jsonl"
    return (
        []
        if not path.exists()
        else [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    )


def _external_store(tmp_path):
    return QuickScanWorkStore(
        tmp_path / "quick_scan_work.sqlite",
        clock=lambda: float((tmp_path / "store-clock.txt").read_text("utf-8")),
    )


def _expire_child_lease(tmp_path):
    store = _external_store(tmp_path)
    with closing(store._connect()) as connection:
        rows = list(connection.execute("SELECT work_item_id,lease_expires_at FROM work_item"))
    assert len(rows) == 1 and rows[0]["lease_expires_at"] is not None
    (tmp_path / "store-clock.txt").write_text(
        str(rows[0]["lease_expires_at"] + 1), encoding="utf-8"
    )
    return store, rows[0]["work_item_id"]


@pytest.mark.parametrize(
    "mcp,hybrid,deepseek",
    [
        (False, False, False),
        (False, True, False),
        (True, False, False),
        (True, True, False),
        (True, False, True),
    ],
)
def test_os_external_cli_cold_then_independent_warm_without_http_or_search_key(
    tmp_path, monkeypatch, mcp, hybrid, deepseek
):
    argv = _subprocess_inputs(tmp_path, monkeypatch, mcp=mcp, hybrid=hybrid, deepseek=deepseek)
    cold = _external_spawn(tmp_path, argv, stub=True, label="cold")
    assert cold.returncode == 0, cold.stdout[-3000:] + cold.stderr[-3000:]
    first = json.loads((tmp_path / "result.json").read_text("utf-8"))
    receipt = first["execution_receipts"]["IQS_05"]
    sends = _external_sends(tmp_path)
    expected = (
        ["initialize", "notifications/initialized", "tools/list", "tools/call", "model"]
        if mcp
        else ["search", "model"]
    )
    assert [row["kind"] for row in sends] == expected
    assert len({row["pid"] for row in sends}) == 1 and sends[0]["pid"] != os.getpid()
    assert sends[-1]["has_context"] and sends[-1]["native_tools"] == hybrid
    if deepseek:
        assert sends[-1]["thinking"] == {"effort": "high"}
    assert receipt["search_status"] == "executed" and first["answers"]["IQS_05"]["score"] == 6
    assert "SYNTHETIC_PRIVATE_SESSION" not in json.dumps(first)
    assert "PRIVATE_CHILD_THINKING" not in json.dumps(first)
    store = _external_store(tmp_path)
    before = store.get_quick_scan_budget_status("quick-scan-test-policy")
    assert before["requests"] == len(expected) and before["spent_micros"] == (
        22000 if mcp else 13000
    )
    assert before["reserved_micros"] == 0
    warm = _external_spawn(tmp_path, argv, stub=False, label="warm")
    assert warm.returncode == 0, warm.stdout[-3000:] + warm.stderr[-3000:]
    assert (
        json.loads((tmp_path / "result.json").read_text("utf-8"))["execution_receipts"]["IQS_05"]
        == receipt
    )
    assert (
        _external_sends(tmp_path) == sends
        and store.get_quick_scan_budget_status("quick-scan-test-policy") == before
    )
    assert not (tmp_path / "network-attempts.jsonl").exists()
    assert b"PRIVATE_CHILD_THINKING" not in store.path.read_bytes()


@pytest.mark.parametrize(
    "mcp,point",
    [
        (False, "search_settled"),
        (True, "search_settled"),
        (True, "settled:initialize"),
        (True, "settled:initialized"),
        (True, "settled:discovery"),
    ],
)
def test_os_external_cli_terminated_after_paid_result_resumes_without_repaying(
    tmp_path, monkeypatch, mcp, point
):
    argv = _subprocess_inputs(tmp_path, monkeypatch, mcp=mcp)
    _external_kill(tmp_path, argv, point)
    sends = _external_sends(tmp_path)
    assert not (tmp_path / "result.json").exists() and all(row["kind"] != "model" for row in sends)
    store, work_id = _expire_child_lease(tmp_path)
    assert store.get_item(work_id)["status"] == "leased"
    resumed = _external_spawn(tmp_path, argv, stub=True, label="resumed")
    assert resumed.returncode == 0, resumed.stdout[-3000:] + resumed.stderr[-3000:]
    after = _external_sends(tmp_path)
    expected = (
        ["initialize", "notifications/initialized", "tools/list", "tools/call", "model"]
        if mcp
        else ["search", "model"]
    )
    assert [row["kind"] for row in after] == expected
    assert after[: len(sends)] == sends and len({row["pid"] for row in after}) == 2
    assert store.get_quick_scan_budget_status("quick-scan-test-policy")["spent_micros"] == (
        22000 if mcp else 13000
    )
    assert (
        json.loads((tmp_path / "result.json").read_text("utf-8"))["answers"]["IQS_05"]["score"] == 6
    )
    assert not (tmp_path / "network-attempts.jsonl").exists()


@pytest.mark.parametrize(
    "mcp,point",
    [
        (False, "sent:search"),
        (True, "sent:initialize"),
        (True, "sent:notifications/initialized"),
        (True, "sent:tools/list"),
        (True, "sent:tools/call"),
        (True, "sent:model"),
    ],
)
def test_os_external_cli_terminated_with_unknown_send_never_automatically_repeats(
    tmp_path, monkeypatch, mcp, point
):
    argv = _subprocess_inputs(tmp_path, monkeypatch, mcp=mcp)
    _external_kill(tmp_path, argv, point)
    sends = _external_sends(tmp_path)
    store, work_id = _expire_child_lease(tmp_path)
    assert store.get_item(work_id)["status"] == "leased"
    before = store.get_quick_scan_budget_status("quick-scan-test-policy")
    assert before["reserved_micros"] > 0
    # Keep a valid synthetic key and HTTP stub: absence of either must not be
    # what prevents a second paid send. The actual durable unknown must stop it.
    retry = _external_spawn(tmp_path, argv, stub=True, label="unknown-reopen")
    assert retry.returncode != 0
    assert _external_sends(tmp_path) == sends
    if point == "sent:model":
        assert store.get_item(work_id)["status"] == "uncertain"
    failure = json.loads((tmp_path / "result.json").read_text("utf-8"))
    assert failure["answers"]["IQS_05"]["score"] is None
    assert store.get_answer_checkpoint(work_id) is None
    after = store.get_quick_scan_budget_status("quick-scan-test-policy")
    assert (after["requests"], after["spent_micros"], after["reserved_micros"]) == (
        before["requests"],
        before["spent_micros"],
        before["reserved_micros"],
    )
    assert not (tmp_path / "network-attempts.jsonl").exists()


@pytest.mark.parametrize("mcp", [False, True])
def test_os_external_cli_missing_search_key_never_reserves_or_sends_a_new_request(
    tmp_path, monkeypatch, mcp
):
    argv = _subprocess_inputs(tmp_path, monkeypatch, mcp=mcp)
    result = _external_spawn(tmp_path, argv, stub=False, label="missing-key")
    assert result.returncode != 0 and _external_sends(tmp_path) == []
    assert b"credentials_unavailable_cache_only" in result.stdout
    store = _external_store(tmp_path)
    status = store.get_quick_scan_budget_status("quick-scan-test-policy")
    assert status["requests"] == status["spent_micros"] == status["reserved_micros"] == 0
    with closing(store._connect()) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM quick_scan_external_operation").fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT COUNT(*) FROM quick_scan_mcp_stage").fetchone()[0] == 0
    assert not (tmp_path / "network-attempts.jsonl").exists()


@pytest.mark.parametrize("mutation", ["disabled", "rights", "manifest"])
def test_os_external_cli_cached_answer_cannot_bypass_changed_frozen_authority(
    tmp_path, monkeypatch, mutation
):
    argv = _subprocess_inputs(tmp_path, monkeypatch, mcp=True)
    cold = _external_spawn(tmp_path, argv, stub=True, label="cold")
    assert cold.returncode == 0, cold.stdout[-3000:]
    original = (tmp_path / "result.json").read_bytes()
    sends = _external_sends(tmp_path)
    store = _external_store(tmp_path)
    before = store.get_quick_scan_budget_status("quick-scan-test-policy")
    path = tmp_path / "search-synthetic.json"
    document = json.loads(path.read_text("utf-8"))
    if mutation == "disabled":
        document["external_routes"][0]["enabled"] = False
    elif mutation == "rights":
        document["external_routes"][0]["storage_rights"]["confirmed"] = False
    else:
        document["execution_plan"]["question_manifest_sha256"] = "e" * 64
    path.write_text(json.dumps(document), encoding="utf-8")
    rejected = _external_spawn(tmp_path, argv, stub=False, label="rejected")
    assert rejected.returncode != 0 and (tmp_path / "result.json").read_bytes() == original
    assert (
        _external_sends(tmp_path) == sends
        and store.get_quick_scan_budget_status("quick-scan-test-policy") == before
    )
    assert not (tmp_path / "network-attempts.jsonl").exists()


def _complete_subprocess_inputs(tmp_path, monkeypatch, *, mcp, deepseek):
    from src.utils.quick_scan_observation_context import manifest_question_bindings
    from src.utils.quick_scan_result_outbox import canonical_sha256
    from tests.integration import test_qa_c06_02_e2e as complete

    argv = _subprocess_inputs(tmp_path, monkeypatch, mcp=mcp, deepseek=deepseek)
    # Derive a single-question STANDARD fixture from the existing complete
    # synthetic fixture, retaining its real rubric and resource contracts.
    manifest = json.loads(complete.MANIFEST_BYTES)
    question = next(q for q in manifest["questions"] if q["id"] == "IQS_05")
    question["prompt"] = question["prompt"].replace(complete.ENTITY_ID, "ENT_SYNTHETIC")
    question["prompt_sha256"] = hashlib.sha256(question["prompt"].encode()).hexdigest()
    manifest["profile"]["entity_id"] = "ENT_SYNTHETIC"
    manifest.update(questions=[question], question_count=1)
    manifest_path = tmp_path / "manifest-synthetic.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "questions.json").write_text(
        json.dumps(
            {"questions": [{"question_id": "IQS_05", "text": question["prompt"]}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    identity_sha = hashlib.sha256((tmp_path / "identity-synthetic.json").read_bytes()).hexdigest()
    authority = json.loads(complete._authority_bytes(identity_sha))
    context = authority["observation_context"]
    context.update(
        manifest_file_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        manifest_content_sha256=canonical_sha256(manifest),
    )
    selected = context["questions"]["IQS_05"]
    selected.update(
        frozen_prompt_sha256=question["prompt_sha256"],
        work_prompt_sha256=hashlib.sha256(question["prompt"].strip().encode()).hexdigest(),
    )
    selected["metadata"].update(
        manifest_question_bindings(manifest, question), entity_id="ENT_SYNTHETIC"
    )
    context["questions"] = {"IQS_05": selected}
    authority["observation_context_sha256"] = canonical_sha256(context)
    authority_path = tmp_path / "authority-synthetic.json"
    authority_path.write_text(json.dumps(authority, ensure_ascii=False), encoding="utf-8")
    search_path = tmp_path / "search-synthetic.json"
    search = json.loads(search_path.read_text("utf-8"))
    search["execution_plan"].update(
        question_manifest_sha256=context["manifest_file_sha256"],
        information_as_of=manifest["profile"]["as_of"],
    )
    search_path.write_text(json.dumps(search), encoding="utf-8")
    body = complete._standard_answer("IQS_05", large=True)
    for evidence in body["evidence"]:
        evidence["url"] = "https://fixture.example/ir"
    (tmp_path / "standard-synthetic.json").write_text(json.dumps(body), encoding="utf-8")
    return [*argv, "--c06-authority", str(authority_path)], authority_path, body


@pytest.mark.parametrize("mcp,deepseek", [(False, False), (True, False), (True, True)])
def test_os_external_cli_full_standard_answer_seals_and_recovers_without_http(
    tmp_path, monkeypatch, mcp, deepseek
):
    argv, authority_path, body = _complete_subprocess_inputs(
        tmp_path, monkeypatch, mcp=mcp, deepseek=deepseek
    )
    cold = _external_spawn(tmp_path, argv, stub=True, label="cold")
    assert cold.returncode == 0, cold.stdout[-3500:] + cold.stderr[-2000:]
    public = json.loads((tmp_path / "result.json").read_text("utf-8"))
    receipt = public["execution_receipts"]["IQS_05"]
    work_id = receipt["search_binding"]["external_context_use"]["work_item_id"]
    store = _external_store(tmp_path)
    standard = store.get_standard_answer(work_id)
    assert standard is not None
    delivery = store.get_result_delivery(work_id)
    assert delivery["state"] == "ready"
    from src.utils.quick_scan_result_outbox import validate_exchange_package

    validate_exchange_package(delivery["package"])
    observation = delivery["package"]["items"][0]["observation"]
    assert observation["answer"] == standard["answer"] == body
    assert observation["answer"]["score"] == 8
    assert len(observation["answer"]["evidence"]) == len(body["evidence"]) == 18
    assert {e["url"] for e in observation["answer"]["evidence"]} == {"https://fixture.example/ir"}
    assert "PRIVATE_CHILD_THINKING" not in json.dumps(observation)
    assert "SYNTHETIC_PRIVATE_SESSION" not in json.dumps(delivery["package"])
    original_delivery = delivery  # Getter returns a fresh immutable-byte snapshot.
    sends = _external_sends(tmp_path)
    budget = store.get_quick_scan_budget_status("quick-scan-test-policy")
    warm = _external_spawn(tmp_path, argv, stub=False, label="warm")
    assert warm.returncode == 0, warm.stdout[-3500:]
    seal = _external_spawn(
        tmp_path,
        ["--seal-deliveries", "--c06-authority", str(authority_path)],
        stub=False,
        label="seal",
    )
    assert seal.returncode == 0, seal.stdout[-3500:]
    report = json.loads(
        [line for line in seal.stdout.decode("utf-8").splitlines() if line.startswith("{")][-1]
    )
    assert report["model_calls"] == 0 and len(report["already_sealed"]) == 1
    assert report["sealed"] == report["blocked"] == []
    assert store.get_result_delivery(work_id) == original_delivery
    assert store.get_quick_scan_budget_status("quick-scan-test-policy") == budget
    assert _external_sends(tmp_path) == sends and not (tmp_path / "network-attempts.jsonl").exists()
