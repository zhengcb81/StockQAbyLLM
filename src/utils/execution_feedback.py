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


def warn_feedback_unavailable(message: str) -> None:
    """Report an unavailable diagnostic sink at the last local output boundary."""
    # If stderr is also unavailable, the caller's original outcome still owns
    # the exit status. There is no further sink to recurse into here.
    with suppress(Exception):
        print(message, file=sys.stderr)


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


def emit_execution_observation(diagnostic, *, context=None, environ=None):
    """Publish an invocation failure without opening any business state."""
    return publish(
        diagnostic, context or {}, os.environ if environ is None else environ, write_observations
    )


def publish(diagnostic, context, env, project):
    token = uuid4().hex
    directory = inbox(env)
    invocation = context.get("invocation_id") or "cli-invocation-" + token
    run = env.get("RESEARCH_FEEDBACK_RUN_ID") or context.get("run_id") or invocation
    attempt = env.get("RESEARCH_FEEDBACK_ATTEMPT_ID") or invocation
    step = diagnostic["stage"] + "/" + diagnostic["references"].get("question_id", "invocation")
    # One inherited parent attempt may run the same question or CLI more than once.
    step += "/" + token
    evidence = "stockqa-invocation/1/" + token
    fingerprint = hashlib.sha256(_bytes(diagnostic)).hexdigest()
    row = {
        "schema_version": "feedback-observation/1",
        "owner": OWNER,
        "origin_owner": OWNER,
        "run_id": run,
        "attempt_id": attempt,
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
        "job_status": "cancelled" if diagnostic["stage"] == "cli.cancelled" else "failed",
        "cost_state": "not_applicable" if local_only else "unknown",
    }
    native_error = None
    try:
        publish_native(directory, row, token=token, receipt=receipt)
    except OSError as error:
        native_error = error
    if env.get("RESEARCH_FEEDBACK_OUT"):
        project(env["RESEARCH_FEEDBACK_OUT"], [row])
    if native_error:
        raise native_error
    return [row]


def read_execution(observation, env):
    evidence = observation.get("evidence_ref")
    match = (
        re.fullmatch(r"stockqa-invocation/1/([0-9a-f]{32})", evidence or "")
        if isinstance(evidence, str)
        else None
    )
    if not match:
        return None
    try:
        for directory in dict.fromkeys((inbox(env), DEFAULT_INBOX)):
            path = directory / ("feedback-" + match[1] + ".cause.json")
            if path.is_file():
                break
        else:
            return None
        receipt = json.loads(path.read_bytes())
        native = receipt["observation"]
        if not all(
            observation.get(k) == native.get(k)
            for k in (
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
            return None
        if (
            native["details_ref"]
            != "stockqa-failure-diagnostic/1/"
            + match[1]
            + "#"
            + hashlib.sha256(_bytes(receipt["diagnostic"])).hexdigest()
        ):
            return None
        return receipt
    except (OSError, ValueError, KeyError, TypeError):
        return None
