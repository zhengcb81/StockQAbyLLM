"""Owner-side evidence for failed StockQA invocations, separate from paid work."""

import hashlib
import json
import os
import re
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from contextlib import suppress
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

OWNER = "stockqa"
DEFAULT_INBOX = Path(__file__).resolve().parents[2] / "logs" / ".feedback" / "observations"
DEFAULT_LOG_DIR = Path("logs")
RECEIPT_MESSAGE = "execution_failure_receipt"
PHASE_MESSAGE = "execution_phase"
_ID = re.compile(r"[A-Za-z0-9_.:/@-]{1,200}")
_EVIDENCE = re.compile(r"stockqa-invocation/1/([0-9a-f]{32})")
_LOG_NAME = re.compile(r"stock_qa_\d{8}\.log(?:\.1)?")


def warn_feedback_unavailable(message: str) -> None:
    """Report an unavailable diagnostic sink at the last local output boundary."""
    # If stderr is also unavailable, the caller's original outcome still owns
    # the exit status. There is no further sink to recurse into here.
    stream = sys.stderr
    if stream is None:
        return
    with suppress(Exception):
        print(message, file=stream)


@lru_cache(maxsize=1)
def _version():
    root = Path(__file__).resolve().parents[2]
    git = shutil.which("git")
    if git is None or not (root / ".git").exists():
        return "unversioned"
    try:
        # This argv contains only the resolved executable, this repository's
        # own root and fixed Git arguments, with no shell interpolation.
        revision = subprocess.check_output(  # nosec B603
            [str(Path(git).absolute()), "-C", str(root), "rev-parse", "--verify", "HEAD"],
            stderr=subprocess.DEVNULL,
            timeout=3,
            text=True,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "unversioned"
    return revision if re.fullmatch("[a-f0-9]{40,64}", revision) else "unversioned"


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def _atomic(path, body):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def inbox(env):
    return Path(env.get("RESEARCH_FEEDBACK_DIR") or DEFAULT_INBOX)


def observation_read_dirs(env, *, observation_dir: Path | None = None) -> tuple[Path, ...]:
    """Keep an explicitly selected native read root independent of the code directory."""
    if observation_dir is not None:
        return (observation_dir,)
    return tuple(dict.fromkeys((inbox(env), DEFAULT_INBOX)))


def publish_native(directory, row, *, token=None, receipt=None):
    """Commit evidence before the marker that lets a consumer ingest the row."""
    token = token or uuid4().hex
    for destination in dict.fromkeys((directory, DEFAULT_INBOX)):
        try:
            _write_native(destination, row, token, receipt)
            return
        except OSError:
            if destination == DEFAULT_INBOX:
                raise
            warn_feedback_unavailable("StockQA feedback destination unavailable, using owner inbox")


def _write_native(directory, row, token, receipt):
    directory.mkdir(parents=True, exist_ok=True)
    name = directory / ("feedback-" + token)
    if receipt is not None:
        _atomic(name.with_suffix(".cause.json"), _bytes(receipt))
    _atomic(name.with_suffix(".jsonl"), _bytes(row) + b"\n")
    _atomic(
        name.with_suffix(".ready"),
        _bytes({k: row[k] for k in ("owner", "run_id", "attempt_id", "step_id")}),
    )


def write_observations(path, rows):
    """Append UTF-8 JSONL; every exported value must already be a string."""
    payload = []
    for row in rows:
        if not isinstance(row, dict) or not row:
            raise ValueError("feedback observation must be a non-empty object")
        for key, value in row.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("feedback observation fields must be strings")
        payload.append(json.dumps(row, ensure_ascii=False) + "\n")
    with Path(path).open("a", encoding="utf-8", newline="\n") as stream:
        stream.write("".join(payload))


def _public_id(value):
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def execution_references(context, env):
    """Select one identity for the caller response, owner evidence and F4."""
    invocation = context.get("invocation_id") or "cli-invocation-" + uuid4().hex
    return {
        "invocation_id": invocation,
        "run_id": context.get("run_id")
        or _public_id(env.get("RESEARCH_FEEDBACK_RUN_ID"))
        or "cli-run-" + invocation.removeprefix("cli-invocation-"),
        "attempt_id": context.get("attempt_id")
        or _public_id(env.get("RESEARCH_FEEDBACK_ATTEMPT_ID"))
        or invocation,
    }


def execution_phase(stage, context, references, *, exit_code=None):
    result = {
        "schema": "stockqa.runtime_phase/1",
        "stage": stage,
        "entry": context.get("entry"),
        "run_id": context["run_id"],
        "attempt_id": context["attempt_id"],
        "invocation_id": context["invocation_id"],
        "version": _version(),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "references": references,
    }
    if stage == "cli.started":
        result["options"] = context.get("options", [])
    if exit_code is not None:
        result["exit_code"] = exit_code
    return result


def emit_execution_observation(diagnostic, *, context=None, environ=None, fallback=None):
    """Publish an invocation failure without opening any business state."""
    return publish(
        diagnostic,
        context or {},
        os.environ if environ is None else environ,
        write_observations,
        fallback=fallback,
    )


def publish(diagnostic, context, env, project, *, fallback=None):
    token = uuid4().hex
    directory = inbox(env)
    identity = execution_references(context, env)
    step = diagnostic["stage"] + "/" + diagnostic["references"].get("question_id", "invocation")
    # One inherited parent attempt may run the same question or CLI more than once.
    step += "/" + token
    evidence = "stockqa-invocation/1/" + token
    fingerprint = hashlib.sha256(_bytes(diagnostic)).hexdigest()
    row = {
        "schema_version": "feedback-observation/1",
        "owner": OWNER,
        "origin_owner": OWNER,
        "run_id": identity["run_id"],
        "attempt_id": identity["attempt_id"],
        "step_id": step,
        "check_id": "execution-outcome",
        "expected": "completed",
        "actual": "execution_failed",
        "disposition": "deviation",
        "version": _version(),
        "issue_key": "stockqa-execution-failed",
        "evidence_ref": evidence,
        "details_ref": "stockqa-failure-diagnostic/1/" + token + "#" + fingerprint,
    }
    if diagnostic["stage"] == "question.original_terminal":
        row.update(actual="already_terminal", expected="already_terminal", disposition="expected")
        row.pop("issue_key", None)
    if diagnostic.get("refusal_code"):
        row["refusal_code"] = diagnostic["refusal_code"]
    elif diagnostic["stage"] == "cli.arguments":
        row["refusal_code"] = "invalid_cli_arguments"
    else:
        refusal = next((node["code"] for node in diagnostic["chain"] if node.get("code")), None)
        if refusal:
            row["refusal_code"] = refusal
    local_only = diagnostic["stage"].startswith("owner.local.") or diagnostic["stage"] in {
        "cli.arguments",
        "inspect.request",
        "question.claim",
        "question.original_terminal",
        "spend.preflight",
    }
    receipt = {
        "observation": row,
        "diagnostic": diagnostic,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "job_status": (
            "cancelled"
            if diagnostic["stage"] in {"cli.cancelled", "provider.async.cancelled"}
            else "failed"
        ),
        "cost_state": "not_applicable" if local_only else "unknown",
    }
    native_error = None
    try:
        publish_native(directory, row, token=token, receipt=receipt)
    except OSError as error:
        native_error = error
        if fallback is not None:
            fallback(receipt)
    if env.get("RESEARCH_FEEDBACK_OUT"):
        try:
            project(env["RESEARCH_FEEDBACK_OUT"], [row])
        except (OSError, ValueError):
            _warn_incomplete(row)
            return [row]
    if native_error is not None and fallback is None:
        raise native_error
    if native_error is not None:
        _warn_incomplete(row)
    return [row]


def _warn_incomplete(row):
    fields = {
        key: row[key]
        for key in ("run_id", "attempt_id", "evidence_ref", "refusal_code")
        if key in row
    }
    warn_feedback_unavailable("execution failure tracking incomplete " + _bytes(fields).decode())


def _valid_receipt(receipt, token):
    row = receipt["observation"]
    return (
        row["owner"] == OWNER
        and isinstance(receipt["recorded_at"], str)
        and receipt.get("job_status") in {"failed", "cancelled"}
        and row["evidence_ref"] == "stockqa-invocation/1/" + token
        and row["details_ref"]
        == "stockqa-failure-diagnostic/1/"
        + token
        + "#"
        + hashlib.sha256(_bytes(receipt["diagnostic"])).hexdigest()
    )


def _readable_paths(directory, pattern):
    try:
        yield from directory.glob(pattern)
    except OSError:
        return


def _native_receipts(directories, token=None):
    basename = "feedback-" + (token or "*") + ".cause.json"
    for directory in directories:
        for path in _readable_paths(directory, basename):
            try:
                receipt = json.loads(path.read_bytes())
                if _valid_receipt(receipt, path.name[9:-11]):
                    yield receipt
            except (OSError, ValueError, KeyError, TypeError, RecursionError):
                continue


def _log_records(log_dir, prefix, match):
    for path in _readable_paths(log_dir, "stock_qa_*.log*"):
        if not _LOG_NAME.fullmatch(path.name):
            continue
        try:
            with path.open(encoding="utf8", errors="replace") as stream:
                for line in stream:
                    _, found, body = line.partition(" - " + prefix + " ")
                    if found and match in body:
                        try:
                            yield json.loads(body)
                        except (ValueError, RecursionError):
                            continue
        except OSError:
            continue


def _log_receipts(log_dir, match):
    for receipt in _log_records(log_dir, RECEIPT_MESSAGE, match):
        try:
            token = _EVIDENCE.fullmatch(receipt["observation"]["evidence_ref"])
            if token and _valid_receipt(receipt, token[1]):
                yield receipt
        except (KeyError, TypeError, RecursionError):
            continue


def read_execution(observation, env, *, observation_dir: Path | None = None, log_dir=None):
    evidence = observation.get("evidence_ref")
    token = _EVIDENCE.fullmatch(evidence) if isinstance(evidence, str) else None
    if token is None:
        return None
    directories = observation_read_dirs(env, observation_dir=observation_dir)
    for candidates in (
        _native_receipts(directories, token[1]),
        _log_receipts(log_dir or DEFAULT_LOG_DIR, evidence),
    ):
        for receipt in candidates:
            native = receipt["observation"]
            if all(
                observation.get(key) == native.get(key)
                for key in (
                    "owner",
                    "run_id",
                    "attempt_id",
                    "step_id",
                    "check_id",
                    "actual",
                    "expected",
                    "disposition",
                    "evidence_ref",
                    "details_ref",
                )
            ):
                return receipt
    return None


def runtime_history(run_id, attempt_id, env, *, observation_dir=None, log_dir=None):
    """Read the original attempt without opening a business ledger or appending logs."""
    if not _public_id(run_id) or not _public_id(attempt_id):
        raise ValueError("invalid runtime identity")
    directories = observation_read_dirs(env, observation_dir=observation_dir)
    log_dir = log_dir or DEFAULT_LOG_DIR
    failures: dict[str, dict] = {}
    for candidates in (_native_receipts(directories), _log_receipts(log_dir, run_id)):
        for receipt in candidates:
            row = receipt["observation"]
            if row.get("run_id") == run_id and row.get("attempt_id") == attempt_id:
                failures.setdefault(row["evidence_ref"], receipt)
    phases = [
        phase
        for phase in _log_records(log_dir, PHASE_MESSAGE, run_id)
        if isinstance(phase, dict)
        and phase.get("schema") == "stockqa.runtime_phase/1"
        and phase.get("run_id") == run_id
        and phase.get("attempt_id") == attempt_id
        and isinstance(phase.get("recorded_at"), str)
    ]
    phases.sort(key=lambda phase: phase["recorded_at"])
    outcomes = sorted(failures.values(), key=lambda entry: entry["recorded_at"])
    last_phase = phases[-1] if phases else None
    terminal = last_phase if last_phase and last_phase.get("stage") == "cli.exited" else None
    if terminal is not None:
        # One host attempt may contain multiple overlapping CLI invocations.
        # An earlier sibling exit cannot close a later orphaned dispatch.
        closed = {
            phase.get("invocation_id") for phase in phases if phase.get("stage") == "cli.exited"
        }
        if any(phase.get("invocation_id") not in closed for phase in phases):
            terminal = None
    if terminal is not None:
        status = "completed" if terminal.get("exit_code") == 0 else "failed"
    elif outcomes and (
        not phases
        or outcomes[-1]["diagnostic"].get("stage") == "spend.preflight"
        or outcomes[-1]["diagnostic"].get("stage", "").startswith("cli.")
    ):
        status = outcomes[-1]["job_status"]
    else:
        status = "outcome_unknown"
    cost = "unknown"
    if outcomes and all(event.get("cost_state") == "not_applicable" for event in outcomes):
        if not any(phase.get("stage") == "transport.intent" for phase in phases):
            cost = "not_applicable"
    return {
        "schema": "stockqa.runtime_history/1",
        "run_id": run_id,
        "attempt_id": attempt_id,
        "status": "available" if phases or failures else "unavailable_or_absent",
        "invocation_status": status,
        "cost_state": cost,
        "phases": phases,
        "failures": outcomes,
    }
