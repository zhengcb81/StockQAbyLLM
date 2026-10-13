"""Read-only owner inspection through the existing Q09/Q13 ledger algorithms.

Trusted deployment configuration supplies the policy, not a consumer request.
The quote is a conservative shared-budget ceiling, not a model price forecast.
Approval/paid dispatch must separately recheck and reserve via original Q09.
"""

import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path
from typing import Callable

from src.utils.quick_scan_result_outbox import canonical_sha256
from src.utils.quick_scan_work_store import (
    SCHEMA_VERSION,
    QuickScanWorkStore,
    _budget_policy_projection,
)


class _Borrowed:
    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def close(self):
        pass  # The one explicit inspection transaction owns the handle.


class ReadOnlyWorkView(QuickScanWorkStore):
    def __init__(self, path):
        raw_path = Path(path)
        for entry in (raw_path, *raw_path.parents):
            try:
                meta = entry.lstat()
            except FileNotFoundError:
                continue
            if entry.is_symlink() or getattr(meta, "st_file_attributes", 0) & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
            ):
                raise ValueError("refresh_executor_store_unavailable")
        self.path = raw_path.resolve()
        self._clock: Callable[[], float] = time.time
        self._inspection_connection = None
        if not self.path.is_file():
            raise ValueError("refresh_executor_store_unavailable")
        if any(
            self.path.with_name(self.path.name + s).exists() for s in ("-wal", "-shm", "-journal")
        ):
            raise ValueError("refresh_executor_store_busy")

    def _connect(self):
        if self._inspection_connection is not None:
            return _Borrowed(self._inspection_connection)
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {18, SCHEMA_VERSION}:
                raise ValueError("refresh_executor_schema_unavailable")
            self._validate_schema(connection, schema_version=version)
        except Exception:
            connection.close()
            raise
        return connection

    def _transaction(self):
        raise ValueError("refresh_inspection_is_read_only")


def inspect_refresh(path, request, *, policy, schema_version="1.0.0"):
    if schema_version not in {"1.0.0", "1.1.0", "1.2.0", "1.3.0"}:
        raise ValueError("refresh_inspection_schema_unsupported")
    required = {
        "entity_id",
        "question_ids",
        "scope_bindings",
        "policy_id",
        "route_id",
        "provider_config_ref",
        "model",
    }
    if schema_version in {"1.2.0", "1.3.0"}:
        required |= {"cohort_id", "work_decision_refs"}
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("refresh_inspection_request_invalid")
    normalized = _budget_policy_projection(policy)
    route = normalized["route_identity"].get(request["route_id"])
    if (
        request["policy_id"] != normalized["policy_id"]
        or route is None
        or route["provider_config_ref"] != request["provider_config_ref"]
        or route["model"] != request["model"]
    ):
        raise ValueError("refresh_executor_policy_route_mismatch")
    bindings = request["scope_bindings"]
    if (
        not isinstance(bindings, dict)
        or set(bindings) != set(request["question_ids"])
        or any(
            not isinstance(b, dict)
            or set(b) != {"scope", "scope_id"}
            or b["scope"] not in {"entity", "security", "segment"}
            or (b["scope"] == "entity" and b["scope_id"] != request["entity_id"])
            for b in bindings.values()
        )
    ):
        raise ValueError("refresh_inspection_scope_invalid")
    view = ReadOnlyWorkView(path)
    with closing(view._connect()) as connection:
        connection.execute("BEGIN")
        # Bind schema and rows to the same native read transaction. A schema
        # change after the initial connection check must not be overlooked.
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in {18, SCHEMA_VERSION}:
            raise ValueError("refresh_executor_schema_unavailable")
        view._validate_schema(connection, schema_version=version)
        view._inspection_connection = connection
        try:
            stored = connection.execute(
                "SELECT * FROM quick_scan_budget_policy WHERE policy_id=?",
                (request["policy_id"],),
            ).fetchone()
            if stored is None:
                raise ValueError("refresh_executor_policy_unavailable")
            if (
                stored["policy_snapshot_sha256"] != normalized["policy_snapshot_sha256"]
                or stored["policy_version"] != normalized["policy_version"]
            ):
                raise ValueError("refresh_executor_policy_changed")
            comparisons = {
                k: normalized[k]
                for k in (
                    "currency",
                    "pricing_basis",
                    "pricing_ref",
                    "max_cost_micros",
                    "max_requests",
                    "max_cost_per_attempt_micros",
                    "max_in_flight_total",
                    "route_limits_json",
                )
            }
            comparisons["quota_group_limits_json"] = normalized["group_limits_json"]
            if any(stored[k] != v for k, v in comparisons.items()):
                raise ValueError("refresh_executor_policy_changed")
            status = view.get_quick_scan_budget_status(request["policy_id"])
            work = view.owner_refresh_projection(
                entity_id=request["entity_id"],
                question_ids=request["question_ids"],
                scope_bindings=bindings,
            )
            if schema_version in {"1.2.0", "1.3.0"}:
                from src.utils.quick_scan_known_outcome_journal import _read
                from src.utils.quick_scan_work_control import project_owner_control

                refs = request["work_decision_refs"]
                if not isinstance(refs, list) or not 1 <= len(refs) <= 500:
                    raise ValueError("refresh_control_refs_invalid")
                seen = set()
                all_refs = set()
                for ref in refs:
                    if not isinstance(ref, dict) or set(ref) != {
                        "work_item_id",
                        "decision_id",
                    }:
                        raise ValueError("refresh_control_ref_invalid")
                    key = (ref["work_item_id"], ref["decision_id"])
                    if key in all_refs:
                        raise ValueError("refresh_control_duplicate_ref")
                    all_refs.add(key)
                    item = view._item(connection, ref["work_item_id"])
                    qid = item["question_id"]
                    row = connection.execute(
                        "SELECT * FROM quick_scan_owner_work_decision WHERE decision_id=? AND work_item_id=?",
                        (ref["decision_id"], ref["work_item_id"]),
                    ).fetchone()
                    if row is None:
                        raise ValueError("refresh_control_decision_missing")
                    document = _read(view, connection, row)
                    targeted = schema_version == "1.3.0"
                    if document["protocol"] != "stockqa.owner_work_decision/" + (
                        "1.1.0" if targeted else "1.0.0"
                    ):
                        raise ValueError("refresh_control_explicit_target_dialect_required")
                    physical = document["request"]["physical"]
                    target = document["grant"].get("comparison_target")
                    if targeted:
                        if any(
                            target[k] != request[k]
                            for k in (
                                "route_id",
                                "provider_config_ref",
                                "model",
                                "policy_id",
                            )
                        ):
                            raise ValueError("refresh_control_target_changed")
                    elif (
                        physical["route_id"] != request["route_id"]
                        or physical["route_provider"] != request["provider_config_ref"]
                    ):
                        raise ValueError("refresh_control_route_changed")
                    scope = bindings.get(qid)
                    local = (
                        item["entity_id"] == request["entity_id"]
                        and scope is not None
                        and item["scope"] == scope["scope"]
                        and item["scope_id"] == scope["scope_id"]
                    )
                    # Each subject reads the same global refs. Validate all native
                    # proofs first, then select this exact local scope; IQS checks
                    # the aggregated controls cover every requested ref once.
                    if targeted and not local:
                        continue
                    if (
                        not local
                        or qid in seen
                        or qid not in work
                        or work[qid]["work_item_id"] != item["work_item_id"]
                    ):
                        raise ValueError("refresh_control_source_work_changed")
                    projected = project_owner_control(
                        document,
                        source_work=dict(item),
                        subject_ref=physical["subject_binding"]["subject_ref"],
                        provider=(target["provider"] if targeted else physical["provider"]),
                        model=request["model"],
                        now=time.time(),
                        cohort_id=request["cohort_id"],
                    )
                    work[qid].update(
                        owner_control=projected["owner_control"],
                        control_dispatch=projected["decision"],
                        controlled_generation=projected["generation"],
                    )
                    seen.add(qid)
                if seen != set(request["question_ids"]):
                    raise ValueError("refresh_control_scope_incomplete")
            allowed_currencies = (
                {"USD", "CNY"} if schema_version in {"1.1.0", "1.2.0", "1.3.0"} else {"USD"}
            )
            if status["currency"] not in allowed_currencies:
                raise ValueError("refresh_executor_currency_unsupported")
            remaining = max(
                0,
                status["max_cost_micros"] - status["spent_micros"] - status["reserved_micros"],
            )
            requests = max(0, status["max_requests"] - status["requests"])
            quote = dict(
                currency=status["currency"],
                max_cost_micros=remaining,
                max_http_requests=requests,
                budget_state_sha256=canonical_sha256(
                    dict(
                        policy_sha256=stored["policy_snapshot_sha256"],
                        status=status,
                        work_items=work,
                        ledger_ref=canonical_sha256(str(view.path)),
                    )
                ),
                budget_available=(
                    status["unreconciled_attempts"] == 0
                    and requests > 0
                    and remaining >= normalized["max_cost_per_attempt_micros"]
                ),
            )
            return dict(
                protocol="stockqa.refresh_inspection/" + schema_version,
                request_sha256=canonical_sha256(request),
                quote=quote,
                work_items=work,
                ledger_budget_sha256=canonical_sha256(
                    dict(
                        policy_sha256=stored["policy_snapshot_sha256"],
                        status=status,
                        ledger_ref=canonical_sha256(str(view.path)),
                    )
                ),
                max_cost_per_attempt_micros=normalized["max_cost_per_attempt_micros"],
                pricing_basis="remaining_shared_Q09_budget_cap",
                reservation_created=False,
                API_requests=0,
            )
        finally:
            view._inspection_connection = None
            connection.rollback()
