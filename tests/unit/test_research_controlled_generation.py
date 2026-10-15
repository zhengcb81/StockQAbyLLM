"""Native SQLite comparison grants; synthetic fixtures, no HTTP or owner gold."""

import copy
from contextlib import ExitStack

import pytest

from src.utils import quick_scan_work_transport as transport
from src.utils.quick_scan_work_control import project_owner_control
from tests.unit import test_quick_scan_known_outcome as original
from tests.unit.test_controlled_refresh_binding import create
from tests.unit.test_owner_comparison_target_grant import permission
from tests.unit.test_w15_owner_refresh import _binding


def material(tmp_path, monkeypatch, pack_size):
    policy = original._policy()
    policy["budget"]["currency"] = "CNY"
    route = copy.deepcopy(policy["routes"][0])
    route.update(id="research-target", provider_config_ref="deepseek", model="deepseek-flash")
    policy["routes"].append(route)
    monkeypatch.setattr(original, "_policy", lambda: copy.deepcopy(policy))
    store, clock, members, pack, receipt, _, _ = original.fixture(tmp_path)
    clock[0] += 121
    original.resolve(store, members, pack, receipt)
    source = store.get_item(members[0]["work_item_id"])
    target = dict(
        provider="deepseek",
        model="deepseek-flash",
        route_id="research-target",
        provider_config_ref="deepseek",
        policy_id=policy["policy_id"],
        owner_policy_sha256="a" * 64,
    )
    document = store.record_owner_retry_decision(
        source["work_item_id"],
        purpose="comparison",
        retry_after_seconds=0,
        owner_authority=permission(target),
        idempotency_key="research-protocol-synthetic",
        comparison_controls=dict(
            pack_size=pack_size, thinking_enabled=True, max_output_tokens=16384
        ),
    )
    subject = document["request"]["physical"]["subject_binding"]["subject_ref"]
    control = project_owner_control(
        document,
        source_work=source,
        subject_ref=subject,
        provider="deepseek",
        model="deepseek-flash",
        now=clock[0],
        cohort_id="SYNTHETIC_RESEARCH_PROTOCOL",
    )["owner_control"]
    binding = _binding(
        protocol="stockqa.owner_refresh_binding/4.0.0",
        subject_context_schema="stockqa.quick_scan_observation_context/2.0.0",
        owner_control=control,
        subject_key=subject["analysis_subject_id"]
        + "@"
        + str(subject["analysis_subject_revision"]),
        perimeter_sha256=subject["perimeter_sha256"],
        entity_id=source["entity_id"],
        question_id=source["question_id"],
        scope=source["scope"],
        scope_id=source["scope_id"],
        identity_revision=source["identity_revision"],
        provider="deepseek",
        model="deepseek-flash",
    )
    keys = (
        "entity_id",
        "question_id",
        "scope",
        "scope_id",
        "identity_revision",
        "source_binding_version",
        "identity_state",
        "source_binding_ref",
        "identity_snapshot_sha256",
        "question_fingerprint",
    )
    spec = {k: source[k] for k in keys}
    spec.update(source_binding_refs=["BND_FIXTURE"], generation=2, routing_fingerprint="a" * 64)
    item = create(store, binding, spec)
    lease = store.claim(item["work_item_id"], lease_seconds=300)
    return store, item, lease, policy


@pytest.mark.parametrize("pack_size", [1, 3, 12])
def test_registered_deepseek_chat_payload_is_accepted_without_writing_or_dispatch(
    tmp_path, monkeypatch, pack_size
):
    store, item, lease, policy = material(tmp_path, monkeypatch, pack_size)
    with ExitStack() as scope:
        scope.enter_context(
            transport.bind_quick_scan_work(store, item["work_item_id"], lease)
            if pack_size == 1
            else transport.bind_quick_scan_question_pack(store, ((item["work_item_id"], lease),))
        )
        scope.enter_context(
            transport.bind_owner_refresh_guard(lambda: None)
        )  # explicitly fixture-only
        scope.enter_context(
            transport.bind_quick_scan_route(
                route_id="research-target", provider="deepseek", model_requested="deepseek-flash"
            )
        )
        scope.enter_context(transport.bind_quick_scan_budget(store, policy))
        before = store.path.read_bytes()
        transport.assert_controlled_generation_payload(
            dict(model="deepseek-flash", thinking={"type": "enabled"}, max_tokens=16384),
            provider="deepseek",
            protocol="deepseek_chat_completions",
        )
        assert store.path.read_bytes() == before and store.list_attempts(item["work_item_id"]) == []


@pytest.mark.parametrize(
    "fault",
    [
        "thinking",
        "thinking_shape",
        "thinking_type_list",
        "thinking_type_missing",
        "tokens",
        "boolean_tokens",
        "missing_tokens",
        "model",
        "provider",
        "guard",
        "route_missing",
        "budget_missing",
        "route",
        "protocol",
    ],
)
def test_chat_support_never_weakens_existing_native_controls(tmp_path, monkeypatch, fault):
    store, item, lease, policy = material(tmp_path, monkeypatch, 3)
    body = dict(model="deepseek-flash", thinking={"type": "enabled"}, max_tokens=16384)
    provider = "deepseek"
    protocol = "deepseek_chat_completions"
    route_id = "research-target"
    if fault == "thinking":
        body["thinking"] = {"type": "disabled"}
    elif fault == "thinking_shape":
        body["thinking"] = "enabled"
    elif fault == "thinking_type_list":
        body["thinking"] = {"type": []}
    elif fault == "thinking_type_missing":
        body["thinking"] = {}
    elif fault == "tokens":
        body["max_tokens"] = 4096
    elif fault == "boolean_tokens":
        body["max_tokens"] = True
    elif fault == "missing_tokens":
        body.pop("max_tokens")
    elif fault == "model":
        body["model"] = "another-model"
    elif fault == "provider":
        provider = "mimo"
    elif fault == "route":
        route_id = "another-route"
    elif fault == "protocol":
        protocol = "unverified_chat"
    with ExitStack() as scope:
        scope.enter_context(
            transport.bind_quick_scan_question_pack(store, ((item["work_item_id"], lease),))
        )
        if fault != "guard":
            scope.enter_context(transport.bind_owner_refresh_guard(lambda: None))
        if fault != "route_missing":
            scope.enter_context(
                transport.bind_quick_scan_route(
                    route_id=route_id, provider="deepseek", model_requested="deepseek-flash"
                )
            )
        if fault != "budget_missing":
            scope.enter_context(transport.bind_quick_scan_budget(store, policy))
        before = store.path.read_bytes()
        error_code = (
            "owner_scope_required"
            if fault == "guard"
            else (
                "approved_target_route_mismatch"
                if fault in {"route_missing", "budget_missing", "route"}
                else (
                    "route_mismatch"
                    if fault in {"model", "provider"}
                    else "protocol_unverified" if fault == "protocol" else "payload_mismatch"
                )
            )
        )
        with pytest.raises(
            transport.QuickScanWorkPersistenceError, match="controlled_generation_" + error_code
        ):
            transport.assert_controlled_generation_payload(
                body, provider=provider, protocol=protocol
            )
        assert store.path.read_bytes() == before and store.list_attempts(item["work_item_id"]) == []
