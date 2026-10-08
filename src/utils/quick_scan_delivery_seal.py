"""Q10 residual glue: settle a Q07 answer checkpoint into a C06 delivery.

One direction only — checkpoint -> sealed package, or a durable block record.
Nothing here re-asks a question, replaces a sealed package, or invents an
authority/execution field. Restart recovery re-runs the SAME function with
zero model calls (JOB-07 / DB-07).

Authority 1.0.0 keeps its historical compact behaviour unchanged. Authority
2.0.0 seals ONLY a complete Observation assembled from the durable frozen
context, the durable full standard answer and the original attempt instants;
anything missing is a durable block, and a historical package that cannot be
improved stays exactly as it was written.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.utils.logger import get_logger
from src.utils.quick_scan_c06_adapter import (
    MissingC06Fields,
    build_c06_package,
    build_complete_c06_package,
)
from src.utils.quick_scan_c06_authority import (
    AUTHORITY_SCHEMA_VERSION_V2,
    authority_adapter_input,
)
from src.utils.quick_scan_observation_context import bind_context_to_work_item
from src.utils.quick_scan_work_store import QuickScanWorkStore

logger = get_logger(__name__)

BLOCK_AUTHORITY = "c06_authority_unavailable"
BLOCK_ADAPTER = "c06_adapter_missing_fields"
BLOCK_CONTEXT = "c06_observation_context_unavailable"
BLOCK_CONTEXT_CONFLICT = "c06_observation_context_conflict"
BLOCK_STANDARD_ANSWER = "c06_standard_answer_unavailable"
BLOCK_ATTEMPT = "c06_attempt_send_intent_unavailable"
HISTORICAL_READONLY = "historical_package_readonly"

__all__ = [
    "AUTHORITY_SCHEMA_VERSION_V2",
    "BLOCK_ADAPTER",
    "BLOCK_ATTEMPT",
    "BLOCK_AUTHORITY",
    "BLOCK_CONTEXT",
    "BLOCK_CONTEXT_CONFLICT",
    "BLOCK_STANDARD_ANSWER",
    "HISTORICAL_READONLY",
    "seal_pending_deliveries",
    "seal_result_delivery",
]


def _send_intent_iso(epoch: Any) -> str | None:
    """The original dispatch instant as UTC ISO-8601, never a clock read now."""
    if epoch is None:
        return None
    try:
        instant = datetime.fromtimestamp(float(epoch), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return instant.isoformat(timespec="seconds").replace("+00:00", "Z")


def _complete_package(
    store: QuickScanWorkStore,
    work_item_id: str,
    authority: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Build the complete package for one checkpoint, or name the missing input."""
    checkpoint = store.get_answer_checkpoint(work_item_id)
    if checkpoint is None:
        return None, BLOCK_ADAPTER
    stored = store.get_observation_context(work_item_id)
    if stored is None:
        return None, BLOCK_CONTEXT
    if stored["context_sha256"] != authority.get("observation_context_sha256"):
        return None, BLOCK_CONTEXT_CONFLICT
    standard = store.get_standard_answer(work_item_id)
    if standard is None:
        return None, BLOCK_STANDARD_ANSWER
    item = store.get_item(work_item_id)
    try:
        bound = bind_context_to_work_item(
            stored["context"], work_item=item, question_id=item["question_id"]
        )
    except ValueError as error:
        logger.warning("封存上下文绑定拒绝（%s）：%s", work_item_id, error)
        return None, BLOCK_CONTEXT
    try:
        transmission = store.get_attempt_transmission(checkpoint["attempt_id"])
    except KeyError:
        return None, BLOCK_ATTEMPT
    started_at = _send_intent_iso(transmission.get("send_intent_at"))
    if started_at is None:
        return None, BLOCK_ATTEMPT
    try:
        package = build_complete_c06_package(
            checkpoint["payload"],
            authority=authority_adapter_input(authority),
            context=bound,
            standard_answer=standard["answer"],
            started_at=started_at,
        )
    except MissingC06Fields as error:
        logger.warning("完整 C06 封存拒绝（%s）：%s", work_item_id, error)
        return None, BLOCK_ADAPTER
    return package, None


def _block(store: QuickScanWorkStore, work_item_id: str, reason_code: str) -> dict[str, Any]:
    record = store.mark_result_delivery_blocked(work_item_id, reason_code)
    logger.warning("C06 封存阻断（%s）：%s", work_item_id, reason_code)
    return {
        "action": "blocked",
        "work_item_id": work_item_id,
        "state": record["state"],
        "block_code": reason_code,
    }


def _sealed_outcome(work_item_id: str, record: dict[str, Any], action: str) -> dict[str, Any]:
    return {
        "action": action,
        "work_item_id": work_item_id,
        "state": record["state"],
        "package_id": record["package_id"],
        "item_id": record.get("item_id"),
        "observation_id": record.get("observation_id"),
        "payload_sha256": record.get("payload_sha256"),
        "delivery_key": record.get("delivery_key"),
    }


def seal_result_delivery(
    store: QuickScanWorkStore,
    work_item_id: str,
    *,
    authority: dict[str, Any] | None,
) -> dict[str, Any]:
    """Seal the checkpoint of one result-ready work item, or durably block it.

    Returns a bounded action record: ``already_sealed`` / ``sealed`` /
    ``superseded`` / ``blocked`` / ``left_in_flight``. A missing or invalid
    authority and any field the adapter refuses to derive both become a durable
    block while the work item stays ``result_ready`` — the answer is never
    re-asked.
    """
    from src.utils.quick_scan_result_outbox import canonical_bytes

    existing = store.get_result_delivery(work_item_id)
    sealed = existing is not None and existing.get("package") is not None

    if isinstance(authority, dict) and authority.get("schema_version") == (
        AUTHORITY_SCHEMA_VERSION_V2
    ):
        package, missing = _complete_package(store, work_item_id, authority)
        if package is None or missing is not None:
            reason = missing or BLOCK_ADAPTER
            if not sealed or existing is None:
                return _block(store, work_item_id, reason)
            # A historical sealed package is read-only: it is neither downgraded
            # to a block nor silently upgraded from its compact body.
            return {
                "action": "left_in_flight",
                "work_item_id": work_item_id,
                "state": existing["state"],
                "reason": HISTORICAL_READONLY,
                "block_code": reason,
            }
        if sealed and existing is not None:
            if existing["package_bytes"] == canonical_bytes(package):
                return {
                    "action": "already_sealed",
                    "work_item_id": work_item_id,
                    "state": existing["state"],
                    "package_id": existing["package_id"],
                }
            if existing["state"] != "ready":
                # send_uncertain must be reconciled first, and a terminal
                # delivery is never reopened or re-dispatched.
                return {
                    "action": "left_in_flight",
                    "work_item_id": work_item_id,
                    "state": existing["state"],
                    "reason": (
                        "reconcile_before_supersede"
                        if existing["state"] == "send_uncertain"
                        else "terminal_delivery_readonly"
                    ),
                }
            superseded = store.supersede_result_delivery(work_item_id, package)
            logger.info(
                "C06 新修订已封存（%s）→ revision %s（supersedes %s）",
                work_item_id,
                superseded.get("revision"),
                superseded.get("supersedes_revision"),
            )
            return {
                **_sealed_outcome(work_item_id, superseded, "superseded"),
                "revision": superseded.get("revision"),
                "supersedes_revision": superseded.get("supersedes_revision"),
                "supersedes_package_id": superseded.get("supersedes_package_id"),
            }
        record = store.prepare_result_delivery(work_item_id, package)
        logger.info("C06 完整包已封存（%s）→ %s", work_item_id, record["package_id"])
        revisions = store.list_delivery_revisions(work_item_id)
        return {
            **_sealed_outcome(work_item_id, record, "sealed"),
            "revision": revisions[-1]["revision"] if revisions else None,
        }

    if sealed and existing is not None:
        return {
            "action": "already_sealed",
            "work_item_id": work_item_id,
            "state": existing["state"],
            "package_id": existing["package_id"],
        }
    if existing is not None and existing["state"] != "blocked":
        # ready / send_uncertain / terminal: the immutable package exists
        # (or the delivery is mid-flight); never rebuild or replace it.
        return {
            "action": "left_in_flight",
            "work_item_id": work_item_id,
            "state": existing["state"],
        }

    checkpoint = store.get_answer_checkpoint(work_item_id)
    if checkpoint is None:
        raise ValueError("result-ready work item has no answer checkpoint")

    try:
        adapter_input = authority_adapter_input(authority)
    except MissingC06Fields as error:
        logger.warning("C06 封存阻断（%s）：%s", work_item_id, error)
        return _block(store, work_item_id, BLOCK_AUTHORITY)

    try:
        compact = build_c06_package(checkpoint["payload"], authority=adapter_input)
    except MissingC06Fields as error:
        logger.warning("C06 封存阻断（%s）：%s", work_item_id, error)
        return _block(store, work_item_id, BLOCK_ADAPTER)

    record = store.prepare_result_delivery(work_item_id, compact)
    logger.info("C06 包已封存（%s）→ %s", work_item_id, record["package_id"])
    return _sealed_outcome(work_item_id, record, "sealed")


def seal_pending_deliveries(
    store: QuickScanWorkStore,
    *,
    authority: dict[str, Any] | None,
    limit: int = 100,
) -> dict[str, Any]:
    """Restart/finish entry: seal every settle-able checkpoint with LLM=0.

    Scans three waiting groups — checkpoints with no delivery row yet,
    deliveries durably blocked only because the authority used to be missing,
    and deliveries already sealed but not yet dispatched. Mid-flight and
    terminal deliveries are never touched.
    """
    sealed: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    already: list[dict[str, Any]] = []
    untouched: list[dict[str, Any]] = []
    seen: set[str] = set()

    candidates = list(store.list_result_ready_without_delivery(limit=limit))
    for record in store.list_result_deliveries(states=["blocked", "ready"], limit=limit):
        candidates.append(record["work_item_id"])

    for work_item_id in candidates:
        if work_item_id in seen:
            continue
        seen.add(work_item_id)
        try:
            outcome = seal_result_delivery(store, work_item_id, authority=authority)
        except ValueError as error:
            logger.error("封存跳过 %s：%s", work_item_id, error)
            untouched.append({"work_item_id": work_item_id, "reason": str(error)})
            continue
        if outcome["action"] in {"sealed", "superseded"}:
            sealed.append(outcome)
        elif outcome["action"] == "blocked":
            blocked.append(outcome)
        elif outcome["action"] == "already_sealed":
            already.append(outcome)
        else:
            untouched.append(outcome)

    return {
        "sealed": sealed,
        "blocked": blocked,
        "already_sealed": already,
        "untouched": untouched,
        "model_calls": 0,
    }
