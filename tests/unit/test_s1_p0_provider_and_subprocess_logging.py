"""S1-08 regression tests: StockQA provider error logging and subprocess stderr capture."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from src.providers.llm_provider import LLMProvider
from src.utils import quick_scan_owner_budget_permissions as perms_mod
from src.utils import quick_scan_owner_research as research_mod
from src.utils import quick_scan_work_decision_authority as auth_mod


class _BuggySyncProvider(LLMProvider):
    provider_name = "s1-test-provider"

    def __init__(self):
        self.calls = 0
        self.api_key = "test-key"
        self.require_search = False
        self.max_retries = 3
        self.format_repair_budget = 0
        self.retry_strategy = type("_R", (), {"get_wait_time": staticmethod(lambda _: 0.0)})()

        class _BuggyClient:
            supports_web_search = False

            def send_request(inner_self, prompt, **kwargs):
                self.calls += 1
                raise KeyError("internal-coding-bug-key")

        self.client = _BuggyClient()


def test_sync_provider_does_not_retry_programming_error_and_logs_traceback(caplog):
    prov = _BuggySyncProvider()
    with caplog.at_level(logging.ERROR, logger="src.providers.llm_provider"):
        with pytest.raises(KeyError, match="internal-coding-bug-key"):
            prov._call_llm_api("hello", max_retries=3)

    assert prov.calls == 1, "Programming error (KeyError) must fail fast without retrying 3 times"
    assert any(rec.exc_info is not None for rec in caplog.records)


def test_owner_research_source_has_no_devnull_stderr():
    src = Path(research_mod.__file__).read_text(encoding="utf-8")
    assert "stderr=subprocess.DEVNULL" not in src


def test_owner_budget_permissions_logs_redacted_stderr_helper(caplog):
    with caplog.at_level(logging.ERROR, logger="src.utils.quick_scan_owner_budget_permissions"):
        perms_mod._log_subprocess_stderr(
            "owner_exposure_native_permissions_rejected",
            1,
            b"RuntimeError: permission_check_boom api_key=SECRET777\n",
        )

    assert any(
        "permission_check_boom" in rec.getMessage() and "SECRET777" not in rec.getMessage()
        for rec in caplog.records
    )


def test_work_decision_authority_logs_stderr_on_rejection():
    src = Path(auth_mod.__file__).read_text(encoding="utf-8")
    assert "owner_work_native_rejected exit_code=%d stderr=%s" in src
