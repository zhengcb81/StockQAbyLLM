"""S1-08 regression tests: StockQA provider error logging and subprocess stderr capture."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from src.providers.llm_provider import LLMProvider
from src.utils import quick_scan_owner_budget_permissions as perms_mod
from src.utils import quick_scan_owner_research as research_mod
from src.utils import quick_scan_work_decision_authority as auth_mod
from src.utils.quick_scan_owner_refresh import (
    OwnerRefreshClient,
    OwnerRefreshRejected,
    OwnerRefreshSession,
)
from src.utils.quick_scan_owner_refresh_api import adapt_owner_refresh
from src.utils.quick_scan_refresh_consent import OwnerConsentEndpoint


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


# These literals are synthetic and exercise the original owner process boundary.
_SYNTHETIC_FAILURE = (
    b"old-progress-only\n"
    + b"x" * 9500
    + b"\nTraceback (most recent call last):\n"
    + b"PermissionError: synthetic native owner denied\n"
    + b"Authorization: Bearer synthetic-bearer+/=\n"
    + b'{"Authorization": "Basic synthetic-basic+/="}\n'
    + b"Authorization: Bearer\r\n\tsynthetic-folded+/=\n"
    + b"Authorization: Bearer\nsynthetic-unindented-bearer+/=\n"
    + b"PermissionError: kept after unindented bearer\n"
    + b"Authorization: Basic \r\nsynthetic-unindented-basic+/=\r\n"
    + b"OSError: kept after unindented basic\n"
    + b"Authorization: Bearer\r\n  ValueError: kept after missing credential\n"
    + b"owner link: https://example.invalid/?api_key=synthetic-url-key&healthy=unchanged\n"
    + b"Authorization was checked.\n"
)
_SYNTHETIC_CREDENTIALS = (
    "synthetic-bearer+/=",
    "synthetic-basic+/=",
    "synthetic-folded+/=",
    "synthetic-unindented-bearer+/=",
    "synthetic-unindented-basic+/=",
    "synthetic-url-key",
)


def _native_cli(code_root, module, failure):
    path = code_root / "stockwiki" / (module + ".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"import sys\nsys.stderr.buffer.write({failure!r})\nraise SystemExit(23)\n",
        encoding="utf-8",
    )


def _owner_refresh(tmp_path, *, process_env=None):
    code_root = tmp_path / "wiki"
    _native_cli(code_root, "quick_scan_routes_cli", b"")
    schema = tmp_path / "iqs" / "schemas" / "quick_scan" / "query-capabilities.schema.json"
    schema.parent.mkdir(parents=True)
    schema.write_text("{}", encoding="utf-8")
    config = tmp_path / "owner.json"
    config.write_text(
        json.dumps(
            {
                "protocol": "stockqa.owner_refresh_config/1.0.0",
                "stockwiki_code_root": str(code_root),
                "stockwiki_workspace_root": str(tmp_path / "owner"),
                "iqs_code_root": str(tmp_path / "iqs"),
                "iqs_release_root": str(tmp_path / "release"),
                "subject_key": "fixture",
                "scope": "entity",
                "scope_id": "fixture",
                "ttl_hours": 1,
                "runs_dir": str(tmp_path / "runs"),
            }
        ),
        encoding="utf-8",
    )
    native = object.__new__(OwnerRefreshSession)
    native.client = OwnerRefreshClient(config, process_env=process_env)
    return adapt_owner_refresh(native), code_root


@pytest.mark.parametrize("override_path", ["explicit-fixture", None])
def test_subject_dispatch_uses_selected_environment(tmp_path, monkeypatch, override_path):
    monkeypatch.setenv("PATH", "ambient-fixture")
    monkeypatch.setenv("WINDIR", "ambient-only")
    monkeypatch.setenv("ZAI_API_KEY", "synthetic-never-an-actual-key")
    process_env = (
        None
        if override_path is None
        else {"PATH": override_path, "ZAI_API_KEY": "synthetic-explicit"}
    )
    owner, _ = _owner_refresh(tmp_path, process_env=process_env)
    setup = owner.subject_dispatch_setup()

    assert setup["environment"]["PATH"] == (override_path or "ambient-fixture")
    assert ("WINDIR" in setup["environment"]) is (override_path is None)
    assert "ZAI_API_KEY" not in setup["environment"]
    if process_env is not None:
        assert process_env == {"PATH": override_path, "ZAI_API_KEY": "synthetic-explicit"}


def test_owner_capability_failure_keeps_safe_stderr_tail(tmp_path, caplog, capsys):
    owner, code_root = _owner_refresh(tmp_path)
    _native_cli(code_root, "quick_scan_query_v2_cli", _SYNTHETIC_FAILURE)
    service = research_mod.OwnerResearchServices.__new__(research_mod.OwnerResearchServices)
    service._assert_registered_inputs = lambda **_: None
    service.owner = owner

    with pytest.raises(
        research_mod.OwnerResearchRejected, match="subject_dispatch_capability_cli_failed"
    ):
        service.preflight_subject_dispatch()

    assert "PermissionError: synthetic native owner denied" in caplog.text
    assert "old-progress-only" not in caplog.text
    assert all(value not in caplog.text for value in _SYNTHETIC_CREDENTIALS)
    assert "healthy=unchanged" in caplog.text
    assert "Authorization was checked." in caplog.text
    assert "PermissionError: kept after unindented bearer" in caplog.text
    assert "OSError: kept after unindented basic" in caplog.text
    assert "ValueError: kept after missing credential" in caplog.text
    assert capsys.readouterr().out == ""
    assert not list((tmp_path / "runs").iterdir())


def test_owner_policy_failure_keeps_safe_stderr_tail(tmp_path, caplog, capsys):
    files = {"work_store", "budget_policy_file"}
    roots = {name: tmp_path / name for name in perms_mod.PATH_KEYS - files}
    for path in roots.values():
        path.mkdir()
    _native_cli(roots["stockwiki_code_root"], "quick_scan_refresh_owner_cli", _SYNTHETIC_FAILURE)
    owned = {name: tmp_path / (name + ".json") for name in files}
    config = tmp_path / "admission.json"
    config.write_text(
        json.dumps(
            {
                **{name: str(path) for name, path in (roots | owned).items()},
                "protocol": "stockqa.refresh_admission_config/1.0.0",
                "consumer_id": "fixture",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="owner_exposure_native_permissions_rejected"):
        perms_mod.current_policy(
            config,
            {"consumer_id": "fixture"},
            ledger_path=owned["work_store"],
            budget_policy_file=owned["budget_policy_file"],
        )
    assert "PermissionError: synthetic native owner denied" in caplog.text
    assert all(value not in caplog.text for value in _SYNTHETIC_CREDENTIALS)
    assert "healthy=unchanged" in caplog.text
    assert capsys.readouterr().out == ""
    assert all(not path.exists() for path in owned.values())


def test_owner_decision_failure_keeps_safe_stderr_tail(tmp_path, caplog, capsys):
    code_root = tmp_path / "wiki"
    _native_cli(code_root, "quick_scan_refresh_owner_cli", _SYNTHETIC_FAILURE)
    authority = auth_mod.StockWikiWorkDecisionAuthority(
        OwnerConsentEndpoint(
            {
                "stockwiki_code_root": str(code_root),
                "stockwiki_workspace_root": str(tmp_path / "owner"),
                "iqs_code_root": str(tmp_path / "iqs"),
                "iqs_release_root": str(tmp_path / "release"),
            }
        ),
        work_store=tmp_path / "work.sqlite",
        budget_policy_file=tmp_path / "policy.json",
        consumer_id="fixture",
        confirmation_id="rc1_" + "a" * 64,
    )

    with pytest.raises(OwnerRefreshRejected, match="owner_work_native_rejected"):
        authority.authorize({"kind": "known_response_resolution"})

    assert "PermissionError: synthetic native owner denied" in caplog.text
    assert all(value not in caplog.text for value in _SYNTHETIC_CREDENTIALS)
    assert "healthy=unchanged" in caplog.text
    assert capsys.readouterr().out == ""
    assert not authority.work_store.exists()


def test_cropped_stderr_window_does_not_log_mid_value(tmp_path, caplog):
    owner, code_root = _owner_refresh(tmp_path)
    native_failure = (
        b"Authorization: Bearer "
        + b"synthetic-private-window" * 550
        + b"\nPermissionError: synthetic marker after giant header\n"
    )
    _native_cli(code_root, "quick_scan_query_v2_cli", native_failure)
    service = research_mod.OwnerResearchServices.__new__(research_mod.OwnerResearchServices)
    service._assert_registered_inputs = lambda **_: None
    service.owner = owner

    with pytest.raises(
        research_mod.OwnerResearchRejected, match="subject_dispatch_capability_cli_failed"
    ):
        service.preflight_subject_dispatch()

    assert "synthetic-private-window" not in caplog.text
    assert "PermissionError: synthetic marker after giant header" in caplog.text


def test_cropped_stderr_window_never_logs_unindented_header_value(tmp_path, caplog):
    owner, code_root = _owner_refresh(tmp_path)
    wrapped = b"\nsynthetic-unindented-window+/=\napi_key="
    final = b"\nPermissionError: kept after partial header\n"
    # The 8 KiB window opens at the last two letters of the header's scheme.
    padding = b"x" * (8192 - 2 - len(wrapped) - len(final))
    _native_cli(
        code_root, "quick_scan_query_v2_cli", b"Authorization: Bearer" + wrapped + padding + final
    )
    service = research_mod.OwnerResearchServices.__new__(research_mod.OwnerResearchServices)
    service._assert_registered_inputs = lambda **_: None
    service.owner = owner

    with pytest.raises(
        research_mod.OwnerResearchRejected, match="subject_dispatch_capability_cli_failed"
    ):
        service.preflight_subject_dispatch()

    assert "synthetic-unindented-window" not in caplog.text
    assert "PermissionError: kept after partial header" in caplog.text
