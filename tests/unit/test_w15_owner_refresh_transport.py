"""Typed owner transport substitute for refusal tests, never a real golden."""

import base64
import copy
import hashlib
import json
import os
import sqlite3
from contextlib import ExitStack, closing
from pathlib import Path

import pytest

from src.utils.quick_scan_owner_refresh import (
    OwnerRefreshClient,
    OwnerRefreshRejected,
    _read,
)
from src.utils.quick_scan_question_manifest import load_question_manifest
from src.utils.quick_scan_result_outbox import canonical_sha256
from src.utils.quick_scan_work_store import QuickScanWorkStore
from tests.unit.owner_refresh_port_test_support import approved_owner_refresh_port
from tests.unit.test_qa_net01_question_manifest import (
    IDENTITY,
    UUID_ENTITY,
    _manifest,
    _write_manifest,
)

NOW = "2026-10-09T12:00:00Z"


@pytest.mark.parametrize("owner_changes", [True, False])
def test_capacity_retry_rechecks_owner_before_reservation(tmp_path, monkeypatch, owner_changes):
    """Real budget capacity wait; owner RPC is the only substituted boundary."""
    import src.utils.quick_scan_work_transport as transport
    from tests.unit.test_quick_scan_budget import _policy, _reserve
    from tests.unit.test_quick_scan_work_store import _spec
    from tests.unit.test_w15_owner_refresh import _binding

    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    policy = _policy(max_cost=100)
    _reserve(store, policy, "DISPATCH_busy")
    current = ["route_A"]
    anchor_reads = []
    waits = []

    def owner_guard():
        anchor_reads.append(current[0])
        if current[0] != "route_A":
            raise ValueError("owner_refresh_current_changed")

    def release_capacity(seconds):
        waits.append(seconds)
        if owner_changes:
            current[0] = "route_B"
        store.record_budget_outcome(
            "DISPATCH_busy",
            outcome="confirmed_failure",
            http_status_code=429,
            actual_cost=0,
            cost_source_ref="synthetic-no-HTTP",
        )

    monkeypatch.setattr(transport.time, "sleep", release_capacity)
    row = None
    lease = None
    with ExitStack() as bindings:
        bindings.enter_context(transport.bind_quick_scan_budget(store, policy))
        bindings.enter_context(
            transport.bind_quick_scan_route(
                route_id="route-a1",
                provider="provider-a1",
                model_requested="fixture-a",
                quota_group="A",
            )
        )
        row = store.create_or_attach(
            **_spec(generation=2),
            run_id="RUN_CAPACITY",
            scan_id="SCAN_CAPACITY",
            owner_refresh_binding=_binding(provider="provider-a1", model="fixture-a"),
        )
        lease = store.claim(row["work_item_id"], lease_seconds=300)
        bindings.enter_context(transport.bind_quick_scan_work(store, row["work_item_id"], lease))
        bindings.enter_context(
            transport.bind_owner_refresh_guard(
                approved_owner_refresh_port(
                    store=store,
                    check=owner_guard,
                    work_item_ids=(row["work_item_id"],),
                    question_ids=(row["question_id"],),
                )
            )
        )
        if owner_changes:
            with pytest.raises(
                transport.QuickScanWorkPersistenceError, match="owner_refresh_guard_rejected"
            ):
                transport.begin_quick_scan_send(
                    "synthetic prompt", "system", model_requested="fixture-a"
                )
        else:
            assert (
                transport.begin_quick_scan_send(
                    "synthetic prompt", "system", model_requested="fixture-a"
                )
                is not None
            )
    assert len(waits) == 1
    assert len(anchor_reads) >= 2 and anchor_reads[-1] == current[0]
    if owner_changes:
        with closing(sqlite3.connect(store.path)) as con:
            assert con.execute("SELECT COUNT(*) FROM quick_scan_budget_attempt").fetchone()[0] == 1
        assert [a["phase"] for a in store.list_attempts(row["work_item_id"])] == ["prepared"]
    else:
        assert store.get_quick_scan_budget_status(policy["policy_id"])["requests"] == 2


def _fixture(tmp_path, qid="IQS_01"):
    document = _manifest([qid])
    document["module_release_id"] = "modrel_" + "f" * 64
    q = document["questions"][0]
    q.update(rubric_version="1.0.0", metric_id="score.test")
    path = _write_manifest(tmp_path, document)
    manifest = load_question_manifest(path)
    route = {
        "decision_id": "route_" + "d" * 64,
        "identity_ref": "unit-only",
        "as_of": "2026-10-08",
        "scope": "entity",
        "segment_id": None,
    }
    route_raw = json.dumps(route).encode()
    binding = dict(
        route_identity_ref="unit-only",
        subject_key="ASJ_TEST:1",
        perimeter_sha256="e" * 64,
        subject={"fixture": "synthetic"},
    )
    snap = dict(
        decision_id=route["decision_id"],
        subject_key=binding["subject_key"],
        perimeter_sha256=binding["perimeter_sha256"],
        entity_id=UUID_ENTITY,
        identity_revision=1,
        scope="entity",
        scope_id=UUID_ENTITY,
        manifest_raw_sha256=manifest["manifest_sha256"],
        route_raw_sha256=hashlib.sha256(route_raw).hexdigest(),
        module_locks=document["module_locks"],
        module_package_id=document["module_package_id"],
        module_release_id=document["module_release_id"],
        subject_binding=binding,
        route_raw_base64=base64.b64encode(route_raw).decode(),
        manifest_raw_base64=base64.b64encode(path.read_bytes()).decode(),
    )
    current = dict(
        expected_decision_id=route["decision_id"],
        anchor_version=1,
        owner_source_binding_refs=IDENTITY["source_binding_refs"],
        snapshot=snap,
        execution_validation={"new_execution_authorized": True},
    )
    plan = dict(
        protocol="stockwiki.module_refresh_plan/1.0.0",
        schema_version="1.0.0",
        subject_key=binding["subject_key"],
        identity_revision=1,
        perimeter_sha256=binding["perimeter_sha256"],
        decision_id=route["decision_id"],
        anchor_version=1,
        manifest_raw_sha256=manifest["manifest_sha256"],
        module_package_id=document["module_package_id"],
        module_release_id=document["module_release_id"],
        module_locks=document["module_locks"],
        provider="P1",
        model="M1",
        now=NOW,
        ttl_hours=120,
        scope="entity",
        scope_id=UUID_ENTITY,
        information_cutoff=route["as_of"],
        cutoff_policy="exact_period",
        model_API_requests=0,
        dispatch_started=False,
        fields=[
            dict(
                field_id=q["id"],
                decision="dispatch_new_work",
                generation=1,
                request_identity_key="unit-request",
                freshness_status="absent",
                reuse_observation_id=None,
            )
        ],
    )
    plan["refresh_id"] = "refresh_" + canonical_sha256(plan)
    plan["reused_observations"] = {}

    class Transport(OwnerRefreshClient):
        def _call(self, operation, extra):
            if operation == "current":
                return copy.deepcopy(self.current_fixture)
            if operation == "anchor":
                return dict(copy.deepcopy(self.current_fixture), new_execution_authorized=False)
            if operation == "refresh":
                return copy.deepcopy(self.plan_fixture)
            raise AssertionError(operation)

    client = Transport.__new__(Transport)
    client.config = dict(
        subject_key=binding["subject_key"],
        scope="entity",
        scope_id=UUID_ENTITY,
        ttl_hours=120,
        runs_dir=str(tmp_path / "temporary"),
    )
    client.current_fixture, client.plan_fixture = current, plan
    identity = dict(IDENTITY, entity_id=UUID_ENTITY)
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    return client, manifest, store, identity


@pytest.mark.parametrize(
    "field,value",
    [("provider", "P2"), ("model", "M2"), ("anchor_version", 2), ("manifest_raw_sha256", "a" * 64)],
)
def test_self_resealed_plan_binding_conflict_refused_before_work(tmp_path, field, value):
    client, manifest, store, identity = _fixture(tmp_path)
    client.plan_fixture[field] = value
    client.plan_fixture["refresh_id"] = "refresh_" + canonical_sha256(
        {
            k: v
            for k, v in client.plan_fixture.items()
            if k not in {"refresh_id", "reused_observations"}
        }
    )
    with pytest.raises(OwnerRefreshRejected, match="owner_refresh_plan_binding_mismatch"):
        client.prepare(manifest, store, identity=identity, provider="P1", model="M1", now=NOW)
    assert store.find_work_items(entity_id=UUID_ENTITY, question_ids=["IQS_01"])["IQS_01"] == []
    assert list(Path(client.config["runs_dir"]).iterdir()) == []


def test_incomplete_and_duplicate_question_plan_refused(tmp_path):
    client, manifest, store, identity = _fixture(tmp_path)
    client.plan_fixture["fields"] = []
    with pytest.raises(OwnerRefreshRejected, match="owner_refresh_plan_question_mismatch"):
        client.prepare(manifest, store, identity=identity, provider="P1", model="M1", now=NOW)


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e400}', b"[1]", b"{"])
def test_owner_json_is_strict_and_never_chooses_last_value(raw):
    with pytest.raises(OwnerRefreshRejected, match="owner_refresh_invalid_json"):
        _read(raw)


def test_unresolved_work_keeps_original_context_and_expired_unsent_lease_resumes(tmp_path):
    from src.core.models import Question
    from src.runners.llm_runner import QuickScanWorkLifecycle

    client, manifest, store, identity = _fixture(tmp_path)
    clock = [1800000000.0]
    store.clock = lambda: clock[0]
    session = client.prepare(manifest, store, identity=identity, provider="P1", model="M1", now=NOW)
    q = Question(text=manifest["questions"][0]["prompt"], question_id="IQS_01")
    lifecycle = QuickScanWorkLifecycle(
        store,
        entity_id=UUID_ENTITY,
        identity=identity,
        run_id="RUN_1",
        scan_id="SCAN_1",
        owner_refresh_session=session,
        scope_by_question=session.scope_bindings,
        transport_managed=True,
        lease_seconds=1,
    )
    first = lifecycle.before_question(q)
    assert first["claimed"] is True
    client.plan_fixture["fields"][0].update(decision="resume_existing_work", freshness_status=None)
    client.plan_fixture["refresh_id"] = "refresh_" + canonical_sha256(
        {
            k: v
            for k, v in client.plan_fixture.items()
            if k not in {"refresh_id", "reused_observations"}
        }
    )
    clock[0] += 2
    resumed = client.prepare(manifest, store, identity=identity, provider="P1", model="M1", now=NOW)
    next_lifecycle = QuickScanWorkLifecycle(
        store,
        entity_id=UUID_ENTITY,
        identity=identity,
        run_id="RUN_2",
        scan_id="SCAN_1",
        owner_refresh_session=resumed,
        scope_by_question=resumed.scope_bindings,
        transport_managed=True,
    )
    again = next_lifecycle.before_question(q)
    assert again["claimed"] is True and again["work_item_id"] == first["work_item_id"]
    assert again["lease"].lease_epoch == first["lease"].lease_epoch + 1


@pytest.mark.parametrize(
    "change",
    [
        {"protocol": "stockwiki.route_store_cli/2.0.0"},
        {"paid_dispatch_started": True},
        {"unexpected_command": "ignored?"},
    ],
)
def test_owner_public_wire_refuses_unregistered_versions_or_authority(change, monkeypatch):
    from src.utils.quick_scan_owner_refresh import _validate_owner_wire

    configured = os.environ.get("IQS_ROUTE_TEST_CODE_ROOT")
    if not configured:
        pytest.skip("original IQS schema root not configured; set IQS_ROUTE_TEST_CODE_ROOT")
    code_root = Path(configured)
    value = dict(
        protocol="stockwiki.route_store_cli/1.0.0",
        status="rejected",
        error_code="route_current_unavailable",
    )
    _validate_owner_wire(value, code_root)
    value.update(change)
    with pytest.raises(OwnerRefreshRejected, match="owner_refresh_wire_schema_mismatch"):
        _validate_owner_wire(value, code_root)


def test_partial_delivery_resume_uses_original_outbox_and_missing_owner_ack_never_reasks(tmp_path):
    """Actual Q10 persistence; owner transport is the explicitly typed boundary.

    The original result-ready item may be ACKed while another item is still
    pending. W15 cannot relabel that old checkpoint as a new-owner dispatch.
    """
    from src.core.models import Question
    from src.runners.llm_runner import QuickScanWorkLifecycle
    from tests.unit.test_q10_delivery import (
        JR2_CONSUMER,
        JR2_SOURCE,
        _ack_for,
        _jr2_ready,
        _lifecycle,
        _seed_checkpoint,
    )

    client, manifest, store, identity = _fixture(tmp_path, qid="IQS_05")
    original_store, wid, ready = _jr2_ready(tmp_path)
    pending = _seed_checkpoint(original_store, _lifecycle(original_store), "IQS_06")["handle"][
        "work_item_id"
    ]
    original_store.bind_result_delivery_consumer(wid, JR2_CONSUMER, source_ref=JR2_SOURCE)
    begun = original_store.begin_result_delivery(wid)
    original = (begun["request_bytes"], begun["idempotency_key"], begun["package_sha256"])
    q = Question(text=manifest["questions"][0]["prompt"], question_id="IQS_05")
    client.plan_fixture["fields"][0].update(decision="resume_existing_work", freshness_status=None)
    client.plan_fixture["refresh_id"] = "refresh_" + canonical_sha256(
        {
            k: v
            for k, v in client.plan_fixture.items()
            if k not in {"refresh_id", "reused_observations"}
        }
    )
    session = client.prepare(manifest, store, identity=identity, provider="P1", model="M1", now=NOW)
    lifecycle = QuickScanWorkLifecycle(
        store,
        entity_id=UUID_ENTITY,
        identity=identity,
        run_id="RUN_2",
        scan_id="SCAN_1",
        owner_refresh_session=session,
        scope_by_question=session.scope_bindings,
        transport_managed=True,
    )
    blocked = lifecycle.before_question(q)
    assert blocked == {
        "claimed": False,
        "reason": "resume_original_work_context_required",
        "work_item_id": wid,
    }
    original_store.confirm_result_delivery_not_sent(wid, "connection_refused_before_write")
    resumed = original_store.begin_result_delivery(wid)
    assert (
        resumed["request_bytes"],
        resumed["idempotency_key"],
        resumed["package_sha256"],
    ) == original
    assert original_store.apply_result_delivery_ack(wid, _ack_for(ready))["state"] == "delivered"
    assert original_store.get_item(pending)["status"] == "result_ready"
    client.plan_fixture["fields"][0].update(
        decision="dispatch_new_work", freshness_status="absent", generation=2
    )
    client.plan_fixture["refresh_id"] = "refresh_" + canonical_sha256(
        {
            k: v
            for k, v in client.plan_fixture.items()
            if k not in {"refresh_id", "reused_observations"}
        }
    )
    after_ack = client.prepare(
        manifest, store, identity=identity, provider="P1", model="M1", now=NOW
    )
    with pytest.raises(OwnerRefreshRejected, match="owner_refresh_ack_observation_missing"):
        after_ack.binding("IQS_05")
    assert len(store.find_work_items(entity_id=UUID_ENTITY, question_ids=["IQS_05"])["IQS_05"]) == 1
    assert original_store.get_result_delivery(wid)["package_bytes"] == original[0]


def _registered_paid_entry(client, manifest, store, identity, *, information_cutoff=None):
    from tests.unit.test_quick_scan_work_store import (
        _created,
        _successful_search_attempt,
    )

    client.plan_fixture["provider"] = "openai"
    client.plan_fixture["refresh_id"] = "refresh_" + canonical_sha256(
        {
            k: v
            for k, v in client.plan_fixture.items()
            if k not in {"refresh_id", "reused_observations"}
        }
    )
    original = client.prepare(
        manifest, store, identity=identity, provider="openai", model="M1", now=NOW
    )
    qid = manifest["questions"][0]["id"]
    binding = original.binding(qid)
    if information_cutoff is not None:
        binding["information_cutoff"] = information_cutoff
    spec = dict(
        identity,
        question_id=qid,
        scope_id=identity["entity_id"],
        generation=binding["target_generation"],
        owner_refresh_binding=binding,
    )
    work = _created(store, **spec)
    lease, attempt, receipt = _successful_search_attempt(
        store, work["work_item_id"], provider="openai", model="M1"
    )
    entries = {
        qid: dict(
            kind="work",
            work_item_id=work["work_item_id"],
            specification=dict(generation=work["generation"], owner_refresh_binding=binding),
        )
    }
    return entries, receipt


def test_paid_native_view_does_not_replan_or_require_new_execution(tmp_path):
    client, manifest, store, identity = _fixture(tmp_path)
    entries, receipt = _registered_paid_entry(client, manifest, store, identity)
    original_call = client._call
    calls = []

    def expired_new_send(operation, extra):
        calls.append(operation)
        if operation in {"current", "refresh"}:
            raise OwnerRefreshRejected("manual route override expired at execution")
        return original_call(operation, extra)

    client._call = expired_new_send
    before = store.path.read_bytes()
    session = client.prepare(
        manifest,
        store,
        identity=identity,
        provider="openai",
        model="M1",
        now=NOW,
        recovery_entries=entries,
    )
    assert calls == [] and session.current is None
    assert session.plan["protocol"] == "stockqa.registered_response_recovery_view/1.0.0"
    assert session.recovery_only and session.plan["new_execution_authorized"] is False
    assert session.plan["fields"][0]["decision"] == "resume_existing_work"
    assert store.path.read_bytes() == before
    with pytest.raises(OwnerRefreshRejected, match="cannot_dispatch"):
        session.binding(manifest["questions"][0]["id"])
    with pytest.raises(OwnerRefreshRejected, match="cannot_dispatch"):
        session.register_subject_dispatch({})


@pytest.mark.parametrize("fault", ["subject", "scope", "decision", "anchor", "withdrawn"])
def test_paid_native_view_checks_original_scope_without_today_anchor_gate(tmp_path, fault):
    client, manifest, store, identity = _fixture(tmp_path)
    entries, _ = _registered_paid_entry(client, manifest, store, identity)
    entry = next(iter(entries.values()))
    binding = entry["specification"]["owner_refresh_binding"]
    if fault == "subject":
        binding["subject_key"] = "other-subject"
    elif fault == "scope":
        binding["scope_id"] = "other-scope"
    elif fault == "decision":
        binding["decision_id"] = "route_" + "0" * 64
    elif fault == "anchor":
        binding["anchor_version"] = 99
    else:

        def withdrawn(operation, extra):
            raise OwnerRefreshRejected("route_current_unavailable")

        client._call = withdrawn
    before = store.path.read_bytes()
    if fault == "withdrawn":
        session = client.prepare(
            manifest,
            store,
            identity=identity,
            provider="openai",
            model="M1",
            now=NOW,
            recovery_entries=entries,
        )
        assert session.recovery_only and session.current is None
    else:
        with pytest.raises(OwnerRefreshRejected):
            client.prepare(
                manifest,
                store,
                identity=identity,
                provider="openai",
                model="M1",
                now=NOW,
                recovery_entries=entries,
            )
    assert store.path.read_bytes() == before


def test_paid_native_anchor_survives_real_iqs_manual_override_expiry(tmp_path, monkeypatch):
    """Real IQS expiry oracle + original SQLite safe; owner RPC alone is typed."""
    import importlib.util
    import sys

    configured_iqs = os.environ.get("IQS_ROUTE_TEST_CODE_ROOT")
    iqs = (
        Path(configured_iqs).resolve()
        if configured_iqs
        else Path(__file__).resolve().parents[3] / "invest-quick-scan"
    )
    if not (iqs / "tests/test_routing.py").is_file():
        pytest.skip("requires sibling IQS source for real offline route oracle")
    monkeypatch.syspath_prepend(str(iqs / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "paid_expiry_routing_fixture", iqs / "tests/test_routing.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixture = module.RoutingFixture()
    fixture.setUp()
    try:
        expiry = "2026-10-10T00:00:00Z"
        decision = fixture.route(
            now_utc=NOW,
            user_policy={
                "manual_overrides": [
                    dict(
                        module_id="semiconductors",
                        decision="selected",
                        actor="synthetic-owner",
                        reason="Synthetic reviewed classification",
                        valid_until=expiry,
                    )
                ]
            },
        )
        module.routing.validate_recorded_route_execution(decision, now_utc=NOW, root=fixture.root)
        with pytest.raises(ValueError, match="expired at execution"):
            module.routing.validate_recorded_route_execution(
                decision, now_utc=expiry, root=fixture.root
            )
        client, manifest, store, identity = _fixture(tmp_path)
        entries, receipt = _registered_paid_entry(client, manifest, store, identity)
        original_call = client._call

        def original_owner(operation, extra):
            if operation in {"current", "refresh"}:
                module.routing.validate_recorded_route_execution(
                    decision, now_utc=expiry, root=fixture.root
                )
                pytest.fail("new send was unexpectedly allowed after override deadline")
            return original_call(operation, extra)

        client._call = original_owner
        before = store.path.read_bytes()
        session = client.prepare(
            manifest,
            store,
            identity=identity,
            provider="openai",
            model="M1",
            now=expiry,
            recovery_entries=entries,
        )
        assert session.recovery_only and session.plan["new_execution_authorized"] is False
        assert store.path.read_bytes() == before
        work_id = next(iter(entries.values()))["work_item_id"]
        assert (
            store.get_attempt_response(store.list_attempts(work_id)[-1]["attempt_id"])["receipt"]
            == receipt
        )
    finally:
        fixture.doCleanups()


def test_paid_view_uses_registered_scan_cutoff_not_older_classification_date(tmp_path):
    client, manifest, store, identity = _fixture(tmp_path)
    entries, _ = _registered_paid_entry(
        client, manifest, store, identity, information_cutoff="2026-10-09"
    )
    original_binding = next(iter(entries.values()))["specification"]["owner_refresh_binding"]
    assert (
        json.loads(base64.b64decode(client.current_fixture["snapshot"]["route_raw_base64"]))[
            "as_of"
        ]
        == "2026-10-08"
    )
    session = client.prepare(
        manifest,
        store,
        identity=identity,
        provider="openai",
        model="M1",
        now=NOW,
        recovery_entries=entries,
    )
    assert session.plan["information_cutoff"] == "2026-10-09"
    assert original_binding["information_cutoff"] == "2026-10-09"
