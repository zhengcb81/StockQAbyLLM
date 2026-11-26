"""Public local F4 feedback port for an original StockQA paid-settlement projection."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from src.utils._quick_scan_owner_io import deny_network
from src.utils.quick_scan_feedback_observation import (
    FAILED_SETTLED,
    OWNER,
    SETTLED_NO_ANSWER,
    read_feedback_attempt,
)
from src.utils.quick_scan_feedback_registry import (
    feedback_ledger,
    native_observation_retained,
    separate_feedback_state,
)
from src.utils.quick_scan_result_outbox import canonical_bytes, strict_json_loads

_KINDS = ("triage", "repair", "replay", "report", "reconcile")
_EVIDENCE = re.compile(r"qa:([A-Za-z0-9_.-]+)/g([1-9][0-9]*)")
_ATTEMPT = re.compile(r"[A-Za-z0-9_.-]+")
_ISSUE = "qa-settlement/1/"


@dataclass(frozen=True)
class SettlementRef:
    evidence: str
    work_item_id: str
    generation: int
    attempt_id: str

    @classmethod
    def from_refs(cls, evidence: object, attempt: object) -> SettlementRef | None:
        if not isinstance(evidence, str) or not isinstance(attempt, str):
            return None
        match = _EVIDENCE.fullmatch(evidence)
        if match and _ATTEMPT.fullmatch(attempt):
            return cls(evidence, match[1], int(match[2]), attempt)
        return None

    @classmethod
    def from_observation(cls, observation: dict) -> SettlementRef | None:
        # Older rows carry a native attempt in attempt_id. New rows keep that
        # reference separately because the outer run can override attempt_id.
        attempt: object
        details = observation.get("details_ref")
        if isinstance(details, str) and details.startswith("qa-attempt/1/"):
            attempt = details.removeprefix("qa-attempt/1/")
        else:
            attempt = observation.get("attempt_id")
        if observation.get("origin_owner", OWNER) == OWNER:
            return cls.from_refs(observation.get("evidence_ref"), attempt)
        return None

    @classmethod
    def from_issue(cls, value: object) -> SettlementRef | None:
        if isinstance(value, str) and value.startswith(_ISSUE):
            evidence, _, attempt = value.removeprefix(_ISSUE).rpartition("/")
            return cls.from_refs(evidence, attempt)
        return None

    @property
    def issue(self) -> str:
        return f"{_ISSUE}{self.evidence}/{self.attempt_id}"


def _case(row: dict) -> SettlementRef | None:
    if (
        row.get("owner") == OWNER
        and row.get("issue_key") == "settled-answer-replayed"
        and row.get("check_id") == "settled-disposition"
        and row.get("disposition") == "deviation"
        and row.get("expected") == SETTLED_NO_ANSWER
        and row.get("actual") in ("retry_scheduled", "send_uncertain")
    ):
        return SettlementRef.from_observation(row)
    return None


def _read(ledger: Path, case: SettlementRef) -> dict | None:
    from src.utils.quick_scan_refresh_inspect import ReadOnlyWorkView

    try:
        return read_feedback_attempt(
            ReadOnlyWorkView(ledger), case.work_item_id, case.generation, case.attempt_id
        )
    except (KeyError, OSError, sqlite3.Error, ValueError):
        return None


def _settled(ledger: Path, case: SettlementRef) -> dict | None:
    proof = _read(ledger, case)
    if proof is None or proof["status"] != FAILED_SETTLED:
        return None
    return {
        "schema_version": "stockqa.feedback_settlement/1",
        "work_item_id": case.work_item_id,
        "generation": case.generation,
        "attempt_id": case.attempt_id,
        "status": proof["status"],
        "receipt_sha256": proof["receipt_sha256"],
        "settled_reason": proof["settled_reason"],
        "API_requests": 0,
        "new_attempts": 0,
        "new_generations": 0,
    }


def _version(proof: dict) -> str:
    return "stockqa:settlement/1/" + hashlib.sha256(canonical_bytes(proof)).hexdigest()


def _read_action(
    ledger: Path | None,
    state: Path,
    kind: str,
    payload: dict,
    *,
    observation_dir: Path | None,
    log_dir: Path | None = None,
) -> dict:
    def native_path(ref: SettlementRef, observation: dict | None = None) -> Path | None:
        from src.utils.execution_feedback import observation_read_dirs

        origin: tuple[str, str] | None = None
        if observation is not None:
            if "run_id" in observation:
                run, attempt = observation["run_id"], observation.get("attempt_id")
                if (
                    not isinstance(run, str)
                    or not isinstance(attempt, str)
                    or not run
                    or not attempt
                ):
                    return None
                origin = (run, attempt)
            elif observation.get("attempt_id", ref.attempt_id) != ref.attempt_id:
                return None  # A canonical native query never supplies another attempt label.
        if ledger is not None and origin is None:
            return ledger
        directories = observation_read_dirs(os.environ, observation_dir=observation_dir)
        registries = (state, *(directory / "bindings.sqlite" for directory in directories))

        def registered(pair: tuple[str, str] | None) -> Path | None:
            for registry in dict.fromkeys(registries):
                path = feedback_ledger(registry, ref.evidence, ref.attempt_id, origin=pair)
                if path is not None and (ledger is None or path.resolve() == ledger.resolve()):
                    return path
            return None

        path = registered(origin)
        if path is not None or origin is None:
            return path
        if any(
            native_observation_retained(directory, ref.evidence, ref.attempt_id, origin)
            for directory in directories
        ):
            # A retained pre-binding ready record proves the original outer pair.
            # The exact native receipt is still read from the owner below.
            return ledger if ledger is not None else registered(None)
        return None

    row = payload.get("observation", {})
    if kind in {"triage", "reconcile"} and isinstance(row, dict):
        from src.utils.execution_feedback import read_execution

        failure = read_execution(row, os.environ, observation_dir=observation_dir, log_dir=log_dir)
        if failure is not None:
            native = failure["observation"]
            if kind == "triage":
                result = {
                    "decision": "needs_evidence",
                    "issue_ref": native["evidence_ref"],
                    "evidence_ref": native["evidence_ref"],
                    "diagnostic": failure["diagnostic"],
                }
                if "refusal_code" in native:
                    result["gate_review"] = {
                        "decision": "needs_evidence",
                        "reason": "Check the original refusal against a valid input before editing.",
                        "evidence_ref": native["evidence_ref"],
                    }
                return result
            cost = failure["cost_state"]
            return {
                "status": "known" if cost == "not_applicable" else "cost_unknown",
                "job_status": failure["job_status"],
                "cost_state": cost,
                "evidence_ref": native["evidence_ref"],
            }
    if kind == "reconcile":
        case = SettlementRef.from_observation(row)
        path = native_path(case, row) if case else None
        proof = _read(path, case) if path and case else None
        return {
            "status": "known" if proof and proof["cost_state"] == "known" else "cost_unknown",
            "job_status": proof["job_status"] if proof else "unconfirmed",
            "cost_state": proof["cost_state"] if proof else "unknown",
            "evidence_ref": row.get("evidence_ref"),
        }
    if kind == "repair":
        case = SettlementRef.from_issue(payload.get("issue_ref"))
        path = native_path(case) if case else None
        proof = (
            _settled(path, case)
            if path and case and payload.get("generation") == case.attempt_id
            else None
        )
        return (
            {"fixed_version": _version(proof), "publication": proof}
            if proof
            else {"status": "needs_evidence"}
        )
    case = _case(row)
    path = native_path(case, row) if case else None
    proof = _settled(path, case) if path and case else None
    if kind == "triage":
        if case and proof:
            return {"decision": "repair", "issue_ref": case.issue, "generation": case.attempt_id}
        return {"decision": "needs_evidence"}
    if (
        case
        and proof
        and case.issue == payload.get("issue_ref")
        and payload.get("issue_owner") == OWNER
        and payload.get("acquisition") == "reuse_only"
    ):
        version = _version(proof)
        if version == payload.get("fixed_version"):
            return {"verdict": "verified", "verified_version": version, "evidence_ref": version}
    return {"verdict": "unknown"}


def _report(con: sqlite3.Connection, payload: dict) -> dict:
    if payload.get("origin_owner") != OWNER or payload.get("verdict") != "verified":
        return {"recorded": False}
    rows = con.execute(
        "SELECT request, result FROM feedback_actions WHERE kind = 'replay' AND occurrence = ?",
        (payload.get("occurrence_id"),),
    )
    expected = {
        "verdict": "verified",
        "verified_version": payload.get("fixed_version"),
        "evidence_ref": payload.get("evidence_ref"),
    }
    for request, result in rows:
        original = json.loads(request)["payload"]
        if json.loads(result) == expected and all(
            original.get(key) == payload.get(key) for key in ("issue_ref", "original_occurrence_id")
        ):
            return {"recorded": True, "report": payload}
    return {"recorded": False}


def _lookup(state: Path, key: str) -> dict:
    if not state.is_file():
        return {}
    with closing(sqlite3.connect(state.resolve().as_uri() + "?mode=ro", uri=True)) as con:
        if (
            con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='feedback_actions'"
            ).fetchone()
            is None
        ):
            return {}
        row = con.execute(
            "SELECT request, result FROM feedback_actions WHERE id = ?", (key,)
        ).fetchone()
    return {"receipt": {"request": json.loads(row[0]), "result": json.loads(row[1])}} if row else {}


def _validate(request: dict, kind: str) -> None:
    key = request.get("action_id")
    payload = request.get("payload")
    if (
        request.get("schema_version") != "feedback-action/1"
        or request.get("owner") != OWNER
        or request.get("kind") != kind
        or not isinstance(payload, dict)
        or request.get("method") != ("lookup" if kind == "reconcile" else "submit")
        or not isinstance(key, str)
        or re.fullmatch(r"[0-9a-f]{64}", key) is None
    ):
        raise ValueError("invalid owner action")
    wire = json.dumps(
        [OWNER, kind, key, payload],
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if request.get("request_digest") != hashlib.sha256(wire.encode("ascii")).hexdigest():
        raise ValueError("invalid owner action digest")


def _submit(
    ledger: Path | None,
    state: Path,
    kind: str,
    request: dict,
    *,
    observation_dir: Path | None = None,
    log_dir: Path | None = None,
) -> dict:
    _validate(request, kind)
    payload = request["payload"]
    if kind == "reconcile":
        return _read_action(
            ledger, state, kind, payload, observation_dir=observation_dir, log_dir=log_dir
        )
    if ledger is not None:
        separate_feedback_state(state, ledger)
    state.parent.mkdir(parents=True, exist_ok=True)
    raw = canonical_bytes(request)
    key = request["action_id"]
    with closing(sqlite3.connect(state, timeout=30)) as con, con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS feedback_actions (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, occurrence TEXT,
                request BLOB NOT NULL, result BLOB NOT NULL
            )""")
        con.execute("BEGIN IMMEDIATE")
        previous = con.execute(
            "SELECT request,result FROM feedback_actions WHERE id = ?", (key,)
        ).fetchone()
        if previous:
            if previous[0] != raw:
                raise ValueError("action request changed")
            return cast(dict, json.loads(previous[1]))
        result = (
            _report(con, payload)
            if kind == "report"
            else _read_action(
                ledger, state, kind, payload, observation_dir=observation_dir, log_dir=log_dir
            )
        )
        con.execute(
            "INSERT INTO feedback_actions VALUES (?, ?, ?, ?, ?)",
            (key, kind, payload.get("occurrence_id"), raw, canonical_bytes(result)),
        )
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ledger", type=Path, help="explicitly supplied original ledger for historical replay"
    )
    parser.add_argument("--state", type=Path, help="durable local feedback action receipts")
    parser.add_argument(
        "--observation-dir",
        type=Path,
        help="read native failure and settlement records from this existing StockQA inbox",
    )
    parser.add_argument("--log-dir", type=Path, help="original runtime log directory")
    parser.add_argument("--run-id")
    parser.add_argument("--attempt-id")
    parser.add_argument("kind", choices=(*_KINDS, "get-action", "runtime-history"))
    parser.add_argument("action_id", nargs="?")
    args = parser.parse_args(argv)
    sys.addaudithook(deny_network)
    try:
        if args.kind == "runtime-history":
            from src.utils.execution_feedback import runtime_history

            response = runtime_history(
                args.run_id,
                args.attempt_id,
                os.environ,
                observation_dir=args.observation_dir,
                log_dir=args.log_dir,
            )
        elif args.state is None:
            parser.error("--state is required for feedback actions")
        elif args.kind == "get-action":
            response = _lookup(args.state, args.action_id)
        else:
            request = strict_json_loads(sys.stdin.read())
            response = {
                "result": _submit(
                    args.ledger,
                    args.state,
                    args.kind,
                    request,
                    observation_dir=args.observation_dir,
                    log_dir=args.log_dir,
                )
            }
            if args.kind == "reconcile":
                response["state"] = "done"
    except (
        AttributeError,
        KeyError,
        OSError,
        RecursionError,
        TypeError,
        ValueError,
        sqlite3.Error,
    ):
        parser.exit(2, "StockQA feedback action unavailable or invalid\n")
    print(json.dumps(response, ensure_ascii=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
