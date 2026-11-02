"""Read-only owner inspection through the existing Q09/Q13 ledger algorithms.

Trusted deployment configuration supplies the policy, not a consumer request.
The quote is a conservative shared-budget ceiling, not a model price forecast.
Approval/paid dispatch must separately recheck and reserve via original Q09.
"""

import copy
import hashlib
import json
import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path
from typing import Callable

from src.utils.quick_scan_refresh_admission_journal import read_record
from src.utils.quick_scan_result_outbox import canonical_sha256
from src.utils.quick_scan_work_store import (
    SCHEMA_VERSION,
    QuickScanWorkStore,
    _budget_policy_projection,
)

# Keep every previously supported read-only schema. Inspection must not migrate
# an owner's v22 database merely because the writer now knows v23.
READ_ONLY_SCHEMA_VERSIONS = frozenset({18, 22, 23, 24, 25, SCHEMA_VERSION})


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
        sidecars = tuple(
            raw_path.with_name(raw_path.name + suffix) for suffix in ("-wal", "-shm", "-journal")
        )
        for entry in (raw_path, *raw_path.parents, *sidecars):
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
        # Sidecar existence is normal concurrency, not corruption. SQLite's
        # mode=ro transaction reads committed WAL data and rejects actual locks.
        # Never delete sidecars or read with immutable=1 to bypass the writer.
        if any(p.exists() and not p.is_file() for p in sidecars):
            raise ValueError("refresh_executor_store_unavailable")

    def _connect(self):
        if self._inspection_connection is not None:
            return _Borrowed(self._inspection_connection)
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in READ_ONLY_SCHEMA_VERSIONS:
                raise ValueError("refresh_executor_schema_unavailable")
            self._validate_schema(connection, schema_version=version, audit_records=False)
        except Exception:
            connection.close()
            raise
        return connection

    def get_registered_refresh_action(self, confirmation_id: str) -> dict | None:
        """Read the original immutable action through its native journal reader."""
        with closing(self._connect()) as connection:
            result: dict | None = read_record(connection, confirmation_id)
            return result

    def continuation_binding_snapshot(
        self, *, entity_id, question_ids, scope_bindings, comparison_refs=()
    ):
        """Read continuation rows/bindings atomically, without claiming authority.

        The owner retains the SQLite transaction lifecycle. Finished unrelated
        rows are not inspected; exact comparison references can select their
        original delivered row. Missing bindings remain None for the consumer
        to report, rather than inventing or initializing owner records.
        """
        refs = set(comparison_refs)
        snapshot = ReadOnlyWorkView(self.path)
        with closing(snapshot._connect()) as connection:
            connection.execute("BEGIN")
            snapshot._inspection_connection = connection
            try:
                rows = snapshot.owner_refresh_projection(
                    entity_id=entity_id,
                    question_ids=question_ids,
                    scope_bindings=scope_bindings,
                    include_owner_decisions=bool(refs),
                )
                result = {}
                for qid, row in rows.items():
                    comparison = (
                        row["status"] == "delivered"
                        and row.get("retry_purpose") == "comparison"
                        and (row["work_item_id"], row.get("retry_decision_id")) in refs
                    )
                    if (
                        row["status"] not in {"pending", "leased", "uncertain", "result_ready"}
                        and not comparison
                    ):
                        continue
                    result[qid] = {
                        "work": row,
                        "binding_record": snapshot.get_owner_refresh_binding(row["work_item_id"]),
                    }
                return result
            finally:
                snapshot._inspection_connection = None

    def _transaction(self):
        raise ValueError("refresh_inspection_is_read_only")


def delivery_scope_projection(path: Path, request: dict) -> dict:
    """Project original work and sealed packages for the requested delivery scope."""
    store = ReadOnlyWorkView(path)
    items = []
    work_items = []
    consumers = []
    qids = request["question_ids"]
    if not isinstance(qids, list) or not qids or len(set(qids)) != len(qids):
        raise ValueError("original_work_scope_invalid")
    bindings = request.get("scope_bindings") or {
        qid: {"scope": "entity", "scope_id": request["entity_id"]} for qid in qids
    }
    if set(bindings) != set(qids) or any(
        not isinstance(value, dict)
        or set(value) != {"scope", "scope_id"}
        or value["scope"] not in {"entity", "security", "segment"}
        or not isinstance(value["scope_id"], str)
        or not value["scope_id"]
        or value["scope"] == "entity"
        and value["scope_id"] != request["entity_id"]
        for value in bindings.values()
    ):
        raise ValueError("original_work_scope_invalid")
    work_ids = request.get("work_ids")
    registered = {}
    if request.get("confirmation_id"):
        record = store.get_registered_refresh_action(request["confirmation_id"])
        if record is None or record["receipt"]["confirmation_id"] != request["confirmation_id"]:
            raise ValueError("original_refresh_action_missing")
        registered = {
            row["work_item_id"]: row
            for row in record["receipt"]["items"]
            if row["entity_id"] == request["entity_id"]
            and row["question_id"] in qids
            and all(row[key] == bindings[row["question_id"]][key] for key in ("scope", "scope_id"))
        }
        references = {
            row["question_id"]
            for row in record["inputs"].get("references", [])
            if row["subject_ref"]["entity_id"] == request["entity_id"]
            and row["question_id"] in qids
            and all(
                row["subject_ref"][key] == bindings[row["question_id"]][key]
                for key in ("scope", "scope_id")
            )
        }
        covered = {row["question_id"] for row in registered.values()}
        if covered | references != set(qids):
            raise ValueError("original_registered_scope_mismatch")
        work_ids = list(registered)
        for qid in references - covered:
            items.append(
                {
                    "question_id": qid,
                    "work_item_id": None,
                    "status": "referenced",
                    "reason": "original_observation_reference",
                }
            )
    elif work_ids is None:
        if not request.get("read_only_scope"):
            raise ValueError("original_refresh_action_required")
        projection = store.owner_refresh_projection(
            entity_id=request["entity_id"], question_ids=qids, scope_bindings=bindings
        )
        work_ids = [projection[qid]["work_item_id"] for qid in qids if qid in projection]
        for qid in qids:
            if qid not in projection:
                work_items.append({"question_id": qid, "work_item_id": None, "status": "missing"})
                items.append(
                    {
                        "question_id": qid,
                        "work_item_id": None,
                        "status": "not_exportable",
                        "reason": "missing",
                    }
                )
    for work_id in work_ids:
        work = store.get_item(work_id)
        qid = work["question_id"]
        if (
            work["entity_id"] != request["entity_id"]
            or qid not in qids
            or any(work[key] != bindings[qid][key] for key in ("scope", "scope_id"))
        ):
            raise ValueError("original_work_scope_mismatch")
        if work_id in registered and any(
            work[key] != registered[work_id][key]
            for key in (
                "work_item_id",
                "entity_id",
                "question_id",
                "generation",
                "scope",
                "scope_id",
            )
        ):
            raise ValueError("original_registered_work_mismatch")
        work_items.append(
            {key: work[key] for key in ("question_id", "work_item_id", "status", "generation")}
        )
        if request.get("read_only_scope"):
            continue
        row = store.get_result_delivery(work_id)
        if request.get("receiver_only"):
            binding = None if row is None else row.get("consumer_binding")
            consumers.append(
                {
                    "work_item_id": work_id,
                    "consumer": None if binding is None else binding["consumer"],
                    "work_store": str(path.resolve()),
                    "package_id": None if row is None else row.get("package_id"),
                    "observation_id": None if row is None else row.get("observation_id"),
                }
            )
            continue
        if (
            row is None
            or row["state"] not in {"ready", "delivered", "send_uncertain"}
            or row["package_bytes"] is None
        ):
            items.append(
                {
                    "work_item_id": work_id,
                    "status": "not_exportable",
                    "reason": None if row is None else row.get("block_code") or row["state"],
                }
            )
            continue
        raw = row["package_bytes"]
        items.append(
            {
                "work_item_id": work_id,
                "status": "exportable",
                "observation_id": row["observation_id"],
                "package_id": row["package_id"],
                "package_json": raw.decode("utf8"),
                "package_bytes_sha256": hashlib.sha256(raw).hexdigest(),
                "delivery_state": row["state"],
            }
        )
    return {
        "items": items,
        "work_items": work_items,
        "consumers": consumers,
        "API_requests": 0,
        "network_calls": 0,
    }


def continuation_bank_bindings(path: Path, request: dict) -> dict:
    """Resolve the exact original continuation bank for a subject and scope."""
    view = ReadOnlyWorkView(path)
    result = []
    controls = request.get("controlled_work_refs", [])
    controlled_refs = {(ref["work_item_id"], ref["decision_id"]) for ref in controls}
    snapshot = view.continuation_binding_snapshot(
        entity_id=request["entity_id"],
        question_ids=list(request["scope_bindings"]),
        scope_bindings=request["scope_bindings"],
        comparison_refs=controlled_refs,
    )
    for qid, entry in snapshot.items():
        row, record = entry["work"], entry["binding_record"]
        if record is None:
            raise ValueError("existing_work_binding_unavailable")
        binding = record["binding"]
        if any(
            binding.get(k) != request[k]
            for k in (
                "entity_id",
                "subject_key",
                "perimeter_sha256",
                "identity_revision",
                "information_cutoff",
            )
        ):
            raise ValueError("existing_work_subject_binding_changed")
        if any(binding.get(k) != v for k, v in request["scope_bindings"][qid].items()):
            raise ValueError("existing_work_scope_changed")
        result.append(
            {
                "question_id": qid,
                "work_item_id": row["work_item_id"],
                "generation": row["generation"],
                "manifest_raw_sha256": binding["manifest_raw_sha256"],
                "binding_sha256": canonical_sha256(binding),
            }
        )
    return {
        "protocol": "iqs.original_work_bank_bindings/1.0.0",
        "rows": result,
        "API_requests": 0,
        "database_writes": 0,
    }


def inspect_refresh(
    path,
    request,
    *,
    policy,
    schema_version="1.0.0",
    admission_config=None,
    budget_policy_file=None,
    _snapshot_view=None,
):
    if schema_version == "1.6.0":
        return _inspect_ordered_refresh(path, request, policy=policy)
    if schema_version not in {"1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0"}:
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
    if schema_version in {"1.4.0", "1.5.0"}:
        required |= {"cohort_id", "continuation_decision_ref", "refresh_request"}
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
    exposure_control = None
    if schema_version in {"1.4.0", "1.5.0"}:
        if admission_config is None or budget_policy_file is None:
            raise ValueError("refresh_exposure_native_deployment_required")
        from .quick_scan_owner_budget_continuation import accepted_control
        from .quick_scan_owner_budget_permissions import current_policy

        native_policy = current_policy(
            admission_config,
            request["refresh_request"],
            ledger_path=path,
            budget_policy_file=budget_policy_file,
        )
        if (
            request["continuation_decision_ref"]
            != request["refresh_request"]["continuation_decision_ref"]
            or request["cohort_id"] != request["refresh_request"]["cohort_id"]
        ):
            raise ValueError("refresh_exposure_native_action_mismatch")
        _, exposure_control = accepted_control(
            native_policy, request["refresh_request"], path, now=time.time()
        )
        if native_policy["qa"]["provider_config_ref"] != request["provider_config_ref"]:
            raise ValueError("owner_exposure_original_route_alias_mismatch")
        if any(
            c["route_id"] != request["route_id"]
            or c["provider"] != native_policy["provider"]
            or c["model"] != request["model"]
            or c["subject_ref"]["entity_id"] != request["entity_id"]
            for c in exposure_control["cells"]
        ):
            raise ValueError("refresh_exposure_native_target_mismatch")
    view = _snapshot_view if _snapshot_view is not None else ReadOnlyWorkView(path)
    with closing(view._connect()) as connection:
        if _snapshot_view is None:
            connection.execute("BEGIN")
        # Bind schema and rows to the same native read transaction. A schema
        # change after the initial connection check must not be overlooked.
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in READ_ONLY_SCHEMA_VERSIONS:
            raise ValueError("refresh_executor_schema_unavailable")
        view._validate_schema(connection, schema_version=version, audit_records=False)
        if _snapshot_view is None:
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
                    checkpoint_recovery = (
                        document["request"].get("purpose") == "retry_unusable_checkpoint"
                    )
                    if (
                        document["protocol"]
                        != "stockqa.owner_work_decision/"
                        + ("1.1.0" if targeted else "1.2.0" if checkpoint_recovery else "1.0.0")
                        or targeted
                        and checkpoint_recovery
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
                    if not local or qid in seen or qid not in work:
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
                    if work[qid]["work_item_id"] != item["work_item_id"]:
                        # A recorded controlled action may already have its
                        # exact successor. Its binding, rather than the obsolete
                        # source-is-latest condition, identifies that successor.
                        registered_retry = document["request"].get("purpose") in {
                            "retry_unusable_checkpoint",
                            "retry_unusable_answer",
                            "comparison",
                        }
                        target_binding = (
                            view.get_owner_refresh_binding(work[qid]["work_item_id"])
                            if registered_retry
                            else None
                        )
                        if (
                            not registered_retry
                            or target_binding is None
                            or work[qid]["generation"] != projected["generation"]
                            or canonical_sha256(target_binding["binding"].get("owner_control"))
                            != canonical_sha256(projected["owner_control"])
                        ):
                            raise ValueError("refresh_control_source_work_changed")
                    work[qid].update(
                        owner_control=projected["owner_control"],
                        control_dispatch=projected["decision"],
                        controlled_generation=projected["generation"],
                    )
                    seen.add(qid)
                if seen != set(request["question_ids"]):
                    raise ValueError("refresh_control_scope_incomplete")
            allowed_currencies = (
                {"USD", "CNY"}
                if schema_version in {"1.1.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0"}
                else {"USD"}
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
            if exposure_control is not None:
                if version != SCHEMA_VERSION:
                    raise ValueError("owner_exposure_writer_schema_required")
                from .quick_scan_owner_budget_continuation import (
                    risk_hold_total,
                    validate_retained_rows,
                )

                refs = validate_retained_rows(connection, exposure_control, request["policy_id"])
                marks = ",".join("?" for _ in refs)
                blockers = connection.execute(
                    "SELECT COUNT(*) FROM quick_scan_budget_attempt WHERE policy_id=? AND status IN ('response_unpriced','failure_unpriced','outcome_uncertain') AND budget_attempt_id NOT IN ("  # nosec B608
                    + marks
                    + ")",
                    (request["policy_id"], *refs),
                ).fetchone()[0]
                risk_hold = risk_hold_total(
                    connection,
                    request["policy_id"],
                    prospective_ref=request["continuation_decision_ref"],
                    prospective_control=exposure_control,
                )
                remaining = max(0, remaining - risk_hold)
                physical = len(exposure_control["cells"])
                max_new = min(requests, exposure_control["max_new_http_requests"])
                cell = exposure_control["cells"][0]
                group_limits = json.loads(stored["quota_group_limits_json"])
                route_limits = json.loads(stored["route_limits_json"])
                capacities = connection.execute(
                    "SELECT COALESCE(SUM(in_flight),0),COALESCE(SUM(CASE WHEN quota_group=? THEN in_flight ELSE 0 END),0),COALESCE(SUM(CASE WHEN route_id=? THEN in_flight ELSE 0 END),0) FROM quick_scan_budget_attempt WHERE policy_id=?",
                    (cell["quota_group"], cell["route_id"], request["policy_id"]),
                ).fetchone()
                capacity_available = (
                    capacities[0] < stored["max_in_flight_total"]
                    and capacities[1] < group_limits[cell["quota_group"]]
                    and capacities[2] < route_limits[cell["route_id"]]
                )
                from .quick_scan_owner_budget_continuation import cell_key

                if schema_version == "1.5.0":
                    from .quick_scan_owner_budget_continuation_v15 import (
                        assert_independent,
                    )

                    assert_independent(connection, exposure_control)
                already_allocated = (
                    connection.execute(
                        "SELECT 1 FROM quick_scan_owner_continuation_cell WHERE policy_id=? AND cell_key=?",
                        (request["policy_id"], cell_key(cell)),
                    ).fetchone()
                    is not None
                )

                quote.update(
                    max_cost_micros=min(
                        remaining, max_new * normalized["max_cost_per_attempt_micros"]
                    ),
                    max_http_requests=max_new,
                    planned_physical_requests=physical,
                    retained_unreconciled_attempts=len(refs),
                    cash_actual_complete=False,
                    budget_available=blockers == 0
                    and capacity_available
                    and not already_allocated
                    and max_new >= physical
                    and remaining >= physical * normalized["max_cost_per_attempt_micros"],
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
            if _snapshot_view is None:
                view._inspection_connection = None
                connection.rollback()


def _inspect_ordered_refresh(path, request, *, policy):
    """Compose native 1.1 reads inside one original ledger read snapshot."""
    from .quick_scan_owner_refresh_journal import ordered_targets

    if not isinstance(request, dict) or "ordered_route_targets" not in request:
        raise ValueError("refresh_inspection_request_invalid")
    selected = ordered_targets(request["ordered_route_targets"], model=request.get("model"))
    if any(selected[0][key] != request.get(key) for key in ("route_id", "provider_config_ref")):
        raise ValueError("refresh_executor_policy_route_mismatch")
    frozen_policy = copy.deepcopy(policy)
    normalized = _budget_policy_projection(frozen_policy)
    for target in selected:
        if normalized["route_identity"].get(target["route_id"]) != {
            key: target[key] for key in ("provider_config_ref", "model", "quota_group")
        }:
            raise ValueError("refresh_executor_policy_route_mismatch")
    view = ReadOnlyWorkView(path)
    inspections = []
    with closing(view._connect()) as connection:
        connection.execute("BEGIN")
        view._inspection_connection = connection
        try:
            for target in selected:
                current = {
                    key: copy.deepcopy(value)
                    for key, value in request.items()
                    if key != "ordered_route_targets"
                }
                current.update(
                    {key: target[key] for key in ("route_id", "provider_config_ref", "model")}
                )
                inspections.append(
                    inspect_refresh(
                        path,
                        current,
                        policy=frozen_policy,
                        schema_version="1.1.0",
                        _snapshot_view=view,
                    )
                )
        finally:
            view._inspection_connection = None
            connection.rollback()
    original = inspections[0]
    if any(
        value["ledger_budget_sha256"] != original["ledger_budget_sha256"] for value in inspections
    ):
        raise ValueError("refresh_executor_budget_snapshot_changed")
    result = copy.deepcopy(original)
    result.update(
        protocol="stockqa.refresh_inspection/1.6.0", request_sha256=canonical_sha256(request)
    )
    # These are alternative routes under ONE cap, never independent budgets.
    result["quote"].update(
        max_cost_micros=min(value["quote"]["max_cost_micros"] for value in inspections),
        max_http_requests=min(value["quote"]["max_http_requests"] for value in inspections),
        budget_available=all(value["quote"]["budget_available"] for value in inspections),
        budget_state_sha256=canonical_sha256(
            dict(
                ordered_route_targets=selected,
                quotes=[value["quote"]["budget_state_sha256"] for value in inspections],
            )
        ),
    )
    return result
