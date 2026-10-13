"""Immutable physical question packs using the existing logical work ledger.

This module never owns a second execution or budget state machine. Each grouped
operation invokes the released single-item store methods in nested savepoints.
External-context-v1 proofs deliberately remain single-question contracts.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager

_DDL_V17_ADDITIONS = [
    """CREATE TABLE quick_scan_question_pack (
        pack_id TEXT PRIMARY KEY, execution_attempt_id TEXT NOT NULL UNIQUE,
        route_id TEXT NOT NULL, provider TEXT NOT NULL, model_requested TEXT NOT NULL,
        prompt_sha256 TEXT NOT NULL, provider_prompt_sha256 TEXT NOT NULL,
        request_payload_sha256 TEXT NOT NULL,
        model_resolution_json TEXT NOT NULL CHECK(json_valid(model_resolution_json)),
        manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)),
        manifest_sha256 TEXT NOT NULL, member_count INTEGER NOT NULL CHECK(member_count BETWEEN 1 AND 30),
        prepared_at REAL NOT NULL)""",
    """CREATE TABLE quick_scan_question_pack_member (
        pack_id TEXT NOT NULL REFERENCES quick_scan_question_pack(pack_id),
        ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 29),
        work_item_id TEXT NOT NULL REFERENCES work_item(work_item_id),
        question_id TEXT NOT NULL, attempt_id TEXT NOT NULL UNIQUE REFERENCES attempt(attempt_id),
        lease_epoch INTEGER NOT NULL CHECK(lease_epoch>0), lease_token TEXT NOT NULL,
        fingerprint_json TEXT NOT NULL CHECK(json_valid(fingerprint_json)),
        PRIMARY KEY(pack_id,ordinal), UNIQUE(pack_id,work_item_id), UNIQUE(pack_id,question_id))""",
    """CREATE TABLE quick_scan_question_pack_send (
        pack_id TEXT PRIMARY KEY REFERENCES quick_scan_question_pack(pack_id),
        budget_attempt_id TEXT NOT NULL UNIQUE REFERENCES quick_scan_budget_attempt(budget_attempt_id),
        intent_time REAL NOT NULL)""",
    """CREATE TABLE quick_scan_question_pack_outcome (
        pack_id TEXT PRIMARY KEY REFERENCES quick_scan_question_pack_send(pack_id),
        physical_outcome TEXT NOT NULL CHECK(physical_outcome IN ('response_available','unknown','confirmed_failure')),
        members_uncertain INTEGER NOT NULL CHECK(members_uncertain IN (0,1)),
        receipt_json TEXT NOT NULL CHECK(json_valid(receipt_json)), receipt_sha256 TEXT NOT NULL,
        recorded_at REAL NOT NULL)""",
]
for _table in (
    "quick_scan_question_pack",
    "quick_scan_question_pack_member",
    "quick_scan_question_pack_send",
    "quick_scan_question_pack_outcome",
):
    for _action in ("UPDATE", "DELETE"):
        _DDL_V17_ADDITIONS.append(
            f"CREATE TRIGGER {_table}_no_{_action.lower()} BEFORE {_action} ON {_table} "
            f"BEGIN SELECT RAISE(ABORT,'immutable question pack'); END"
        )

DDL_V17_ADDITIONS: tuple[str, ...] = tuple(_DDL_V17_ADDITIONS)

_FINGERPRINT_FIELDS = (
    "entity_id",
    "question_id",
    "generation",
    "scope",
    "scope_id",
    "identity_revision",
    "source_binding_version",
    "identity_state",
    "source_binding_ref",
    "source_binding_refs_json",
    "identity_snapshot_sha256",
    "question_fingerprint",
    "routing_fingerprint",
)
_COMMON_FIELDS = tuple(
    k
    for k in _FINGERPRINT_FIELDS
    if k not in {"question_id", "question_fingerprint", "routing_fingerprint", "generation"}
)


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _sha(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _fingerprint(item):
    return {key: item[key] for key in _FINGERPRINT_FIELDS}


@contextmanager
def _operation(store, pack_id):
    previous = getattr(store._transaction_state, "question_pack_id", None)
    store._transaction_state.question_pack_id = pack_id
    try:
        with store._transaction() as connection:
            yield connection
    finally:
        store._transaction_state.question_pack_id = previous


def guard_individual(connection, store, attempt_id):
    from .quick_scan_work_store import WorkConflictError

    row = connection.execute(
        "SELECT pack_id FROM quick_scan_question_pack_member WHERE attempt_id=?", (attempt_id,)
    ).fetchone()
    if (
        row is not None
        and getattr(store._transaction_state, "question_pack_id", None) != row["pack_id"]
    ):
        raise WorkConflictError("question pack member requires the atomic question pack operation")


def _read(connection, pack_id):
    from .quick_scan_work_store import WorkConflictError

    header = connection.execute(
        "SELECT * FROM quick_scan_question_pack WHERE pack_id=?", (pack_id,)
    ).fetchone()
    if header is None:
        return None
    result = dict(header)
    members = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM quick_scan_question_pack_member WHERE pack_id=? ORDER BY ordinal",
            (pack_id,),
        )
    ]
    manifest = json.loads(result.pop("manifest_json"))
    expected = [
        {
            k: row[k]
            for k in (
                "ordinal",
                "work_item_id",
                "question_id",
                "attempt_id",
                "lease_epoch",
                "lease_token",
                "fingerprint_json",
            )
        }
        for row in members
    ]
    if (
        manifest != expected
        or _sha(manifest) != result["manifest_sha256"]
        or len(members) != result["member_count"]
        or [row["ordinal"] for row in members] != list(range(len(members)))
    ):
        raise WorkConflictError("question pack immutable membership mismatch")
    result["members"] = members
    result["model_resolution"] = json.loads(result.pop("model_resolution_json"))
    _validate_frozen(connection, result)
    send = connection.execute(
        "SELECT * FROM quick_scan_question_pack_send WHERE pack_id=?", (pack_id,)
    ).fetchone()
    result.update(
        budget_attempt_id=None,
        intent_time=None,
        phase="prepared",
        physical_outcome=None,
        members_uncertain=False,
        execution_receipt=None,
        receipt_sha256=None,
    )
    if send is not None:
        result.update(
            budget_attempt_id=send["budget_attempt_id"],
            intent_time=send["intent_time"],
            phase="send_intent",
        )
    outcome = connection.execute(
        "SELECT * FROM quick_scan_question_pack_outcome WHERE pack_id=?", (pack_id,)
    ).fetchone()
    if outcome is not None:
        from .quick_scan_work_store import quick_scan_receipt_sha256

        frozen_receipt = json.loads(outcome["receipt_json"])
        if quick_scan_receipt_sha256(frozen_receipt) != outcome["receipt_sha256"]:
            raise WorkConflictError("question pack physical receipt hash mismatch")
        result.update(
            physical_outcome=outcome["physical_outcome"],
            members_uncertain=bool(outcome["members_uncertain"]),
            phase=(
                "uncertain"
                if outcome["members_uncertain"] or outcome["physical_outcome"] == "unknown"
                else outcome["physical_outcome"]
            ),
            execution_receipt=json.loads(outcome["receipt_json"]),
            receipt_sha256=outcome["receipt_sha256"],
        )
        from .quick_scan_work_store import QuickScanWorkStore

        physical = connection.execute(
            "SELECT * FROM quick_scan_attempt_response WHERE attempt_id=?",
            (result["budget_attempt_id"],),
        ).fetchone()
        if physical is not None:
            physical_record = QuickScanWorkStore._response_record(connection, physical)
            if "native_search_events" in physical_record:
                result["native_search_events"] = physical_record["native_search_events"]
                result["execution_receipt"]["web_search_calls"] = physical_record[
                    "native_search_events"
                ]
    return result


def _validate_frozen(connection, pack):
    from src.providers.model_resolution import model_resolution_sha256

    from .quick_scan_work_store import WorkConflictError, _safe, _sha256

    for key in ("pack_id", "execution_attempt_id", "route_id", "provider", "model_requested"):
        _safe(pack[key], key)
    for key in (
        "prompt_sha256",
        "provider_prompt_sha256",
        "request_payload_sha256",
        "manifest_sha256",
    ):
        _sha256(pack[key], key)
    frozen_resolution_sha = model_resolution_sha256(pack["model_resolution"])
    common = None
    for member in pack["members"]:
        item = connection.execute(
            "SELECT * FROM work_item WHERE work_item_id=?", (member["work_item_id"],)
        ).fetchone()
        attempt = connection.execute(
            "SELECT * FROM attempt WHERE attempt_id=?", (member["attempt_id"],)
        ).fetchone()
        resolution = connection.execute(
            "SELECT policy_sha256 FROM quick_scan_attempt_resolution WHERE attempt_id=?",
            (member["attempt_id"],),
        ).fetchone()
        request_key = "REQ_" + _sha(
            {
                "pack_id": pack["pack_id"],
                "work_item_id": member["work_item_id"],
                "ordinal": member["ordinal"],
                "payload_sha256": pack["request_payload_sha256"],
                "model_resolution": pack["model_resolution"],
            }
        )
        if (
            item is None
            or attempt is None
            or resolution is None
            or member["question_id"] != item["question_id"]
            or item["identity_state"] != "verified"
            or _canonical(_fingerprint(item)) != member["fingerprint_json"]
            or attempt["work_item_id"] != member["work_item_id"]
            or attempt["lease_epoch"] != member["lease_epoch"]
            or attempt["lease_token"] != member["lease_token"]
            or attempt["request_cache_key"] != request_key
            or any(
                attempt[k] != pack[k]
                for k in ("route_id", "provider", "model_requested", "prompt_sha256")
            )
            or resolution["policy_sha256"] != frozen_resolution_sha
        ):
            raise WorkConflictError("question pack frozen request/member identity mismatch")
        actual_common = {k: item[k] for k in _COMMON_FIELDS}
        if common is not None and common != actual_common:
            raise WorkConflictError("question pack frozen company/scope mismatch")
        common = actual_common


def _validate_members(store, connection, pack, *, guard_subject, now):
    from .quick_scan_owner_refresh_journal import guard_send as guard_owner_send
    from .quick_scan_subject_journal import guard_send as guard_subject_send
    from .quick_scan_work_store import Lease, WorkConflictError

    for member in pack["members"]:
        item = store._item(connection, member["work_item_id"])
        store._assert_lease(item, Lease(member["lease_token"], member["lease_epoch"]), now)
        if _canonical(_fingerprint(item)) != member["fingerprint_json"]:
            raise WorkConflictError("question pack work fingerprint changed")
        attempt = connection.execute(
            "SELECT * FROM attempt WHERE attempt_id=?", (member["attempt_id"],)
        ).fetchone()
        if attempt is None or any(
            attempt[k] != pack[k]
            for k in ("route_id", "provider", "model_requested", "prompt_sha256")
        ):
            raise WorkConflictError("question pack request identity changed")
        if (
            attempt["work_item_id"] != member["work_item_id"]
            or attempt["lease_epoch"] != member["lease_epoch"]
            or attempt["lease_token"] != member["lease_token"]
            or attempt["phase"] != pack["phase"]
        ):
            raise WorkConflictError("question pack attempt phase or lease changed")
        if connection.execute(
            "SELECT 1 FROM answer_checkpoint WHERE work_item_id=?", (member["work_item_id"],)
        ).fetchone():
            raise WorkConflictError("question pack member already has an answer checkpoint")
        guard_owner_send(connection, dict(item), now=now)
        if guard_subject:
            guard_subject_send(connection, dict(item), member["attempt_id"])
            row = connection.execute(
                "SELECT receipt_json FROM quick_scan_subject_send_receipt WHERE attempt_id=?",
                (member["attempt_id"],),
            ).fetchone()
            if row is not None:
                request = json.loads(row["receipt_json"])["dispatch_request"]
                if (
                    request["execution_attempt_id"] != pack["execution_attempt_id"]
                    or request["dispatch_prompt_sha256"] != pack["provider_prompt_sha256"]
                ):
                    raise WorkConflictError(
                        "question pack shared subject dispatch identity mismatch"
                    )


def prepare(
    store,
    members,
    *,
    route_id,
    provider,
    model_requested,
    execution_attempt_id,
    prompt_sha256,
    provider_prompt_sha256,
    request_payload_sha256,
    model_resolution=None,
    search_mode="native_search",
):
    from src.providers.model_resolution import normalize_model_resolution

    from .quick_scan_work_store import Lease, WorkConflictError, _safe, _sha256

    if search_mode != "native_search":
        raise ValueError(
            "question packs currently require native_search; external context v1 is single-question"
        )
    if not isinstance(members, (list, tuple)) or not 1 <= len(members) <= 30:
        raise ValueError("question pack requires 1..30 distinct ordered members")
    ids = []
    for member in members:
        if (
            not isinstance(member, dict)
            or set(member) != {"work_item_id", "lease"}
            or not isinstance(member["lease"], Lease)
        ):
            raise ValueError("question pack member requires exact work_item_id and Lease")
        ids.append(_safe(member["work_item_id"], "work_item_id"))
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate question pack work item")
    for key, value in (
        ("route_id", route_id),
        ("provider", provider),
        ("model_requested", model_requested),
        ("execution_attempt_id", execution_attempt_id),
    ):
        _safe(value, key)
    for key, value in (
        ("prompt_sha256", prompt_sha256),
        ("provider_prompt_sha256", provider_prompt_sha256),
        ("request_payload_sha256", request_payload_sha256),
    ):
        _sha256(value, key)
    resolution = normalize_model_resolution(model_resolution)
    pack_id = "PACK_" + uuid.uuid4().hex
    with _operation(store, pack_id) as connection:
        items = [store._item(connection, work_id) for work_id in ids]
        now = store._now()
        for item, member in zip(items, members):
            store._assert_lease(item, member["lease"], now)
            if item["identity_state"] != "verified" or any(
                item[k] != items[0][k] for k in _COMMON_FIELDS
            ):
                raise WorkConflictError(
                    "question pack requires one verified company, scope and frozen identity"
                )
        if len({item["question_id"] for item in items}) != len(items):
            raise ValueError("duplicate question pack question id")
        if connection.execute(
            "SELECT 1 FROM quick_scan_question_pack WHERE execution_attempt_id=?",
            (execution_attempt_id,),
        ).fetchone():
            raise WorkConflictError("question pack physical execution id was already prepared")
        rows = []
        for ordinal, (item, member) in enumerate(zip(items, members)):
            if connection.execute(
                "SELECT 1 FROM answer_checkpoint WHERE work_item_id=?", (item["work_item_id"],)
            ).fetchone():
                raise WorkConflictError("question pack member already has an answer checkpoint")
            request_key = "REQ_" + _sha(
                {
                    "pack_id": pack_id,
                    "work_item_id": item["work_item_id"],
                    "ordinal": ordinal,
                    "payload_sha256": request_payload_sha256,
                    "model_resolution": resolution,
                }
            )
            attempt = store.prepare_attempt(
                item["work_item_id"],
                member["lease"],
                route_id=route_id,
                provider=provider,
                model_requested=model_requested,
                request_cache_key=request_key,
                prompt_sha256=prompt_sha256,
                model_resolution=resolution,
            )
            rows.append(
                dict(
                    ordinal=ordinal,
                    work_item_id=item["work_item_id"],
                    question_id=item["question_id"],
                    attempt_id=attempt["attempt_id"],
                    lease_epoch=member["lease"].lease_epoch,
                    lease_token=member["lease"].lease_token,
                    fingerprint_json=_canonical(_fingerprint(item)),
                )
            )
        connection.execute(
            "INSERT INTO quick_scan_question_pack VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                pack_id,
                execution_attempt_id,
                route_id,
                provider,
                model_requested,
                prompt_sha256,
                provider_prompt_sha256,
                request_payload_sha256,
                _canonical(resolution),
                _canonical(rows),
                _sha(rows),
                len(rows),
                now,
            ),
        )
        for row in rows:
            connection.execute(
                "INSERT INTO quick_scan_question_pack_member VALUES (?,?,?,?,?,?,?,?)",
                (
                    pack_id,
                    row["ordinal"],
                    row["work_item_id"],
                    row["question_id"],
                    row["attempt_id"],
                    row["lease_epoch"],
                    row["lease_token"],
                    row["fingerprint_json"],
                ),
            )
        return _read(connection, pack_id)


def mark(store, pack_id, *, budget_policy, budget_route):
    from .quick_scan_work_store import Lease, WorkConflictError

    with _operation(store, pack_id) as connection:
        pack = _read(connection, pack_id)
        if pack is None or pack["phase"] != "prepared":
            raise WorkConflictError(
                "question pack is not prepared; a durable send intent cannot be repeated"
            )
        _validate_members(store, connection, pack, guard_subject=True, now=store._now())
        if not isinstance(budget_route, dict) or any(
            budget_route.get(k) != pack[k] for k in ("route_id", "provider", "model_requested")
        ):
            raise WorkConflictError("question pack budget route mismatch")
        budget_id = "PACK_BUDGET_" + pack_id.removeprefix("PACK_")
        store.reserve_budget_attempt(
            budget_policy,
            budget_attempt_id=budget_id,
            **budget_route,
            model_resolution=pack["model_resolution"],
        )
        for row in pack["members"]:
            store.mark_send_intent(
                row["work_item_id"],
                Lease(row["lease_token"], row["lease_epoch"]),
                row["attempt_id"],
            )
        now = store._now()
        connection.execute(
            "INSERT INTO quick_scan_question_pack_send VALUES (?,?,?)", (pack_id, budget_id, now)
        )
        return _read(connection, pack_id)


def validate_for_post(store, pack_id):
    from .quick_scan_work_store import WorkConflictError

    with store._transaction() as connection:
        pack = _read(connection, pack_id)
        if pack is None or pack["phase"] != "send_intent":
            raise WorkConflictError("question pack has no unresolved send intent")
        _validate_members(store, connection, pack, guard_subject=True, now=store._now())
        return pack


def record(
    store,
    pack_id,
    *,
    outcome,
    execution_receipt,
    http_status_code=None,
    request_id=None,
    failure_category=None,
    provider_error_code=None,
    actual_cost=None,
    cost_source_ref=None,
):
    from src.providers.model_resolution import (
        model_resolution_allowed,
        model_resolution_sha256,
    )

    from .quick_scan_subject_journal import guard_response
    from .quick_scan_work_store import (
        Lease,
        LeaseFencedError,
        WorkConflictError,
        _sanitized_receipt,
        quick_scan_receipt_sha256,
    )

    with _operation(store, pack_id) as connection:
        pack = _read(connection, pack_id)
        if pack is None or pack["phase"] != "send_intent":
            raise WorkConflictError("question pack has no unresolved send intent")
        if not isinstance(execution_receipt, dict):
            raise ValueError("question pack requires original physical provider receipt")
        receipt = _sanitized_receipt(execution_receipt)
        if (
            any(
                receipt.get(k) != pack[v]
                for k, v in (
                    ("attempt_id", "execution_attempt_id"),
                    ("prompt_sha256", "provider_prompt_sha256"),
                    ("request_payload_sha256", "request_payload_sha256"),
                    ("requested_model", "model_requested"),
                )
            )
            or receipt.get("provider") not in {"mimo", "minimax", "openai", "deepseek"}
            or receipt.get("model_resolution_sha256")
            != model_resolution_sha256(pack["model_resolution"])
        ):
            raise WorkConflictError("question pack physical provider receipt identity mismatch")
        if receipt.get("http_status_code") != http_status_code:
            raise WorkConflictError("question pack HTTP outcome conflicts with original receipt")
        actual_provider = receipt.get("provider")
        search_protocol = receipt.get("search_protocol")
        if outcome == "response_available" and (
            not isinstance(actual_provider, str)
            or not isinstance(search_protocol, str)
            or not model_resolution_allowed(
                actual_provider,
                search_protocol,
                pack["model_requested"],
                receipt.get("actual_model"),
                pack["model_resolution"],
            )
        ):
            raise WorkConflictError("question pack actual model does not match frozen resolution")
        if request_id is not None and request_id != receipt.get("request_id"):
            raise WorkConflictError("question pack request id conflicts with original receipt")
        request_id = receipt.get("request_id")
        receipt_sha = quick_scan_receipt_sha256(receipt)
        now = store._now()
        late = False
        for row in pack["members"]:
            guard_response(connection, row["attempt_id"], receipt)
            try:
                store._assert_lease(
                    store._item(connection, row["work_item_id"]),
                    Lease(row["lease_token"], row["lease_epoch"]),
                    now,
                )
            except LeaseFencedError:
                late = True
        for row in pack["members"]:
            lease = Lease(row["lease_token"], row["lease_epoch"])
            try:
                store._assert_lease(store._item(connection, row["work_item_id"]), lease, now)
            except LeaseFencedError:
                store.recover_expired(row["work_item_id"])
                store.note_late_receipt(
                    row["work_item_id"],
                    lease,
                    row["attempt_id"],
                    receipt_sha256=receipt_sha,
                    execution_receipt=execution_receipt,
                )
            else:
                store.record_attempt_outcome(
                    row["work_item_id"],
                    lease,
                    row["attempt_id"],
                    outcome="unknown" if late else outcome,
                    execution_receipt=execution_receipt,
                    http_status_code=http_status_code,
                    receipt_sha256=receipt_sha,
                    request_id=request_id,
                    failure_category=failure_category,
                    provider_error_code=provider_error_code,
                )
        store.record_budget_outcome(
            pack["budget_attempt_id"],
            outcome=outcome,
            http_status_code=http_status_code,
            actual_cost=actual_cost,
            cost_source_ref=cost_source_ref,
            execution_receipt=execution_receipt,
        )
        connection.execute(
            "INSERT INTO quick_scan_question_pack_outcome VALUES (?,?,?,?,?,?)",
            (
                pack_id,
                outcome,
                int(late or outcome == "unknown"),
                _canonical(receipt),
                receipt_sha,
                now,
            ),
        )
        return _read(connection, pack_id)


def get(store, pack_id):
    from .quick_scan_work_store import _safe

    _safe(pack_id, "pack_id")
    with store._transaction() as connection:
        return _read(connection, pack_id)


def validate_tables(connection):
    for row in connection.execute("SELECT pack_id FROM quick_scan_question_pack"):
        pack = _read(connection, row["pack_id"])
        for member in pack["members"]:
            attempt = connection.execute(
                "SELECT * FROM attempt WHERE attempt_id=?", (member["attempt_id"],)
            ).fetchone()
            if attempt is None or attempt["work_item_id"] != member["work_item_id"]:
                raise ValueError("question pack logical attempt mismatch")
