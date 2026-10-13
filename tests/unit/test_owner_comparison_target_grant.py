"""Trusted-port target evolution over real native journals, synthetic only.

These fixture grants are not StockWiki owner approvals or company gold labels.
"""

import copy
from dataclasses import replace

import pytest

from src.utils.quick_scan_work_control import project_owner_control
from tests.unit.test_quick_scan_known_outcome import (
    authority,
    fixture,
    immutable,
    resolve,
)


def target():
    return dict(
        provider="minimax",
        model="MiniMax-M3",
        route_id="minimax-target",
        provider_config_ref="minimax-config",
        policy_id="pack-policy",
        owner_policy_sha256="a" * 64,
    )


def setup(tmp_path):
    store, clock, members, pack, receipt, _, _ = fixture(tmp_path)
    clock[0] += 121
    resolve(store, members, pack, receipt)
    return store, clock, store.get_item(members[0]["work_item_id"])


def permission(value):
    class TargetFixtureAuthority(type(authority())):
        def authorize(self, request):
            return replace(super().authorize(request), comparison_target=copy.deepcopy(value))

    return TargetFixtureAuthority()


def record(store, work, perms, *, purpose="comparison"):
    return store.record_owner_retry_decision(
        work["work_item_id"],
        purpose=purpose,
        retry_after_seconds=0,
        owner_authority=perms,
        idempotency_key="target-evolution",
        comparison_controls=(
            dict(pack_size=1, thinking_enabled=True, max_output_tokens=32768)
            if purpose == "comparison"
            else None
        ),
    )


def test_explicit_new_target_document_preserves_old_source_and_fees_and_only_allows_granted_model(
    tmp_path,
):
    store, clock, work = setup(tmp_path)
    physical = immutable(store)
    document = record(store, work, permission(target()))
    assert document["protocol"] == "stockqa.owner_work_decision/1.1.0"
    assert document["request"]["physical"]["provider"] == "mimo"
    assert document["request"]["physical"]["model_requested"] == "mimo-v2.6-flash"
    assert document["grant"]["comparison_target"] == target()
    projected = project_owner_control(
        document,
        source_work=work,
        subject_ref=document["request"]["physical"]["subject_binding"]["subject_ref"],
        provider="minimax",
        model="MiniMax-M3",
        now=clock[0],
        cohort_id="B01_SYNTHETIC_MINIMAX",
    )
    assert projected["decision"] == "dispatch_new_generation"
    assert projected["owner_control"]["comparison_target"] == target()
    assert immutable(store) == physical and store.get_item(work["work_item_id"]) == work
    assert store.get_owner_retry_decision(work["work_item_id"]) == document
    with pytest.raises(ValueError, match="target"):
        project_owner_control(
            document,
            source_work=work,
            subject_ref=document["request"]["physical"]["subject_binding"]["subject_ref"],
            provider="minimax",
            model="MiniMax-M3-other",
            now=clock[0],
            cohort_id="B01_SYNTHETIC_MINIMAX",
        )


@pytest.mark.parametrize(
    "fault", ["model_empty", "policy_foreign", "extra", "bad_hash", "wrong_purpose"]
)
def test_trusted_port_cannot_supply_malformed_or_unrelated_target(tmp_path, fault):
    store, _, work = setup(tmp_path)
    value = target()
    purpose = "comparison"
    if fault == "model_empty":
        value["model"] = ""
    elif fault == "policy_foreign":
        value["policy_id"] = "OTHER_LEDGER_POLICY"
    elif fault == "extra":
        value["approved"] = True
    elif fault == "bad_hash":
        value["owner_policy_sha256"] = "unknown"
    else:
        purpose = "retry_unusable_answer"
    before = store.path.read_bytes()
    with pytest.raises((ValueError, TypeError)):
        record(store, work, permission(value), purpose=purpose)
    assert store.path.read_bytes() == before


def test_legacy_grant_keeps_exact_original_document_shape_and_source_route_constraint(
    tmp_path,
):
    store, clock, work = setup(tmp_path)
    document = record(store, work, authority())
    assert document["protocol"] == "stockqa.owner_work_decision/1.0.0"
    assert "comparison_target" not in document["grant"]
    with pytest.raises(ValueError):
        project_owner_control(
            document,
            source_work=work,
            subject_ref=document["request"]["physical"]["subject_binding"]["subject_ref"],
            provider="minimax",
            model="MiniMax-M3",
            now=clock[0],
            cohort_id="B01_SYNTHETIC_MINIMAX",
        )
