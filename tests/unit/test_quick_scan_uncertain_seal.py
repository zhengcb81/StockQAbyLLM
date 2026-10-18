"""Isolated research budget seals/amendments; synthetic temporary SQLite only."""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from src.utils.quick_scan_work_store import (
    BudgetAdmissionError,
    QuickScanWorkStore,
    WorkConflictError,
)
from src.utils.quick_scan_work_transport import (
    QuickScanBudgetDeferredError,
    QuickScanWorkPersistenceError,
    begin_quick_scan_send,
    bind_owner_refresh_guard,
    bind_quick_scan_budget,
    bind_quick_scan_route,
)


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def task(label):
    return {"task_id": label, "task_sha256": sha("frozen task protocol: " + label)}


def policy(cost=1, requests=12, cap=0.1, concurrency=1):
    return {
        "configured": True,
        "policy_id": "same_original_policy",
        "policy_version": "v1",
        "budget": {
            "currency": "USD",
            "max_cost": cost,
            "max_requests": requests,
            "max_cost_per_attempt": cap,
        },
        "cost_policy": {
            "pricing_basis": "verified_rate_card",
            "pricing_ref": "offline-synthetic-rate",
            "reserve_before_dispatch": True,
            "unknown_actual_cost_action": "retain_reservation_and_pause",
        },
        "dispatch": {"max_in_flight_total": concurrency},
        "quota_groups": [{"id": "group", "max_in_flight": concurrency}],
        "routes": [
            {
                "id": "route",
                "provider_config_ref": "fixture",
                "model": "fixture-model",
                "quota_group": "group",
                "max_in_flight": concurrency,
                "eligible": True,
                "unavailable_reason": None,
            }
        ],
    }


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="stockqa-budget-isolation-")
        self.path = Path(self.temp.name) / "work.sqlite"
        self.store = QuickScanWorkStore(self.path)
        self.policy = policy()
        self.store.configure_quick_scan_budget(self.policy)

    def tearDown(self):
        self.temp.cleanup()

    def reserve(self, label, independent=None):
        kwargs = {} if independent is None else {"independent_task": independent}
        return self.store.reserve_budget_attempt(
            self.policy,
            budget_attempt_id="DISPATCH_" + label,
            route_id="route",
            provider="fixture",
            model_requested="fixture-model",
            quota_group="group",
            **kwargs,
        )

    def unknown(self, label="unknown", independent=None):
        self.reserve(label, independent)
        self.store.record_budget_outcome(
            "DISPATCH_" + label, outcome="unknown", http_status_code=None
        )

    def rows(self):
        with closing(sqlite3.connect(self.path)) as c:
            return c.execute(
                "SELECT * FROM quick_scan_budget_attempt ORDER BY budget_attempt_id"
            ).fetchall()

    def seal(self, label="unknown", source=None, change=None):
        with closing(sqlite3.connect(self.path)) as c:
            snapshot = c.execute(
                "SELECT policy_snapshot_sha256 FROM quick_scan_budget_policy"
            ).fetchone()[0]
        audit = {
            "operator_ref": "fixture-reviewed-operator",
            "input_sha256": sha("original-input"),
            "events_sha256": sha("original-request-failed-events"),
            "review_reason": "Fixture process stopped; unknown outcome retained; original task will never resend.",
            "no_retry": True,
            "original_process_stopped": True,
            "policy_snapshot_sha256": snapshot,
            "reserved_cost_micros": 100000,
        }
        audit.update(change or {})
        return self.store.seal_uncertain_budget_attempt(
            "DISPATCH_" + label, source_task=source or task("S1"), audit=audit
        )

    def test_explicit_seal_preserves_original_budget_row(self):
        self.unknown()
        before = self.rows()
        self.seal()
        self.assertEqual(before, self.rows())

    def test_seal_is_no_reconciliation_and_survives_restart(self):
        self.unknown()
        before = self.rows()
        a = self.seal()
        self.assertEqual(a, self.seal())
        self.assertEqual(before, self.rows())
        self.store = QuickScanWorkStore(self.path)
        status = self.store.get_quick_scan_budget_status(
            self.policy["policy_id"], include_isolation=True
        )
        self.assertEqual(
            (status["spent_micros"], status["reserved_micros"], status["requests"]), (0, 100000, 1)
        )
        self.assertEqual((status["in_flight"], status["unreconciled_attempts"]), (1, 1))
        self.assertEqual(
            (status["effective_in_flight"], status["blocking_unreconciled_attempts"]), (0, 0)
        )
        with closing(sqlite3.connect(self.path)) as c:
            self.assertEqual(
                c.execute("SELECT COUNT(*) FROM quick_scan_budget_terminal").fetchone()[0], 0
            )
            for table in ["quick_scan_uncertain_seal", "quick_scan_research_task"]:
                with self.assertRaises(sqlite3.IntegrityError):
                    c.execute(f"DELETE FROM {table}")
        with self.assertRaises(WorkConflictError):
            self.seal(change={"review_reason": "changed"})

    def test_original_and_alias_task_cannot_retry(self):
        self.unknown()
        self.seal()
        with self.assertRaisesRegex(BudgetAdmissionError, "research_task_sealed_no_retry"):
            self.reserve("retry", task("S1"))
        with self.assertRaisesRegex(ValueError, "identity conflicts"):
            self.reserve("alias", {"task_id": "new-name", "task_sha256": task("S1")["task_sha256"]})
        self.assertEqual(len(self.rows()), 1)

    def test_default_callers_still_pause_and_unsealed_unknown_still_blocks(self):
        self.unknown()
        with self.assertRaisesRegex(BudgetAdmissionError, "reconciliation_required"):
            self.reserve("unsealed", task("S2"))
        self.seal()
        with self.assertRaisesRegex(BudgetAdmissionError, "reconciliation_required"):
            self.reserve("legacy")
        self.reserve("next", task("S2"))
        self.store.record_budget_outcome("DISPATCH_next", outcome="unknown", http_status_code=None)
        with self.assertRaisesRegex(BudgetAdmissionError, "reconciliation_required"):
            self.reserve("third", task("S3"))
        self.assertEqual(len(self.rows()), 2)

    def test_exact_remaining_cost_and_request_limits_remain(self):
        for label, cost, requests, reason in [
            ("cost", 0.199999, 10, "budget_cost_limit"),
            ("requests", 1, 1, "budget_request_limit"),
        ]:
            with self.subTest(label=label):
                self.path = Path(self.temp.name) / (label + ".sqlite")
                self.store = QuickScanWorkStore(self.path)
                self.policy = policy(cost=cost, requests=requests)
                self.unknown()
                self.seal()
                with self.assertRaisesRegex(BudgetAdmissionError, reason):
                    self.reserve("next", task("S2"))
                status = self.store.get_quick_scan_budget_status(self.policy["policy_id"])
                self.assertEqual((status["reserved_micros"], status["requests"]), (100000, 1))

    def test_source_registration_and_audit_cas_reject_wrong_claims(self):
        self.unknown(independent=task("S1"))
        for change in [
            {"no_retry": False},
            {"original_process_stopped": False},
            {"reserved_cost_micros": 1},
            {"policy_snapshot_sha256": sha("wrong")},
        ]:
            with self.subTest(change=change), self.assertRaises((ValueError, WorkConflictError)):
                self.seal(change=change)
        with self.assertRaises(WorkConflictError):
            self.seal(source=task("wrong-task"))
        self.seal()

    def test_only_unknown_budget_dispatch_can_be_sealed(self):
        self.reserve("unknown")
        with self.assertRaises(WorkConflictError):
            self.seal()
        self.store.record_budget_outcome(
            "DISPATCH_unknown", outcome="response_available", http_status_code=200
        )
        with self.assertRaises(WorkConflictError):
            self.seal()
        with self.assertRaisesRegex(BudgetAdmissionError, "reconciliation_required"):
            self.reserve("next", task("S2"))

    def test_native_transport_continuation_and_owner_guard(self):
        self.unknown()
        self.seal()
        calls = []
        with bind_quick_scan_budget(
            self.store, self.policy, independent_task=task("S2")
        ), bind_quick_scan_route(
            route_id="route",
            provider="fixture",
            model_requested="fixture-model",
            quota_group="group",
        ), bind_owner_refresh_guard(
            lambda: calls.append("checked")
        ):
            permit = begin_quick_scan_send("public prompt", "public system")
            permit.consume_for_post()
            with self.assertRaises(QuickScanWorkPersistenceError):
                permit.consume_for_post()
        self.assertGreaterEqual(len(calls), 2)
        with closing(sqlite3.connect(self.path)) as c:
            row = c.execute(
                "SELECT task_id FROM quick_scan_research_attempt WHERE budget_attempt_id=?",
                (permit.attempt_id,),
            ).fetchone()
            self.assertEqual(row, ("S2",))
        status = self.store.get_quick_scan_budget_status(
            self.policy["policy_id"], include_isolation=True
        )
        self.assertEqual(
            (status["reserved_micros"], status["in_flight"], status["effective_in_flight"]),
            (200000, 2, 1),
        )

        def deny():
            raise ValueError("fixture revoked owner guard")

        before = self.rows()
        with bind_quick_scan_budget(
            self.store, self.policy, independent_task=task("S3")
        ), bind_owner_refresh_guard(deny):
            with self.assertRaisesRegex(
                QuickScanWorkPersistenceError, "owner_refresh_guard_rejected"
            ):
                begin_quick_scan_send("prompt", "system")
        self.assertEqual(before, self.rows())

    def test_capacity_count_excludes_only_sealed_unknown(self):
        self.unknown()
        self.seal()
        self.reserve("next", task("S2"))
        with self.assertRaisesRegex(BudgetAdmissionError, "dispatch_global_capacity_full"):
            self.reserve("extra", task("S3"))

    def test_transaction_rollback_and_policy_no_reset(self):
        self.unknown()
        self.seal()
        before = self.rows()
        changed = json.loads(json.dumps(self.policy))
        changed["budget"]["max_requests"] = 350
        from src.utils.quick_scan_work_store import BudgetPolicyConflict

        with self.assertRaises(BudgetPolicyConflict):
            self.store.configure_quick_scan_budget(changed)
        changed["policy_version"] = "v2"
        with self.assertRaises(BudgetPolicyConflict):
            self.store.configure_quick_scan_budget(changed)
        self.assertEqual(before, self.rows())

    def test_spent_plus_reserved_exact_boundary_not_a_new_ledger(self):
        self.policy = policy(cost=0.407993, requests=40)
        self.store = QuickScanWorkStore(Path(self.temp.name) / "precise.sqlite")
        self.path = self.store.path
        for i in range(34):
            label = "settled_" + str(i)
            self.reserve(label)
            self.store.record_budget_outcome(
                "DISPATCH_" + label,
                outcome="response_available",
                http_status_code=200,
                actual_cost=Decimal("0.207993") if i == 0 else 0,
                cost_source_ref="explicit-synthetic-not-real-provider",
            )
        self.unknown()
        before = self.rows()
        self.seal()
        self.reserve("next", task("S2"))
        self.assertEqual(before, [r for r in self.rows() if r[0] != "DISPATCH_next"])
        status = self.store.get_quick_scan_budget_status(
            self.policy["policy_id"], include_isolation=True
        )
        self.assertEqual(
            (status["spent_micros"], status["reserved_micros"], status["requests"]),
            (207993, 200000, 36),
        )
        self.store.record_budget_outcome(
            "DISPATCH_next",
            outcome="response_available",
            http_status_code=200,
            actual_cost=Decimal("0.000001"),
            cost_source_ref="explicit-synthetic-not-real-provider",
        )
        with self.assertRaisesRegex(BudgetAdmissionError, "budget_cost_limit"):
            self.reserve("no_room", task("S3"))

    def test_concurrent_admission_still_atomic_after_seal(self):
        from concurrent.futures import ThreadPoolExecutor

        self.unknown()
        self.seal()

        def attempt(label):
            store = QuickScanWorkStore(self.path)
            try:
                store.reserve_budget_attempt(
                    self.policy,
                    budget_attempt_id="DISPATCH_" + label,
                    route_id="route",
                    provider="fixture",
                    model_requested="fixture-model",
                    quota_group="group",
                    independent_task=task(label),
                )
                return "admitted"
            except BudgetAdmissionError as error:
                return error.reason

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, ["S2", "S3"]))
        self.assertEqual(sorted(results), ["admitted", "dispatch_global_capacity_full"])
        self.assertEqual(len(self.rows()), 2)

    def test_schema19_native_migration_preserves_every_old_row(self):
        from src.utils.quick_scan_work_store import SCHEMA_VERSION
        from tests.c06_legacy_fixture import remove_c06_additions

        path = Path(self.temp.name) / "legacy19.sqlite"
        # Populate through the native API, then remove all later empty additive
        # metadata using the shared disposable historical-fixture builder.
        native = QuickScanWorkStore(path)
        native.configure_quick_scan_budget(self.policy)
        native.reserve_budget_attempt(
            self.policy,
            budget_attempt_id="DISPATCH_old",
            route_id="route",
            provider="fixture",
            model_requested="fixture-model",
            quota_group="group",
        )
        native.record_budget_outcome("DISPATCH_old", outcome="unknown", http_status_code=None)
        with closing(sqlite3.connect(path)) as c:
            remove_c06_additions(c, after_version=19)
            c.execute("PRAGMA user_version=19")
            c.commit()
            c.row_factory = sqlite3.Row
            QuickScanWorkStore._validate_schema(c, schema_version=19)
        with closing(sqlite3.connect(path)) as c:
            tables = [
                r[0]
                for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
            before = {t: c.execute(f"SELECT * FROM {t}").fetchall() for t in tables}
        migrated = QuickScanWorkStore(path)
        with closing(sqlite3.connect(path)) as c:
            self.assertEqual(
                before, {t: c.execute(f"SELECT * FROM {t}").fetchall() for t in tables}
            )
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(
            migrated.get_quick_scan_budget_status(self.policy["policy_id"])["reserved_micros"],
            100000,
        )

    def amendment(self, new=None):
        from src.utils.quick_scan_work_store import _budget_policy_projection

        new = new or json.loads(json.dumps(self.policy))
        new["policy_version"] = "v2"
        new["budget"]["max_cost"] = 25
        new["budget"]["max_requests"] = 350
        new["cost_policy"]["pricing_ref"] = "frozen-multiple-model-rates"
        new["quota_groups"].append({"id": "group2", "max_in_flight": 1})
        new["routes"].append(
            {
                "id": "route2",
                "provider_config_ref": "fixture2",
                "model": "fixture-model2",
                "quota_group": "group2",
                "max_in_flight": 1,
                "eligible": True,
                "unavailable_reason": None,
            }
        )
        kwargs = {
            "old_policy": self.policy,
            "new_policy": new,
            "expected_policy_snapshot_sha256": _budget_policy_projection(self.policy)[
                "policy_snapshot_sha256"
            ],
            "audit": {
                "operator_ref": "fixture-reviewed-operator",
                "review_reason": "Explicit synthetic multi-model continuation request",
                "input_sha256": sha("frozen-amendment-input"),
            },
        }
        return kwargs

    def test_explicit_amendment_admits_reviewed_policy(self):
        self.store.amend_isolated_research_budget(**self.amendment())

    def new_owner_work(self):
        item = self.store.create_or_attach(
            entity_id="ENT_FIXTURE",
            question_id="CORE_01",
            generation=1,
            scope="entity",
            scope_id="ENT_FIXTURE",
            identity_revision=1,
            source_binding_version=1,
            identity_state="verified",
            source_binding_ref="BND_FIXTURE",
            source_binding_refs=["BND_FIXTURE"],
            identity_snapshot_sha256=sha("identity"),
            question_fingerprint=sha("question"),
            routing_fingerprint=sha("routing"),
            run_id="RUN_FIXTURE",
            scan_id="SCAN_FIXTURE",
        )
        lease = self.store.claim(item["work_item_id"], lease_seconds=60)
        return item["work_item_id"], lease

    def test_independent_binding_never_bypasses_owner_work_path(self):
        from src.utils.quick_scan_work_transport import bind_quick_scan_work

        self.unknown()
        self.seal()
        work_id, lease = self.new_owner_work()
        before = self.rows()
        with bind_quick_scan_work(self.store, work_id, lease), bind_quick_scan_budget(
            self.store, self.policy, independent_task=task("S2")
        ), bind_quick_scan_route(
            route_id="route",
            provider="fixture",
            model_requested="fixture-model",
            quota_group="group",
        ):
            with self.assertRaisesRegex(
                QuickScanWorkPersistenceError, "research_continuation_cannot_bind_owner_work"
            ):
                begin_quick_scan_send("public", "system")
        self.assertEqual(before, self.rows())

    def test_amendment_never_changes_policy_of_owner_work_store(self):
        from src.utils.quick_scan_work_store import BudgetPolicyConflict

        self.new_owner_work()
        with self.assertRaisesRegex(BudgetPolicyConflict, "cannot affect owner work"):
            self.store.amend_isolated_research_budget(**self.amendment())

    def test_explicit_amendment_preserves_unknown_and_allows_new_native_route(self):
        self.unknown()
        self.seal()
        before = self.rows()
        kwargs = self.amendment()
        first = self.store.amend_isolated_research_budget(**kwargs)
        self.assertEqual(first, self.store.amend_isolated_research_budget(**kwargs))
        self.assertEqual(before, self.rows())
        self.store = QuickScanWorkStore(self.path)
        status = self.store.get_quick_scan_budget_status(
            self.policy["policy_id"], include_isolation=True
        )
        self.assertEqual(
            (
                status["max_requests"],
                status["max_cost_micros"],
                status["reserved_micros"],
                status["requests"],
                status["in_flight"],
            ),
            (350, 25000000, 100000, 1, 1),
        )
        self.assertEqual(
            self.store.configure_quick_scan_budget(kwargs["new_policy"])["policy_version"], "v2"
        )
        from src.utils.quick_scan_work_store import BudgetPolicyConflict

        with self.assertRaises(BudgetPolicyConflict):
            self.store.configure_quick_scan_budget(self.policy)
        with bind_quick_scan_budget(
            self.store, kwargs["new_policy"], independent_task=task("S2")
        ), bind_quick_scan_route(
            route_id="route2",
            provider="fixture2",
            model_requested="fixture-model2",
            quota_group="group2",
        ):
            permit = begin_quick_scan_send("public new experiment", "public new system")
            self.assertIsNotNone(permit)
        with closing(sqlite3.connect(self.path)) as c:
            self.assertEqual(
                c.execute("SELECT COUNT(*) FROM quick_scan_research_budget_amendment").fetchone()[
                    0
                ],
                1,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("DELETE FROM quick_scan_research_budget_amendment")

    def test_amendment_rejects_unsealed_unknown_active_and_unpriced(self):
        from src.utils.quick_scan_work_store import BudgetPolicyConflict

        self.unknown()
        with self.assertRaises(BudgetPolicyConflict):
            self.store.amend_isolated_research_budget(**self.amendment())
        self.seal()
        self.reserve("active", task("S2"))
        with self.assertRaises(BudgetPolicyConflict):
            self.store.amend_isolated_research_budget(**self.amendment())
        self.store.record_budget_outcome(
            "DISPATCH_active", outcome="response_available", http_status_code=200
        )
        with self.assertRaises(BudgetPolicyConflict):
            self.store.amend_isolated_research_budget(**self.amendment())

    def test_amendment_rejects_cap_decrease_route_changes_and_stale_cas(self):
        from src.utils.quick_scan_work_store import BudgetPolicyConflict

        self.unknown()
        self.seal()
        kwargs = self.amendment()
        before = self.rows()

        def change(group, key, value):
            proposal = json.loads(json.dumps(kwargs))
            proposal["new_policy"][group][key] = value
            return proposal

        variants = [
            change("budget", "max_requests", 1),
            change("budget", "currency", "CNY"),
            change("budget", "max_cost_per_attempt", 0.01),
            change("dispatch", "max_in_flight_total", 2),
            change("cost_policy", "pricing_basis", "user_cap"),
        ]
        for field, value in [
            ("provider_config_ref", "other"),
            ("max_in_flight", 2),
            ("eligible", False),
        ]:
            proposal = json.loads(json.dumps(kwargs))
            proposal["new_policy"]["routes"][0][field] = value
            variants.append(proposal)
        proposal = json.loads(json.dumps(kwargs))
        proposal["expected_policy_snapshot_sha256"] = sha("stale")
        variants.append(proposal)
        for proposal in variants:
            with self.subTest(proposal=proposal), self.assertRaises(BudgetPolicyConflict):
                self.store.amend_isolated_research_budget(**proposal)
        self.assertEqual(before, self.rows())
        with closing(sqlite3.connect(self.path)) as c:
            self.assertEqual(
                c.execute("SELECT COUNT(*) FROM quick_scan_research_budget_amendment").fetchone()[
                    0
                ],
                0,
            )
