"""Opt-in live web-search E2E; all files and logs are created in a disposable temp root."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest


def _read_live_cli_result(output_path, completed):
    """Load structured CLI output or fail with secret-safe process metadata."""
    if not output_path.is_file():
        summary = {
            "cli_returncode": getattr(completed, "returncode", None),
            "result_file_present": False,
            "stdout_chars": len(getattr(completed, "stdout", "") or ""),
            "stderr_chars": len(getattr(completed, "stderr", "") or ""),
        }
        raise AssertionError(
            "Live CLI produced no result file: " + json.dumps(summary, sort_keys=True)
        )

    raw = output_path.read_bytes()
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        summary = {
            "cli_returncode": getattr(completed, "returncode", None),
            "result_file_present": True,
            "result_json_valid": False,
            "result_bytes": len(raw),
            "result_sha256": hashlib.sha256(raw).hexdigest(),
        }
        raise AssertionError(
            "Live CLI produced invalid JSON: " + json.dumps(summary, sort_keys=True)
        )
    if not isinstance(result, dict):
        summary = {
            "cli_returncode": getattr(completed, "returncode", None),
            "result_file_present": True,
            "result_json_valid": True,
            "result_json_type": type(result).__name__,
        }
        raise AssertionError(
            "Live CLI result is not an object: " + json.dumps(summary, sort_keys=True)
        )
    return result


def test_live_cli_missing_result_reports_only_safe_metadata(tmp_path):
    completed = SimpleNamespace(
        returncode=17, stdout="api_key=sentinel-secret", stderr="Bearer sentinel-token"
    )
    with pytest.raises(AssertionError) as error:
        _read_live_cli_result(tmp_path / "missing.json", completed)
    summary = str(error.value)
    assert "result_file_present" in summary
    assert "stdout_chars" in summary and "stderr_chars" in summary
    assert "sentinel-secret" not in summary and "sentinel-token" not in summary


def test_live_cli_invalid_json_reports_digest_not_payload(tmp_path):
    output_path = tmp_path / "result.json"
    output_path.write_text("api_key=sentinel-payload", encoding="utf-8")
    completed = SimpleNamespace(returncode=0, stdout="", stderr="")
    with pytest.raises(AssertionError) as error:
        _read_live_cli_result(output_path, completed)
    summary = str(error.value)
    assert "result_json_valid" in summary
    assert "result_sha256" in summary and "sentinel-payload" not in summary


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("STOCKQA_RUN_LIVE_E2E") != "1" or not os.environ.get("STOCKQA_OPENAI_API_KEY"),
    reason="Set STOCKQA_RUN_LIVE_E2E=1 and STOCKQA_OPENAI_API_KEY to run the real-provider E2E",
)
def test_live_openai_search_runs_public_company_through_cli_and_cleans_local_artifacts():
    """Exercise a real public-company query and verify provider execution receipts."""
    repo_root = Path(__file__).resolve().parents[2]
    api_key = os.environ["STOCKQA_OPENAI_API_KEY"]
    model = os.environ.get("STOCKQA_OPENAI_MODEL", "gpt-4.1-mini")
    original_cwd = Path.cwd()

    # TemporaryDirectory guarantees cleanup after success, assertion failure, timeout, or API error.
    with tempfile.TemporaryDirectory(prefix="stockqa-live-quick-scan-") as temporary_root:
        sandbox = Path(temporary_root).resolve()
        (sandbox / "logs").mkdir()
        questions_path = sandbox / "questions.json"
        questions_path.write_text(
            json.dumps(
                {
                    "categories": [
                        {
                            "category": "quality",
                            "questions": [
                                {
                                    "question_id": "IQS_05",
                                    "text": (
                                        "Using current public web sources, rate Microsoft's "
                                        "durable competitive advantage from 1 to 10. Return a "
                                        "strict JSON answer with question_id=IQS_05, a status, "
                                        "an integer score from 1 to 10 or null, and a concise "
                                        "description. Do not include markdown."
                                    ),
                                }
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (sandbox / "llm_apis.json").write_text(
            json.dumps(
                {
                    "default_provider": "openai",
                    "providers": {
                        "openai": {
                            "enabled": True,
                            "api_key": "",
                            "model": model,
                            "base_url": "https://api.openai.com/v1/chat/completions",
                            "max_retries": 1,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        child_env = os.environ.copy()
        child_env["PYTHONPATH"] = str(repo_root) + os.pathsep + child_env.get("PYTHONPATH", "")
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        child_env["OPENAI_API_KEY"] = api_key
        output_path = sandbox / "result.json"
        command = [
            sys.executable,
            "-X",
            "utf8",
            str(repo_root / "main_with_llm.py"),
            "--company",
            "Microsoft Corporation",
            "--entity-id",
            "issuer:US5949181045",
            "--provider",
            "openai",
            "--config",
            str(questions_path),
            "--output",
            str(output_path),
            "--require-search",
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=sandbox,
                env=child_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
                check=False,
            )
        finally:
            assert Path.cwd() == original_cwd

        result = _read_live_cli_result(output_path, completed)
        assert completed.returncode == 0, "Live quick-scan CLI returned a failure status"
        answer = result["answers"]["IQS_05"]
        receipt = result["execution_receipts"]["IQS_05"]

        assert result["entity"]["name"] == "Microsoft Corporation"
        assert result["observed_at"].endswith("Z")
        assert answer["question_id"] == "IQS_05"
        assert answer["status"] in {"scored", "unknown", "insufficient_evidence", "error"}
        assert (answer["score"] is None) == (answer["status"] != "scored")
        assert answer["score"] is None or 1 <= answer["score"] <= 10
        assert receipt["search_status"] == "executed"
        assert receipt["request_id"]
        assert receipt["response_id"]
        assert receipt["actual_model"]
        assert receipt["source_urls"]
        assert receipt["answered_at"].endswith("Z")

    assert not sandbox.exists(), "Live E2E left its temporary API/config/output sandbox behind"


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("STOCKQA_RUN_LIVE_E2E") != "1" or not os.environ.get("MINIMAX_API_KEY"),
    reason="Set STOCKQA_RUN_LIVE_E2E=1 and MINIMAX_API_KEY for one MiniMax-M3 search E2E",
)
def test_live_minimax_search_runs_public_company_through_cli_and_cleans_local_artifacts():
    """One public CLI question through MiniMax-M3 with a real search-call receipt."""
    repo_root = Path(__file__).resolve().parents[2]
    original_cwd = Path.cwd()

    with tempfile.TemporaryDirectory(prefix="stockqa-live-minimax-") as temporary_root:
        sandbox = Path(temporary_root).resolve()
        (sandbox / "logs").mkdir()
        questions_path = sandbox / "questions.json"
        questions_path.write_text(
            json.dumps(
                {
                    "categories": [
                        {
                            "category": "quality",
                            "questions": [
                                {
                                    "question_id": "IQS_05",
                                    "text": (
                                        "Search the web for Microsoft's official FY2025 annual "
                                        "report and current evidence. Then rate its durable "
                                        "enterprise software switching-cost advantage from 1 to 10. "
                                        "Return strict JSON with question_id IQS_05, status, score or "
                                        "null, and a concise description. Do not include markdown."
                                    ),
                                }
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (sandbox / "llm_apis.json").write_text(
            json.dumps(
                {
                    "default_provider": "minimax",
                    "providers": {
                        "minimax": {
                            "enabled": True,
                            "api_key": "",
                            "model": "MiniMax-M3",
                            "base_url": "https://api.minimaxi.com/v1/responses",
                            "max_retries": 1,
                            "format_repair_budget": 0,
                            "timeout": 160,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        child_env = os.environ.copy()
        child_env["PYTHONPATH"] = str(repo_root) + os.pathsep + child_env.get("PYTHONPATH", "")
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        output_path = sandbox / "result.json"
        command = [
            sys.executable,
            "-X",
            "utf8",
            str(repo_root / "main_with_llm.py"),
            "--company",
            "Microsoft Corporation",
            "--entity-id",
            "issuer:US5949181045",
            "--provider",
            "minimax",
            "--config",
            str(questions_path),
            "--output",
            str(output_path),
            "--require-search",
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=sandbox,
                env=child_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
                check=False,
            )
        finally:
            assert Path.cwd() == original_cwd

        if completed.returncode != 0:
            failure = {"exit": completed.returncode, "output_exists": output_path.exists()}
            if output_path.exists():
                partial = _read_live_cli_result(output_path, completed)
                failed_answer = partial.get("answers", {}).get("IQS_05", {})
                failed_receipt = partial.get("execution_receipts", {}).get("IQS_05", {})
                failure.update(
                    answer_status=failed_answer.get("status"),
                    http_status_code=failed_receipt.get("http_status_code"),
                    response_status=failed_receipt.get("response_status"),
                    search_status=failed_receipt.get("search_status"),
                    search_calls=[
                        {
                            "status": call.get("status"),
                            "action_type": call.get("action_type"),
                            "source_count": len(call.get("source_urls") or []),
                        }
                        for call in failed_receipt.get("web_search_calls") or []
                        if isinstance(call, dict)
                    ],
                    source_count=len(failed_receipt.get("source_urls") or []),
                    failure_type=failed_receipt.get("failure_type"),
                    dispatch_outcome=(failed_receipt.get("dispatch_outcome") or {}).get("state"),
                )
            pytest.fail(f"Live MiniMax CLI failed: {failure}")
        result = _read_live_cli_result(output_path, completed)
        answer = result["answers"]["IQS_05"]
        receipt = result["execution_receipts"]["IQS_05"]
        assert result["entity"]["name"] == "Microsoft Corporation"
        assert result["observed_at"].endswith("Z")
        assert answer["question_id"] == "IQS_05"
        assert answer["status"] in {"scored", "unknown", "insufficient_evidence", "error"}
        assert (answer["score"] is None) == (answer["status"] != "scored")
        assert receipt["provider"] == "minimax"
        assert receipt["requested_model"] == "MiniMax-M3"
        safe_receipt_summary = {
            "http_status_code": receipt["http_status_code"],
            "response_status": receipt["response_status"],
            "actual_model": receipt["actual_model"],
            "search_status": receipt["search_status"],
            "search_calls": [
                {
                    "status": call.get("status"),
                    "action_type": call.get("action_type"),
                    "source_count": len(call.get("source_urls", [])),
                }
                for call in receipt["web_search_calls"]
            ],
            "source_count": len(receipt["source_urls"]),
            "request_id_present": bool(receipt["request_id"]),
            "response_id_present": bool(receipt["response_id"]),
            "attempt_id_present": bool(receipt["attempt_id"]),
        }
        assert receipt["actual_model"] == "MiniMax-M3", safe_receipt_summary
        assert receipt["search_status"] == "executed", safe_receipt_summary
        assert receipt["response_id"]
        assert receipt["attempt_id"]
        assert receipt["search_receipt_id"]
        assert receipt["source_urls"]
        matching_calls = [
            call
            for call in receipt["web_search_calls"]
            if call["id"] == receipt["search_receipt_id"]
            and call["status"] == "completed"
            and call["action_type"] == "search"
        ]
        assert matching_calls
        assert any(
            set(call.get("source_urls", [])) & set(receipt["source_urls"])
            for call in matching_calls
        ), safe_receipt_summary
        assert receipt["answered_at"].endswith("Z")

    assert not sandbox.exists(), "Live MiniMax E2E left its temporary sandbox behind"


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("STOCKQA_RUN_LIVE_E2E") != "1" or not os.environ.get("MINIMAX_API_KEY"),
    reason="Set STOCKQA_RUN_LIVE_E2E=1 and MINIMAX_API_KEY for one MiniMax-M3 Anthropic E2E",
)
def test_live_minimax_anthropic_search_runs_public_company_through_cli_and_cleans_local_artifacts():
    """Exercise the configured MiniMax Anthropic Messages search route end to end."""
    repo_root = Path(__file__).resolve().parents[2]
    original_cwd = Path.cwd()

    with tempfile.TemporaryDirectory(prefix="stockqa-live-minimax-anthropic-") as temporary_root:
        sandbox = Path(temporary_root).resolve()
        (sandbox / "logs").mkdir()
        questions_path = sandbox / "questions.json"
        questions_path.write_text(
            json.dumps(
                {
                    "categories": [
                        {
                            "category": "quality",
                            "questions": [
                                {
                                    "question_id": "IQS_05",
                                    "text": (
                                        "Run exactly one web_search query for Alphabet Inc.'s "
                                        + "official FY2025 annual report. Using only that search "
                                        + "result, rate the durability of Google Search's distribution "
                                        + "advantage from 1 to 10; do not issue a second search. Return only "
                                        "strict JSON with question_id IQS_05, status, integer score "
                                        "or null, and a concise description. Do not include markdown."
                                    ),
                                }
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        # Server Tools are documented on the global API host, but API keys can be
        # region-bound. Allow an explicit endpoint override; never infer the region.
        endpoint = os.environ.get(
            "STOCKQA_MINIMAX_ANTHROPIC_BASE_URL",
            "https://api.minimaxi.com/anthropic/v1/messages",
        )
        (sandbox / "llm_apis.json").write_text(
            json.dumps(
                {
                    "default_provider": "minimax",
                    "providers": {
                        "minimax": {
                            "enabled": True,
                            "api_key": "",
                            "model": "MiniMax-M3",
                            "base_url": endpoint,
                            "max_retries": 1,
                            "format_repair_budget": 0,
                            "timeout": 180,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        child_env = os.environ.copy()
        child_env["PYTHONPATH"] = str(repo_root) + os.pathsep + child_env.get("PYTHONPATH", "")
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        output_path = sandbox / "result.json"
        command = [
            sys.executable,
            "-X",
            "utf8",
            str(repo_root / "main_with_llm.py"),
            "--company",
            "Alphabet Inc.",
            "--entity-id",
            "issuer:US02079K3059",
            "--provider",
            "minimax",
            "--config",
            str(questions_path),
            "--output",
            str(output_path),
            "--require-search",
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=sandbox,
                env=child_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=210,
                check=False,
            )
        finally:
            assert Path.cwd() == original_cwd

        result = _read_live_cli_result(output_path, completed)
        answer = result["answers"]["IQS_05"]
        receipt = result["execution_receipts"]["IQS_05"]
        answer_error = (
            answer.get("description")
            if answer.get("status") in {"error", "insufficient_evidence"}
            else None
        )
        if isinstance(answer_error, str):
            api_key = os.environ.get("MINIMAX_API_KEY")
            if api_key:
                answer_error = answer_error.replace(api_key, "[REDACTED]")
            answer_error = answer_error[:180]
        safe_summary = {
            "cli_returncode": completed.returncode,
            "answer_status": answer.get("status"),
            "answer_score": answer.get("score"),
            "answer_error": answer_error,
            "http_status_code": receipt.get("http_status_code"),
            "response_status": receipt.get("response_status"),
            "stop_reason": receipt.get("stop_reason"),
            "actual_model": receipt.get("actual_model"),
            "search_status": receipt.get("search_status"),
            "search_call_count": len(receipt.get("web_search_calls", [])),
            "search_calls": [
                {
                    "has_id": bool(call.get("id")),
                    "status": call.get("status"),
                    "source_count": len(call.get("source_urls", [])),
                    "error_code": call.get("error_code"),
                }
                for call in receipt.get("web_search_calls", [])
            ],
            "attempts": [
                {
                    "http_status_code": attempt.get("http_status_code"),
                    "failure_type": attempt.get("failure_type"),
                    "provider_error_code": attempt.get("provider_error_code"),
                }
                for attempt in receipt.get("attempts", [])
                if isinstance(attempt, dict)
            ],
            "search_receipt_id_present": bool(receipt.get("search_receipt_id")),
            "source_count": len(receipt.get("source_urls", [])),
            "request_id_present": bool(receipt.get("request_id")),
            "response_id_present": bool(receipt.get("response_id")),
            "attempt_id_present": bool(receipt.get("attempt_id")),
        }
        safe_summary_text = json.dumps(safe_summary, ensure_ascii=True, sort_keys=True)
        assert completed.returncode == 0, safe_summary_text
        assert result["entity"]["name"] == "Alphabet Inc."
        assert result["observed_at"].endswith("Z")
        assert answer["status"] in {"scored", "insufficient_evidence"}, safe_summary_text
        if answer["status"] == "scored":
            assert type(answer["score"]) is int and 1 <= answer["score"] <= 10, safe_summary_text
        else:
            assert answer["score"] is None, safe_summary_text
        assert receipt["provider"] == "minimax", safe_summary_text
        assert receipt["requested_model"] == "MiniMax-M3"
        assert receipt["actual_model"] == "MiniMax-M3", safe_summary_text
        request_id = receipt.get("request_id")
        assert request_id is None or (
            isinstance(request_id, str) and bool(request_id.strip())
        ), safe_summary_text
        assert receipt["response_id"]
        assert receipt["attempt_id"]
        assert receipt["search_status"] == "executed", safe_summary_text
        assert len(receipt["web_search_calls"]) == 1, safe_summary_text
        assert receipt["search_receipt_id"] == receipt["web_search_calls"][0]["id"]
        assert receipt["source_urls"]
        assert set(receipt["source_urls"]) == set(receipt["web_search_calls"][0]["source_urls"])
        assert receipt["answered_at"].endswith("Z")

    assert not sandbox.exists(), "Live MiniMax Anthropic E2E left its temporary sandbox behind"


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("STOCKQA_RUN_LIVE_E2E") != "1" or not os.environ.get("MIMO_API_KEY"),
    reason="Set STOCKQA_RUN_LIVE_E2E=1 and MIMO_API_KEY for one MiMo web-search E2E",
)
def test_live_mimo_search_runs_public_company_through_cli_and_cleans_local_artifacts():
    """Verify MiMo emits URL-citation search evidence through the public CLI."""
    repo_root = Path(__file__).resolve().parents[2]
    original_cwd = Path.cwd()
    endpoint = os.environ.get("STOCKQA_MIMO_BASE_URL", "https://api.xiaomimimo.com/v1").rstrip("/")
    model = os.environ.get("STOCKQA_MIMO_MODEL", "mimo-v2.6-flash")

    with tempfile.TemporaryDirectory(prefix="stockqa-live-mimo-") as temporary_root:
        sandbox = Path(temporary_root).resolve()
        (sandbox / "logs").mkdir()
        questions_path = sandbox / "questions.json"
        questions_path.write_text(
            json.dumps(
                {
                    "categories": [
                        {
                            "category": "quality",
                            "questions": [
                                {
                                    "question_id": "IQS_05",
                                    "text": (
                                        "Force one web search for Microsoft's latest official quarterly "
                                        "earnings release. Rate whether Microsoft has a durable enterprise "
                                        "software distribution advantage from 1 to 10. Return strict JSON "
                                        "with question_id IQS_05, status, score or null, and a concise "
                                        "description. Do not include markdown."
                                    ),
                                }
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (sandbox / "llm_apis.json").write_text(
            json.dumps(
                {
                    "default_provider": "mimo",
                    "providers": {
                        "mimo": {
                            "enabled": True,
                            "api_key": "",
                            "model": model,
                            "base_url": endpoint,
                            "max_retries": 1,
                            "format_repair_budget": 0,
                            "timeout": 160,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        child_env = os.environ.copy()
        child_env["PYTHONPATH"] = str(repo_root) + os.pathsep + child_env.get("PYTHONPATH", "")
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        output_path = sandbox / "result.json"
        command = [
            sys.executable,
            "-X",
            "utf8",
            str(repo_root / "main_with_llm.py"),
            "--company",
            "Microsoft Corporation",
            "--entity-id",
            "issuer:US5949181045",
            "--provider",
            "mimo",
            "--config",
            str(questions_path),
            "--output",
            str(output_path),
            "--require-search",
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=sandbox,
                env=child_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
                check=False,
            )
        finally:
            assert Path.cwd() == original_cwd

        result = _read_live_cli_result(output_path, completed)
        answer = result["answers"]["IQS_05"]
        receipt = result["execution_receipts"]["IQS_05"]
        answer_error = (
            answer.get("description")
            if answer.get("status") in {"error", "insufficient_evidence"}
            else None
        )
        if isinstance(answer_error, str):
            for env_name in ("MIMO_API_KEY", "MIMO_PLAN_API_KEY"):
                secret = os.environ.get(env_name)
                if secret:
                    answer_error = answer_error.replace(secret, "[REDACTED]")
            answer_error = answer_error[:180]
        safe_summary = {
            "cli_returncode": completed.returncode,
            "answer_status": answer.get("status"),
            "answer_error": answer_error,
            "http_status_code": receipt.get("http_status_code"),
            "actual_model": receipt.get("actual_model"),
            "search_status": receipt.get("search_status"),
            "search_call_count": len(receipt.get("web_search_calls", [])),
            "source_count": len(receipt.get("source_urls", [])),
            "response_id_present": bool(receipt.get("response_id")),
            "search_receipt_id_present": bool(receipt.get("search_receipt_id")),
        }
        safe_text = json.dumps(safe_summary, sort_keys=True)
        assert completed.returncode == 0, safe_text
        assert result["entity"]["name"] == "Microsoft Corporation"
        assert result["observed_at"].endswith("Z")
        assert answer["status"] in {"scored", "insufficient_evidence"}, safe_text
        if answer["status"] == "scored":
            assert type(answer["score"]) is int and 1 <= answer["score"] <= 10, safe_text
        else:
            assert answer["score"] is None, safe_text
        assert receipt["provider"] == "mimo"
        assert receipt["requested_model"] == model
        assert receipt["actual_model"] == model, safe_text
        assert receipt["response_id"], safe_text
        assert receipt["search_status"] == "executed", safe_text
        assert receipt["search_receipt_id"] == receipt["response_id"]
        assert receipt["source_urls"], safe_text
        assert receipt["web_search_calls"][0]["evidence_basis"] == "url_citation_annotations"
        assert receipt["answered_at"].endswith("Z")

    assert not sandbox.exists(), "Live MiMo E2E left its temporary sandbox behind"
