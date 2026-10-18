"""Explicit bounded agent turns inside the ORIGINAL native work database.

This records no conversation, answers, accounting or independent owner grant.
Every turn still goes through the native lease/owner/subject/budget/send guards.
Only a validated, durable visible response may precede a continuation.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import uuid
from contextlib import contextmanager

DDL_RESEARCH_SESSION = (
    """CREATE TABLE quick_scan_research_session (
        session_id TEXT PRIMARY KEY,
        work_item_id TEXT NOT NULL REFERENCES work_item(work_item_id),
        lease_epoch INTEGER NOT NULL,
        lease_token TEXT NOT NULL,
        registration_json TEXT NOT NULL,
        registration_sha256 TEXT NOT NULL,
        created_at REAL NOT NULL,
        UNIQUE(work_item_id,lease_epoch))""",
    """CREATE TABLE quick_scan_research_turn (
        session_id TEXT NOT NULL REFERENCES quick_scan_research_session(session_id),
        turn_index INTEGER NOT NULL CHECK(turn_index>=1),
        attempt_id TEXT NOT NULL UNIQUE REFERENCES attempt(attempt_id),
        request_cache_key TEXT NOT NULL,
        intent_sha256 TEXT NOT NULL,
        PRIMARY KEY(session_id,turn_index),
        UNIQUE(session_id,intent_sha256),
        UNIQUE(session_id,request_cache_key))""",
    """CREATE TRIGGER quick_scan_research_session_no_update BEFORE UPDATE ON quick_scan_research_session
        BEGIN SELECT RAISE(ABORT,'research session is immutable'); END""",
    """CREATE TRIGGER quick_scan_research_session_no_delete BEFORE DELETE ON quick_scan_research_session
        BEGIN SELECT RAISE(ABORT,'research session is immutable'); END""",
    """CREATE TRIGGER quick_scan_research_turn_no_update BEFORE UPDATE ON quick_scan_research_turn
        BEGIN SELECT RAISE(ABORT,'research turn is immutable'); END""",
    """CREATE TRIGGER quick_scan_research_turn_no_delete BEFORE DELETE ON quick_scan_research_turn
        BEGIN SELECT RAISE(ABORT,'research turn is immutable'); END""",
)
_ACTIVE: contextvars.ContextVar[tuple | None] = contextvars.ContextVar(
    "native_research_session", default=None
)


def _encode(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def _sha(value):
    return hashlib.sha256(_encode(value).encode("utf8")).hexdigest()


def _fail(reason):
    from .quick_scan_work_store import WorkConflictError

    raise WorkConflictError("native_research_session_" + reason)


def register(
    store,
    owner,
    work_item_id,
    lease,
    *,
    task_sha256,
    route_id,
    provider,
    model_requested,
    max_turns=24,
    model_resolution=None,
    provider_actual=None,
):
    """Register once per actual native work/lease epoch, before its first send.

    An actual original owner session is mandatory. Later calls may recover the
    identical registration; they cannot renew the turn cap or replace inputs.
    """
    from src.providers.model_resolution import model_resolution_sha256

    from .quick_scan_owner_refresh import OwnerRefreshSession
    from .quick_scan_refresh_execution import ApprovedRefreshSession
    from .quick_scan_work_store import QuickScanWorkStore, _sha256

    native = owner.native if isinstance(owner, ApprovedRefreshSession) else owner
    if (
        not isinstance(store, QuickScanWorkStore)
        or not isinstance(native, OwnerRefreshSession)
        or native.store is not store
    ):
        _fail("actual_owner_required")
    if isinstance(owner, ApprovedRefreshSession) and owner.service.store is not store:
        _fail("actual_owner_store_mismatch")
    if type(max_turns) is not int or not 1 <= max_turns <= 32:
        _fail("turn_limit_invalid")
    _sha256(task_sha256, "task_sha256")
    owner.check()
    with store._transaction() as connection:
        item = store._item(connection, work_item_id)
        store._assert_lease(item, lease, store._now())
        qid = item["question_id"]
        if qid not in owner.fields:
            _fail("question_not_approved")
        row = connection.execute(
            "SELECT * FROM quick_scan_owner_refresh_binding WHERE work_item_id=?", (work_item_id,)
        ).fetchone()
        # Read through the native owner getter so its own integrity checks run.
        binding = store.get_owner_refresh_binding(work_item_id)
        if row is None or binding is None:
            _fail("owner_binding_missing")
        bound = binding["binding"]
        for key in (
            "manifest_raw_sha256",
            "decision_id",
            "anchor_version",
            "subject_key",
            "provider",
            "model",
        ):
            if bound.get(key) != owner.plan.get(key):
                _fail("owner_binding_mismatch")
        actual = provider if provider_actual is None else provider_actual
        if actual != bound.get("provider") or model_requested != bound.get("model"):
            _fail("owner_route_mismatch")
        context = store.get_observation_context(work_item_id)
        if context is None or _sha(context["context"]) != _sha(native.observation_context):
            _fail("observation_context_mismatch")
        registration = {
            "protocol": "stockqa.native-research-session/1.0.0",
            "work_item_id": work_item_id,
            "task_sha256": task_sha256,
            "owner_binding_sha256": _sha(bound),
            "observation_context_sha256": _sha(context["context"]),
            "route_id": route_id,
            "provider": provider,
            "provider_actual": actual,
            "model_requested": model_requested,
            "model_resolution_sha256": model_resolution_sha256(model_resolution),
            "max_turns": max_turns,
        }
        encoded = _encode(registration)
        prior = connection.execute(
            "SELECT * FROM quick_scan_research_session WHERE work_item_id=? AND lease_epoch=?",
            (work_item_id, lease.lease_epoch),
        ).fetchone()
        if prior is not None:
            if (
                prior["lease_token"] != lease.lease_token
                or prior["registration_json"] != encoded
                or prior["registration_sha256"] != _sha(registration)
            ):
                _fail("registration_conflict")
            return prior["session_id"]
        if connection.execute(
            "SELECT 1 FROM attempt WHERE work_item_id=? LIMIT 1", (work_item_id,)
        ).fetchone():
            _fail("cannot_adopt_prior_attempts")
        session_id = "RSESSION_" + uuid.uuid4().hex
        connection.execute(
            "INSERT INTO quick_scan_research_session VALUES (?,?,?,?,?,?,?)",
            (
                session_id,
                work_item_id,
                lease.lease_epoch,
                lease.lease_token,
                encoded,
                _sha(registration),
                store._now(),
            ),
        )
    owner.check()
    return session_id


@contextmanager
def bind(store, work_item_id, lease, session_id, *, intent_sha256):
    """Bind only this native work; never applies to budget-only or pack sends."""
    if _ACTIVE.get() is not None:
        _fail("nested_binding")
    from .quick_scan_work_store import _sha256

    _sha256(intent_sha256, "research intent hash")
    value = (store, work_item_id, lease, session_id, intent_sha256)
    token = _ACTIVE.set(value)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def active_id(store, work_item_id, lease):
    value = _ACTIVE.get()
    if value is None:
        return None
    if value[:3] != (store, work_item_id, lease):
        _fail("active_work_mismatch")
    return value[3]


def guard_turn(
    store,
    connection,
    item,
    lease,
    previous,
    *,
    session_id,
    route_id,
    provider,
    model_requested,
    request_cache_key,
    model_resolution,
    intent_sha256,
):
    """Run in prepare_attempt's SAME transaction before allocating the attempt."""
    from src.providers.model_resolution import model_resolution_sha256

    from .quick_scan_work_store import _sha256

    _sha256(intent_sha256, "research intent hash")
    from .quick_scan_research_output import read

    row = connection.execute(
        "SELECT * FROM quick_scan_research_session WHERE session_id=?", (session_id,)
    ).fetchone()
    if (
        row is None
        or row["work_item_id"] != item["work_item_id"]
        or row["lease_epoch"] != lease.lease_epoch
        or row["lease_token"] != lease.lease_token
    ):
        _fail("registration_not_current")
    reg = json.loads(row["registration_json"])
    if _sha(reg) != row["registration_sha256"] or _encode(reg) != row["registration_json"]:
        _fail("registration_corrupt")
    for key, value in (
        ("route_id", route_id),
        ("provider", provider),
        ("model_requested", model_requested),
        ("model_resolution_sha256", model_resolution_sha256(model_resolution)),
    ):
        if reg.get(key) != value:
            _fail("route_or_resolution_changed")
    turns = connection.execute(
        "SELECT * FROM quick_scan_research_turn WHERE session_id=? ORDER BY turn_index",
        (session_id,),
    ).fetchall()
    if len(turns) >= reg["max_turns"] or [t["turn_index"] for t in turns] != list(
        range(1, len(turns) + 1)
    ):
        _fail("turn_limit_or_chain_invalid")
    if any(t["intent_sha256"] == intent_sha256 for t in turns):
        _fail("intent_already_sent_inspect_original")
    if any(t["request_cache_key"] == request_cache_key for t in turns):
        _fail("duplicate_request")
    if (previous is None) != (not turns) or (
        previous is not None and turns[-1]["attempt_id"] != previous["attempt_id"]
    ):
        _fail("original_attempt_chain_mismatch")
    if previous is not None:
        if (
            previous["lease_epoch"] != lease.lease_epoch
            or previous["lease_token"] != lease.lease_token
            or previous["phase"] != "response_available"
        ):
            _fail("previous_response_unknown_or_unusable")
        output = read(store, connection, previous["attempt_id"])
        if output is None:
            _fail("previous_visible_output_missing")
    return len(turns) + 1


def attach_turn(connection, session_id, turn_index, attempt_id, request_cache_key, intent_sha256):
    connection.execute(
        "INSERT INTO quick_scan_research_turn VALUES (?,?,?,?,?)",
        (session_id, turn_index, attempt_id, request_cache_key, intent_sha256),
    )


def active_intent_sha256():
    value = _ACTIVE.get()
    return None if value is None else value[4]


def find_attempt_for_intent(store, session_id, intent_sha256):
    """Original native attempt lookup only. This function never dispatches.

    The first-party owner adapter scopes the session to its own task/work before
    calling; a bare SDK ID or caller-supplied response is never an answer proof.
    """
    from contextlib import closing

    from .quick_scan_research_output import read
    from .quick_scan_work_store import _sha256

    _sha256(intent_sha256, "research intent hash")
    with closing(store._connect()) as connection:
        row = connection.execute(
            "SELECT t.attempt_id FROM quick_scan_research_turn t JOIN quick_scan_research_session s USING(session_id) WHERE s.session_id=? AND t.intent_sha256=?",
            (session_id, intent_sha256),
        ).fetchone()
        if row is None:
            return None
        attempt = connection.execute(
            "SELECT * FROM attempt WHERE attempt_id=?", (row["attempt_id"],)
        ).fetchone()
        output = read(store, connection, row["attempt_id"])
        return {
            "attempt": {
                key: attempt[key]
                for key in (
                    "attempt_id",
                    "work_item_id",
                    "phase",
                    "route_id",
                    "provider",
                    "model_requested",
                    "prompt_sha256",
                    "request_cache_key",
                    "ordinal",
                )
            },
            "visible_output": output,
        }


def find_attempt_for_task_intent(store, work_item_id, task_sha256, intent_sha256):
    """Recover the session join from native rows, never a process-local handle."""
    from contextlib import closing

    from .quick_scan_work_store import _sha256

    _sha256(task_sha256, "research task hash")
    _sha256(intent_sha256, "research intent hash")
    with closing(store._connect()) as connection:
        rows = connection.execute(
            "SELECT DISTINCT s.* FROM quick_scan_research_session s "
            "JOIN quick_scan_research_turn t USING(session_id) "
            "WHERE s.work_item_id=? AND t.intent_sha256=?",
            (work_item_id, intent_sha256),
        ).fetchall()
    matches = []
    for row in rows:
        registration = json.loads(row["registration_json"])
        if (
            _encode(registration) != row["registration_json"]
            or _sha(registration) != row["registration_sha256"]
        ):
            _fail("registration_corrupt")
        if registration.get("task_sha256") == task_sha256:
            matches.append(row["session_id"])
    if len(matches) > 1:
        _fail("ambiguous_original_intent")
    if not matches:
        return None
    return find_attempt_for_intent(store, matches[0], intent_sha256)
