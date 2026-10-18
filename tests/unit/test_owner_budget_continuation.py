"""Original SQLite budget semantics, synthetic unit data, no production authority."""

import copy
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.utils import quick_scan_owner_budget_continuation as continuation
from src.utils.quick_scan_owner_budget_continuation import (
    DDL_V24_ADDITIONS,
    exposure_snapshot,
    future_request_core,
    validate_control,
)
from src.utils.quick_scan_work_store import BudgetAdmissionError, QuickScanWorkStore
from tests.unit import test_quick_scan_uncertain_seal as fixture


@pytest.fixture
def original(tmp_path, request):
    store = QuickScanWorkStore(
        tmp_path / "executor.sqlite",
        clock=lambda: datetime(2026, 10, 18, 12, tzinfo=timezone.utc).timestamp(),
    )
    policy = fixture.policy(
        **dict({"cost": 25, "requests": 350, "concurrency": 2}, **getattr(request, "param", {}))
    )
    store.configure_quick_scan_budget(policy)
    for label in ("unknown", "response"):
        store.reserve_budget_attempt(
            policy,
            budget_attempt_id="DISPATCH_" + label,
            route_id="route",
            provider="fixture",
            model_requested="fixture-model",
            quota_group="group",
        )
    for label in ("unknown", "response"):
        store.record_budget_outcome(
            "DISPATCH_" + label,
            outcome="unknown" if label == "unknown" else "response_available",
            http_status_code=None if label == "unknown" else 200,
        )
    return store, policy


def test_old_default_still_pauses(original):
    store, policy = original
    with pytest.raises(BudgetAdmissionError, match="reconciliation_required"):
        store.reserve_budget_attempt(
            policy,
            budget_attempt_id="DISPATCH_new",
            route_id="route",
            provider="fixture",
            model_requested="fixture-model",
            quota_group="group",
        )


def test_snapshot_is_readonly_and_does_not_price_unknown(original):
    store, policy = original
    before = store.path.read_bytes()
    got = exposure_snapshot(store.path, policy, ["DISPATCH_unknown", "DISPATCH_response"])
    assert store.path.read_bytes() == before
    assert got["requests"] == 2 and got["reserved_micros"] == 200000
    assert got["cash_actual_complete"] is False
    assert {a["http_status_code"] for a in got["attempts"]} == {None, 200}
    assert all(a["actual_cost_micros"] is None for a in got["attempts"])


def test_snapshot_requires_exact_budget_only_refs(original):
    store, policy = original
    for ids in (
        [],
        ["DISPATCH_unknown"],
        ["DISPATCH_unknown"] * 2,
        ["DISPATCH_unknown", "missing"],
    ):
        with pytest.raises(ValueError):
            exposure_snapshot(store.path, policy, ids)


def test_future_core_removes_only_one_known_reference_field():
    raw = {
        "protocol": "iqs.refresh_request/1.4.0",
        "cohort_id": "C",
        "consumer_id": "iqs",
        "continuation_decision_ref": "be1_" + "a" * 64,
        "question_ids": ["Q"],
    }
    core = future_request_core(raw)
    assert core == {k: v for k, v in raw.items() if k != "continuation_decision_ref"}
    assert raw["continuation_decision_ref"]
    with pytest.raises(ValueError):
        future_request_core(dict(raw, continuation_review_ref="ambiguous"))
    with pytest.raises(ValueError):
        future_request_core(dict(raw, protocol="iqs.refresh_request/1.3.0"))


def control(original):
    store, policy = original
    return {
        "protocol": "stockqa.owner_budget_continuation/1.0.0",
        "mode": "explicit_repeat_exposure",
        "reference_scope": "unknown_reference_scope",
        "purpose": "independent_comparison",
        "consumer_id": "iqs",
        "cohort_id": "new-controlled-cohort",
        "ledger_path": str(store.path.resolve()),
        "snapshot": exposure_snapshot(
            store.path, policy, ["DISPATCH_unknown", "DISPATCH_response"]
        ),
        "future_request_core": {
            "protocol": "iqs.refresh_request/1.4.0",
            "cohort_id": "new-controlled-cohort",
            "consumer_id": "iqs",
            "question_ids": ["Q"],
            "subject_refs": [{"entity_id": "ENT_fixture"}],
        },
        "cells": [
            {
                "cell_id": "C1",
                "question_ids": ["Q"],
                "question_body_sha256": {"Q": "a" * 64},
                "subject_ref": {"entity_id": "ENT_fixture"},
                "provider": "mimo",
                "model": "fixture-model",
                "route_id": "route",
                "quota_group": "group",
                "generation": 1,
                "manifest_raw_sha256": "c" * 64,
                "controls": {
                    "thinking_enabled": True,
                    "max_output_tokens": 4096,
                    "strategy_id": "free_agent",
                    "search_policy_sha256": "b" * 64,
                    "replicate_ordinal": 1,
                },
            }
        ],
        "max_new_http_requests": 1,
        "valid_from": "2026-10-18T00:00:00Z",
        "expires_at": "2026-10-19T00:00:00Z",
        "no_retry_original_intents": True,
        "unknown_cash_remains_unknown": True,
        "retained_original_requests": True,
        "original_process_stopped": True,
        "stop_evidence_ref": "owner-reviewed-stop-evidence",
        "additional_risk_hold_micros": 0,
    }


def test_control_full_shape_and_unknown_scope(original):
    assert validate_control(control(original))["reference_scope"] == "unknown_reference_scope"


@pytest.mark.parametrize(
    "change",
    [
        {"reference_scope": "disjoint"},
        {"unknown_cash_remains_unknown": False},
        {"retained_original_requests": False},
        {"no_retry_original_intents": False},
        {"max_new_http_requests": True},
        {"max_new_http_requests": 0},
        {"additional_risk_hold_micros": -1},
        {"authorized": True},
        {"continuation_review_ref": "legacy-name"},
        {"cells": []},
    ],
)
def test_control_rejects_self_authorization_or_erasing_risk(original, change):
    with pytest.raises(ValueError):
        validate_control(dict(control(original), **change))


def test_schema_addition_keeps_existing_rows_and_new_tables_empty(original):
    store, _ = original
    with closing(sqlite3.connect(store.path)) as db:
        tables = [
            r[0]
            for r in db.execute("select name from sqlite_master where type='table' order by name")
        ]
        old = {t: list(db.execute('select * from "' + t + '"')) for t in tables}
        # This test uses the exact original schema migration, not ad-hoc DDL.
        assert db.execute("pragma user_version").fetchone()[0] == 24
        for table in ("quick_scan_owner_continuation_cell",):
            assert db.execute("select count(*) from " + table).fetchone()[0] == 0
        assert DDL_V24_ADDITIONS
        assert old == {t: list(db.execute('select * from "' + t + '"')) for t in tables}


def synthetic_cell(original, monkeypatch, *, risk_hold=0, qid="Q"):
    """Internal transaction unit fixture; deliberately not a native owner positive."""
    from src.utils import quick_scan_refresh_admission_journal as journal

    store, policy = original
    cache_key = "REQ_" + continuation.digest(qid)
    question = {"id": qid, "question": "Synthetic control question"}
    controlled = control(original)
    controlled["additional_risk_hold_micros"] = risk_hold
    ref = "ACCEPT_TEST" if qid == "Q" else "ACCEPT_TEST_" + qid
    controlled["cohort_id"] += "_" + qid
    controlled["future_request_core"]["cohort_id"] = controlled["cohort_id"]
    controlled["future_request_core"]["question_ids"] = [qid]
    controlled["cells"][0]["question_ids"] = [qid]
    controlled["cells"][0]["question_body_sha256"] = {qid: continuation.digest(question)}
    request = dict(controlled["future_request_core"], continuation_decision_ref=ref)
    owner_policy = {
        "protocol": "stockwiki.refresh_owner_policy/1.4.0",
        "allowed_consumer_ids": ["iqs"],
        "model": "fixture-model",
        "provider": "mimo",
        "qa": {"provider_config_ref": "fixture"},
        "budget_exposure_acceptance": {"continuation_decision_ref": ref, "control": controlled},
    }
    spec = {
        "entity_id": "ENT_fixture",
        "question_id": qid,
        "generation": 1,
        "scope": "entity",
        "scope_id": "ENT_fixture",
        "identity_revision": 1,
        "source_binding_version": 1,
        "identity_state": "verified",
        "source_binding_ref": "BND_US_FIX",
        "source_binding_refs": ["BND_US_FIX"],
        "identity_snapshot_sha256": "a" * 64,
        "question_fingerprint": "b" * 64,
        "routing_fingerprint": "c" * 64,
    }
    spec["owner_refresh_binding"] = {
        "protocol": "stockqa.owner_refresh_binding/1.0.0",
        "subject_key": "ENT_fixture@1",
        "perimeter_sha256": "a" * 64,
        "decision_id": "route_" + "b" * 64,
        "anchor_version": 1,
        "manifest_raw_sha256": "c" * 64,
        "provider": "mimo",
        "model": "fixture-model",
        "information_cutoff": "2026-10-13",
        "request_identity_key": cache_key,
        "target_generation": 1,
        "question_id": qid,
        "scope": "entity",
        "scope_id": "ENT_fixture",
        "entity_id": "ENT_fixture",
        "identity_revision": 1,
    }
    row = store.create_or_attach(**spec, run_id="R", scan_id="S")
    lease = store.claim(row["work_item_id"], lease_seconds=600)
    attempt = store.prepare_attempt(
        row["work_item_id"],
        lease,
        route_id="route",
        provider="fixture",
        model_requested="fixture-model",
        request_cache_key=cache_key,
        prompt_sha256=continuation.digest(question),
    )
    confirmation = "rc1_" + continuation.digest(qid)
    proof = {
        "owner_policy": owner_policy,
        "request": request,
        "confirmation": {"confirmation_id": confirmation},
        "preview": {"items": [{"question_id": qid, "subject_ref": {"entity_id": "ENT_fixture"}}]},
    }
    bound_spec = dict(spec)
    action = {
        "confirmation_id": confirmation,
        "confirmation_sha256": "a" * 64,
        "preview_id": "rv1_" + "d" * 64,
        "scope_sha256": "e" * 64,
        "policy_sha256": continuation.digest(owner_policy),
        "execution_inputs_sha256": "f" * 64,
    }
    items = [{k: row[k] for k in journal.ITEM_FIELDS}]
    with store._transaction() as db:
        value = journal.inputs(action, [bound_spec], "R", "S")
        journal.publish(db, value, items, registered_at=store._now())
        continuation.register_cells(
            db,
            proof,
            items,
            [bound_spec],
            {"subject": {"manifest": {"questions": [question]}}},
            ledger_path=store.path,
            now=store._now(),
        )
    session = SimpleNamespace(
        proof=proof,
        service=SimpleNamespace(store=store),
        check=lambda: None,
        entries={qid: {"kind": "work", "work_item_id": row["work_item_id"]}},
    )
    monkeypatch.setattr(continuation, "_current_session", lambda: session)
    return attempt["attempt_id"], lease, row, session


def send_synthetic_cell(original, prepared):
    store, policy = original
    attempt, lease, row, _ = prepared
    return store.mark_send_intent(
        row["work_item_id"],
        lease,
        attempt,
        budget_policy=policy,
        budget_route={
            "route_id": "route",
            "provider": "fixture",
            "model_requested": "fixture-model",
            "quota_group": "group",
        },
    )


def test_internal_budget_transaction_keeps_old_rows_and_reserves_new(original, monkeypatch):
    store, policy = original
    before = continuation.exposure_snapshot(
        store.path, policy, ["DISPATCH_unknown", "DISPATCH_response"]
    )
    prepared = synthetic_cell(original, monkeypatch)
    send_synthetic_cell(original, prepared)
    after = continuation.exposure_snapshot(
        store.path, policy, ["DISPATCH_unknown", "DISPATCH_response"]
    )
    assert after["attempts"] == before["attempts"]
    assert after["requests"] == 3 and after["reserved_micros"] == 300000
    assert store.get_quick_scan_budget_status(policy["policy_id"])["in_flight"] == 2


def test_internal_cell_cannot_be_resent_with_new_attempt(original, monkeypatch):
    store, _ = original
    prepared = synthetic_cell(original, monkeypatch)
    send_synthetic_cell(original, prepared)
    attempt, _, _, _ = prepared
    with pytest.raises(Exception):
        # The original prepared/send state itself also refuses the same send.
        send_synthetic_cell(original, prepared)
    with closing(store._connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM quick_scan_budget_attempt").fetchone()[0] == 3
        assert (
            db.execute(
                "SELECT request_counted FROM quick_scan_budget_attempt WHERE budget_attempt_id=?",
                (attempt,),
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("mutation", ["third_unknown", "source_row", "model", "current_proof"])
def test_internal_projection_rejects_new_unknown_or_drift(original, monkeypatch, mutation):
    store, _ = original
    prepared = synthetic_cell(original, monkeypatch)
    if mutation == "third_unknown":
        # A previously in-flight third request becomes unknown after approval.
        with store._transaction() as db:
            db.execute(
                "INSERT INTO quick_scan_budget_attempt SELECT 'DISPATCH_third',work_attempt_id,policy_id,policy_version,route_id,provider,model_requested,quota_group,currency,pricing_basis,pricing_ref,reserved_cost_micros,actual_cost_micros,request_counted,in_flight,cost_source_ref,status,http_status_code,created_at,send_intent_at,completed_at,reconciled_at FROM quick_scan_budget_attempt WHERE budget_attempt_id='DISPATCH_unknown'"
            )
    elif mutation == "source_row":
        with store._transaction() as db:
            db.execute(
                "UPDATE quick_scan_budget_attempt SET reserved_cost_micros=200000 WHERE budget_attempt_id='DISPATCH_unknown'"
            )
    elif mutation == "model":
        prepared[3].proof["owner_policy"]["model"] = "other-model"
    else:
        prepared[3].proof["request"]["cohort_id"] = "caller-changed-cohort"
    with pytest.raises((BudgetAdmissionError, ValueError)):
        send_synthetic_cell(original, prepared)
    with closing(store._connect()) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM quick_scan_budget_attempt WHERE budget_attempt_id=?",
                (prepared[0],),
            ).fetchone()[0]
            == 0
        )


def test_task_uses_original_per_session_search_limit(monkeypatch):
    from types import SimpleNamespace

    control = {
        "cells": [
            {
                "question_ids": ["Q"],
                "controls": {
                    "strategy_id": "free_agent",
                    "search_policy_sha256": continuation.digest(
                        continuation.NO_EXTERNAL_SEARCH_POLICY
                    ),
                },
            }
        ]
    }
    monkeypatch.setattr(continuation, "accepted_control", lambda *a, **k: ("ref", control))
    owner = SimpleNamespace(
        proof={
            "request": {
                "protocol": "iqs.refresh_request/1.4.0",
                "source_query": {"payload": {"information_cutoff": "2026-10-13"}},
            },
            "owner_policy": {},
        },
        service=SimpleNamespace(store=SimpleNamespace(path="unused", _now=lambda: "now")),
    )
    task = SimpleNamespace(
        limits={"max_search_queries_per_session": 0},
        raw={"information_cutoff": "2026-10-13", "strategy_id": "free_agent"},
        questions=[{"question_id": "Q"}],
    )
    continuation.assert_research_task(owner, task, search_service=None)
    task.limits = {"max_search_queries": 0}
    with pytest.raises(ValueError, match="registered_task_controls"):
        continuation.assert_research_task(owner, task, search_service=None)


@pytest.mark.parametrize(
    "original,expected",
    [({"cost": 0.2}, "budget_cost_limit"), ({"requests": 2}, "budget_request_limit")],
    indirect=["original"],
)
def test_original_cumulative_caps_still_include_old_exposure(original, monkeypatch, expected):
    prepared = synthetic_cell(original, monkeypatch)
    with pytest.raises(BudgetAdmissionError, match=expected):
        send_synthetic_cell(original, prepared)
    with closing(original[0]._connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM quick_scan_budget_attempt").fetchone()[0] == 2


def test_conservative_holds_deduplicate_by_acceptance_and_include_prospective(
    original, monkeypatch
):
    prepared = synthetic_cell(original, monkeypatch, risk_hold=300000)
    store = original[0]
    with closing(store._connect()) as db:
        same = dict(
            prepared[3].proof["owner_policy"]["budget_exposure_acceptance"]["control"],
            additional_risk_hold_micros=300000,
        )
        assert (
            continuation.risk_hold_total(
                db, "same_original_policy", prospective_ref="ACCEPT_TEST", prospective_control=same
            )
            == 300000
        )
        assert (
            continuation.risk_hold_total(
                db, "same_original_policy", prospective_ref="ANOTHER", prospective_control=same
            )
            == 600000
        )


def test_global_native_cell_unique_even_if_acceptance_reference_is_renamed(original, monkeypatch):
    synthetic_cell(original, monkeypatch)
    with original[0]._transaction() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO quick_scan_owner_continuation_cell SELECT policy_id,'RENAMED',cell_key,confirmation_id,owner_policy_sha256,work_item_ids_json,cell_json,control_json FROM quick_scan_owner_continuation_cell"
            )


def test_caller_callback_is_not_an_original_approved_owner():
    from src.utils.quick_scan_work_transport import _OWNER_REFRESH_GUARD

    token = _OWNER_REFRESH_GUARD.set(lambda: None)
    try:
        assert continuation._current_session() is None
    finally:
        _OWNER_REFRESH_GUARD.reset(token)


def test_initial_carrier_refuses_multiple_cells_even_different_labels(original):
    value = control(original)
    import copy

    other = copy.deepcopy(value["cells"][0])
    other["cell_id"] = "C2"
    other["controls"]["replicate_ordinal"] = 2
    value["cells"].append(other)
    with pytest.raises(ValueError, match="cells_required"):
        continuation.validate_control(value)


def test_extra_owner_risk_hold_is_not_fabricated_cash_or_free_capacity(original, monkeypatch):
    prepared = synthetic_cell(original, monkeypatch, risk_hold=25000000)
    with pytest.raises(BudgetAdmissionError, match="budget_cost_limit"):
        send_synthetic_cell(original, prepared)
    with closing(original[0]._connect()) as db:
        rows = db.execute(
            "SELECT actual_cost_micros,reserved_cost_micros FROM quick_scan_budget_attempt"
        ).fetchall()
        assert len(rows) == 2 and all(tuple(r) == (None, 100000) for r in rows)


def test_semantic_cell_ignores_incidental_labels_generation_and_manifest_envelope(original):
    cell = control(original)["cells"][0]
    renamed = dict(cell, cell_id="NEW_LABEL", generation=99, manifest_raw_sha256="f" * 64)
    assert continuation.cell_key(renamed) == continuation.cell_key(cell)
    import copy

    replicate = copy.deepcopy(cell)
    replicate["controls"]["replicate_ordinal"] += 1
    assert continuation.cell_key(replicate) != continuation.cell_key(cell)


def test_retained_unknown_still_consumes_original_global_inflight_slot(original, monkeypatch):
    first = synthetic_cell(original, monkeypatch)
    send_synthetic_cell(original, first)
    second = synthetic_cell(original, monkeypatch, qid="Q2")
    with pytest.raises(BudgetAdmissionError, match="dispatch_global_capacity_full"):
        send_synthetic_cell(original, second)
    with closing(original[0]._connect()) as db:
        assert db.execute(
            "SELECT SUM(in_flight),COUNT(*) FROM quick_scan_budget_attempt"
        ).fetchone()[:] == (2, 3)


@pytest.mark.parametrize("unsupported", ["owner_vendor", "cell_vendor", "strategy"])
def test_unsupported_carrier_is_rejected_before_quote_or_any_new_send(
    original, monkeypatch, unsupported
):
    prepared = synthetic_cell(original, monkeypatch)
    owner_policy = prepared[3].proof["owner_policy"]
    if unsupported == "owner_vendor":
        owner_policy["provider"] = "deepseek"
    elif unsupported == "cell_vendor":
        owner_policy["budget_exposure_acceptance"]["control"]["cells"][0]["provider"] = "deepseek"
    else:
        owner_policy["budget_exposure_acceptance"]["control"]["cells"][0]["controls"][
            "strategy_id"
        ] = "fixed_pack"
    with pytest.raises(ValueError, match="supported_carrier_required"):
        continuation.accepted_control(
            owner_policy, prepared[3].proof["request"], original[0].path, now=original[0]._now()
        )
    with closing(original[0]._connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM quick_scan_budget_attempt").fetchone()[0] == 2


@pytest.fixture
def fresh(tmp_path):
    store = QuickScanWorkStore(
        tmp_path / "fresh.sqlite",
        clock=lambda: datetime(2026, 10, 18, 12, tzinfo=timezone.utc).timestamp(),
    )
    policy = fixture.policy(**dict({"cost": 30, "requests": 20, "concurrency": 3}))
    store.configure_quick_scan_budget(policy)
    return store, policy


def test_fresh_snapshot_is_explicit_and_empty(fresh):
    store, policy = fresh
    before = store.path.read_bytes()
    got = exposure_snapshot(store.path, policy, fresh=True, owner_ack_ref="owner-test-ack")
    assert store.path.read_bytes() == before
    assert got["protocol"] == "stockqa.owner_exposure_snapshot/1.2.0"
    assert got["fresh_budget"] is True and got["owner_ack_ref"] == "owner-test-ack"
    assert got["attempts"] == [] and got["requests"] == 0 and got["reserved_micros"] == 0


def test_fresh_snapshot_requires_owner_ack(fresh):
    store, policy = fresh
    with pytest.raises(ValueError, match="owner_exposure_fresh_ack_required"):
        exposure_snapshot(store.path, policy, fresh=True)


def test_fresh_snapshot_rejects_non_empty_policy(fresh):
    store, policy = fresh
    store.reserve_budget_attempt(
        policy,
        budget_attempt_id="ATTEMPT_fresh_unknown",
        route_id="route",
        provider="fixture",
        model_requested="fixture-model",
        quota_group="group",
    )
    with pytest.raises(ValueError, match="owner_exposure_fresh_budget_not_empty"):
        exposure_snapshot(store.path, policy, fresh=True, owner_ack_ref="owner-test-ack")


def test_fresh_snapshot_counts_dispatch_search_spend(fresh):
    store, policy = fresh
    store.reserve_budget_attempt(
        policy,
        budget_attempt_id="DISPATCH_fresh_search_spend",
        route_id="route",
        provider="fixture",
        model_requested="fixture-model",
        quota_group="group",
    )
    snap = exposure_snapshot(store.path, policy, fresh=True, owner_ack_ref="owner-test-ack")
    assert snap["attempts"] == [] and snap["requests"] == 0
    assert snap["reserved_micros"] > 0


def test_fresh_retained_rows_are_empty(fresh):
    store, policy = fresh
    snap = exposure_snapshot(store.path, policy, fresh=True, owner_ack_ref="owner-test-ack")
    with closing(sqlite3.connect(store.path)) as db:
        db.row_factory = sqlite3.Row
        assert (
            continuation.validate_retained_rows(
                db,
                {"protocol": "stockqa.owner_budget_continuation/1.0.0", "snapshot": snap},
                policy["policy_id"],
            )
            == []
        )


def test_fresh_snapshot_rejects_other_dialect(fresh):
    store, policy = fresh
    with pytest.raises(ValueError, match="owner_exposure_fresh_dialect_unsupported"):
        exposure_snapshot(
            store.path, policy, dialect="1.5.0", fresh=True, owner_ack_ref="owner-test-ack"
        )


def test_fresh_snapshot_rejects_explicit_refs(fresh):
    store, policy = fresh
    with pytest.raises(ValueError, match="owner_exposure_fresh_refs_forbidden"):
        exposure_snapshot(
            store.path,
            policy,
            ["DISPATCH_a", "DISPATCH_b"],
            fresh=True,
            owner_ack_ref="owner-test-ack",
        )


def test_search_exposure_task_requires_matching_service(monkeypatch):
    exposure = {
        "protocol": "iqs.search_exposure/1.0.0",
        "consumer_id": "invest-quick-scan",
        "search_policy_ref": "policy.json",
        "search_policy_sha256": "a" * 64,
        "max_search_queries_per_session": 2,
        "max_search_cost_micros": 50000,
        "routes": [
            {
                "route_id": "brave",
                "provider": "brave",
                "credential_env": "BRAVE_API_KEY",
                "enabled": True,
                "max_queries": 2,
            }
        ],
        "storage_rights": {"confirmed": True, "entitlement_ref": "owner-terms"},
        "approved_by": "owner",
        "approved_at": "2026-10-18T12:00:00Z",
    }
    control = {
        "cells": [
            {
                "question_ids": ["Q"],
                "controls": {
                    "strategy_id": "free_agent",
                    "search_policy_sha256": continuation.digest(exposure),
                },
            }
        ]
    }
    monkeypatch.setattr(continuation, "accepted_control", lambda *a, **k: ("ref", control))
    owner = SimpleNamespace(
        proof={
            "owner_policy": {},
            "request": {
                "protocol": "iqs.refresh_request/1.4.0",
                "source_query": {"payload": {"information_cutoff": "2026-10-13"}},
            },
        },
        service=SimpleNamespace(store=SimpleNamespace(path="unused", _now=lambda: "now")),
    )
    task = SimpleNamespace(
        limits={"max_search_queries_per_session": 2},
        raw={
            "information_cutoff": "2026-10-13",
            "strategy_id": "free_agent",
            "execution_profile": {"search_exposure": exposure},
        },
        questions=[{"question_id": "Q"}],
    )
    continuation.assert_research_task(owner, task, search_service=object())
    with pytest.raises(ValueError, match="registered_search_exposure_required"):
        continuation.assert_research_task(owner, task, search_service=None)
    mismatched = copy.deepcopy(task)
    mismatched.raw["execution_profile"]["search_exposure"]["max_search_queries_per_session"] = 1
    with pytest.raises(ValueError, match="registered_search_exposure_required"):
        continuation.assert_research_task(owner, mismatched, search_service=object())
