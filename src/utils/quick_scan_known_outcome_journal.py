"""Owner-authorized logical resolutions over immutable, settled native receipts.

No HTTP, new work generation, fee ledger or provider-failure classification lives
here. This candidate requires a trusted deployment owner port; a consumer dict
is never an approval. The production StockWiki confirmation adapter is separate.
"""

from __future__ import annotations

import copy
import math
import re
import sqlite3
from abc import ABC, abstractmethod
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from .quick_scan_result_outbox import (
    canonical_bytes,
    canonical_sha256,
    strict_json_loads,
)

DDL_V19_ADDITIONS = (
    """CREATE TABLE quick_scan_owner_work_decision (
        decision_id TEXT PRIMARY KEY, work_item_id TEXT NOT NULL REFERENCES work_item(work_item_id),
        attempt_id TEXT NOT NULL REFERENCES attempt(attempt_id),
        kind TEXT NOT NULL CHECK(kind IN ('known_response_resolution','retry_decision')),
        idempotency_key TEXT NOT NULL,
        document_json TEXT NOT NULL CHECK(json_valid(document_json)), document_sha256 TEXT NOT NULL,
        decided_at REAL NOT NULL, UNIQUE(work_item_id,idempotency_key))""",
    """CREATE TRIGGER quick_scan_owner_work_decision_no_update
       BEFORE UPDATE ON quick_scan_owner_work_decision
       BEGIN SELECT RAISE(ABORT,'owner work decision is immutable'); END""",
    """CREATE TRIGGER quick_scan_owner_work_decision_no_delete
       BEFORE DELETE ON quick_scan_owner_work_decision
       BEGIN SELECT RAISE(ABORT,'owner work decision is immutable'); END""",
)


@dataclass(frozen=True)
class OwnerWorkDecisionGrant:
    """An immutable response from the configured first-party owner port.

    Content hashes are integrity checks, not authentication. Only the trusted
    port may authenticate its operator and existing durable confirmation.
    """

    owner_store_id: str
    principal_id: str
    confirmation_id: str
    action_id: str
    request_sha256: str
    confirmed_at: str
    comparison_target: dict | None = None


class OwnerWorkDecisionAuthority(ABC):
    """Trusted deployment adapter, never constructed from model/request JSON.

    It must independently read the original owner principal, confirmation,
    subject/current anchor, field permission, request action and policy. The
    same request is re-authorized before commit to detect permission changes.
    An implementation must not call a paid provider or mutate an owner DB.
    """

    @abstractmethod
    def authorize(self, decision_request: dict) -> OwnerWorkDecisionGrant:
        raise NotImplementedError


def _utc(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


def _safe(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value):
        raise ValueError("owner_work_" + label + "_invalid")
    return value


def _sha(value):
    if not isinstance(value, str) or re.fullmatch("[a-f0-9]{64}", value) is None:
        raise ValueError("owner_work_hash_invalid")
    return value


def validate_comparison_target(target, request):
    """A trusted port's explicit target; never a consumer permission flag."""
    if (
        not isinstance(target, dict)
        or set(target)
        != {
            "provider",
            "model",
            "route_id",
            "provider_config_ref",
            "policy_id",
            "owner_policy_sha256",
        }
        or request.get("kind") != "retry_decision"
        or request.get("purpose") != "comparison"
    ):
        raise ValueError("owner_work_comparison_target_invalid")
    for key in ("provider", "model", "route_id", "provider_config_ref", "policy_id"):
        _safe(target[key], "comparison_target_" + key)
    _sha(target["owner_policy_sha256"])
    if target["policy_id"] != request["physical"]["budget_policy_id"]:
        raise ValueError("owner_work_comparison_target_policy_mismatch")
    return copy.deepcopy(target)


def _authorize(authority, request, now):
    if not isinstance(authority, OwnerWorkDecisionAuthority):
        raise TypeError("owner_work_trusted_authority_required")
    grant = authority.authorize(copy.deepcopy(request))
    if not isinstance(grant, OwnerWorkDecisionGrant):
        raise TypeError("owner_work_grant_required")
    for field in ("owner_store_id", "principal_id", "action_id"):
        _safe(getattr(grant, field), field)
    if not re.fullmatch("rc1_[a-f0-9]{64}", grant.confirmation_id):
        raise ValueError("owner_work_confirmation_invalid")
    if grant.request_sha256 != canonical_sha256(request):
        raise ValueError("owner_work_authorized_request_mismatch")
    stamp = datetime.fromisoformat(grant.confirmed_at.replace("Z", "+00:00"))
    if stamp.utcoffset() is None or stamp.timestamp() > now:
        raise ValueError("owner_work_confirmation_future")
    result = asdict(grant)
    if grant.comparison_target is None:
        # The original grant/document byte shape is unchanged.
        result.pop("comparison_target")
    else:
        result["comparison_target"] = validate_comparison_target(grant.comparison_target, request)
    return result


def _physical(store, con, item, attempt):
    """Re-derive settled physical evidence, not a caller receipt or status flag."""
    from .quick_scan_question_pack_journal import _fingerprint, _read
    from .quick_scan_subject_journal import (
        bound_context,
        guard_response,
        receipt_record,
    )
    from .quick_scan_work_store import WorkConflictError

    latest = con.execute(
        "SELECT attempt_id FROM attempt WHERE work_item_id=? ORDER BY ordinal DESC LIMIT 1",
        (item["work_item_id"],),
    ).fetchone()
    if latest is None or latest["attempt_id"] != attempt["attempt_id"]:
        raise WorkConflictError("owner_work_latest_attempt_required")
    if attempt["phase"] not in {"response_available", "uncertain"}:
        raise WorkConflictError("owner_work_known_response_required")
    member = con.execute(
        "SELECT * FROM quick_scan_question_pack_member WHERE attempt_id=?",
        (attempt["attempt_id"],),
    ).fetchone()
    # Native packs freeze the actual request payload before POST. Older single
    # attempts lack this pre-send payload proof and are deliberately unsupported.
    if member is None:
        raise WorkConflictError("owner_work_frozen_native_pack_required")
    pack = _read(con, member["pack_id"])
    if pack is None or pack["physical_outcome"] != "response_available":
        raise WorkConflictError("owner_work_known_response_required")
    budget = con.execute(
        "SELECT * FROM quick_scan_budget_attempt WHERE budget_attempt_id=?",
        (pack["budget_attempt_id"],),
    ).fetchone()
    terminal = con.execute(
        "SELECT * FROM quick_scan_budget_terminal WHERE budget_attempt_id=?",
        (pack["budget_attempt_id"],),
    ).fetchone()
    if (
        budget is None
        or terminal is None
        or budget["status"] != "settled"
        or terminal["terminal_outcome"] != "completed"
        or budget["actual_cost_micros"] is None
        or budget["in_flight"] != 0
        or budget["request_counted"] != 1
        or terminal["settled_at"] != budget["reconciled_at"]
    ):
        raise WorkConflictError("owner_work_physical_not_settled")
    response = store._response_for_checkpoint(con, attempt, pack["receipt_sha256"])
    receipt = response["receipt"]
    if (
        response["receipt_sha256"]
        not in {attempt["receipt_sha256"], attempt["late_receipt_sha256"]}
        or attempt["phase"] == "response_available"
        and (
            attempt["receipt_sha256"] != response["receipt_sha256"]
            or attempt["http_status_code"] != receipt.get("http_status_code")
            or attempt["request_id"] != receipt.get("request_id")
        )
    ):
        raise WorkConflictError("owner_work_logical_receipt_mismatch")
    physical_row = con.execute(
        "SELECT * FROM quick_scan_attempt_response WHERE attempt_id=?",
        (pack["budget_attempt_id"],),
    ).fetchone()
    if physical_row is None:
        raise WorkConflictError("owner_work_physical_response_missing")
    physical = store._response_record(con, physical_row)
    if (
        physical["receipt_sha256"] != response["receipt_sha256"]
        or physical["receipt"] != receipt
        or receipt.get("attempt_id") != pack["execution_attempt_id"]
        or receipt.get("request_payload_sha256") != pack["request_payload_sha256"]
        or receipt.get("prompt_sha256") != pack["provider_prompt_sha256"]
        or receipt.get("requested_model") != pack["model_requested"]
        or type(receipt.get("http_status_code")) is not int
        or not 200 <= receipt["http_status_code"] < 300
    ):
        raise WorkConflictError("owner_work_physical_receipt_mismatch")
    context = bound_context(con, item)
    pre = con.execute(
        "SELECT * FROM quick_scan_subject_send_receipt WHERE attempt_id=?",
        (attempt["attempt_id"],),
    ).fetchone()
    if context is None or pre is None:
        raise WorkConflictError("owner_work_context2_dispatch_required")
    original_owner = receipt_record(con, pre)
    from .quick_scan_owner_refresh_journal import read as owner_binding

    original_binding = owner_binding(con, item["work_item_id"])
    guard_response(con, attempt["attempt_id"], receipt)
    expected = next(m for m in pack["members"] if m["attempt_id"] == attempt["attempt_id"])
    if (
        expected["work_item_id"] != item["work_item_id"]
        or expected["lease_epoch"] != attempt["lease_epoch"]
        or expected["lease_token"] != attempt["lease_token"]
        or item["lease_epoch"] != attempt["lease_epoch"]
    ):
        raise WorkConflictError("owner_work_member_fence_mismatch")
    return dict(
        work_fingerprint=_fingerprint(item),
        subject_binding=context["subject_binding"],
        subject_dispatch_receipt_sha256=original_owner["receipt_sha256"],
        owner_refresh_binding_sha256=(
            None if original_binding is None else original_binding["binding_sha256"]
        ),
        attempt_id=attempt["attempt_id"],
        lease_epoch=attempt["lease_epoch"],
        lease_token=attempt["lease_token"],
        pack_id=pack["pack_id"],
        pack_manifest_sha256=pack["manifest_sha256"],
        execution_attempt_id=pack["execution_attempt_id"],
        receipt_sha256=response["receipt_sha256"],
        response_sha256=response["response_sha256"],
        response_status=receipt.get("response_status"),
        request_payload_sha256=pack["request_payload_sha256"],
        prompt_sha256=pack["prompt_sha256"],
        provider_prompt_sha256=pack["provider_prompt_sha256"],
        provider=receipt["provider"],
        route_id=pack["route_id"],
        route_provider=pack["provider"],
        model_requested=pack["model_requested"],
        actual_model=response["model_resolved"],
        model_resolution_sha256=receipt["model_resolution_sha256"],
        budget_attempt_id=pack["budget_attempt_id"],
        budget_policy_id=budget["policy_id"],
        budget_policy_version=budget["policy_version"],
        budget_currency=budget["currency"],
        actual_cost_micros=budget["actual_cost_micros"],
        physical_terminal="completed",
        settled_at=terminal["settled_at"],
    )


def _read(store, con, row):
    from .quick_scan_work_store import WorkConflictError

    document = strict_json_loads(row["document_json"])
    protocol = document.get("protocol")
    target = document.get("grant", {}).get("comparison_target")
    if protocol == "stockqa.owner_work_decision/1.1.0":
        validate_comparison_target(target, document["request"])
    elif protocol != "stockqa.owner_work_decision/1.0.0" or "comparison_target" in document.get(
        "grant", {}
    ):
        raise WorkConflictError("owner_work_document_version_mismatch")
    if (
        canonical_bytes(document).decode() != row["document_json"]
        or canonical_sha256(document) != row["document_sha256"]
        or document["decision_id"] != row["decision_id"]
        or document["request"]["kind"] != row["kind"]
        or document["request"]["work_item_id"] != row["work_item_id"]
        or document["request"]["idempotency_key"] != row["idempotency_key"]
        or document["request"]["attempt_id"] != row["attempt_id"]
        or document["decided_at"] != _utc(row["decided_at"])
        or document["grant"]["request_sha256"] != canonical_sha256(document["request"])
        or document["decision_id"]
        != "owd1_" + canonical_sha256({k: v for k, v in document.items() if k != "decision_id"})
    ):
        raise WorkConflictError("owner_work_durable_decision_mismatch")
    item = store._item(con, row["work_item_id"])
    attempt = con.execute(
        "SELECT * FROM attempt WHERE attempt_id=?", (row["attempt_id"],)
    ).fetchone()
    if attempt is None or _physical(store, con, item, attempt) != document["request"]["physical"]:
        raise WorkConflictError("owner_work_original_proof_changed")
    return document


def _existing(store, con, work_id, key, request):
    from .quick_scan_work_store import WorkConflictError

    row = con.execute(
        "SELECT * FROM quick_scan_owner_work_decision WHERE work_item_id=? AND idempotency_key=?",
        (work_id, key),
    ).fetchone()
    if row is None:
        return None
    old = _read(store, con, row)
    if old["request"] != request:
        raise WorkConflictError("owner_work_idempotency_conflict")
    return old


def _persist(con, request, grant, now, *, next_retry_at=None):
    document = dict(
        protocol=(
            "stockqa.owner_work_decision/1.1.0"
            if "comparison_target" in grant
            else "stockqa.owner_work_decision/1.0.0"
        ),
        request=request,
        grant=grant,
        decided_at=_utc(now),
        next_retry_at=next_retry_at,
    )
    document["decision_id"] = "owd1_" + canonical_sha256(document)
    encoded = canonical_bytes(document).decode()
    con.execute(
        "INSERT INTO quick_scan_owner_work_decision "
        "(decision_id,work_item_id,attempt_id,kind,idempotency_key,document_json,document_sha256,decided_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            document["decision_id"],
            request["work_item_id"],
            request["attempt_id"],
            request["kind"],
            request["idempotency_key"],
            encoded,
            canonical_sha256(document),
            now,
        ),
    )
    return document


def resolution_request(
    store,
    con,
    work_item_id,
    *,
    attempt_id,
    expected_lease,
    receipt_sha256,
    request_payload_sha256,
    reason,
    idempotency_key,
    diagnostic=None,
    now,
):
    from .quick_scan_work_store import Lease, LeaseFencedError, WorkConflictError

    _safe(work_item_id, "work_id")
    _safe(attempt_id, "attempt_id")
    _safe(idempotency_key, "idempotency_key")
    _sha(receipt_sha256)
    _sha(request_payload_sha256)
    if not isinstance(expected_lease, Lease):
        raise TypeError("owner_work_original_lease_required")
    if reason not in {
        "output_length",
        "answer_validation_rejected",
        "missing_question_answer",
        "known_response_no_accepted_answer",
    }:
        raise ValueError("owner_work_resolution_reason_invalid")
    item = store._item(con, work_item_id)
    attempt = con.execute(
        "SELECT * FROM attempt WHERE attempt_id=? AND work_item_id=?",
        (attempt_id, work_item_id),
    ).fetchone()
    if attempt is None:
        raise WorkConflictError("owner_work_attempt_missing")
    if (
        attempt["lease_token"] != expected_lease.lease_token
        or attempt["lease_epoch"] != expected_lease.lease_epoch
        or item["lease_epoch"] != expected_lease.lease_epoch
    ):
        raise LeaseFencedError("owner_work_original_lease_mismatch")
    if item["status"] == "leased" and item["lease_expires_at"] > now:
        raise LeaseFencedError("owner_work_active_lease_until_" + _utc(item["lease_expires_at"]))
    if item["status"] == "leased" and item["lease_token"] != expected_lease.lease_token:
        raise LeaseFencedError("owner_work_current_lease_mismatch")
    if item["status"] == "uncertain" and item["uncertain_attempt_id"] != attempt_id:
        raise LeaseFencedError("owner_work_uncertain_attempt_mismatch")
    if con.execute(
        "SELECT 1 FROM answer_checkpoint WHERE work_item_id=?", (work_item_id,)
    ).fetchone():
        raise WorkConflictError("owner_work_existing_checkpoint_preserved")
    physical = _physical(store, con, item, attempt)
    if (
        physical["receipt_sha256"] != receipt_sha256
        or physical["request_payload_sha256"] != request_payload_sha256
    ):
        raise WorkConflictError("owner_work_caller_proof_mismatch")
    if reason == "output_length":
        if physical["response_status"] != "length" or diagnostic is not None:
            raise WorkConflictError("owner_work_length_receipt_required")
    elif (
        not isinstance(diagnostic, dict)
        or set(diagnostic) != {"code", "artifact_ref", "artifact_sha256", "response_sha256"}
        or diagnostic["code"]
        != (
            {
                "missing_question_answer": "missing_question_answer",
                "answer_validation_rejected": "standard_answer_invalid",
                "known_response_no_accepted_answer": "known_response_no_accepted_answer",
            }[reason]
        )
        or diagnostic["response_sha256"] != physical["response_sha256"]
        or physical["response_status"]
        not in (
            {"completed", "length"}
            if reason == "known_response_no_accepted_answer"
            else {"completed"}
        )
    ):
        raise WorkConflictError("owner_work_bound_validation_diagnostic_required")
    if diagnostic is not None:
        _safe(diagnostic["artifact_ref"], "diagnostic_ref")
        _sha(diagnostic["artifact_sha256"])
    request = dict(
        kind="known_response_resolution",
        work_item_id=work_item_id,
        attempt_id=attempt_id,
        idempotency_key=idempotency_key,
        physical=physical,
        reason=reason,
        diagnostic=copy.deepcopy(diagnostic),
    )
    return request, item


def resolve(
    store,
    work_item_id,
    *,
    attempt_id,
    expected_lease,
    receipt_sha256,
    request_payload_sha256,
    reason,
    owner_authority,
    idempotency_key,
    diagnostic=None,
):
    from .quick_scan_work_store import LeaseFencedError, WorkConflictError

    with store._transaction() as con:
        now = store._now()
        request, item = resolution_request(
            store,
            con,
            work_item_id,
            attempt_id=attempt_id,
            expected_lease=expected_lease,
            receipt_sha256=receipt_sha256,
            request_payload_sha256=request_payload_sha256,
            reason=reason,
            idempotency_key=idempotency_key,
            diagnostic=diagnostic,
            now=now,
        )
        old = _existing(store, con, work_item_id, idempotency_key, request)
        grant = _authorize(owner_authority, request, now)
        if _authorize(owner_authority, request, store._now()) != grant:
            raise PermissionError("owner_work_authority_changed")
        commit_now = store._now()
        if not math.isfinite(commit_now) or commit_now < now:
            raise ValueError("owner_work_commit_clock_invalid")
        if old is not None:
            return old
        if item["status"] not in {"leased", "uncertain"}:
            raise WorkConflictError("owner_work_resolution_state_invalid")
        saved = _persist(con, request, grant, commit_now)
        count = con.execute(
            "UPDATE work_item SET status='failed',lease_token=NULL,lease_expires_at=NULL,"
            "uncertain_attempt_id=NULL,updated_at=? WHERE work_item_id=? AND status=? AND lease_epoch=?",
            (commit_now, work_item_id, item["status"], expected_lease.lease_epoch),
        ).rowcount
        if count != 1:
            raise LeaseFencedError("owner_work_commit_fence_changed")
        store._event(
            con,
            work_item_id,
            "known_response_resolved",
            expected_lease.lease_epoch,
            commit_now,
            attempt_id=attempt_id,
            old=item["status"],
            new="failed",
        )
        return saved


def retry_request(
    store,
    con,
    work_item_id,
    *,
    purpose,
    retry_after_seconds,
    idempotency_key,
    comparison_controls=None,
    now,
):
    from .quick_scan_work_store import WorkConflictError

    _safe(work_item_id, "work_id")
    _safe(idempotency_key, "idempotency_key")
    if (
        type(retry_after_seconds) not in {int, float}
        or not math.isfinite(retry_after_seconds)
        or not 0 <= retry_after_seconds <= 86400
    ):
        raise ValueError("owner_work_retry_delay_invalid")
    if purpose not in {"retry_unusable_answer", "retry_unknown_answer", "comparison"}:
        raise ValueError("owner_work_retry_purpose_invalid")
    if purpose == "comparison":
        if (
            not isinstance(comparison_controls, dict)
            or set(comparison_controls) != {"pack_size", "thinking_enabled", "max_output_tokens"}
            or type(comparison_controls["pack_size"]) is not int
            or not 1 <= comparison_controls["pack_size"] <= 30
            or type(comparison_controls["thinking_enabled"]) is not bool
            or type(comparison_controls["max_output_tokens"]) is not int
            or not 1 <= comparison_controls["max_output_tokens"] <= 32768
        ):
            raise ValueError("owner_work_comparison_controls_invalid")
    elif comparison_controls is not None:
        raise ValueError("owner_work_unexpected_comparison_controls")
    item = store._item(con, work_item_id)
    attempt = con.execute(
        "SELECT * FROM attempt WHERE work_item_id=? ORDER BY ordinal DESC LIMIT 1",
        (work_item_id,),
    ).fetchone()
    if attempt is None:
        raise WorkConflictError("owner_work_known_attempt_required")
    checkpoint = con.execute(
        "SELECT * FROM answer_checkpoint WHERE work_item_id=?", (work_item_id,)
    ).fetchone()
    resolution = None
    checkpoint_sha = None
    if purpose == "retry_unusable_answer" or (
        purpose == "comparison" and item["status"] == "failed"
    ):
        row = con.execute(
            "SELECT * FROM quick_scan_owner_work_decision WHERE work_item_id=? AND kind='known_response_resolution' ORDER BY decided_at DESC,decision_id DESC LIMIT 1",
            (work_item_id,),
        ).fetchone()
        if item["status"] != "failed" or row is None or checkpoint is not None:
            raise WorkConflictError("owner_work_known_resolution_required")
        resolution = _read(store, con, row)["decision_id"]
    else:
        if checkpoint is None or item["status"] not in {"result_ready", "delivered"}:
            raise WorkConflictError("owner_work_lawful_checkpoint_required")
        saved = store.get_answer_checkpoint(work_item_id)
        if saved is None or checkpoint["attempt_id"] != attempt["attempt_id"]:
            raise WorkConflictError("owner_work_checkpoint_attempt_mismatch")
        answer = saved["payload"]["answer"]
        if purpose == "retry_unknown_answer" and answer["status"] not in {
            "unknown",
            "insufficient_evidence",
        }:
            raise WorkConflictError("owner_work_unknown_answer_required")
        if (
            con.execute(
                "SELECT 1 FROM quick_scan_standard_answer WHERE work_item_id=?",
                (work_item_id,),
            ).fetchone()
            is None
        ):
            raise WorkConflictError("owner_work_standard_checkpoint_required")
        checkpoint_sha = checkpoint["payload_sha256"]
    physical = _physical(store, con, item, attempt)
    request = dict(
        kind="retry_decision",
        work_item_id=work_item_id,
        attempt_id=attempt["attempt_id"],
        idempotency_key=idempotency_key,
        physical=physical,
        purpose=purpose,
        source_resolution_id=resolution,
        source_checkpoint_sha256=checkpoint_sha,
        target_generation=item["generation"] + 1,
        retry_after_seconds=retry_after_seconds,
        comparison_controls=copy.deepcopy(comparison_controls),
        requires_prior_delivery=checkpoint is not None and purpose != "comparison",
    )
    return request, item


def retry(
    store,
    work_item_id,
    *,
    purpose,
    retry_after_seconds,
    owner_authority,
    idempotency_key,
    comparison_controls=None,
):
    with store._transaction() as con:
        now = store._now()
        request, item = retry_request(
            store,
            con,
            work_item_id,
            purpose=purpose,
            retry_after_seconds=retry_after_seconds,
            idempotency_key=idempotency_key,
            comparison_controls=comparison_controls,
            now=now,
        )
        old = _existing(store, con, work_item_id, idempotency_key, request)
        grant = _authorize(owner_authority, request, now)
        if _authorize(owner_authority, request, store._now()) != grant:
            raise PermissionError("owner_work_authority_changed")
        commit_now = store._now()
        if not math.isfinite(commit_now) or commit_now < now:
            raise ValueError("owner_work_commit_clock_invalid")
        if old is not None:
            return old
        return _persist(
            con,
            request,
            grant,
            commit_now,
            next_retry_at=_utc(commit_now + retry_after_seconds),
        )


def get(store, work_item_id):
    _safe(work_item_id, "work_id")
    with closing(store._connect()) as con:
        return projection(store, con, work_item_id)


def projection(store, con, work_item_id):
    row = con.execute(
        "SELECT * FROM quick_scan_owner_work_decision WHERE work_item_id=? AND kind='retry_decision' ORDER BY decided_at DESC,rowid DESC LIMIT 1",
        (work_item_id,),
    ).fetchone()
    return None if row is None else _read(store, con, row)


def validate_tables(connection):
    # Use the existing pure store validator without opening/initializing a DB.
    from .quick_scan_work_store import QuickScanWorkStore

    for row in connection.execute("SELECT * FROM quick_scan_owner_work_decision"):
        _read(QuickScanWorkStore, connection, row)
