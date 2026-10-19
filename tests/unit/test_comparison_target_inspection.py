"""Synthetic native SQLite: target-route proof cannot rewrite source evidence."""

import copy

import pytest

from src.utils.quick_scan_refresh_inspect import inspect_refresh
from src.utils.quick_scan_work_decision_inspect import inspect_work_decision
from tests.unit import test_quick_scan_known_outcome as original
from tests.unit.test_owner_comparison_target_grant import permission, record, target
from tests.unit.test_quick_scan_question_pack_store import _policy


def prepared(tmp_path, monkeypatch):
    policy = _policy()
    policy["budget"]["currency"] = "CNY"
    route = copy.deepcopy(policy["routes"][0])
    route.update(
        id="minimax-target", provider_config_ref="minimax-config", model="MiniMax-M3.1-Flash"
    )
    policy["routes"].append(route)
    monkeypatch.setattr(original, "_policy", lambda: copy.deepcopy(policy))
    store, clock, members, pack, receipt, _, _ = original.fixture(tmp_path)
    clock[0] += 121
    original.resolve(store, members, pack, receipt)
    work = store.get_item(members[0]["work_item_id"])
    document = record(store, work, permission(target()))
    return store, clock, work, document, policy


def target_route():
    return {k: v for k, v in target().items() if k not in {"provider", "owner_policy_sha256"}}


def test_native_explicit_target_read_and_refresh_keep_original_physical_and_fees(
    tmp_path, monkeypatch
):
    store, clock, work, document, policy = prepared(tmp_path, monkeypatch)
    before = store.path.read_bytes()
    result = inspect_work_decision(
        store.path,
        document["request"],
        policy=policy,
        clock=lambda: clock[0],
        comparison_target=target_route(),
    )
    assert result["protocol"] == "stockqa.work_decision_inspection/1.1.0"
    assert result["target_route"] == target_route()
    assert result["request"]["physical"]["model_requested"] == "mimo-v2.6-flash"
    request = dict(
        entity_id=work["entity_id"],
        question_ids=[work["question_id"]],
        scope_bindings={work["question_id"]: dict(scope=work["scope"], scope_id=work["scope_id"])},
        **target_route(),
        cohort_id="B01_SYNTHETIC_TARGET",
        work_decision_refs=[
            dict(work_item_id=work["work_item_id"], decision_id=document["decision_id"])
        ],
    )
    monkeypatch.setattr("src.utils.quick_scan_refresh_inspect.time.time", lambda: clock[0])
    output = inspect_refresh(store.path, request, policy=policy, schema_version="1.3.0")
    control = output["work_items"][work["question_id"]]["owner_control"]
    assert control["comparison_target"] == target()
    assert output["API_requests"] == result["database_writes"] == 0
    assert store.path.read_bytes() == before
    with pytest.raises(ValueError):
        inspect_refresh(store.path, request, policy=policy, schema_version="1.2.0")


@pytest.mark.parametrize(
    "fault",
    ["route", "model", "config", "policy", "extra", "source_model", "source_cost"],
)
def test_wrong_target_or_resealed_source_never_becomes_authority(tmp_path, monkeypatch, fault):
    store, clock, _, document, policy = prepared(tmp_path, monkeypatch)
    requested = copy.deepcopy(document["request"])
    route = target_route()
    fields = {
        "route": "route_id",
        "model": "model",
        "config": "provider_config_ref",
        "policy": "policy_id",
    }
    if fault in fields:
        route[fields[fault]] = "FOREIGN"
    elif fault == "extra":
        route["approved"] = True
    elif fault == "source_model":
        requested["physical"]["model_requested"] = "MiniMax-M3.1-Flash"
    else:
        requested["physical"]["actual_cost_micros"] = 1
    before = store.path.read_bytes()
    with pytest.raises((ValueError, TypeError, RuntimeError)):
        inspect_work_decision(
            store.path,
            requested,
            policy=policy,
            clock=lambda: clock[0],
            comparison_target=route,
        )
    assert store.path.read_bytes() == before


def test_targeted_read_partitions_valid_global_refs_but_requires_complete_local_scope(
    tmp_path, monkeypatch
):
    store, clock, work, document, policy = prepared(tmp_path, monkeypatch)
    pack = store.get_question_pack(document["request"]["physical"]["pack_id"])
    second = pack["members"][1]
    original.resolve(
        store,
        [
            dict(
                work_item_id=member["work_item_id"],
                lease=original.Lease(member["lease_token"], member["lease_epoch"]),
            )
            for member in pack["members"]
        ],
        pack,
        store.get_attempt_response(second["attempt_id"])["receipt"],
        index=1,
    )
    other = store.get_item(second["work_item_id"])
    decision = record(store, other, permission(target()))
    request = dict(
        entity_id=work["entity_id"],
        question_ids=[work["question_id"]],
        scope_bindings={work["question_id"]: dict(scope=work["scope"], scope_id=work["scope_id"])},
        **target_route(),
        cohort_id="B01_SYNTHETIC_TARGET",
        work_decision_refs=[
            dict(work_item_id=work["work_item_id"], decision_id=document["decision_id"]),
            dict(work_item_id=other["work_item_id"], decision_id=decision["decision_id"]),
        ],
    )
    monkeypatch.setattr("src.utils.quick_scan_refresh_inspect.time.time", lambda: clock[0])
    before = store.path.read_bytes()
    output = inspect_refresh(store.path, request, policy=policy, schema_version="1.3.0")
    assert set(output["work_items"]) == {work["question_id"]}
    assert (
        output["work_items"][work["question_id"]]["owner_control"]["decision_id"]
        == document["decision_id"]
    )
    request["work_decision_refs"] = request["work_decision_refs"][1:]
    with pytest.raises(ValueError):
        inspect_refresh(store.path, request, policy=policy, schema_version="1.3.0")
    assert store.path.read_bytes() == before
