"""Q10 residual glue: settle a Q07 answer checkpoint into a C06 delivery.

One direction only — checkpoint -> sealed package, or a durable block record.
Nothing here re-asks a question, replaces a sealed package, or invents an
authority/execution field. Restart recovery re-runs the SAME function with
zero model calls (JOB-07 / DB-07).
"""

from __future__ import annotations

from typing import Any

from src.utils.logger import get_logger
from src.utils.quick_scan_c06_adapter import MissingC06Fields, build_c06_package
from src.utils.quick_scan_c06_authority import authority_adapter_input
from src.utils.quick_scan_work_store import QuickScanWorkStore

logger = get_logger(__name__)

BLOCK_AUTHORITY = "c06_authority_unavailable"
BLOCK_ADAPTER = "c06_adapter_missing_fields"

__all__ = [
    "BLOCK_ADAPTER",
    "BLOCK_AUTHORITY",
    "seal_pending_deliveries",
    "seal_result_delivery",
]


def seal_result_delivery(
    store: QuickScanWorkStore,
    work_item_id: str,
    *,
    authority: dict[str, Any] | None,
) -> dict[str, Any]:
    """Seal the checkpoint of one result-ready work item, or durably block it.

    Returns a bounded action record: ``already_sealed`` / ``sealed`` /
    ``blocked`` / ``left_in_flight``. A missing or invalid authority and any
    field the adapter refuses to derive both become a durable block while the
    work item stays ``result_ready`` — the answer is never re-asked.
    """
    existing = store.get_result_delivery(work_item_id)
    if existing is not None:
        if existing.get("package") is not None:
            return {
                "action": "already_sealed",
                "work_item_id": work_item_id,
                "state": existing["state"],
                "package_id": existing["package_id"],
            }
        if existing["state"] != "blocked":
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
        record = store.mark_result_delivery_blocked(work_item_id, BLOCK_AUTHORITY)
        logger.warning("C06 封存阻断（%s）：%s", work_item_id, error)
        return {
            "action": "blocked",
            "work_item_id": work_item_id,
            "state": record["state"],
            "block_code": BLOCK_AUTHORITY,
        }

    try:
        package = build_c06_package(checkpoint["payload"], authority=adapter_input)
    except MissingC06Fields as error:
        record = store.mark_result_delivery_blocked(work_item_id, BLOCK_ADAPTER)
        logger.warning("C06 封存阻断（%s）：%s", work_item_id, error)
        return {
            "action": "blocked",
            "work_item_id": work_item_id,
            "state": record["state"],
            "block_code": BLOCK_ADAPTER,
        }

    record = store.prepare_result_delivery(work_item_id, package)
    logger.info("C06 包已封存（%s）→ %s", work_item_id, record["package_id"])
    return {
        "action": "sealed",
        "work_item_id": work_item_id,
        "state": record["state"],
        "package_id": record["package_id"],
        "item_id": record["item_id"],
        "observation_id": record["observation_id"],
        "payload_sha256": record["payload_sha256"],
        "delivery_key": record["delivery_key"],
    }


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
        if outcome["action"] == "sealed":
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
