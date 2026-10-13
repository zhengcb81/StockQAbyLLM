import copy
import time

import pytest

from src.utils.quick_scan_refresh_inspect import inspect_refresh
from tests.unit.test_owner_control_projection import prepared
from tests.unit.test_quick_scan_question_pack_store import _policy


def setup(tmp_path, monkeypatch):
    store, clock, work, decision = prepared(tmp_path)
    monkeypatch.setattr(time, "time", lambda: clock[0])
    request = dict(
        entity_id=work["entity_id"],
        question_ids=[work["question_id"]],
        scope_bindings={work["question_id"]: dict(scope=work["scope"], scope_id=work["scope_id"])},
        policy_id="pack-policy",
        route_id="route-pack",
        provider_config_ref="mimo-config",
        model="mimo-v2.6-flash",
        cohort_id="B01_NATIVE_CONTROL",
        work_decision_refs=[
            dict(work_item_id=work["work_item_id"], decision_id=decision["decision_id"])
        ],
    )
    policy = _policy()
    policy["budget"]["currency"] = "CNY"
    return store, clock, work, decision, request, policy


def test_explicit_native_inspection_uses_exact_durable_decision_and_leaves_database_unchanged(
    tmp_path, monkeypatch
):
    store, clock, work, decision, request, policy = setup(tmp_path, monkeypatch)
    before = store.path.read_bytes()
    out = inspect_refresh(store.path, request, policy=policy, schema_version="1.2.0")
    row = out["work_items"][work["question_id"]]
    assert out["protocol"] == "stockqa.refresh_inspection/1.2.0"
    assert row["control_dispatch"] == "deferred_unknown"
    assert row["owner_control"]["decision_id"] == decision["decision_id"]
    clock[0] += 60
    after = inspect_refresh(store.path, request, policy=policy, schema_version="1.2.0")
    assert after["work_items"][work["question_id"]]["controlled_generation"] == 2
    assert after["quote"]["budget_state_sha256"] != out["quote"]["budget_state_sha256"]
    assert store.path.read_bytes() == before and out["API_requests"] == 0


@pytest.mark.parametrize(
    "fault",
    ["unknown_decision", "duplicate", "other_work", "other_scope", "missing_ref", "old_dialect"],
)
def test_control_scope_cannot_be_forged_expanded_or_implicitly_enabled(
    tmp_path, monkeypatch, fault
):
    store, _, work, _, request, policy = setup(tmp_path, monkeypatch)
    before = store.path.read_bytes()
    version = "1.2.0"
    if fault == "unknown_decision":
        request["work_decision_refs"][0]["decision_id"] = "owd1_" + "0" * 64
    elif fault == "duplicate":
        request["work_decision_refs"] *= 2
    elif fault == "other_work":
        request["work_decision_refs"][0]["work_item_id"] = "WORK_OTHER"
    elif fault == "other_scope":
        request["scope_bindings"][work["question_id"]]["scope_id"] = "ENT_OTHER"
    elif fault == "missing_ref":
        request["work_decision_refs"] = []
    else:
        version = "1.1.0"
    with pytest.raises((ValueError, KeyError)):
        inspect_refresh(store.path, request, policy=policy, schema_version=version)
    assert store.path.read_bytes() == before
