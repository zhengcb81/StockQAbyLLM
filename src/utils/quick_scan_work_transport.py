"""One-shot durable send boundary for identified quick-scan questions.

The module is deliberately optional: legacy callers without a bound work item keep
the existing client behavior. Quick-scan orchestrators bind a previously claimed
work item, and the ordered provider cascade supplies route identity before each
provider invocation. Only hashes and sanitized transport metadata reach SQLite.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

from src.utils.quick_scan_work_store import (
    BudgetAdmissionError,
    Lease,
    LeaseFencedError,
    QuickScanWorkStore,
    SendPermit,
    _cost_to_micros,
    _safe_text,
    _sanitized_receipt,
)
from src.utils.quick_scan_work_store import quick_scan_receipt_sha256 as _receipt_sha256


class QuickScanWorkPersistenceError(RuntimeError):
    """Durable work state could not be written; no fallback dispatch is allowed."""


class QuickScanWorkUncertainError(RuntimeError):
    """A request may have reached the provider; do not retry or fail over blindly."""

    def __init__(self, attempt_id: str, attempt_receipt: Optional[dict] = None):
        self.attempt_id = attempt_id
        self.attempt_receipt = _sanitized_receipt(attempt_receipt)
        super().__init__("quick-scan transport outcome is uncertain")


class QuickScanBudgetDeferredError(RuntimeError):
    """A durable budget or concurrency gate denied this dispatch."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


_CAPACITY_FULL_REASONS = frozenset(
    {
        "dispatch_global_capacity_full",
        "dispatch_quota_group_capacity_full",
        "dispatch_route_capacity_full",
    }
)
_CAPACITY_WAIT_SECONDS = 30.0
_CAPACITY_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class QuickScanWorkBinding:
    store: QuickScanWorkStore
    work_item_id: str
    lease: Lease


@dataclass(frozen=True)
class QuickScanRouteBinding:
    route_id: Any
    provider: Any
    model_requested: Any
    quota_group: Any = None


@dataclass(frozen=True)
class QuickScanBudgetBinding:
    store: QuickScanWorkStore
    policy: dict
    cost_resolver: Optional[Callable[[dict], Optional[dict]]] = None


@dataclass
class QuickScanSendAttempt:
    """Private one-shot transport handle created immediately before one HTTP POST."""

    binding: Optional[QuickScanWorkBinding]
    attempt_id: str
    permit: SendPermit
    budget_binding: Optional[QuickScanBudgetBinding] = None
    budget_attempt_id: Optional[str] = None
    _consumed: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def consume_for_post(self) -> None:
        """Consume this in-memory handle exactly once before entering HTTP transport."""
        with self._lock:
            if self._consumed:
                raise QuickScanWorkPersistenceError("send permit already consumed")
            expected_epoch = 0 if self.binding is None else self.binding.lease.lease_epoch
            if (
                self.permit.attempt_id != self.attempt_id
                or self.permit.lease_epoch != expected_epoch
            ):
                raise QuickScanWorkPersistenceError("send permit does not match this attempt")
            self._consumed = True

    def record_response(
        self,
        *,
        http_status_code: Optional[int],
        request_id: Optional[str],
        receipt: Any,
    ) -> None:
        if not self._consumed:
            raise QuickScanWorkPersistenceError("HTTP response arrived without a consumed permit")
        if type(http_status_code) is not int or not 200 <= http_status_code < 300:
            self.record_failure(
                {
                    "http_status_code": http_status_code,
                    "request_id": request_id,
                    "failure_type": "unexpected_response_status",
                }
            )
            raise QuickScanWorkUncertainError(self.attempt_id)
        receipt_sha256 = _receipt_sha256(receipt)
        actual_cost, cost_source_ref = _resolved_cost(self.budget_binding, receipt)
        try:
            if self.binding is not None:
                self.binding.store.record_attempt_outcome(
                    self.binding.work_item_id,
                    self.binding.lease,
                    self.attempt_id,
                    outcome="response_available",
                    http_status_code=http_status_code,
                    receipt_sha256=receipt_sha256,
                    request_id=request_id,
                    actual_cost=actual_cost,
                    cost_source_ref=cost_source_ref,
                )
            elif self.budget_binding is not None and self.budget_attempt_id is not None:
                self.budget_binding.store.record_budget_outcome(
                    self.budget_attempt_id,
                    outcome="response_available",
                    http_status_code=http_status_code,
                    actual_cost=actual_cost,
                    cost_source_ref=cost_source_ref,
                )
        except LeaseFencedError as error:
            if self.binding is None:
                raise RuntimeError("record_response: work binding is not bound")
            try:
                self.binding.store.note_late_receipt(
                    self.binding.work_item_id,
                    self.binding.lease,
                    self.attempt_id,
                    receipt_sha256=receipt_sha256,
                    **self._late_budget_details(
                        outcome="response_available",
                        http_status_code=http_status_code,
                        actual_cost=actual_cost,
                        cost_source_ref=cost_source_ref,
                    ),
                )
            except Exception as late_error:
                raise QuickScanWorkPersistenceError(
                    "could not retain late quick-scan response receipt"
                ) from late_error
            raise QuickScanWorkUncertainError(
                self.attempt_id, _sanitized_receipt(receipt)
            ) from error
        except Exception as error:
            raise QuickScanWorkPersistenceError(
                "could not persist quick-scan response receipt"
            ) from error

    def record_failure(self, receipt: Any) -> None:
        if not self._consumed:
            raise QuickScanWorkPersistenceError("transport failed without a consumed permit")
        safe = _sanitized_receipt(receipt)
        status = safe.get("http_status_code")
        provider_code = safe.get("provider_error_code")
        failure_category: Optional[str] = None
        outcome = "unknown"

        if status in (401, 403):
            outcome = "confirmed_failure"
            failure_category = "authentication_rejected"
        elif status == 404:
            outcome = "confirmed_failure"
            failure_category = "model_or_endpoint_unavailable"
        elif status == 429:
            if provider_code in {
                "insufficient_quota",
                "quota_exceeded",
                "billing_hard_limit_reached",
                "account_quota_exceeded",
                "insufficient_funds",
            }:
                outcome = "confirmed_failure"
                failure_category = "quota_exhausted"
            elif provider_code in {
                "rate_limit_exceeded",
                "too_many_requests",
                "rate_limited",
            }:
                outcome = "confirmed_failure"
                failure_category = "rate_limited"

        receipt_sha256 = _receipt_sha256(safe)
        actual_cost, cost_source_ref = _resolved_cost(self.budget_binding, safe)
        try:
            if self.binding is not None:
                self.binding.store.record_attempt_outcome(
                    self.binding.work_item_id,
                    self.binding.lease,
                    self.attempt_id,
                    outcome=outcome,
                    http_status_code=status if type(status) is int else None,
                    receipt_sha256=receipt_sha256,
                    failure_category=failure_category,
                    provider_error_code=provider_code if isinstance(provider_code, str) else None,
                    request_id=(
                        safe.get("request_id") if isinstance(safe.get("request_id"), str) else None
                    ),
                    actual_cost=actual_cost,
                    cost_source_ref=cost_source_ref,
                )
            elif self.budget_binding is not None and self.budget_attempt_id is not None:
                self.budget_binding.store.record_budget_outcome(
                    self.budget_attempt_id,
                    outcome=outcome,
                    http_status_code=status if type(status) is int else None,
                    actual_cost=actual_cost,
                    cost_source_ref=cost_source_ref,
                )
        except LeaseFencedError as error:
            if self.binding is None:
                raise RuntimeError("record_failure: work binding is not bound")
            try:
                self.binding.store.note_late_receipt(
                    self.binding.work_item_id,
                    self.binding.lease,
                    self.attempt_id,
                    receipt_sha256=receipt_sha256,
                    **self._late_budget_details(
                        outcome=outcome,
                        http_status_code=status if type(status) is int else None,
                        actual_cost=actual_cost,
                        cost_source_ref=cost_source_ref,
                    ),
                )
            except Exception as late_error:
                raise QuickScanWorkPersistenceError(
                    "could not retain late quick-scan failure receipt"
                ) from late_error
            raise QuickScanWorkUncertainError(self.attempt_id, safe) from error
        except Exception as error:
            raise QuickScanWorkPersistenceError(
                "could not persist quick-scan transport failure"
            ) from error
        if outcome == "unknown":
            raise QuickScanWorkUncertainError(self.attempt_id, safe)

    def _late_budget_details(
        self,
        *,
        outcome: str,
        http_status_code: Optional[int],
        actual_cost: Optional[object],
        cost_source_ref: Optional[str],
    ) -> dict:
        if self.budget_binding is None or self.budget_attempt_id is None:
            return {}
        return {
            "budget_outcome": outcome,
            "http_status_code": http_status_code,
            "actual_cost": actual_cost,
            "cost_source_ref": cost_source_ref,
        }


_WORK_BINDING: contextvars.ContextVar[Optional[QuickScanWorkBinding]] = contextvars.ContextVar(
    "quick_scan_work_binding", default=None
)
_ROUTE_BINDING: contextvars.ContextVar[Optional[QuickScanRouteBinding]] = contextvars.ContextVar(
    "quick_scan_route_binding", default=None
)
_OWN_RESERVATION: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "quick_scan_own_reservation", default=False
)


def own_reservation_held() -> bool:
    """Q09: True while THIS dispatch already holds its own mark-time budget
    reservation. The cascade's preferred-route busy WAIT must not deadlock on
    its own slot — admission (mark_send_intent) remains the authoritative
    capacity gate for every request."""
    return _OWN_RESERVATION.get()


_BUDGET_BINDING: contextvars.ContextVar[Optional[QuickScanBudgetBinding]] = contextvars.ContextVar(
    "quick_scan_budget_binding", default=None
)
_FORMAT_REPAIR: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "quick_scan_format_repair", default=False
)


@contextmanager
def bind_quick_scan_work(
    store: QuickScanWorkStore, work_item_id: str, lease: Lease
) -> Iterator[None]:
    """Bind a trusted, already-created and claimed logical question to this context."""
    token = _WORK_BINDING.set(QuickScanWorkBinding(store, work_item_id, lease))
    try:
        yield
    finally:
        _WORK_BINDING.reset(token)


@contextmanager
def bind_quick_scan_route(
    *, route_id: Any, provider: Any, model_requested: Any, quota_group: Any = None
) -> Iterator[None]:
    """Bind the actual model route used by one ordered-cascade dispatch."""
    route = QuickScanRouteBinding(route_id, provider, model_requested, quota_group)
    token = _ROUTE_BINDING.set(route)
    try:
        yield
    finally:
        _ROUTE_BINDING.reset(token)


@contextmanager
def bind_quick_scan_budget(
    store: QuickScanWorkStore,
    policy: dict,
    *,
    cost_resolver: Optional[Callable[[dict], Optional[dict]]] = None,
) -> Iterator[None]:
    """Bind the current immutable policy to the shared durable budget owner."""
    try:
        store.configure_quick_scan_budget(policy)
    except Exception as error:
        raise QuickScanBudgetDeferredError("budget_policy_unavailable") from error
    binding = QuickScanBudgetBinding(store, policy, cost_resolver)
    token = _BUDGET_BINDING.set(binding)
    try:
        yield
    finally:
        _BUDGET_BINDING.reset(token)


def _resolved_cost(
    binding: Optional[QuickScanBudgetBinding], receipt: Any
) -> tuple[Optional[object], Optional[str]]:
    if binding is None or binding.cost_resolver is None:
        return None, None
    cost_policy = binding.policy.get("cost_policy")
    if (
        not isinstance(cost_policy, dict)
        or cost_policy.get("pricing_basis") != "verified_rate_card"
    ):
        return None, None
    pricing_ref = cost_policy.get("pricing_ref")
    try:
        measurement = binding.cost_resolver(_sanitized_receipt(receipt))
    except Exception:
        return None, None
    if measurement is None:
        return None, None
    if not isinstance(measurement, dict) or set(measurement) != {
        "actual_cost",
        "pricing_ref",
        "source_ref",
    }:
        return None, None
    if measurement["pricing_ref"] != pricing_ref:
        return None, None
    try:
        source_ref = _safe_text(measurement["source_ref"], "cost_source_ref", maximum=300)
        _cost_to_micros(measurement["actual_cost"], "actual_cost", allow_zero=True)
    except (TypeError, ValueError):
        return None, None
    return measurement["actual_cost"], source_ref


@contextmanager
def bind_quick_scan_format_repair() -> Iterator[None]:
    """Authorize exactly the provider's explicit, budgeted format-repair request."""
    token = _FORMAT_REPAIR.set(True)
    try:
        yield
    finally:
        _FORMAT_REPAIR.reset(token)


def begin_quick_scan_send(prompt: str, system_prompt: str) -> Optional[QuickScanSendAttempt]:
    """Commit prepared + send-intent state before permitting the HTTP request."""
    if own_reservation_held():
        # Q09: THIS question's send was already admitted atomically at the
        # lifecycle's mark-time reserve (one send = one reserve); re-admitting
        # here would double-count the ledger and self-deadlock on our own
        # route slot. None = proceed without a second admission — the same
        # contract llm_client already uses when no binding exists.
        return None
    work = _WORK_BINDING.get()
    route = _ROUTE_BINDING.get()
    budget = _BUDGET_BINDING.get()
    if work is None and budget is None:
        return None
    if route is None:
        raise QuickScanWorkPersistenceError("incomplete durable quick-scan route context")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (route.route_id, route.provider, route.model_requested)
    ):
        raise QuickScanWorkPersistenceError("quick-scan provider route identity is incomplete")
    if budget is not None:
        if not isinstance(route.quota_group, str) or not route.quota_group.strip():
            raise QuickScanWorkPersistenceError("quick-scan budget route has no quota group")
        if work is not None and work.store.path.resolve() != budget.store.path.resolve():
            raise QuickScanWorkPersistenceError(
                "work and budget ledgers must share one SQLite file"
            )

    prompt_sha256 = hashlib.sha256((system_prompt + "\0" + prompt).encode("utf-8")).hexdigest()
    prepared = None
    try:
        if work is not None:
            request_identity = json.dumps(
                {
                    "work_item_id": work.work_item_id,
                    "route_id": route.route_id,
                    "provider": route.provider,
                    "model_requested": route.model_requested,
                    "prompt_sha256": prompt_sha256,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            request_cache_key = (
                "REQ_" + hashlib.sha256(request_identity.encode("utf-8")).hexdigest()
            )
            prepared = work.store.prepare_attempt(
                work.work_item_id,
                work.lease,
                route_id=route.route_id,
                provider=route.provider,
                model_requested=route.model_requested,
                request_cache_key=request_cache_key,
                prompt_sha256=prompt_sha256,
                allow_format_repair=_FORMAT_REPAIR.get(),
            )
        budget_route = (
            None
            if budget is None
            else {
                "route_id": route.route_id,
                "provider": route.provider,
                "model_requested": route.model_requested,
                "quota_group": route.quota_group,
            }
        )
        if work is not None:
            if prepared is None:
                raise RuntimeError("begin_quick_scan_send: prepared attempt is missing")
            deadline = time.monotonic() + _CAPACITY_WAIT_SECONDS
            while True:
                try:
                    permit = work.store.mark_send_intent(
                        work.work_item_id,
                        work.lease,
                        prepared["attempt_id"],
                        budget_policy=None if budget is None else budget.policy,
                        budget_route=budget_route,
                    )
                    break
                except BudgetAdmissionError as error:
                    if error.reason not in _CAPACITY_FULL_REASONS or time.monotonic() >= deadline:
                        raise
                    time.sleep(_CAPACITY_POLL_SECONDS)
            return QuickScanSendAttempt(
                work,
                prepared["attempt_id"],
                permit,
                budget_binding=budget,
                budget_attempt_id=prepared["attempt_id"] if budget is not None else None,
            )
        if budget is None:
            raise RuntimeError("begin_quick_scan_send: budget ledger is not bound")
        budget_attempt_id = "DISPATCH_" + uuid.uuid4().hex
        deadline = time.monotonic() + _CAPACITY_WAIT_SECONDS
        while True:
            try:
                budget.store.reserve_budget_attempt(
                    budget.policy,
                    budget_attempt_id=budget_attempt_id,
                    route_id=route.route_id,
                    provider=route.provider,
                    model_requested=route.model_requested,
                    quota_group=route.quota_group,
                )
                break
            except BudgetAdmissionError as error:
                if error.reason not in _CAPACITY_FULL_REASONS or time.monotonic() >= deadline:
                    raise
                time.sleep(_CAPACITY_POLL_SECONDS)
        permit = SendPermit(budget_attempt_id, 0.0, 0)
        return QuickScanSendAttempt(
            None,
            budget_attempt_id,
            permit,
            budget_binding=budget,
            budget_attempt_id=budget_attempt_id,
        )
    except BudgetAdmissionError as error:
        raise QuickScanBudgetDeferredError(error.reason) from error
    except Exception as error:
        raise QuickScanWorkPersistenceError("quick-scan durable send admission failed") from error
