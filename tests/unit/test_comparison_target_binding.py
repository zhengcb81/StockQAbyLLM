"""Native binding4; explicit grants stay separate from old source/fee tables."""

import copy
from contextlib import ExitStack

import pytest

from src.utils import quick_scan_work_transport as transport
from src.utils.quick_scan_work_control import project_owner_control
from src.utils.quick_scan_work_store import QuickScanWorkStore
from tests.unit.test_comparison_target_inspection import prepared, target_route
from tests.unit.test_controlled_refresh_binding import create
from tests.unit.test_owner_comparison_target_grant import target
from tests.unit.test_quick_scan_known_outcome import immutable
from tests.unit.test_w15_owner_refresh import _binding


def material(tmp_path, monkeypatch):
    store, clock, source, document, policy = prepared(tmp_path, monkeypatch)
    subject = document["request"]["physical"]["subject_binding"]["subject_ref"]
    control = project_owner_control(
        document,
        source_work=source,
        subject_ref=subject,
        provider="minimax",
        model="MiniMax-M3.1-Flash",
        now=clock[0],
        cohort_id="SYNTHETIC_CROSS_MODEL",
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
        provider="minimax",
        model="MiniMax-M3.1-Flash",
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
    spec = {key: source[key] for key in keys}
    spec.update(source_binding_refs=["BND_FIXTURE"], generation=2, routing_fingerprint="a" * 64)
    return store, clock, source, binding, spec, policy


def test_new_target_binding_creates_one_generation_and_restart_preserves_source(
    tmp_path, monkeypatch
):
    store, clock, source, binding, spec, _ = material(tmp_path, monkeypatch)
    physical = immutable(store)
    item = create(store, binding, spec)
    assert item["generation"] == 2 and item["status"] == "pending"
    assert store.get_item(source["work_item_id"]) == source and immutable(store) == physical
    again = QuickScanWorkStore(store.path, clock=lambda: clock[0])
    assert again.get_owner_refresh_binding(item["work_item_id"])["binding"] == binding
    assert create(again, binding, spec)["work_item_id"] == item["work_item_id"]


@pytest.mark.parametrize(
    "fault",
    [
        "legacy",
        "target_model",
        "target_route",
        "target_hash",
        "target_missing",
        "source_model",
        "provider",
    ],
)
def test_consumer_cannot_downgrade_or_change_trusted_target(tmp_path, monkeypatch, fault):
    store, _, _, binding, spec, _ = material(tmp_path, monkeypatch)
    if fault == "legacy":
        binding["protocol"] = "stockqa.owner_refresh_binding/3.0.0"
    elif fault == "source_model":
        binding["model"] = "mimo-v2.6-flash"
    elif fault == "provider":
        binding["provider"] = "mimo"
    elif fault == "target_missing":
        binding["owner_control"].pop("comparison_target")
    else:
        binding["owner_control"]["comparison_target"][
            {
                "target_model": "model",
                "target_route": "route_id",
                "target_hash": "owner_policy_sha256",
            }[fault]
        ] = "FORGED"
    before = store.path.read_bytes()
    with pytest.raises((ValueError, RuntimeError)):
        create(store, binding, spec)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "route",
        "config",
        "model",
        "policy",
        "thinking",
        "missing_route",
        "missing_budget",
    ],
)
def test_actual_target_route_and_payload_checked_before_attempt(tmp_path, monkeypatch, fault):
    store, _, _, binding, spec, policy = material(tmp_path, monkeypatch)
    item = create(store, binding, spec)
    lease = store.claim(item["work_item_id"], lease_seconds=300)
    route = dict(
        route_id="minimax-target",
        provider="minimax-config",
        model_requested="MiniMax-M3.1-Flash",
    )
    if fault in {"route", "config", "model"}:
        route[{"route": "route_id", "config": "provider", "model": "model_requested"}[fault]] = (
            "FOREIGN"
        )
    if fault == "policy":
        policy["policy_id"] = "FOREIGN"
    data = dict(model="MiniMax-M3.1-Flash", thinking={"type": "adaptive"}, max_tokens=32768)
    if fault == "thinking":
        data["thinking"] = {"type": "disabled"}
    with ExitStack() as contexts:
        contexts.enter_context(transport.bind_quick_scan_work(store, item["work_item_id"], lease))
        contexts.enter_context(
            transport.bind_owner_refresh_guard(lambda: None)
        )  # fixture-only port
        if fault != "missing_route":
            contexts.enter_context(transport.bind_quick_scan_route(**route))
        if fault != "missing_budget":
            contexts.enter_context(transport.bind_quick_scan_budget(store, policy))
        before = store.path.read_bytes()

        def check():
            transport.assert_controlled_generation_payload(
                data, provider="minimax", protocol="anthropic_messages"
            )

        if fault == "none":
            check()
        else:
            with pytest.raises(transport.QuickScanWorkPersistenceError):
                check()
    assert store.path.read_bytes() == before and store.list_attempts(item["work_item_id"]) == []
