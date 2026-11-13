"""Original owner intent -> native physical send; no second dispatch ledger.

This immutable join makes the received-before-journal crash window inspectable.
It does not authorize work, settle costs, renew leases or replay private history.
"""

from __future__ import annotations

import contextvars
import functools
import hashlib
import json
import re
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


_ENDPOINT_PROFILE_PREFIX = "stockqa.endpoint-profile/1.0.0:"
_ENDPOINT_PROFILE_RE = re.compile(r"^stockqa\.endpoint-profile/1\.0\.0:[a-f0-9]{64}$")


def endpoint_profile_id(endpoint, *, route_id, provider_config_ref, provider, model):
    """Return a versioned opaque identity for one exact endpoint/route tuple.

    The endpoint is an input to the digest only; callers persist only the
    resulting identifier, never a URL that could contain tenant details.
    """
    values = (endpoint, route_id, provider_config_ref, provider, model)
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("native_endpoint_profile_binding_invalid")
    digest = _hash(
        {
            "protocol": "stockqa.endpoint-profile/1.0.0",
            "endpoint": endpoint,
            "route_id": route_id,
            "provider_config_ref": provider_config_ref,
            "provider": provider,
            "model": model,
        }
    )
    return _ENDPOINT_PROFILE_PREFIX + digest


def is_endpoint_profile_id(value):
    return isinstance(value, str) and _ENDPOINT_PROFILE_RE.fullmatch(value) is not None


@contextmanager
def bind_intent(
    store,
    task_sha256,
    intent_sha256,
    work_item_ids,
    *,
    provided_context=None,
    endpoint_profile_id=None,
):
    from .quick_scan_work_store import QuickScanWorkStore
    from .quick_scan_work_store import api_work_store_sha256 as _sha256

    if not isinstance(store, QuickScanWorkStore) or _INTENT.get() is not None:
        raise ValueError("native_research_intent_binding_invalid")
    _sha256(task_sha256, "research task hash")
    _sha256(intent_sha256, "research intent hash")
    if endpoint_profile_id is not None and not is_endpoint_profile_id(endpoint_profile_id):
        raise ValueError("native_endpoint_profile_binding_invalid")
    if (
        not isinstance(work_item_ids, list)
        or not 1 <= len(work_item_ids) <= 30
        or any(not isinstance(w, str) or not w for w in work_item_ids)
        or len(set(work_item_ids)) != len(work_item_ids)
    ):
        raise ValueError("native_research_intent_members_invalid")
    token = _INTENT.set(
        (
            store,
            task_sha256,
            intent_sha256,
            tuple(sorted(work_item_ids)),
            provided_context,
            endpoint_profile_id,
        )
    )
    try:
        yield
    finally:
        _INTENT.reset(token)


def binding_from_row(row) -> dict:
    """Validate/decode an already loaded immutable row, without additional I/O."""
    members = json.loads(row["work_item_ids_json"])
    binding = {
        key: row[key]
        for key in ("task_sha256", "intent_sha256", "native_attempt_id", "request_payload_sha256")
    }
    binding["work_item_ids"] = members
    provided_raw = row["provided_context_json"] if "provided_context_json" in row.keys() else None
    if provided_raw is not None:
        provided = json.loads(provided_raw)
        if _encode(provided) != provided_raw:
            raise ValueError("native_research_provided_context_corrupt")
        binding["provided_context_sha256"] = _hash(provided)
    if _encode(members) != row["work_item_ids_json"] or _hash(binding) != row["binding_sha256"]:
        raise ValueError("native_research_intent_binding_corrupt")
    return binding


def get_intent_response(store, task_sha256, intent_sha256):
    """Only original DB rows are returned, including a truthful missing output."""
    from .quick_scan_work_store import api_work_store_sha256 as _sha256

    _sha256(task_sha256, "research task hash")
    _sha256(intent_sha256, "research intent hash")
    with closing(store.connect()) as connection:
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
    binding = binding_from_row(row)
    members = binding["work_item_ids"]
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
        store, task_sha, intent_sha, members, provided, endpoint_id = bound
        from .quick_scan_work_transport import (
            current_quick_scan_question_pack_binding,
            current_quick_scan_work_binding,
        )

        work = current_quick_scan_work_binding()
        pack = current_quick_scan_question_pack_binding()
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
        from .quick_scan_work_store import api_work_store_sha256 as _sha256

        if provided is not None:
            from .quick_scan_provided_context import (
                api_provided_context_assert_sent as assert_sent_context,
            )

            assert_sent_context(provided, args[0] if args else kwargs["prompt"])
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
        if provided is not None:
            binding["provided_context_sha256"] = _hash(provided)
        with store.transaction() as connection:
            connection.execute(
                "INSERT INTO quick_scan_research_intent (task_sha256,intent_sha256,native_attempt_id,request_payload_sha256,work_item_ids_json,binding_sha256,provided_context_json) VALUES (?,?,?,?,?,?,?)",
                (
                    task_sha,
                    intent_sha,
                    attempt.attempt_id,
                    request_sha,
                    _encode(list(members)),
                    _hash(binding),
                    None if provided is None else _encode(provided),
                ),
            )
        return attempt

    return wrapped


def anchor_original_receipt(attempt, receipt):
    """Anchor the actual intent association in the ORIGINAL immutable receipt."""
    bound = _INTENT.get()
    if bound is None:
        return
    store, task_sha, intent_sha, _members, _provided, endpoint_id = bound
    with closing(store.connect()) as connection:
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
    if endpoint_id is not None:
        if not is_endpoint_profile_id(endpoint_id):
            raise ValueError("native_endpoint_profile_binding_invalid")
        existing_endpoint = receipt.get("endpoint_profile_id")
        if existing_endpoint not in (None, endpoint_id):
            raise ValueError("native_endpoint_profile_receipt_anchor_conflict")
        receipt["endpoint_profile_id"] = endpoint_id
    receipt["research_intent_binding_sha256"] = row["binding_sha256"]


# Additive, versioned owner exports for cross-module consumers.
MODULE_API_PROTOCOL = "stockqa.utils-quick_scan_research_intent-module/1.2.0"
api_research_intent_encode = _encode
api_research_intent_hash = _hash
api_research_intent_ddl_v23 = DDL_RESEARCH_INTENT
api_research_intent_bind = bind_intent
api_research_intent_binding_from_row = binding_from_row
api_research_intent_get_response = get_intent_response
api_research_intent_bind_native_send = bind_native_send
api_research_intent_anchor_original_receipt = anchor_original_receipt
api_research_intent_endpoint_profile_id = endpoint_profile_id
api_research_intent_is_endpoint_profile_id = is_endpoint_profile_id
STORE_TABLE_DEPENDENCIES = frozenset({"quick_scan_budget_attempt", "quick_scan_research_intent"})
MODULE_API_EXPORTS = frozenset(
    set(globals().get("MODULE_API_EXPORTS", ()))
    | {
        "api_research_intent_encode",
        "api_research_intent_hash",
        "api_research_intent_ddl_v23",
        "api_research_intent_bind",
        "api_research_intent_binding_from_row",
        "api_research_intent_get_response",
        "api_research_intent_bind_native_send",
        "api_research_intent_anchor_original_receipt",
        "api_research_intent_endpoint_profile_id",
        "api_research_intent_is_endpoint_profile_id",
        "STORE_TABLE_DEPENDENCIES",
    }
)
