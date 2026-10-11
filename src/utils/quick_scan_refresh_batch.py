"""Atomic composition of original Q13 create_or_attach, never a second queue.

This is an internal storage primitive, NOT user authentication or a public
refresh-admission endpoint. Its trusted caller must bind verified native owner
consent, the current route and exact manifest/subject scope before supplying
specifications. Rechecks may raise but cannot grant authority via JSON flags.
No lease, HTTP attempt, budget reservation or schema migration happens here.
"""

import copy
from contextlib import contextmanager

from src.utils.quick_scan_result_outbox import canonical_bytes
from src.utils.quick_scan_work_store import SCHEMA_VERSION, QuickScanWorkStore

SPEC_FIELDS = {
    "entity_id",
    "question_id",
    "generation",
    "scope",
    "scope_id",
    "identity_revision",
    "source_binding_version",
    "identity_state",
    "source_binding_ref",
    "source_binding_refs",
    "identity_snapshot_sha256",
    "question_fingerprint",
    "routing_fingerprint",
    "owner_refresh_binding",
}


class _TransactionView(QuickScanWorkStore):
    def __init__(self, store, connection):
        # No base initializer/DDL. One outer original transaction owns this.
        self.path, self.clock, self.connection = store.path, store.clock, connection

    @contextmanager
    def _transaction(self):
        yield self.connection


def attach_owner_refresh_batch(store, specifications, *, run_id, scan_id, recheck):
    if (
        not isinstance(specifications, list)
        or not 1 <= len(specifications) <= 20000
        or len(canonical_bytes(specifications)) > 8 * 1024 * 1024
        or not callable(recheck)
    ):
        raise ValueError("refresh_batch_invalid")
    specs = copy.deepcopy(specifications)
    keys = set()
    for spec in specs:
        if not isinstance(spec, dict) or set(spec) != SPEC_FIELDS:
            raise ValueError("refresh_batch_spec_invalid")
        key = tuple(spec[k] for k in ("entity_id", "question_id", "scope", "scope_id"))
        if any(not isinstance(k, str) for k in key):
            raise ValueError("refresh_batch_scope_invalid")
        if key in keys:
            raise ValueError("refresh_batch_duplicate_question_scope")
        keys.add(key)
        binding = spec["owner_refresh_binding"]
        if (
            not isinstance(binding, dict)
            or binding.get("protocol") != "stockqa.owner_refresh_binding/2.0.0"
            or binding.get("subject_context_schema")
            != "stockqa.quick_scan_observation_context/2.0.0"
        ):
            raise ValueError("refresh_batch_subject_context_required")
    items = []
    with store._transaction() as connection:
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ValueError("refresh_batch_executor_schema_unavailable")
        store._validate_schema(connection, schema_version=SCHEMA_VERSION)
        recheck()
        view = _TransactionView(store, connection)
        for spec in specs:
            row = view.create_or_attach(**spec, run_id=run_id, scan_id=scan_id)
            items.append(
                dict(
                    work_item_id=row["work_item_id"],
                    entity_id=row["entity_id"],
                    question_id=row["question_id"],
                    generation=row["generation"],
                    scope=row["scope"],
                    scope_id=row["scope_id"],
                )
            )
        # All original writes roll back if owner/permission changes mid-batch.
        # A callback must not recursively write this ledger or issue a model
        # call while its native transaction is held.
        recheck()
    return dict(
        protocol="stockqa.refresh_batch_registration/1.0.0",
        items=items,
        run_id=run_id,
        scan_id=scan_id,
        dispatch_started=False,
        API_requests=0,
        permission_authenticated=False,
    )


def register_refresh_action(
    store, specifications, *, action, run_id, scan_id, recheck, references=None
):
    """Internal atomic action registration, never an authentication endpoint.

    The first fresh recheck happens before writes. Following writes we check
    stable current permissions without reading our uncommitted ledger through
    another process. Q13's write transaction fences concurrent budget writers.
    Durable replay always rechecks permissions and never creates another work.
    """
    from . import quick_scan_refresh_admission_journal as journal

    if not callable(recheck):
        raise ValueError("refresh_admission_recheck_required")
    value = journal.inputs(action, specifications, run_id, scan_id, references)
    with store._transaction() as connection:
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ValueError("refresh_admission_executor_schema_unavailable")
        store._validate_schema(connection, schema_version=SCHEMA_VERSION)
        existing = journal.read_record(connection, action["confirmation_id"])
        if existing is not None:
            if existing["inputs"] != value:
                raise ValueError("refresh_admission_idempotency_conflict")
            recheck("current_permissions")
            recheck("current_permissions")
            return existing["receipt"]
        recheck("fresh_registration")
        view = _TransactionView(store, connection)
        items = []
        if specifications:
            result = attach_owner_refresh_batch(
                view,
                specifications,
                run_id=run_id,
                scan_id=scan_id,
                recheck=lambda: recheck("current_permissions"),
            )
            items = result["items"]
        else:
            recheck("current_permissions")
        receipt = journal.publish(connection, value, items, registered_at=store._now())
        recheck("current_permissions")
    return receipt
