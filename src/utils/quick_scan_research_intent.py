"""Original owner intent -> native physical send; no second dispatch ledger.

This immutable join makes the received-before-journal crash window inspectable.
It does not authorize work, settle costs, renew leases or replay private history.
"""

from __future__ import annotations

import contextvars
import functools
import hashlib
import json
from contextlib import closing, contextmanager

DDL_RESEARCH_INTENT = (
    """CREATE TABLE quick_scan_research_intent (
        task_sha256 TEXT NOT NULL,
        intent_sha256 TEXT NOT NULL,
        native_attempt_id TEXT NOT NULL REFERENCES quick_scan_budget_attempt(budget_attempt_id),
        request_payload_sha256 TEXT NOT NULL,
        work_item_ids_json TEXT NOT NULL,
        binding_sha256 TEXT NOT NULL,
        PRIMARY KEY(task_sha256,intent_sha256),
        UNIQUE(native_attempt_id))""",
    """CREATE TRIGGER quick_scan_research_intent_no_update BEFORE UPDATE ON quick_scan_research_intent
        BEGIN SELECT RAISE(ABORT,'research intent is immutable'); END""",
    """CREATE TRIGGER quick_scan_research_intent_no_delete BEFORE DELETE ON quick_scan_research_intent
        BEGIN SELECT RAISE(ABORT,'research intent is immutable'); END""",
)
_INTENT: contextvars.ContextVar[tuple | None] = contextvars.ContextVar(
    "original_owner_research_intent", default=None
)


def _encode(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    )


def _hash(value):
    return hashlib.sha256(_encode(value).encode("utf8")).hexdigest()


@contextmanager
def bind_intent(store, task_sha256, intent_sha256, work_item_ids):
    from .quick_scan_work_store import QuickScanWorkStore, _sha256

    if not isinstance(store, QuickScanWorkStore) or _INTENT.get() is not None:
        raise ValueError("native_research_intent_binding_invalid")
    _sha256(task_sha256, "research task hash")
    _sha256(intent_sha256, "research intent hash")
    if (
        not isinstance(work_item_ids, list)
        or not 1 <= len(work_item_ids) <= 30
        or any(not isinstance(w, str) or not w for w in work_item_ids)
        or len(set(work_item_ids)) != len(work_item_ids)
    ):
        raise ValueError("native_research_intent_members_invalid")
    token = _INTENT.set((store, task_sha256, intent_sha256, tuple(sorted(work_item_ids))))
    try:
        yield
    finally:
        _INTENT.reset(token)


def get_intent_response(store, task_sha256, intent_sha256):
    """Only original DB rows are returned, including a truthful missing output."""
    from .quick_scan_work_store import _sha256

    _sha256(task_sha256, "research task hash")
    _sha256(intent_sha256, "research intent hash")
    with closing(store._connect()) as connection:
        row = connection.execute(
            "SELECT * FROM quick_scan_research_intent WHERE task_sha256=? AND intent_sha256=?",
            (task_sha256, intent_sha256),
        ).fetchone()
        single = (
            None
            if row is None
            else connection.execute(
                "SELECT work_item_id FROM attempt WHERE attempt_id=?", (row["native_attempt_id"],)
            ).fetchone()
        )
        pack = (
            None
            if row is None
            else connection.execute(
                "SELECT pack_id FROM quick_scan_question_pack_send WHERE budget_attempt_id=?",
                (row["native_attempt_id"],),
            ).fetchone()
        )
    if row is None:
        return None
    members = json.loads(row["work_item_ids_json"])
    binding = {
        key: row[key]
        for key in ("task_sha256", "intent_sha256", "native_attempt_id", "request_payload_sha256")
    }
    binding["work_item_ids"] = members
    if _encode(members) != row["work_item_ids_json"] or _hash(binding) != row["binding_sha256"]:
        raise ValueError("native_research_intent_binding_corrupt")
    if (single is None) == (pack is None):
        raise ValueError("native_research_intent_original_members_missing_or_ambiguous")
    if pack is not None:
        original_pack = store.get_question_pack(pack["pack_id"])
        actual = sorted(m["work_item_id"] for m in original_pack["members"])
        if original_pack["request_payload_sha256"] != row["request_payload_sha256"]:
            raise ValueError("native_research_intent_original_pack_payload_mismatch")
    else:
        if single is None:
            raise ValueError("native_research_intent_original_single_missing")
        actual = [single["work_item_id"]]
    if members != actual:
        raise ValueError("native_research_intent_original_members_mismatch")
    original = store.get_attempt_response(row["native_attempt_id"])
    if (
        original is not None
        and original["receipt"].get("request_payload_sha256") != row["request_payload_sha256"]
    ):
        raise ValueError("native_research_intent_response_mismatch")
    if (
        original is not None
        and original["receipt"].get("research_intent_binding_sha256") != row["binding_sha256"]
    ):
        raise ValueError("native_research_intent_original_anchor_mismatch")
    output = store.get_research_visible_output(row["native_attempt_id"])
    return dict(
        binding,
        native_receipt=None if original is None else original["receipt"],
        visible_output=output,
    )


def bind_native_send(function):
    """Keep the original send boundary; attach only after its native admission."""

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        bound = _INTENT.get()
        if bound is None:
            return function(*args, **kwargs)
        store, task_sha, intent_sha, members = bound
        from .quick_scan_work_transport import _QUESTION_PACK_BINDING, _WORK_BINDING

        work, pack = _WORK_BINDING.get(), _QUESTION_PACK_BINDING.get()
        if work is not None:
            if work.store is not store or (work.work_item_id,) != members:
                raise ValueError("native_research_intent_active_work_mismatch")
        elif pack is not None:
            if pack.store is not store or tuple(sorted(w for w, _ in pack.members)) != members:
                raise ValueError("native_research_intent_active_pack_mismatch")
        else:
            raise ValueError("native_research_intent_original_owner_work_required")
        if get_intent_response(store, task_sha, intent_sha) is not None:
            raise ValueError("native_research_intent_already_sent_inspect_original")
        from .quick_scan_work_store import _sha256

        request_sha = kwargs.get("request_payload_sha256")
        _sha256(request_sha, "research request hash")
        attempt = function(*args, **kwargs)
        if (
            attempt is None
            or attempt.budget_binding is None
            or attempt.budget_binding.store is not store
        ):
            raise ValueError("native_research_intent_original_budget_required")
        if attempt.binding is not None:
            actual = (attempt.binding.work_item_id,)
        elif attempt.question_pack_binding is not None:
            actual = tuple(sorted(work_id for work_id, _ in attempt.question_pack_binding.members))
        else:
            raise ValueError("native_research_intent_original_owner_work_required")
        if actual != members:
            raise ValueError("native_research_intent_original_members_mismatch")
        binding = dict(
            task_sha256=task_sha,
            intent_sha256=intent_sha,
            native_attempt_id=attempt.attempt_id,
            request_payload_sha256=request_sha,
            work_item_ids=list(members),
        )
        with store._transaction() as connection:
            connection.execute(
                "INSERT INTO quick_scan_research_intent VALUES (?,?,?,?,?,?)",
                (
                    task_sha,
                    intent_sha,
                    attempt.attempt_id,
                    request_sha,
                    _encode(list(members)),
                    _hash(binding),
                ),
            )
        return attempt

    return wrapped


def anchor_original_receipt(attempt, receipt):
    """Anchor the actual intent association in the ORIGINAL immutable receipt."""
    bound = _INTENT.get()
    if bound is None:
        return
    store, task_sha, intent_sha, _members = bound
    with closing(store._connect()) as connection:
        row = connection.execute(
            "SELECT * FROM quick_scan_research_intent WHERE task_sha256=? AND intent_sha256=? AND native_attempt_id=?",
            (task_sha, intent_sha, attempt.attempt_id),
        ).fetchone()
    if (
        row is None
        or not isinstance(receipt, dict)
        or receipt.get("request_payload_sha256") != row["request_payload_sha256"]
    ):
        raise ValueError("native_research_intent_receipt_anchor_missing")
    if (
        receipt.get("research_intent_binding_sha256", row["binding_sha256"])
        != row["binding_sha256"]
    ):
        raise ValueError("native_research_intent_receipt_anchor_conflict")
    receipt["research_intent_binding_sha256"] = row["binding_sha256"]
