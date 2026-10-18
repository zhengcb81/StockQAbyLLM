"""Public synthetic1.5 unit/transaction tests; never native owner authority."""

import copy
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from src.utils import quick_scan_owner_budget_continuation as api
from src.utils import quick_scan_owner_budget_continuation_v15 as v15
from src.utils.quick_scan_work_store import BudgetAdmissionError
from tests.unit.test_owner_budget_continuation import control as old_control
from tests.unit.test_owner_budget_continuation import original


def control(original):
    store, policy = original
    result = old_control(original)
    result["protocol"] = v15.PROTOCOL
    result["snapshot"] = api.exposure_snapshot(store.path, policy, dialect="1.5.0")
    core = result["future_request_core"]
    core["protocol"] = "iqs.refresh_request/1.5.0"
    subject = dict(
        entity_id="ENT_fixture", scope="entity", scope_id="ENT_fixture", perimeter_sha256="d" * 64
    )
    core["subject_refs"] = [subject]
    core["source_query"] = {"payload": {"information_cutoff": "2026-10-18"}}
    cell = result["cells"][0]
    cell["subject_ref"] = subject
    cell["controls"].update(
        strategy_id="module_packs", search_policy_sha256=api.digest(api.NO_EXTERNAL_SEARCH_POLICY)
    )
    profile = dict(
        protocol="iqs.company-scan-profile/1.0.0",
        mode="one_shot_packs",
        question_ids=["Q"],
        packs=[["Q"]],
        pack_by="module",
        max_questions_per_pack=1,
        route_id="route",
        information_cutoff="2026-10-18",
        resource_limits={"max_search_queries_per_session": 0},
        question_bindings=[
            dict(
                question_id="Q",
                question_object_sha256="a" * 64,
                module_id="base",
                module_version="1.0.0",
            )
        ],
        native_pack_group_sha256={
            "Q": api.digest(["ENT_fixture", None, "entity", "ENT_fixture", "d" * 64])
        },
    )
    cell.update(execution_profile=profile, execution_profile_sha256=api.digest(profile))
    core["execution_profile_sha256"] = cell["execution_profile_sha256"]
    return result


def test_complete_snapshot_synthetic_no_migration(original):
    store, policy = original
    before = store.path.read_bytes()
    snap = api.exposure_snapshot(store.path, policy, dialect="1.5.0")
    assert store.path.read_bytes() == before and snap["requests"] == 2
    assert len(snap["attempts"]) == 2 and all(
        row["native"]["kind"] == "budget_only" for row in snap["attempts"]
    )
    assert all(row["actual_cost_micros"] is None for row in snap["attempts"])
    assert api.validate_control(control(original)) == control(original)
    with closing(store._connect()) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 24
        v15.validate_retained_rows(db, control(original), policy["policy_id"])


@pytest.mark.parametrize(
    "mutation",
    [
        "authorized",
        "omit",
        "fake_origin",
        "cash_zero",
        "native_changed",
        "profile_digest",
        "question_body",
        "module",
        "two_packs",
        "search",
        "wrong_strategy",
        "request_profile",
        "reconcile",
        "reservation",
        "retained_requests",
        "retry",
        "zero_http",
        "bool_http",
        "false_stop",
        "second_cell",
        "unknown_provider",
        "wrong_group",
        "different_module_pack",
    ],
)
def test_control_and_complete_original_snapshot_refusals(original, mutation):
    store, policy = original
    c = control(original)
    cell = c["cells"][0]
    if mutation == "authorized":
        c["authorized"] = True
    elif mutation == "omit":
        c["snapshot"]["attempts"].pop()
    elif mutation == "fake_origin":
        c["snapshot"]["attempts"][0]["native"]["kind"] = "native_pack"
    elif mutation == "cash_zero":
        c["snapshot"]["attempts"][0]["actual_cost_micros"] = 0
    elif mutation == "native_changed":
        c["snapshot"]["attempts"][0]["native"]["transport_state"] = "FORGED"
    elif mutation == "profile_digest":
        cell["execution_profile_sha256"] = "e" * 64
    elif mutation == "question_body":
        cell["question_body_sha256"]["Q"] = "e" * 64
    elif mutation == "module":
        cell["execution_profile"]["question_bindings"][0]["module_version"] = "9.0.0"
    elif mutation == "two_packs":
        cell["execution_profile"]["packs"] = [["Q"], ["Q"]]
    elif mutation == "search":
        cell["execution_profile"]["resource_limits"]["max_search_queries_per_session"] = 1
    elif mutation == "wrong_strategy":
        cell["controls"]["strategy_id"] = "free_agent"
    elif mutation == "request_profile":
        c["future_request_core"]["execution_profile_sha256"] = "f" * 64
    elif mutation == "reconcile":
        c["snapshot"]["attempts"][0]["reconciled_at"] = "2026-10-18"
    elif mutation == "reservation":
        c["snapshot"]["attempts"][0]["reserved_cost_micros"] = 0
    elif mutation == "retained_requests":
        c["retained_original_requests"] = False
    elif mutation == "retry":
        c["no_retry_original_intents"] = False
    elif mutation == "zero_http":
        c["max_new_http_requests"] = 0
    elif mutation == "bool_http":
        c["max_new_http_requests"] = True
    elif mutation == "false_stop":
        c["original_process_stopped"] = False
    elif mutation == "second_cell":
        c["cells"].append(copy.deepcopy(cell))
    elif mutation == "unknown_provider":
        cell["provider"] = "another-provider"
    elif mutation == "wrong_group":
        cell["execution_profile"]["native_pack_group_sha256"]["Q"] = "e" * 64
    elif mutation == "different_module_pack":
        cell["execution_profile"]["pack_by"] = "adjacent"
    with pytest.raises(ValueError):
        api.validate_control(c)
        with closing(store._connect()) as db:
            v15.validate_retained_rows(db, c, policy["policy_id"])
        owner = dict(
            protocol="stockwiki.refresh_owner_policy/1.5.0",
            provider="mimo",
            model="fixture-model",
            allowed_consumer_ids=["iqs"],
            budget_exposure_acceptance=dict(continuation_decision_ref="NATIVE_REF", control=c),
        )
        api.accepted_control(
            owner,
            dict(c["future_request_core"], continuation_decision_ref="NATIVE_REF"),
            str(store.path),
            now=store.clock(),
        )


def test_full_profile_hash_is_not_semantic_comparison_identity(original):
    cell = control(original)["cells"][0]
    key = api.cell_key(cell)
    other = copy.deepcopy(cell)
    other.update(cell_id="renamed", generation=12, manifest_raw_sha256="f" * 64)
    other["execution_profile"]["resource_limits"]["wall_seconds"] = 432
    other["execution_profile_sha256"] = api.digest(other["execution_profile"])
    assert api.cell_key(other) == key
    for mutate in ("cutoff", "replicate", "model", "body", "module"):
        changed = copy.deepcopy(cell)
        if mutate == "cutoff":
            changed["execution_profile"]["information_cutoff"] = "2026-10-19"
        elif mutate == "replicate":
            changed["controls"]["replicate_ordinal"] = 2
        elif mutate == "model":
            changed["model"] = "another-model"
        elif mutate == "body":
            changed["execution_profile"]["question_bindings"][0]["question_object_sha256"] = (
                "f" * 64
            )
        else:
            changed["execution_profile"]["question_bindings"][0]["module_version"] = "2.0.0"
        assert api.cell_key(changed) != key


def test_postadmission_factory_requires_entire_approved_profile(original):
    cell = control(original)["cells"][0]
    task = SimpleNamespace(
        raw={
            "protocol": "iqs.company-scan-task/1.0.0",
            "execution_profile": copy.deepcopy(cell["execution_profile"]),
            "execution_profile_sha256": cell["execution_profile_sha256"],
        }
    )
    v15.assert_profile_task(cell, task)
    task.raw["execution_profile"]["resource_limits"]["max_model_requests_per_session"] = 2
    with pytest.raises(ValueError, match="approved_profile"):
        v15.assert_profile_task(cell, task)


def test_current_original_default_caps_and_no_caller_authority(original):
    store, policy = original
    # All structural control checks can pass without granting a native send.
    assert api.validate_control(control(original))
    with pytest.raises(BudgetAdmissionError, match="reconciliation_required"):
        store.reserve_budget_attempt(
            policy,
            budget_attempt_id="DISPATCH_renamed_task",
            route_id="route",
            provider="fixture",
            model_requested="fixture-model",
            quota_group="group",
        )


def test_new_unknown_invalidates_existing_acceptance_without_clearing_old(original):
    store, policy = original
    c = control(original)
    with closing(store._connect()) as db:
        old = [
            tuple(row)
            for row in db.execute(
                "SELECT * FROM quick_scan_budget_attempt ORDER BY budget_attempt_id"
            )
        ]
        # Simulate an independent original legacy row using the same strict
        # table constraints; this is a transaction negative, not owner proof.
        columns = [row[1] for row in db.execute("PRAGMA table_info(quick_scan_budget_attempt)")]
        source = dict(db.execute("SELECT * FROM quick_scan_budget_attempt LIMIT 1").fetchone())
        source["budget_attempt_id"] = "DISPATCH_later_unknown"
        db.execute(
            "INSERT INTO quick_scan_budget_attempt("
            + ",".join(columns)
            + ") VALUES("
            + ",".join("?" for _ in columns)
            + ")",
            tuple(source[k] for k in columns),
        )
        db.commit()
        with pytest.raises(ValueError, match="complete_snapshot_changed"):
            v15.validate_retained_rows(db, c, policy["policy_id"])
        assert [
            tuple(row)
            for row in db.execute(
                "SELECT * FROM quick_scan_budget_attempt WHERE budget_attempt_id!='DISPATCH_later_unknown' ORDER BY budget_attempt_id"
            )
        ] == old


def test_original_native_single_unpriced_projection_and_no_work_retry(tmp_path):
    from src.utils.quick_scan_work_store import QuickScanWorkStore
    from tests.unit.test_quick_scan_budget import _new_work, _policy

    store = QuickScanWorkStore(tmp_path / "native-single.sqlite")
    policy = _policy(max_cost=25, max_cost_per_attempt=0.1)
    store.configure_quick_scan_budget(policy)
    workid, lease, attempt = _new_work(store, "single")
    store.mark_send_intent(
        workid,
        lease,
        attempt["attempt_id"],
        budget_policy=policy,
        budget_route=dict(
            route_id="route-a1",
            provider="provider-a1",
            model_requested="fixture-a",
            quota_group="A",
        ),
    )
    store.record_budget_outcome(
        attempt["attempt_id"], outcome="response_available", http_status_code=200
    )
    snapshot = api.exposure_snapshot(store.path, policy, dialect="1.5.0")
    row = snapshot["attempts"][0]
    assert row["native"]["kind"] == "native_single"
    assert row["native"]["members"][0]["work_item_id"] == workid
    assert row["native"]["members"][0]["subject_ref"] is None
    assert row["native"]["transport_state"] == "known_received"
    assert row["native"]["usable_visible_output"] is False and row["actual_cost_micros"] is None
    assert row["native"]["native_receipt_sha256"] is None


def test_changed_real_module_version_rejected_before_admission(original):
    c = control(original)
    cell = c["cells"][0]
    body = {"module_id": "base", "id": "Q"}
    cell["execution_profile"]["question_bindings"][0]["question_object_sha256"] = api.digest(body)
    bundle = {"manifest": {"module_locks": [{"module_id": "base", "version": "2.0.0"}]}}
    with pytest.raises(ValueError, match="modules_changed"):
        v15.validate_original_bindings(cell, {"Q": body}, {"subject": bundle})


def test_public_observer_only_measures_current_owner_work(tmp_path):
    """Old native leases must not pollute the new scope's wall measurement."""
    import ast
    from pathlib import Path
    from types import SimpleNamespace

    path = Path(__file__).resolve().parents[1] / "integration/test_owner_continuation_v15_public.py"
    tree = ast.parse(path.read_text(encoding="utf8"))
    child = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "child_run"
    )
    function = next(
        node
        for node in child.body
        if isinstance(node, ast.FunctionDef) and node.name == "lease_measurement"
    )
    dbpath = tmp_path / "measurement.sqlite"
    with closing(sqlite3.connect(dbpath)) as db:
        db.execute("CREATE TABLE work_item(work_item_id TEXT,status TEXT,lease_expires_at REAL)")
        db.executemany(
            "INSERT INTO work_item VALUES(?,?,?)",
            [("old", "leased", 10), ("new", "leased", 800), ("another", "leased", 800)],
        )
        db.commit()
    services = SimpleNamespace(
        store=SimpleNamespace(path=dbpath),
        owner=SimpleNamespace(
            entries={
                "Q": {"kind": "work", "work_item_id": "new"},
                "Q2": {"kind": "work", "work_item_id": "another"},
            }
        ),
    )
    namespace = dict(services=services, closing=closing, time=SimpleNamespace(time=lambda: 500))
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
            str(path),
            "exec",
        ),
        namespace,
    )
    assert namespace["lease_measurement"]() == dict(
        lease_active=True,
        original_lease_seconds=600,
        earliest_claim_epoch=200,
        earliest_expiry_epoch=800,
        elapsed_since_first_claim=300,
    )
