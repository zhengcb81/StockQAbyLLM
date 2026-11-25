"""Public offline StockQA failure intake and origin-preserving cold readback."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.utils import logger as diagnostics
from tests.unit.test_qa_question_pack import engine, q

ROOT = Path(__file__).resolve().parents[2]
PYTHON = [sys.executable, "-B", "-X", "utf8"]


def _offline_env(root):
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE"}
    }
    env.update(
        {
            key: str(root)
            for key in (
                "HOME",
                "USERPROFILE",
                "TEMP",
                "TMP",
                "TMPDIR",
                "APPDATA",
                "LOCALAPPDATA",
                "XDG_STATE_HOME",
            )
        }
    )
    guard = root / "sitecustomize.py"
    guard.write_text(
        "import sys\ndef offline(event, args):\n"
        "    if event in ('socket.connect','socket.getaddrinfo','socket.sendto'):\n"
        "        raise RuntimeError('offline test')\nsys.addaudithook(offline)\n",
        encoding="ascii",
    )
    env.update(
        PYTHONPATH=os.pathsep.join([str(root), str(ROOT)]),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUTF8="1",
    )
    return env


def _failure_owner_request(kind, event):
    payload = {"occurrence_id": "test-occurrence", "observation": event}
    action_id = hashlib.sha256(kind.encode()).hexdigest()
    wire = json.dumps(["stockqa", kind, action_id, payload], sort_keys=True, separators=(",", ":"))
    return {
        "schema_version": "feedback-action/1",
        "owner": "stockqa",
        "kind": kind,
        "method": "lookup" if kind == "reconcile" else "submit",
        "action_id": action_id,
        "payload": payload,
        "request_digest": hashlib.sha256(wire.encode()).hexdigest(),
    }


class OpaqueError(RuntimeError):
    def __getattribute__(self, name):
        if name in {"__dict__", "__cause__", "__context__", "__traceback__", "diagnostic"}:
            raise RuntimeError("private property")
        return super().__getattribute__(name)


def test_suppressed_original_and_unreadable_attributes_do_not_skip_failed_finalization(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("RESEARCH_FEEDBACK_DIR", str(tmp_path / "inbox"))
    try:
        raise OpaqueError("private-sentinel")
    except OpaqueError:
        try:
            raise RuntimeError("private-sentinel") from None
        except RuntimeError as exc:
            error = exc
    e, provider, lifecycle = engine()
    provider.error = error
    batch = e.process_questions([q(1)], question_pack_size=2)
    trace = batch.results[0].metadata["failure_diagnostic"]
    assert [x["type"] for x in trace["chain"]] == ["RuntimeError", "OpaqueError"]
    assert len(lifecycle.failed) == 1 and len(provider.calls) == 1
    assert "private-sentinel" not in json.dumps(trace)


@pytest.mark.parametrize(
    ("ending", "terminal_stage"), [("return", "cli.return"), ("raise", "cli.run")]
)
def test_local_step_diagnostic_does_not_explain_independent_invocation_failure(
    tmp_path, monkeypatch, ending, terminal_stage
):
    outbox = tmp_path / "feedback"
    monkeypatch.setenv("RESEARCH_FEEDBACK_DIR", str(outbox))

    @diagnostics.observed_command
    def run():
        diagnostics.record_execution_failure(
            None,
            ValueError("synthetic private decode message"),
            stage="owner.local.received_checkpoint.member_decode",
            explains_invocation=False,
        )
        if ending == "raise":
            raise RuntimeError("synthetic independent exit message")
        return 7

    if ending == "raise":
        with pytest.raises(RuntimeError):
            run()
    else:
        assert run() == 7
    results = [json.loads(path.read_bytes()) for path in outbox.glob("feedback-*.cause.json")]
    assert {r["diagnostic"]["stage"] for r in results} == {
        "owner.local.received_checkpoint.member_decode",
        terminal_stage,
    }
    assert len({r["observation"]["run_id"] for r in results}) == 1
    assert len({r["observation"]["step_id"] for r in results}) == 2
    assert "synthetic private" not in json.dumps(results)


def test_diagnostic_constructor_failure_preserves_original_pack_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_FEEDBACK_DIR", str(tmp_path / "inbox"))
    monkeypatch.setattr(
        diagnostics, "build_failure_diagnostic", Mock(side_effect=OSError("unwritable"))
    )
    e, provider, lifecycle = engine()
    provider.error = OpaqueError("private-sentinel")
    batch = e.process_questions([q(1)], question_pack_size=2)
    assert batch.results[0].answer.status == "error"
    assert len(lifecycle.failed) == len(provider.calls) == 1


@pytest.mark.parametrize("mode", ["parse", "batch", "validation", "inspection"])
def test_ordinary_failure_reaches_outbox_and_cold_owner(mode, tmp_path):
    inbox = tmp_path / "inbox"
    state = tmp_path / "feedback.sqlite"
    env = {
        **_offline_env(tmp_path),
        "RESEARCH_FEEDBACK_DIR": str(inbox),
        "STOCKQA_FEEDBACK_STATE": str(state),
    }
    params = {
        "parse": ["--question-pack-size", "0"],
        "batch": ["--batch", "absent.txt"],
        "validation": ["--company", "Synthetic", "--require-search"],
        "inspection": [
            "--ledger",
            "absent.sqlite",
            "--policy",
            "absent.yaml",
            "--request",
            "absent.json",
        ],
    }[mode]
    program = (
        ["-m", "src.utils.quick_scan_refresh_inspect_cli"]
        if mode == "inspection"
        else [str(ROOT / "main_with_llm.py"), "--output", str(tmp_path / "result.json")]
    )
    done = subprocess.run(
        [*PYTHON, *program, *params], cwd=tmp_path, env=env, capture_output=True, timeout=30
    )
    assert done.returncode != 0
    if mode == "inspection":
        assert json.loads(done.stdout)["error_code"] == "refresh_executor_inspection_rejected"
    (marker,) = inbox.glob("feedback-*.ready")
    (event,) = [json.loads(line) for line in marker.with_suffix(".jsonl").read_bytes().splitlines()]
    identity = json.loads(marker.read_bytes())
    assert all(identity[key] == event[key] for key in ("owner", "run_id", "attempt_id", "step_id"))
    assert event["disposition"] == "deviation" and event["check_id"] == "execution-outcome"
    for kind in ("triage", "reconcile"):
        action = _failure_owner_request(kind, event)
        receipt = subprocess.check_output(
            [*PYTHON, "-m", "src.utils.quick_scan_feedback_owner", "--state", str(state), kind],
            input=json.dumps(action).encode(),
            cwd=tmp_path,
            env=env,
            timeout=30,
        )
        result = json.loads(receipt)["result"]
        assert result["evidence_ref"] == event["evidence_ref"]
        if kind == "triage":
            assert result["issue_ref"] and result["diagnostic"]["chain"]
            assert result["diagnostic"]["entry"]
            options = {item["option"]: item for item in result["diagnostic"]["options"]}
            if mode == "parse":
                assert options["--question-pack-size"]["number"] == "0"
            elif mode == "batch":
                assert options["--batch"]["input_ref"] == hashlib.sha256(b"absent.txt").hexdigest()
                assert result["diagnostic"]["root_type"] == "FileNotFoundError"
            elif mode == "validation":
                assert "--require-search" in options and "--entity-id" not in options
        else:
            assert result["job_status"] == "failed"
            assert result["cost_state"] == (
                "not_applicable" if mode in {"parse", "inspection"} else "unknown"
            )
    assert not (tmp_path / "absent.sqlite").exists()


def test_cold_owner_selects_original_observation_directory(tmp_path):
    old_inbox = tmp_path / "original"
    saved = tmp_path / "owner" / "observations"
    relocated = tmp_path / "other-code"
    relocated.mkdir()
    state = tmp_path / "owner" / "feedback.sqlite"
    env = _offline_env(tmp_path)

    failure = subprocess.run(
        [*PYTHON, str(ROOT / "main_with_llm.py"), "--question-pack-size", "0"],
        cwd=tmp_path,
        env={**env, "RESEARCH_FEEDBACK_DIR": str(old_inbox)},
        capture_output=True,
        timeout=30,
    )
    assert failure.returncode == 2
    shutil.copytree(old_inbox, saved)
    (observation,) = [json.loads(path.read_bytes()) for path in saved.glob("*.jsonl")]
    original_bytes = {path.name: path.read_bytes() for path in saved.iterdir()}
    decoy = tmp_path / "unrelated-inbox"
    request = json.dumps(_failure_owner_request("reconcile", observation)).encode()
    route = [*PYTHON, "-m", "src.utils.quick_scan_feedback_owner", "--state", str(state)]

    def cold(*args, locator):
        result = subprocess.run(
            [*route, *args, "reconcile"],
            input=request,
            cwd=relocated,
            env={**env, "RESEARCH_FEEDBACK_DIR": str(locator)},
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        return json.loads(result.stdout)

    expected = {
        "state": "done",
        "result": {
            "status": "known",
            "job_status": "failed",
            "cost_state": "not_applicable",
            "evidence_ref": observation["evidence_ref"],
        },
    }
    assert cold(locator=saved) == expected
    for _ in range(2):
        assert cold("--observation-dir", str(saved), locator=decoy) == expected
    missing = cold("--observation-dir", str(decoy), locator=saved)
    assert missing["result"]["job_status"] == "unconfirmed"
    assert original_bytes == {path.name: path.read_bytes() for path in saved.iterdir()}
    assert not state.exists() and not decoy.exists()
    assert not list(relocated.iterdir())


@pytest.mark.parametrize(
    "endpoint",
    [
        "continuation_bank",
        "delivery_control",
        "delivery_scope",
        "execution_inspect",
        "model_policy",
        "refresh_admission",
        "registered_permissions",
        "work_decision_apply",
        "work_decision_inspect",
    ],
)
def test_owner_commands_record_early_refusal_without_control_or_network_work(tmp_path, endpoint):
    # No CLI here can reach owner registration or any model. An empty argument
    # list is rejected by the real parser while the canonical inputs are absent.
    inbox = tmp_path / "inbox"
    env = {**_offline_env(tmp_path), "RESEARCH_FEEDBACK_DIR": str(inbox)}
    result = subprocess.run(
        [*PYTHON, "-m", "src.utils.quick_scan_" + endpoint + "_cli"],
        env=env,
        cwd=tmp_path,
        input=b"{}",
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 2
    (ready,) = inbox.glob("feedback-*.ready")
    row = json.loads(ready.with_suffix(".jsonl").read_bytes())
    assert row["actual"] == "execution_failed" and row["refusal_code"] == "invalid_cli_arguments"
    assert not list(tmp_path.glob("*.sqlite"))


def test_public_engine_without_host_settings_persists_an_owner_observation(tmp_path, monkeypatch):
    from src.utils import execution_feedback

    inbox = tmp_path / "native"
    monkeypatch.setattr(execution_feedback, "DEFAULT_INBOX", inbox)
    monkeypatch.delenv("RESEARCH_FEEDBACK_DIR", raising=False)
    e, provider, lifecycle = engine()
    provider.error = RuntimeError("private-sentinel")
    batch = e.process_questions([q(1)], question_pack_size=2)
    assert batch.results[0].answer.status == "error"
    (marker,) = inbox.glob("feedback-*.ready")
    event = json.loads(marker.with_suffix(".jsonl").read_bytes())
    proof = execution_feedback.read_execution(event, {})
    assert proof["diagnostic"]["root_type"] == "RuntimeError"
    assert len(lifecycle.failed) == 1


def test_cold_failure_reader_needs_no_paid_client_or_ledger_writer(tmp_path):
    env = _offline_env(tmp_path)
    program = (
        "import sys; from src.utils import quick_scan_feedback_owner; "
        "from src.utils.quick_scan_result_outbox import strict_json_loads; "
        "assert strict_json_loads('{}') == {}; "
        "assert 'src.providers.llm_client' not in sys.modules; "
        "assert 'src.utils.quick_scan_work_store' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-S", "-B", "-c", program],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("pack_size", [1, 2])
def test_returned_claim_refusal_is_visible_without_sending_or_fabricating_an_exception(
    tmp_path, monkeypatch, pack_size
):
    inbox = tmp_path / "outbox"
    monkeypatch.setenv("RESEARCH_FEEDBACK_DIR", str(inbox))
    e, provider, lifecycle = engine()
    lifecycle.refused.add("CORE_01")
    batch = e.process_questions([q(1)], question_pack_size=pack_size)
    assert not provider.calls and batch.results[0].answer.status == "error"
    (marker,) = inbox.glob("*.ready")
    row = json.loads(marker.with_suffix(".jsonl").read_bytes())
    proof = json.loads(marker.with_suffix(".cause.json").read_bytes())
    assert row["refusal_code"] and proof["diagnostic"]["outcome_source"] == "returned_status"
    assert proof["diagnostic"]["references"]["question_id"] == "CORE_01"


def test_single_primary_failure_survives_a_failure_in_its_lifecycle_callback(tmp_path, monkeypatch):
    from src.core.qa_engine import ProcessingError

    inbox = tmp_path / "outbox"
    monkeypatch.setenv("RESEARCH_FEEDBACK_DIR", str(inbox))
    e, provider, lifecycle = engine()
    primary = ProcessingError(message="private-original-sentinel")
    e.process_question = Mock(side_effect=primary)
    lifecycle.after_question_failed = Mock(side_effect=RuntimeError("private-callback-sentinel"))
    result = e.process_questions([q(1)]).results[0]
    assert result.metadata["failure_diagnostic"]["error_type"] == "ProcessingError"
    assert result.metadata["finalization_diagnostic"]["error_type"] == "RuntimeError"
    assert len(list(inbox.glob("*.ready"))) == 2 and not provider.calls
    assert "private-" not in json.dumps(result.metadata)
    assert lifecycle.after_question_failed.call_args.args[1] is primary


def test_basic_cli_unknown_error_preserves_original_exit_without_publishing_exception_text(
    tmp_path, monkeypatch, capsys
):
    import main as entry
    from src.runners.basic_runner import BasicRunner

    inbox = tmp_path / "inbox"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RESEARCH_FEEDBACK_DIR", str(inbox))
    monkeypatch.setattr(sys, "argv", ["main.py"])
    monkeypatch.setattr(
        BasicRunner, "run", Mock(side_effect=RuntimeError("private-failure-sentinel"))
    )
    assert entry.main() == 1
    observed = capsys.readouterr()
    body = (
        observed.out
        + observed.err
        + "".join(p.read_text(encoding="utf8") for p in tmp_path.rglob("*.log"))
    )
    assert "private-failure-sentinel" not in body
    assert "RuntimeError" in body and len(list(inbox.glob("*.ready"))) == 1


def test_bad_operator_outbox_still_exposes_original_in_owner_inbox(tmp_path, monkeypatch):
    from src.utils import execution_feedback

    native = tmp_path / "owner-native"
    blocked = tmp_path / "unavailable"
    blocked.write_text("occupied")
    monkeypatch.setattr(execution_feedback, "DEFAULT_INBOX", native)
    monkeypatch.setenv("RESEARCH_FEEDBACK_DIR", str(blocked))
    e, provider, lifecycle = engine()
    provider.error = OSError("private-failure")
    result = e.process_questions([q(1)], question_pack_size=2).results[0]
    assert result.answer.status == "error" and len(lifecycle.failed) == 1
    (marker,) = native.glob("*.ready")
    row = json.loads(marker.with_suffix(".jsonl").read_bytes())
    assert (
        execution_feedback.read_execution(row, os.environ)["diagnostic"]["root_type"] == "OSError"
    )


def test_budget_owner_publishes_only_its_own_rule_code():
    from src.utils.quick_scan_work_store import BudgetAdmissionError

    for error, expected in [
        (BudgetAdmissionError.for_rule("budget_request_limit"), "budget_request_limit"),
        (BudgetAdmissionError("private_external_code"), None),
    ]:
        proof = diagnostics.build_failure_diagnostic(error, stage="question.prepare_intent")
        assert proof["chain"][0].get("code") == expected
        assert "private_external_code" not in json.dumps(proof)
