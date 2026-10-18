"""Synthetic isolated native SQLite: 16 settled + 1 dispatched unknown, no HTTP."""

import copy
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src.utils.quick_scan_external_journal import _digest
from src.utils.quick_scan_work_store import SCHEMA_VERSION, Lease, QuickScanWorkStore


def fixture(root):
    store = QuickScanWorkStore(root / "native.sqlite", clock=lambda: 1000.0)
    policy = {
        "configured": True,
        "policy_id": "synthetic-continuation-only",
        "policy_version": "fixture/1",
        "budget": {
            "currency": "CNY",
            "max_cost": 25,
            "max_requests": 150,
            "max_cost_per_attempt": 1,
        },
        "dispatch": {"max_in_flight_total": 1},
        "cost_policy": {
            "pricing_basis": "verified_rate_card",
            "pricing_ref": "synthetic",
            "reserve_before_dispatch": True,
            "unknown_actual_cost_action": "retain_reservation_and_pause",
        },
        "quota_groups": [{"id": "search", "max_in_flight": 1}],
        "routes": [
            {
                "id": "brave",
                "provider_config_ref": "brave",
                "model": "external_retrieval_v1",
                "quota_group": "search",
                "max_in_flight": 1,
            }
        ],
    }
    projection = store.configure_quick_scan_budget(policy)
    item = store.create_or_attach(
        entity_id="ENT_SYNTHETIC_CONTINUATION",
        question_id="Q_fixture",
        generation=1,
        scope="entity",
        scope_id="ENT_SYNTHETIC_CONTINUATION",
        identity_revision=1,
        source_binding_version=1,
        identity_state="provisional",
        source_binding_ref="BND_fixture",
        source_binding_refs=["BND_fixture"],
        identity_snapshot_sha256="a" * 64,
        question_fingerprint="b" * 64,
        routing_fingerprint="c" * 64,
        run_id="fixture",
        scan_id="fixture",
    )
    lease = store.claim(item["work_item_id"], lease_seconds=8000)
    slots = []
    operations = []
    for index in range(36):
        plan = {
            "schema_version": "quick_scan_external_plan/1.1.0",
            "entity_id": item["entity_id"],
            "identity_snapshot_sha256": "a" * 64,
            "question_manifest_sha256": "b" * 64,
            "question_ids": ["Q_fixture"],
            "query_id": "query" + str(index + 1),
            "query": "Explicit synthetic offline query " + str(index + 1),
            "information_as_of": "2026-10-17",
            "locale": "en-US",
            "ttl_seconds": 86400,
            "top_k": 5,
            "company_limit": 30000,
            "snippet_limit": 500,
            "entity_domain_bindings": [
                {
                    "host": "fixture.example",
                    "path_prefix": "/",
                    "binding_kind": "issuer_owned_domain",
                    "source_ref": "synthetic-only",
                    "identity_snapshot_sha256": "a" * 64,
                }
            ],
        }
        request = {
            "plan": plan,
            "route_id": "brave",
            "provider": "brave",
            "quota_group": "search",
            "adapter_version": "stockqa.external_retrieval/1.0.0",
            "search_policy_sha256": "d" * 64,
            "endpoint": "https://api.search.brave.com/res/v1/web/search",
        }
        slots.append(
            {"work_item_id": item["work_item_id"], "dispatch_enabled": True, "request": request}
        )
        if index < 17:
            operation = store.begin_external_search(
                item["work_item_id"], lease, policy=policy, **request
            )
            store.consume_external_search(
                operation["operation_id"], budget_attempt_id=operation["budget_attempt_id"]
            )
            if index < 16:
                receipt = {
                    "origin": "external",
                    "adapter_version": request["adapter_version"],
                    "route_id": "brave",
                    "route_kind": "brave",
                    "attempt_id": operation["budget_attempt_id"],
                    "http_request_count": 1,
                    "http_status_code": 200,
                    "request_id": "synthetic",
                    "outcome": "response_available",
                    "response_body_sha256": "e" * 64,
                    "parse_status": "ok",
                    "provider_result_count": 1,
                    "entries": [
                        {
                            "title": "Synthetic fixture",
                            "url": "https://fixture.example/" + str(index),
                            "snippet": "Synthetic short evidence",
                            "published_at": "2024-01-01",
                            "publisher": None,
                            "query": plan["query"],
                        }
                    ],
                }
                store.record_external_search(
                    operation["operation_id"],
                    receipt=receipt,
                    actual_cost=0.01,
                    cost_source_ref="synthetic",
                )
            operations.append(operation)
    return store, policy, projection, item, lease, slots, operations


def seal(store, policy, projection, operation):
    source = {"task_id": "fixture_original", "task_sha256": "1" * 64}
    store.record_budget_outcome(
        operation["budget_attempt_id"], outcome="unknown", http_status_code=None
    )
    store.seal_uncertain_budget_attempt(
        operation["budget_attempt_id"],
        source_task=source,
        audit={
            "operator_ref": "synthetic-test-not-user-authority",
            "input_sha256": "2" * 64,
            "events_sha256": "3" * 64,
            "review_reason": "synthetic stopped caller, response not durably recoverable; no retry",
            "no_retry": True,
            "original_process_stopped": True,
            "policy_snapshot_sha256": projection["policy_snapshot_sha256"],
            "reserved_cost_micros": 1000000,
        },
    )
    return source


def registration(store, policy, source, slots):
    from src.utils.quick_scan_external_continuation import slot_key
    from src.utils.quick_scan_external_journal import _request_context

    normalized = store.configure_quick_scan_budget(policy)
    allowed = []
    with store._transaction() as connection:
        for slot in slots[17:]:
            row = store._item(connection, slot["work_item_id"])
            ctx = _request_context(
                store,
                connection,
                slot["work_item_id"],
                Lease(row["lease_token"], row["lease_epoch"]),
                normalized=normalized,
                **slot["request"],
            )
            allowed.append(slot_key(ctx))
    reg = {
        "schema": "stockqa.external_research_continuation/1.0.0",
        "operator_ref": "synthetic finite matrix review",
        "production_owner_verified": False,
        "source_task": source,
        "planned_slots": slots,
        "expected_allowed_slot_keys": sorted(allowed),
        "original_matrix_proof": {
            "classification": "operator_reviewed_research_not_preexisting_native_matrix",
            "source_config_raw_sha256": "a" * 64,
            "source_sdk_lock_raw_sha256": "b" * 64,
            "research_binding_raw_sha256": "c" * 64,
            "source_task_input_sha256": source["task_sha256"],
        },
    }
    return {"task_id": "fixture_continuation", "task_sha256": _digest(reg)}, reg


class ContinuationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="continuation-tests-")
        cls.root = Path(cls.temp.name)
        cls.original = cls.root / "original"
        cls.original.mkdir()
        cls.seed = fixture(cls.original)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.directory = self.root / self._testMethodName
        self.directory.mkdir()
        shutil.copyfile(self.original / "native.sqlite", self.directory / "native.sqlite")
        self.store = QuickScanWorkStore(self.directory / "native.sqlite", clock=lambda: 1000.0)
        _, self.policy, self.projection, self.item, self.lease, self.slots, self.ops = self.seed

    def admitted(self):
        source = seal(self.store, self.policy, self.projection, self.ops[-1])
        task, reg = registration(self.store, self.policy, source, self.slots)
        self.store.register_external_research_continuation(
            policy=self.policy, independent_task=task, registration=reg
        )
        return task, reg

    def test_finite_difference_can_continue_after_seal_without_replaying_17(self):
        task, reg = self.admitted()
        self.assertEqual(len(reg["expected_allowed_slot_keys"]), 19)
        before = self.store.get_quick_scan_budget_status(self.policy["policy_id"])
        for slot in self.slots[:17]:
            with self.assertRaises(Exception):
                self.store.begin_external_search(
                    slot["work_item_id"],
                    self.lease,
                    policy=self.policy,
                    independent_task=task,
                    **slot["request"],
                )
        after = self.store.get_quick_scan_budget_status(self.policy["policy_id"])
        self.assertEqual(before, after)
        slot = self.slots[17]
        new = self.store.begin_external_search(
            slot["work_item_id"],
            self.lease,
            policy=self.policy,
            independent_task=task,
            **slot["request"],
        )
        self.assertTrue(new["send_required"])
        self.assertEqual(new["context"]["research_task"], task)
        unknown = self.store.get_external_search(self.ops[-1]["operation_id"])["budget"]
        self.assertEqual(unknown["status"], "outcome_uncertain")
        self.assertEqual(unknown["reserved_cost_micros"], 1000000)

    def test_legacy_no_task_still_blocks_after_seal(self):
        self.admitted()
        slot = self.slots[17]
        before = self.store.get_quick_scan_budget_status(self.policy["policy_id"])
        with self.assertRaises(Exception):
            self.store.begin_external_search(
                slot["work_item_id"], self.lease, policy=self.policy, **slot["request"]
            )
        self.assertEqual(before, self.store.get_quick_scan_budget_status(self.policy["policy_id"]))

    def test_same_original_task_cannot_send(self):
        _, reg = self.admitted()
        slot = self.slots[17]
        with self.assertRaises(Exception):
            self.store.begin_external_search(
                slot["work_item_id"],
                self.lease,
                policy=self.policy,
                independent_task=reg["source_task"],
                **slot["request"],
            )

    def test_unsealed_original_cannot_register(self):
        source = {"task_id": "fixture_original", "task_sha256": "1" * 64}
        task, reg = registration(self.store, self.policy, source, self.slots)
        before = self.store.get_quick_scan_budget_status(self.policy["policy_id"])
        with self.assertRaises(Exception):
            self.store.register_external_research_continuation(
                policy=self.policy, independent_task=task, registration=reg
            )
        self.assertEqual(before, self.store.get_quick_scan_budget_status(self.policy["policy_id"]))

    def test_response_unpriced_is_not_sealable_or_excludable(self):
        self.store.record_budget_outcome(
            self.ops[-1]["budget_attempt_id"], outcome="response_available", http_status_code=200
        )
        source = {"task_id": "fixture_original", "task_sha256": "1" * 64}
        task, reg = registration(self.store, self.policy, source, self.slots)
        with self.assertRaises(Exception):
            self.store.register_external_research_continuation(
                policy=self.policy, independent_task=task, registration=reg
            )
        self.assertEqual(
            self.store.get_external_search(self.ops[-1]["operation_id"])["budget"]["status"],
            "response_unpriced",
        )

    def test_changed_query_is_outside_exact_difference_zero_reservation(self):
        task, _ = self.admitted()
        slot = copy.deepcopy(self.slots[17])
        slot["request"]["plan"]["query"] += " changed"
        before = self.store.get_quick_scan_budget_status(self.policy["policy_id"])
        with self.assertRaisesRegex(ValueError, "outside exact"):
            self.store.begin_external_search(
                slot["work_item_id"],
                self.lease,
                policy=self.policy,
                independent_task=task,
                **slot["request"],
            )
        self.assertEqual(before, self.store.get_quick_scan_budget_status(self.policy["policy_id"]))

    def test_missing_existing_slot_in_original_matrix_refused(self):
        source = seal(self.store, self.policy, self.projection, self.ops[-1])
        task, reg = registration(self.store, self.policy, source, self.slots)
        reg["planned_slots"] = reg["planned_slots"][1:]
        task["task_sha256"] = _digest(reg)
        with self.assertRaisesRegex(ValueError, "omits existing"):
            self.store.register_external_research_continuation(
                policy=self.policy, independent_task=task, registration=reg
            )

    def test_allowlist_omitting_an_undispatched_enabled_slot_refused(self):
        source = seal(self.store, self.policy, self.projection, self.ops[-1])
        task, reg = registration(self.store, self.policy, source, self.slots)
        reg["expected_allowed_slot_keys"] = reg["expected_allowed_slot_keys"][:-1]
        task["task_sha256"] = _digest(reg)
        with self.assertRaisesRegex(ValueError, "difference is not exact"):
            self.store.register_external_research_continuation(
                policy=self.policy, independent_task=task, registration=reg
            )

    def test_task_sha_change_refused(self):
        task, _ = self.admitted()
        task = dict(task, task_sha256="0" * 64)
        slot = self.slots[17]
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.store.begin_external_search(
                slot["work_item_id"],
                self.lease,
                policy=self.policy,
                independent_task=task,
                **slot["request"],
            )

    def test_remaining_semantic_slot_still_requires_exact_frozen_metadata(self):
        task, _ = self.admitted()
        before = self.store.get_quick_scan_budget_status(self.policy["policy_id"])
        for key in ("search_policy_sha256", "query_id"):
            with self.subTest(key=key):
                slot = copy.deepcopy(self.slots[17])
                if key == "search_policy_sha256":
                    slot["request"][key] = "f" * 64
                else:
                    slot["request"]["plan"][key] = "different-id"
                with self.assertRaisesRegex(ValueError, "differs from frozen"):
                    self.store.begin_external_search(
                        slot["work_item_id"],
                        self.lease,
                        policy=self.policy,
                        independent_task=task,
                        **slot["request"],
                    )
        self.assertEqual(before, self.store.get_quick_scan_budget_status(self.policy["policy_id"]))

    def test_policy_hash_and_other_metadata_relabelling_cannot_replay_settled_or_unknown(self):
        from src.utils.quick_scan_external_continuation import slot_key
        from src.utils.quick_scan_external_journal import _request_context

        source = seal(self.store, self.policy, self.projection, self.ops[-1])
        for old_index in (0, 16):
            for change in ("policy_hash", "routing_fingerprint", "query_id", "top_k"):
                with self.subTest(old_index=old_index, change=change):
                    task, reg = registration(
                        self.store, self.policy, source, copy.deepcopy(self.slots)
                    )
                    added = copy.deepcopy(self.slots[old_index])
                    if change == "policy_hash":
                        added["request"]["search_policy_sha256"] = "f" * 64
                    elif change == "query_id":
                        added["request"]["plan"]["query_id"] = "relabeled-query"
                    elif change == "top_k":
                        added["request"]["plan"]["top_k"] = 4
                    else:
                        # Different work UUID and routing fingerprint, same actual issuer/query.
                        row = self.store.create_or_attach(
                            entity_id=self.item["entity_id"],
                            question_id="Q_relabel_" + str(old_index),
                            generation=1,
                            scope="entity",
                            scope_id=self.item["entity_id"],
                            identity_revision=1,
                            source_binding_version=1,
                            identity_state="provisional",
                            source_binding_ref="BND_fixture",
                            source_binding_refs=["BND_fixture"],
                            identity_snapshot_sha256="a" * 64,
                            question_fingerprint="b" * 64,
                            routing_fingerprint="f" * 64,
                            run_id="fixture",
                            scan_id="relabel",
                        )
                        lease = self.store.claim(row["work_item_id"], lease_seconds=8000)
                        if lease is None:
                            row = self.store.get_item(row["work_item_id"])
                            lease = Lease(row["lease_token"], row["lease_epoch"])
                        added["work_item_id"] = row["work_item_id"]
                        added["request"]["plan"]["question_ids"] = [row["question_id"]]
                    reg["planned_slots"].append(added)
                    # The independent probe previously obtained a spurious 20th key here.
                    normalized = self.store.configure_quick_scan_budget(self.policy)
                    with self.store._transaction() as connection:
                        item = self.store._item(connection, added["work_item_id"])
                        context = _request_context(
                            self.store,
                            connection,
                            added["work_item_id"],
                            Lease(item["lease_token"], item["lease_epoch"]),
                            normalized=normalized,
                            **added["request"],
                        )
                    reg["expected_allowed_slot_keys"].append(slot_key(context))
                    task["task_sha256"] = _digest(reg)
                    before = self.store.get_quick_scan_budget_status(self.policy["policy_id"])
                    with self.assertRaisesRegex(ValueError, "duplicate planned"):
                        self.store.register_external_research_continuation(
                            policy=self.policy, independent_task=task, registration=reg
                        )
                    self.assertEqual(
                        before, self.store.get_quick_scan_budget_status(self.policy["policy_id"])
                    )

    def test_registered_difference_idempotent_and_database_reopens(self):
        task, reg = self.admitted()
        slot = self.slots[17]
        operation = self.store.begin_external_search(
            slot["work_item_id"],
            self.lease,
            policy=self.policy,
            independent_task=task,
            **slot["request"],
        )
        self.assertTrue(
            self.store.register_external_research_continuation(
                policy=self.policy, independent_task=task, registration=reg
            )["existing"]
        )
        reopened = QuickScanWorkStore(self.directory / "native.sqlite", clock=lambda: 1000.0)
        self.assertEqual(
            reopened.get_external_search(operation["operation_id"])["context"]["research_task"],
            task,
        )

    def test_original_reservation_and_cap_never_removed(self):
        task, _ = self.admitted()
        slot = self.slots[17]
        before = self.store.get_quick_scan_budget_status(self.policy["policy_id"])
        self.store.begin_external_search(
            slot["work_item_id"],
            self.lease,
            policy=self.policy,
            independent_task=task,
            **slot["request"],
        )
        after = self.store.get_quick_scan_budget_status(self.policy["policy_id"])
        self.assertEqual(after["max_cost_micros"], before["max_cost_micros"])
        self.assertEqual(after["reserved_micros"], before["reserved_micros"] + 1000000)
        self.assertEqual(after["spent_micros"], before["spent_micros"])

    def test_research_does_not_exempt_native_model_unknown(self):
        source = seal(self.store, self.policy, self.projection, self.ops[-1])
        # A second budget-only unknown without an external dispatch must not be excluded.
        self.store.reserve_budget_attempt(
            self.policy,
            budget_attempt_id="DISPATCH_native_fixture",
            route_id="brave",
            provider="brave",
            model_requested="external_retrieval_v1",
            quota_group="search",
            independent_task={"task_id": "separate_native_fixture", "task_sha256": "4" * 64},
        )
        self.store.record_budget_outcome(
            "DISPATCH_native_fixture", outcome="unknown", http_status_code=None
        )
        task, reg = registration(self.store, self.policy, source, self.slots)
        with self.assertRaisesRegex(ValueError, "native uncertainty"):
            self.store.register_external_research_continuation(
                policy=self.policy, independent_task=task, registration=reg
            )

    def test_mixed_fetch_plan_cannot_use_search_route_zero_reservation(self):
        slot = copy.deepcopy(self.slots[17])
        plan = slot["request"]["plan"]
        plan.update(
            schema_version="quick_scan_external_plan/1.2.0",
            operation="fetch",
            query="https://fixture.example/news",
            top_k=1,
            source_operation_id="SEARCH_" + "a" * 64,
            source_receipt_sha256="b" * 64,
        )
        before = self.store.get_quick_scan_budget_status(self.policy["policy_id"])
        with self.assertRaisesRegex(ValueError, "operation does not match"):
            self.store.begin_external_search(
                slot["work_item_id"], self.lease, policy=self.policy, **slot["request"]
            )
        self.assertEqual(before, self.store.get_quick_scan_budget_status(self.policy["policy_id"]))

    def test_search_plan_cannot_use_fetch_route_zero_reservation(self):
        slot = copy.deepcopy(self.slots[17])
        slot["request"].update(
            provider="deepseek_fetch", endpoint="https://api.deepseek.com/v1/tools/fetch"
        )
        before = self.store.get_quick_scan_budget_status(self.policy["policy_id"])
        with self.assertRaisesRegex(ValueError, "operation does not match"):
            self.store.begin_external_search(
                slot["work_item_id"], self.lease, policy=self.policy, **slot["request"]
            )
        self.assertEqual(before, self.store.get_quick_scan_budget_status(self.policy["policy_id"]))

    def test_twenty_one_to_twenty_two_preserves_all_old_attempts(self):
        # Construct an exact v21 SQLite schema fixture; normal _validate_schema(21)
        # verifies every old table/trigger before the v22 upgrade can proceed.
        path = self.directory / "native.sqlite"
        with closing(sqlite3.connect(path)) as connection:
            original = [
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM quick_scan_budget_attempt ORDER BY budget_attempt_id"
                )
            ]
            connection.execute("DROP TABLE quick_scan_research_intent")
            connection.execute("DROP TABLE quick_scan_research_turn")
            connection.execute("DROP TABLE quick_scan_research_session")
            connection.execute("DROP TABLE quick_scan_research_visible_output")
            connection.execute("DROP TRIGGER quick_scan_external_research_admission_no_update")
            connection.execute("DROP TRIGGER quick_scan_external_research_admission_no_delete")
            connection.execute("DROP TABLE quick_scan_external_research_admission")
            connection.execute("PRAGMA user_version=21")
            connection.commit()
        QuickScanWorkStore(path, clock=lambda: 1000.0)
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION
            )
            self.assertEqual(
                original,
                [
                    tuple(row)
                    for row in connection.execute(
                        "SELECT * FROM quick_scan_budget_attempt ORDER BY budget_attempt_id"
                    )
                ],
            )


if __name__ == "__main__":
    unittest.main()
