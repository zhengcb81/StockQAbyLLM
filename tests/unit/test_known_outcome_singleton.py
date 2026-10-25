"""Real native SDK over isolated SQLite; synthetic wire/owner, zero network."""

import copy
import sqlite3
from contextlib import nullcontext

import pytest

from src.providers.llm_research_client import ResearchChatSession
from src.utils.quick_scan_research_intent import bind_intent
from src.utils.quick_scan_work_decision_inspect import (
    derive_work_decision,
    inspect_work_decision,
)
from src.utils.quick_scan_work_store import QuickScanWorkStore
from src.utils.quick_scan_work_transport import (
    bind_quick_scan_budget,
    bind_quick_scan_route,
    bind_quick_scan_work,
    bind_subject_dispatch_registrar,
)
from tests.unit.test_c06_subject_send import receipt as subject_receipt
from tests.unit.test_c06_subject_send import v2
from tests.unit.test_llm_research_client import (
    TOOLS,
    client,
    deny_network,
    response,
    transport,
)
from tests.unit.test_quick_scan_budget import _policy
from tests.unit.test_quick_scan_known_outcome import authority, immutable
from tests.unit.test_w15_owner_refresh import _binding


def single(tmp_path, wire, monkeypatch, *, anchored=True, bound=True, context=True, cost=True):
    """Only the native mocked SDK creates attempts, receipt, charge and intent."""
    from src.providers import llm_research_client as sdk

    clock = [1800000000.0]
    monkeypatch.setattr(sdk, "_utc_now", lambda: "2027-01-15T08:00:00Z")
    wire[0].payloads = [response(actual_model="mimo-v2.6-pro")]
    ctx = v2()
    qid = next(iter(ctx["questions"]))
    metadata = ctx["questions"][qid]["metadata"]
    store = QuickScanWorkStore(tmp_path / "single.sqlite", clock=lambda: clock[0])
    policy = _policy(model="mimo-v2.6-pro", max_cost=5, max_requests=6, max_cost_per_attempt=0.5)
    policy["routes"] = [policy["routes"][0]]
    policy["routes"][0].update(id="research", provider_config_ref="mimo")
    resolver = lambda r: (
        dict(actual_cost=0.001, pricing_ref="fixture-price-v1", source_ref="fixture-only")
        if cost
        else None
    )
    with bind_quick_scan_budget(store, policy, cost_resolver=resolver):
        owner = _binding(
            protocol="stockqa.owner_refresh_binding/2.0.0",
            subject_context_schema="stockqa.quick_scan_observation_context/2.0.0",
            target_generation=1,
            question_id=qid,
            scope=metadata["scope"],
            scope_id=metadata["entity_id"],
            entity_id=metadata["entity_id"],
            identity_revision=1,
            provider="mimo",
            model="mimo-v2.6-pro",
        )
        item = store.create_or_attach(
            entity_id=metadata["entity_id"],
            question_id=qid,
            generation=1,
            scope=metadata["scope"],
            scope_id=metadata["entity_id"],
            identity_revision=1,
            source_binding_version=1,
            identity_state="verified",
            source_binding_ref="BND_FIXTURE",
            source_binding_refs=["BND_FIXTURE"],
            identity_snapshot_sha256=ctx["identity_snapshot_sha256"],
            question_fingerprint=ctx["questions"][qid]["work_prompt_sha256"],
            routing_fingerprint="e" * 64,
            run_id="RUN_SINGLE",
            scan_id="SCAN_SINGLE",
            **({"owner_refresh_binding": owner} if bound and context else {}),
        )
        lease = store.claim(item["work_item_id"], lease_seconds=120)
        if context:
            store.freeze_observation_context_before_send(item["work_item_id"], lease, ctx)
        current = client()
        current.provider_name, current.model = "mimo", "mimo-v2.6-pro"
        current.base_url = "https://api.xiaomimimo.com/v1/chat/completions"
        with bind_quick_scan_route(
            route_id="research", provider="mimo", model_requested="mimo-v2.6-pro", quota_group="A"
        ), bind_quick_scan_work(
            store, item["work_item_id"], lease
        ), bind_subject_dispatch_registrar(
            subject_receipt
        ), (
            bind_intent(store, "a" * 64, "b" * 64, [item["work_item_id"]])
            if anchored
            else nullcontext()
        ):
            output = ResearchChatSession(current, system_prefix="stable", tools=TOOLS).step(
                user_context="one"
            )
        attempt = store.list_attempts(item["work_item_id"])[0]
        saved = store.get_attempt_response(attempt["attempt_id"])
        kwargs = dict(
            attempt_id=attempt["attempt_id"],
            expected_lease=lease,
            receipt_sha256=saved["receipt_sha256"],
            request_payload_sha256=saved["receipt"]["request_payload_sha256"],
            reason="answer_validation_rejected",
            owner_authority=authority(),
            idempotency_key="resolve-single",
            diagnostic=dict(
                code="standard_answer_invalid",
                artifact_ref="single-validation.json",
                artifact_sha256="a" * 64,
                response_sha256=saved["response_sha256"],
            ),
        )
    return store, clock, item, kwargs, policy, output


def intent_for(item, kwargs):
    return dict(
        kind="known_response_resolution",
        work_item_id=item["work_item_id"],
        **{k: kwargs[k] for k in ("attempt_id", "idempotency_key", "reason", "diagnostic")},
    )


def test_native_singleton_resolves_then_derives_lawful_next_generation_without_new_send(
    tmp_path, transport, monkeypatch
):
    store, clock, item, args, policy, _ = single(tmp_path, transport, monkeypatch)
    clock[0] += 121
    before = immutable(store)
    read = derive_work_decision(
        store.path, intent_for(item, args), policy=policy, clock=lambda: clock[0]
    )
    physical = read["request"]["physical"]
    assert physical["proof_protocol"] == "stockqa.native_singleton_known_response/1.0.0"
    assert "pack_id" not in physical and "pack_manifest_sha256" not in physical
    assert physical["budget_attempt_id"] == args["attempt_id"]
    assert physical["request_payload_sha256"] == args["request_payload_sha256"]
    assert physical["research_intent_binding_sha256"]
    assert (
        inspect_work_decision(store.path, read["request"], policy=policy, clock=lambda: clock[0])
        == read
    )
    assert immutable(store) == before
    resolved = store.resolve_known_response(item["work_item_id"], **args)
    assert resolved["request"] == read["request"]
    assert store.get_item(item["work_item_id"])["status"] == "failed"
    decision = store.record_owner_retry_decision(
        item["work_item_id"],
        purpose="retry_unusable_answer",
        retry_after_seconds=0,
        owner_authority=authority(),
        idempotency_key="retry-single",
    )
    assert decision["request"]["target_generation"] == 2
    assert decision["request"]["source_resolution_id"] == resolved["decision_id"]
    assert decision["request"]["physical"] == physical
    from src.utils.quick_scan_work_control import project_owner_control

    control = project_owner_control(
        decision,
        source_work=store.get_item(item["work_item_id"]),
        subject_ref=physical["subject_binding"]["subject_ref"],
        provider="mimo",
        model="mimo-v2.6-pro",
        now=clock[0],
        cohort_id="SYNTHETIC_SINGLE_G2",
    )
    assert control["decision"] == "dispatch_new_generation" and control["generation"] == 2
    assert (
        inspect_work_decision(
            store.path, decision["request"], policy=policy, clock=lambda: clock[0]
        )["request"]
        == decision["request"]
    )
    assert immutable(store) == before
    reopened = QuickScanWorkStore(store.path, clock=lambda: clock[0])
    assert reopened.resolve_known_response(item["work_item_id"], **args) == resolved
    assert reopened.get_owner_retry_decision(item["work_item_id"]) == decision
    assert len(transport[1]) == 1


@pytest.mark.parametrize(
    "fault",
    [
        "unanchored",
        "unbound",
        "contextless",
        "unpriced",
        "active",
        "receipt",
        "payload",
        "diagnostic",
        "dict-authority",
        "revoked",
    ],
)
def test_native_singleton_never_weakens_original_proof_or_authority_guards(
    tmp_path, transport, monkeypatch, fault
):
    store, clock, item, args, policy, _ = single(
        tmp_path,
        transport,
        monkeypatch,
        anchored=fault != "unanchored",
        bound=fault != "unbound",
        context=fault != "contextless",
        cost=fault != "unpriced",
    )
    if fault != "active":
        clock[0] += 121
    if fault == "receipt":
        args["receipt_sha256"] = "0" * 64
    if fault == "payload":
        args["request_payload_sha256"] = "0" * 64
    if fault == "diagnostic":
        args["diagnostic"]["response_sha256"] = "0" * 64
    if fault == "dict-authority":
        args["owner_authority"] = {"approved": True}
    if fault == "revoked":
        args["owner_authority"] = authority(revoke=True)
    before = store.path.read_bytes()
    with pytest.raises((ValueError, RuntimeError, PermissionError, TypeError)):
        store.resolve_known_response(item["work_item_id"], **args)
    assert store.path.read_bytes() == before
    assert len(transport[1]) == 1


@pytest.mark.parametrize(
    "field", ["request_payload_sha256", "work_item_ids_json", "binding_sha256"]
)
def test_native_singleton_rejects_corrupt_original_intent_atomically(
    tmp_path, transport, monkeypatch, field
):
    store, clock, item, args, policy, _ = single(tmp_path, transport, monkeypatch)
    clock[0] += 121
    # Deliberately corrupt only this test-owned fixture; real code never edits these rows.
    with sqlite3.connect(store.path) as con:
        con.execute("DROP TRIGGER quick_scan_research_intent_no_update")
        value = '["WORK_foreign"]' if field == "work_item_ids_json" else "0" * 64
        con.execute("UPDATE quick_scan_research_intent SET " + field + "=?", (value,))
    before = store.path.read_bytes()
    with pytest.raises((ValueError, RuntimeError)):
        derive_work_decision(
            store.path, intent_for(item, args), policy=policy, clock=lambda: clock[0]
        )
    assert store.path.read_bytes() == before


@pytest.mark.parametrize(
    "field",
    [
        "proof_protocol",
        "research_task_sha256",
        "research_intent_sha256",
        "research_intent_binding_sha256",
        "actual_cost_micros",
        "subject_binding",
    ],
)
def test_native_singleton_caller_rehash_cannot_replace_original_read_proof(
    tmp_path, transport, monkeypatch, field
):
    store, clock, item, args, policy, _ = single(tmp_path, transport, monkeypatch)
    clock[0] += 121
    derived = derive_work_decision(
        store.path, intent_for(item, args), policy=policy, clock=lambda: clock[0]
    )
    bad = copy.deepcopy(derived["request"])
    bad["physical"][field] = "foreign"
    before = store.path.read_bytes()
    with pytest.raises((ValueError, RuntimeError, TypeError)):
        inspect_work_decision(store.path, bad, policy=policy, clock=lambda: clock[0])
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("field", ["request_payload_sha256", "task_sha256", "intent_sha256"])
def test_rehashed_singleton_intent_cannot_replace_original_receipt_anchor(
    tmp_path, transport, monkeypatch, field
):
    from src.utils.quick_scan_research_intent import _encode, _hash

    store, clock, item, args, policy, _ = single(tmp_path, transport, monkeypatch)
    clock[0] += 121
    with sqlite3.connect(store.path) as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM quick_scan_research_intent").fetchone()
        binding = {
            k: row[k]
            for k in ("task_sha256", "intent_sha256", "native_attempt_id", "request_payload_sha256")
        }
        binding["work_item_ids"] = [item["work_item_id"]]
        binding[field] = "0" * 64
        con.execute("DROP TRIGGER quick_scan_research_intent_no_update")
        con.execute(
            "UPDATE quick_scan_research_intent SET " + field + "=?,binding_sha256=?",
            (binding[field], _hash(binding)),
        )
    before = store.path.read_bytes()
    with pytest.raises((ValueError, RuntimeError), match="physical_receipt_mismatch"):
        store.resolve_known_response(item["work_item_id"], **args)
    assert store.path.read_bytes() == before
