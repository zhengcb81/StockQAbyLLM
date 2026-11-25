"""Failure provenance without exception text, live providers or production state."""

import builtins
import importlib
import io
import json
import logging
import sqlite3
import sys
from contextlib import closing
from unittest.mock import Mock
from uuid import uuid4

import pytest

from src.utils import execution_feedback
from src.utils import logger as diagnostic_log
from src.utils import quick_scan_feedback_observation as feedback
from src.utils.quick_scan_refresh_admission import AdmissionRejected
from tests.unit.test_qa_question_pack import engine, q

SECRET = "unpublished-secret-in-message-args-and-locals"


@pytest.fixture(autouse=True)
def isolated_observation_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("RESEARCH_FEEDBACK_DIR", str(tmp_path / "inbox"))
    for name in (
        feedback.ENV_OUT,
        feedback.ENV_RUN_ID,
        feedback.ENV_ATTEMPT_ID,
        feedback.ENV_STATE,
    ):
        monkeypatch.delenv(name, raising=False)


class NewFailure(RuntimeError):
    def __str__(self):
        raise AssertionError("exception text must never be formatted")


def nested_failure():
    private_local = SECRET
    try:
        raise NewFailure(private_local)
    except NewFailure:
        try:
            raise LookupError(SECRET)
        except LookupError as inner:
            try:
                raise RuntimeError(SECRET) from inner
            except RuntimeError as error:
                return error


def build(error, stage="question_pack.dispatch", **refs):
    helper = getattr(diagnostic_log, "build_failure_diagnostic", None)
    assert callable(helper), "execution failures need the common structural diagnostic"
    return helper(error, stage=stage, references=refs)


def test_cause_context_and_new_exception_keep_code_positions_without_text():
    value = build(nested_failure(), question_id="CORE_01", work_item_id="work-1")
    assert value["root_type"] == "NewFailure"
    assert [entry["type"] for entry in value["chain"]] == [
        "RuntimeError",
        "LookupError",
        "NewFailure",
    ]
    assert [entry["relation"] for entry in value["chain"]] == ["raised", "cause", "context"]
    assert all(entry["frames"] for entry in value["chain"])
    assert all(
        frame["line"] > 0 and frame["function"]
        for entry in value["chain"]
        for frame in entry["frames"]
    )
    assert value["references"] == {"question_id": "CORE_01", "work_item_id": "work-1"}
    assert SECRET not in json.dumps(value)


def test_real_sqlite_busy_and_owned_admission_code_survive(tmp_path):
    with closing(sqlite3.connect(tmp_path / "isolated.sqlite", timeout=0)) as first, closing(
        sqlite3.connect(tmp_path / "isolated.sqlite", timeout=0)
    ) as second:
        first.execute("CREATE TABLE item(value)")
        first.commit()
        first.execute("BEGIN IMMEDIATE")
        try:
            second.execute("INSERT INTO item VALUES(1)")
        except sqlite3.OperationalError as sqlite_error:
            try:
                raise AdmissionRejected("refresh_admission_commit_unavailable") from sqlite_error
            except AdmissionRejected as error:
                value = build(error)
        finally:
            first.rollback()
    assert value["root_type"] == "OperationalError"
    assert value["chain"][0]["code"] == "refresh_admission_commit_unavailable"
    assert value["chain"][-1]["sqlite_errorcode"] == sqlite3.SQLITE_BUSY
    assert value["chain"][-1]["sqlite_errorname"] == "SQLITE_BUSY"


def test_unknown_reason_and_message_are_not_exported():
    error = NewFailure(SECRET)
    error.reason = SECRET
    error.message = SECRET
    error.error_code = SECRET
    error.diagnostic = {"reason": SECRET, "source_type": SECRET}
    assert SECRET not in json.dumps(build(error))
    owned = AdmissionRejected("refresh_admission_plan_changed:secret-value")
    value = build(owned)
    assert value["chain"][0]["code"] == "refresh_admission_plan_changed"
    assert "secret-value" not in json.dumps(value)


@pytest.mark.parametrize("pack", [False, True])
def test_failure_diagnostic_survives_real_json_serializers(pack):
    e, provider, _ = engine()
    error = nested_failure()
    if pack:
        provider.error = error
    else:
        provider.search_question = Mock(side_effect=error)
    batch = e.process_questions([q(1)], question_pack_size=2 if pack else 1)
    result = batch.results[0]
    assert result.answer.status == "error"
    assert result.metadata["failure_diagnostic"]["root_type"] == "NewFailure"
    legacy = json.loads(json.dumps(batch.to_dict(), ensure_ascii=False))
    public = json.loads(
        json.dumps(
            batch.to_quick_scan_dict(
                entity_id="ENT_TEST",
                company_name="Synthetic",
                provider_name="fixture",
                requested_model="fixture",
            ),
            ensure_ascii=False,
        )
    )
    assert next(iter(legacy.values()))["failure_diagnostic"]["root_type"] == "NewFailure"
    assert (
        public["execution_receipts"]["CORE_01"]["failure_diagnostic"]["root_type"] == "NewFailure"
    )
    assert SECRET not in json.dumps([legacy, public, result.metadata])
    assert len(result.answer.text) < 180


@pytest.mark.parametrize("where", ["claim", "finalize", "failure_finalize"])
def test_pack_claim_and_finalization_failures_also_keep_diagnostics(where):
    e, provider, lifecycle = engine()
    if where == "claim":
        lifecycle.before_question = Mock(side_effect=nested_failure())
    elif where == "finalize":
        lifecycle.after_question = Mock(side_effect=nested_failure())
    else:
        provider.error = nested_failure()
        lifecycle.after_question_failed = Mock(side_effect=nested_failure())
    batch = e.process_questions([q(1)], question_pack_size=2)
    diagnostic = batch.results[0].metadata["failure_diagnostic"]
    assert diagnostic["root_type"] == "NewFailure"
    if where == "failure_finalize":
        assert batch.results[0].metadata["finalization_diagnostic"]["root_type"] == "NewFailure"
    assert SECRET not in json.dumps(batch.to_dict())


def test_logs_and_optional_sidecar_never_read_exception_text_or_change_result(
    tmp_path, monkeypatch, capsys
):
    stream = io.StringIO()
    logger = logging.getLogger("test.execution-diagnostic")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(diagnostic_log.ContentBoundaryFormatter("%(message)s"))
    logger.handlers = [handler]
    logger.setLevel(logging.ERROR)
    configure = getattr(diagnostic_log, "configure_failure_diagnostics", None)
    assert callable(configure), "the normal CLI needs a real diagnostic invocation context"
    token = configure(output=tmp_path / "result.json")
    try:
        diagnostic_log.record_execution_failure(logger, nested_failure(), stage="question.search")
        rows = [
            json.loads(line)
            for line in next((tmp_path / "inbox").glob("feedback-*.jsonl"))
            .read_text(encoding="utf8")
            .splitlines()
        ]
        assert rows[0]["schema_version"] == "feedback-observation/1"
        assert rows[0]["disposition"] == "deviation"
        assert rows[0]["attempt_id"].startswith("cli-invocation-")
        details = json.loads(
            next((tmp_path / "inbox").glob("feedback-*.cause.json")).read_text(encoding="utf8")
        )
        assert rows[0]["details_ref"] == details["observation"]["details_ref"]
        assert details["diagnostic"]["root_type"] == "NewFailure"
        assert SECRET not in stream.getvalue() + json.dumps(rows)
        monkeypatch.setenv(feedback.ENV_OUT, str(tmp_path / "caller.jsonl"))
        monkeypatch.setattr(
            execution_feedback, "write_observations", Mock(side_effect=OSError(SECRET))
        )
        value = diagnostic_log.record_execution_failure(
            logger, nested_failure(), stage="question.search"
        )
        assert value["root_type"] == "NewFailure"
        assert "tracking incomplete" in capsys.readouterr().err
    finally:
        diagnostic_log.reset_failure_diagnostics(token)
        logger.handlers = []
        handler.close()


def test_optional_refresh_projection_failure_is_visible_and_safe(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        feedback, "project_question_receipts", Mock(side_effect=RuntimeError(SECRET))
    )
    result = {"status": "original"}
    assert (
        feedback.emit_refresh_observation(
            result,
            {},
            tmp_path / "missing.sqlite",
            environ={feedback.ENV_OUT: str(tmp_path / "observations.jsonl")},
        )
        == []
    )
    assert result == {"status": "original"}
    stderr = capsys.readouterr().err
    assert "tracking incomplete" in stderr
    assert SECRET not in stderr and str(tmp_path) not in stderr


def test_normal_cli_without_feedback_environment_has_real_invocation_context(monkeypatch, tmp_path):
    import main_with_llm
    from src.runners import llm_runner

    class FailingRunner:
        def __init__(self, **options):
            pass

        def run(self, **options):
            raise nested_failure()

    monkeypatch.setattr(llm_runner, "LLMRunner", FailingRunner)
    monkeypatch.setattr(
        sys,
        "argv",
        ["main_with_llm.py", "--company", "Synthetic", "--output", str(tmp_path / "result.json")],
    )
    assert main_with_llm.main() == 1
    rows = [
        json.loads(line)
        for line in next((tmp_path / "inbox").glob("feedback-*.jsonl"))
        .read_text(encoding="utf8")
        .splitlines()
    ]
    assert rows[0]["step_id"].startswith("cli.run/invocation/")
    assert rows[0]["attempt_id"].startswith("cli-invocation-")
    assert rows[0]["run_id"].startswith("cli-run-")
    assert SECRET not in json.dumps(rows)


def test_broken_sidecar_does_not_change_original_success_or_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        execution_feedback, "write_observations", Mock(side_effect=ValueError(SECRET))
    )
    token = diagnostic_log.configure_failure_diagnostics(output=tmp_path / "result.json")
    try:
        e, provider, lifecycle = engine()
        success = e.process_questions([q(1)], question_pack_size=2)
        assert success.results[0].answer.score == 8 and len(provider.calls) == 1
        original = json.dumps(success.to_dict(), ensure_ascii=False)
        success.results[0].metadata["failure_diagnostic"] = {"not-a-failure": SECRET}
        assert json.dumps(success.to_dict(), ensure_ascii=False) == original
        public = success.to_quick_scan_dict(
            entity_id="ENT_TEST", company_name="Synthetic", provider_name=None, requested_model=None
        )
        assert "failure_diagnostic" not in public["execution_receipts"]["CORE_01"]
        e, provider, lifecycle = engine()
        provider.error = nested_failure()
        failed = e.process_questions([q(1)], question_pack_size=2)
        assert failed.results[0].answer.status == "error"
        assert len(provider.calls) == 1 and len(lifecycle.failed) == 1
        assert failed.results[0].metadata["failure_diagnostic"]["root_type"] == "NewFailure"
    finally:
        diagnostic_log.reset_failure_diagnostics(token)


@pytest.mark.parametrize("where", ["directory", "file"])
def test_optional_log_io_cannot_block_import_logger_or_successful_answer(
    monkeypatch, capsys, where
):
    from pathlib import Path

    from src.core import qa_engine

    with monkeypatch.context() as patch:
        if where == "directory":
            patch.setattr(Path, "mkdir", Mock(side_effect=PermissionError(SECRET)))
        else:
            patch.setattr(logging, "FileHandler", Mock(side_effect=PermissionError(SECRET)))
        importlib.reload(diagnostic_log)
        logger = diagnostic_log.get_logger("isolated-unwritable-log-" + uuid4().hex)
        patch.setattr(qa_engine, "logger", logger)
        try:
            e, _, _ = engine()
            result = e.process_questions([q(1)], question_pack_size=2)
            assert result.results[0].answer.score == 8
        finally:
            diagnostic_log.close_logger(logger)
    captured = capsys.readouterr()
    assert "logging tracking incomplete" in captured.err
    assert SECRET not in captured.err
    assert captured.out == ""


def test_failure_log_publishes_receipts_without_loading_business_inspection(monkeypatch, tmp_path):
    blocking = {
        "quick_scan_feedback_observation",
        "quick_scan_refresh_inspect",
        "quick_scan_work_store",
    }
    attempted = []
    original_import = builtins.__import__

    def import_from_logging(name, *args, **kwargs):
        if name.rsplit(".", 1)[-1] in blocking:
            attempted.append(name)
            raise ImportError("business inspection unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_from_logging)
    diagnostic = diagnostic_log.record_execution_failure(
        None, nested_failure(), stage="question.search"
    )

    assert attempted == []
    rows = list((tmp_path / "inbox").glob("feedback-*.jsonl"))
    receipts = list((tmp_path / "inbox").glob("feedback-*.cause.json"))
    assert len(rows) == len(receipts) == 1
    assert len(list((tmp_path / "inbox").glob("feedback-*.ready"))) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf8"))
    assert receipt["diagnostic"] == diagnostic
    assert diagnostic["root_type"] == "NewFailure"
    assert SECRET not in json.dumps(receipt)


def test_failed_unsent_release_keeps_original_work_and_cold_diagnostic(tmp_path, monkeypatch):
    import subprocess

    from src.runners.llm_runner import QuickScanWorkLifecycle
    from tests.unit.test_execution_feedback_intake import _offline_env
    from tests.unit.test_quick_scan_feedback_owner import _action
    from tests.unit.test_quick_scan_work_transport import _create_claimed_item

    store, work_id, lease = _create_claimed_item(tmp_path)
    store_before = store.path.read_bytes()
    inbox = tmp_path / "inbox"
    cleanup_failure = sqlite3.OperationalError(SECRET)
    release = Mock(side_effect=cleanup_failure)
    monkeypatch.setattr(store, "release_unsent", release)
    lifecycle = QuickScanWorkLifecycle(
        store,
        entity_id="ENT_FIXTURE_CO",
        run_id="fixture-run",
        scan_id="fixture-scan",
        identity={},
        transport_managed=True,
    )
    primary_error = RuntimeError("synthetic-primary-failure")
    lifecycle.after_question_failed(dict(work_item_id=work_id, lease=lease), primary_error)
    release.assert_called_once_with(work_id, lease)
    assert store.path.read_bytes() == store_before
    assert primary_error.args == ("synthetic-primary-failure",)

    (marker,) = inbox.glob("*.ready")
    raw_receipt = marker.with_suffix(".cause.json").read_text(encoding="utf-8")
    observation = json.loads(marker.with_suffix(".jsonl").read_text(encoding="utf-8"))
    diagnostic = json.loads(raw_receipt)["diagnostic"]
    assert diagnostic["stage"] == "owner.local.unsent.release"
    assert diagnostic["root_type"] == "OperationalError"
    assert diagnostic["references"]["work_item_id"] == work_id
    assert SECRET not in raw_receipt

    state = tmp_path / "actions.sqlite"
    request = _action("reconcile", {"occurrence_id": "cleanup-fixture", "observation": observation})
    cold = subprocess.run(
        [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            "-m",
            "src.utils.quick_scan_feedback_owner",
            "--state",
            str(state),
            "reconcile",
        ],
        cwd=tmp_path,
        env={**_offline_env(tmp_path), "RESEARCH_FEEDBACK_DIR": str(inbox)},
        input=json.dumps(request).encode("ascii"),
        capture_output=True,
        timeout=30,
        check=True,
    )
    assert json.loads(cold.stdout) == {
        "state": "done",
        "result": {
            "status": "known",
            "job_status": "failed",
            "cost_state": "not_applicable",
            "evidence_ref": observation["evidence_ref"],
        },
    }
    assert not state.exists()
    assert store.path.read_bytes() == store_before


def test_missing_output_reference_keeps_observable_invocation_without_path_text(tmp_path, capsys):
    class InvalidOutput:
        def __fspath__(self):
            raise ValueError(SECRET)

    token = diagnostic_log.configure_failure_diagnostics(output=InvalidOutput())
    try:
        diagnostic_log.record_execution_failure(None, nested_failure(), stage="fixture.output")
    finally:
        diagnostic_log.reset_failure_diagnostics(token)

    (marker,) = (tmp_path / "inbox").glob("*.ready")
    row = json.loads(marker.read_bytes())
    assert row["run_id"].startswith("cli-run-")
    assert row["attempt_id"].startswith("cli-invocation-")
    stderr = capsys.readouterr().err
    assert "execution invocation output reference unavailable" in stderr
    assert SECRET not in stderr


def test_feedback_version_resolves_local_git_and_uses_fixed_no_shell_argv(tmp_path, monkeypatch):
    fake_executable = tmp_path / "bin" / "git"
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    monkeypatch.setattr(
        execution_feedback, "__file__", str(root / "src/utils/execution_feedback.py")
    )
    resolver = Mock(return_value=str(fake_executable))
    process = Mock(return_value="a" * 40)
    monkeypatch.setattr(execution_feedback.shutil, "which", resolver)
    monkeypatch.setattr(execution_feedback.subprocess, "check_output", process)
    execution_feedback._version.cache_clear()
    try:
        assert execution_feedback._version() == "a" * 40
        resolver.assert_called_once_with("git")
        assert process.call_args.args[0] == [
            str(fake_executable),
            "-C",
            str(root),
            "rev-parse",
            "--verify",
            "HEAD",
        ]
        assert process.call_args.kwargs["shell"] is False
        assert process.call_args.kwargs["timeout"] > 0
    finally:
        execution_feedback._version.cache_clear()
