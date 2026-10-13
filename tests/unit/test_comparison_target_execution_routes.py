"""Synthetic pure scope projection; native authority is checked by admission."""

import copy

import pytest

from src.utils.quick_scan_refresh_admission import AdmissionRejected
from src.utils.quick_scan_result_outbox import canonical_sha256
from tests.unit.test_controlled_refresh_execution import option_session


def configured():
    session = option_session()
    session.proof["request"]["protocol"] = "iqs.refresh_request/1.3.0"
    policy = dict(
        protocol="stockwiki.refresh_owner_policy/1.3.0",
        provider="minimax",
        model="MiniMax-M3",
        qa=dict(route_id="target", provider_config_ref="minimax-config", policy_id="budget"),
    )
    session.proof["owner_policy"] = policy
    target = dict(
        provider="minimax",
        model="MiniMax-M3",
        route_id="target",
        provider_config_ref="minimax-config",
        policy_id="budget",
        owner_policy_sha256=canonical_sha256(policy),
    )
    binding = session.entries["IQS_05"]["specification"]["owner_refresh_binding"]
    binding["protocol"] = "stockqa.owner_refresh_binding/4.0.0"
    binding["owner_control"]["comparison_target"] = target
    budget = dict(
        policy_id="budget",
        routes=[
            dict(
                id="primary",
                provider_config_ref="mimo-config",
                model="mimo-v2.6-flash",
                eligible=True,
            ),
            dict(
                id="target",
                provider_config_ref="minimax-config",
                model="MiniMax-M3",
                eligible=True,
            ),
        ],
    )
    return session, budget


def test_approved_target_bypasses_primary_order_without_mutating_original_policy():
    session, policy = configured()
    old = copy.deepcopy(policy)
    selected = session.execution_routes(policy)
    assert selected == [old["routes"][1]] and policy == old
    selected[0]["eligible"] = False
    assert policy == old
    session.assert_execution_controls(pack_size=3, thinking_enabled=True, max_output_tokens=32768)


@pytest.mark.parametrize(
    "fault",
    ["model", "config", "route", "disabled", "policy", "target_hash", "dialect"],
)
def test_wrong_or_unavailable_target_is_not_replaced_by_priority_first(fault):
    session, policy = configured()
    if fault == "model":
        policy["routes"][1]["model"] = "OTHER"
    elif fault == "config":
        policy["routes"][1]["provider_config_ref"] = "OTHER"
    elif fault == "route":
        policy["routes"][1]["id"] = "OTHER"
    elif fault == "disabled":
        policy["routes"][1]["eligible"] = False
    elif fault == "policy":
        policy["policy_id"] = "OTHER"
    elif fault == "target_hash":
        session.entries["IQS_05"]["specification"]["owner_refresh_binding"]["owner_control"][
            "comparison_target"
        ]["owner_policy_sha256"] = ("0" * 64)
    else:
        session.entries["IQS_05"]["specification"]["owner_refresh_binding"][
            "protocol"
        ] = "stockqa.owner_refresh_binding/3.0.0"
        with pytest.raises(AdmissionRejected):
            session.assert_execution_controls(
                pack_size=3, thinking_enabled=True, max_output_tokens=32768
            )
        return
    with pytest.raises(AdmissionRejected):
        session.execution_routes(policy)
