import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.interfaces.quick_scan_owner_refresh import (
    QUICK_SCAN_OWNER_REFRESH_PORT_PROTOCOL,
)
from src.utils import quick_scan_owner_refresh_api
from src.utils.quick_scan_owner_refresh import OwnerRefreshSession
from src.utils.quick_scan_owner_refresh_api import (
    adapt_owner_refresh,
    is_owner_refresh_port,
)
from src.utils.quick_scan_refresh_execution import ApprovedRefreshSession


def _approved_owner(store):
    native = object.__new__(OwnerRefreshSession)
    native.store = store
    native.observation_context = {"schema": "context-v1", "questions": {"Q1": {}}}
    native.client = SimpleNamespace(config={})
    native.current = {"snapshot": {"subject_binding": {"subject": {"entity_id": "ENT_1"}}}}

    owner = object.__new__(ApprovedRefreshSession)
    owner.native = native
    owner.service = SimpleNamespace(store=store, budget={"configured": True})
    owner.plan = {"perimeter_sha256": "a" * 64, "model": "m1"}
    owner.proof = {
        "request": {"protocol": "iqs.refresh_request/1.6.0"},
        "preview": {"ordered_route_targets": [{"route_id": "r1"}]},
        "confirmation": {"confirmation_id": "C1"},
    }
    owner.record = {"inputs": {"run_id": "RUN_1"}}
    owner.bundle = {"manifest": {"questions": [{"id": "Q1"}]}, "authority": {"id": "A1"}}
    owner.entries = {"Q1": {"kind": "work", "work_item_id": "W1"}}
    owner.fields = {"Q1": {"decision": "resume_existing_work"}}
    owner.work = {"Q1": {"status": "pending"}}
    owner.deferred = {}
    owner.scope_bindings = {"Q1": {"scope": "entity", "scope_id": "ENT_1"}}
    owner.recovery_only = True
    owner.recovery_work_item_ids = ["W1"]
    owner.remaining_work_item_ids = []
    owner.input_raw = {
        "manifest.json": b"manifest",
        "identity.json": b'{"payload":{"entity_id":"ENT_1","canonical_name":"Issuer","listings":[{"market":"US","ticker":"SYN"}]}}',
        "authority.json": b"authority",
        "owner-config.json": b'{"iqs_release_root":"/synthetic/release"}',
        "questions.json": b"questions",
    }
    owner.check = lambda: None
    return owner


def test_owner_port_is_opaque_and_snapshot_is_detached():
    store = object()
    owner = _approved_owner(store)
    port = adapt_owner_refresh(owner)

    assert quick_scan_owner_refresh_api.PUBLIC_API_PROTOCOL == (
        "stockqa.quick_scan_owner_refresh_public_api/1.2.0"
    )
    assert "original_semantic_resources" in quick_scan_owner_refresh_api.__all__
    assert "runtime_projection" in quick_scan_owner_refresh_api.__all__
    assert QUICK_SCAN_OWNER_REFRESH_PORT_PROTOCOL == "stockqa.quick_scan_owner_refresh_port/1.3.0"
    assert is_owner_refresh_port(port)
    assert port.is_bound_to(store)
    snapshot = port.snapshot()
    assert snapshot.kind == "approved"
    assert snapshot.requested_question_ids == ("Q1",)
    assert snapshot.ordered_route_targets == ({"route_id": "r1"},)
    assert not hasattr(port, "__dict__")
    assert not hasattr(port, "_owner")
    for name in ("native", "service", "proof", "entries", "plan", "record"):
        assert not hasattr(port, name)

    snapshot.plan["model"] = "changed"
    snapshot.entries["Q1"]["kind"] = "changed"
    assert owner.plan["model"] == "m1"
    assert owner.entries["Q1"]["kind"] == "work"


def test_runtime_projection_does_not_deepcopy_bundle_or_native_context():
    class ForbiddenDeepcopyDict(dict):
        def __deepcopy__(self, memo):
            raise AssertionError("full_snapshot_deepcopy_was_used")

    owner = _approved_owner(object())
    owner.bundle = ForbiddenDeepcopyDict(owner.bundle)
    owner.native.observation_context = ForbiddenDeepcopyDict(owner.native.observation_context)
    port = adapt_owner_refresh(owner)

    projection = quick_scan_owner_refresh_api.runtime_projection(port)

    assert projection.kind == "approved"
    assert projection.recovery_only is True
    assert projection.ordered_route_targets[0]["route_id"] == "r1"
    assert not hasattr(projection, "bundle")
    assert not hasattr(projection, "observation_context")


def test_work_item_scope_checks_original_registration_without_copying_bundle():
    class ForbiddenDeepcopyDict(dict):
        def __deepcopy__(self, memo):
            raise AssertionError("full_snapshot_deepcopy_was_used")

    store = SimpleNamespace(
        get_item=lambda work_id: {"question_id": "Q1", "work_item_id": work_id},
    )
    owner = _approved_owner(store)
    owner.bundle = ForbiddenDeepcopyDict(owner.bundle)
    port = adapt_owner_refresh(owner)

    port.assert_work_item_scope(store, ("W1",))
    with pytest.raises(ValueError, match="owner_refresh_work_scope_mismatch"):
        port.assert_work_item_scope(store, ("different-work",))


def test_registered_provider_actual_uses_runtime_projection_without_snapshot():
    from src.runners.llm_runner import RegisteredResearchWork

    class ForbiddenDeepcopyDict(dict):
        def __deepcopy__(self, memo):
            raise AssertionError("full_snapshot_deepcopy_was_used")

    owner = _approved_owner(object())
    owner.bundle = ForbiddenDeepcopyDict(owner.bundle)
    owner.native.observation_context = ForbiddenDeepcopyDict(owner.native.observation_context)
    owner.proof["preview"]["ordered_route_targets"] = [
        {
            "route_id": "route-a",
            "provider_config_ref": "provider-a",
            "model": "model-a",
            "provider": "resolved-provider-a",
        }
    ]
    port = adapt_owner_refresh(owner)

    work = RegisteredResearchWork(
        owner=port,
        store=None,
        transport_managed=True,
        route_id="route-a",
        provider_config_ref="provider-a",
        model_requested="model-a",
        model_resolution=None,
        budget_policy=None,
        budget_route=None,
        observation_context=None,
    )

    assert work.provider_actual() == "resolved-provider-a"


def test_runtime_projection_preserves_exact_route_binding_and_rejects_duplicates(monkeypatch):
    from src.runners.llm_runner import RegisteredResearchWork

    route = {
        "route_id": "route-a",
        "provider_config_ref": "provider-a",
        "model": "model-a",
        "provider": "resolved-provider-a",
    }
    projection = SimpleNamespace(
        request_protocol="iqs.refresh_request/1.6.0",
        ordered_route_targets=(route,),
    )
    monkeypatch.setattr(
        quick_scan_owner_refresh_api, "runtime_projection", lambda _owner: projection
    )

    class ProjectionOnlyPort:
        def snapshot(self):
            raise AssertionError("snapshot_must_not_be_used")

    work = RegisteredResearchWork(
        owner=ProjectionOnlyPort(),
        store=None,
        transport_managed=True,
        route_id="route-a",
        provider_config_ref="provider-a",
        model_requested="model-a",
        model_resolution=None,
        budget_policy=None,
        budget_route=None,
        observation_context=None,
    )
    assert work.provider_actual() == "resolved-provider-a"

    projection.ordered_route_targets = ({**route, "model": "today-model"},)
    with pytest.raises(ValueError, match="approved_client_route_mismatch"):
        work.provider_actual()

    projection.ordered_route_targets = (route, route)
    with pytest.raises(ValueError, match="approved_client_route_mismatch"):
        work.provider_actual()


def test_runtime_projection_falls_back_to_legacy_snapshot_and_is_fresh(monkeypatch):
    owner = _approved_owner(object())
    port = adapt_owner_refresh(owner)
    monkeypatch.setattr(type(port), "runtime_projection", None)

    legacy = quick_scan_owner_refresh_api.runtime_projection(port)
    assert legacy.entries["Q1"]["work_item_id"] == "W1"
    assert legacy.ordered_route_targets[0]["route_id"] == "r1"

    owner.entries["Q1"]["work_item_id"] = "W2"
    owner.recovery_only = False
    owner.native.observation_context["questions"]["Q1"]["changed"] = True
    current = quick_scan_owner_refresh_api.runtime_projection(port)

    assert legacy.entries["Q1"]["work_item_id"] == "W1"
    assert current.entries["Q1"]["work_item_id"] == "W2"
    assert legacy.recovery_only is True
    assert current.recovery_only is False
    assert current.observation_context_sha256 != legacy.observation_context_sha256


def test_owner_port_rejects_unregistered_objects_and_other_stores():
    store = object()
    port = adapt_owner_refresh(_approved_owner(store))
    assert not port.is_bound_to(object())
    with pytest.raises(ValueError, match="original_owner_refresh_session_required"):
        adapt_owner_refresh(SimpleNamespace(check=lambda: None))
    assert not is_owner_refresh_port(SimpleNamespace(check=lambda: None))


def test_owner_port_exports_native_input_hashes_without_exposing_source_bytes():
    import hashlib

    store = object()
    owner = _approved_owner(store)
    port = adapt_owner_refresh(owner)

    references = port.native_input_references()
    assert set(references) == set(owner.input_raw)
    assert references["manifest.json"] == {
        "path": "native-input/manifest.json",
        "bytes": len(owner.input_raw["manifest.json"]),
        "sha256": hashlib.sha256(owner.input_raw["manifest.json"]).hexdigest(),
    }
    assert port.identity_payload()["entity_id"] == "ENT_1"
    assert not hasattr(port, "input_raw")
    assert not hasattr(port, "semantic_release_root")
    owner.input_raw["manifest.json"] = b"changed"
    assert (
        references["manifest.json"]["sha256"]
        != hashlib.sha256(owner.input_raw["manifest.json"]).hexdigest()
    )


def _original_iqs_root_or_skip(monkeypatch):
    """Resolve the original IQS scripts root explicitly; absence is a skip.

    The owner layout is consumer configuration, never a hard-coded repo
    position: ``IQS_ORIGINAL_ROOT`` wins, the historical sibling layout is a
    convenience fallback, and without either the test cannot run.
    """
    import os

    import pytest

    configured = os.environ.get("IQS_ORIGINAL_ROOT")
    candidates = [Path(configured)] if configured else []
    if not configured:
        sibling = Path(__file__).resolve().parents[3]
        candidates.append(sibling)
        nested = sibling / "invest-quick-scan"
        candidates.append(nested)
    for candidate in candidates:
        if (candidate / "scripts" / "answer_validation.py").is_file():
            iqs_root = candidate.resolve()
            monkeypatch.syspath_prepend(str(iqs_root / "scripts"))
            return iqs_root
    pytest.skip("original IQS scripts root not configured; set IQS_ORIGINAL_ROOT")


def test_semantic_resources_are_resolved_from_owner_config_without_returning_paths(
    monkeypatch, tmp_path
):
    iqs_root = _original_iqs_root_or_skip(monkeypatch)
    import answer_validation

    release_root = tmp_path / "separate-owner-release"
    release_root.mkdir()
    context = {"schema": "context-v1", "questions": {"Q1": {"metadata": {}}}}
    owner = _approved_owner(object())
    owner.native.observation_context = context
    owner.native.client.config = {
        "iqs_code_root": str(iqs_root),
        "iqs_release_root": str(release_root),
    }
    port = adapt_owner_refresh(owner)

    def dispatch_check_unavailable():
        raise RuntimeError("current dispatch permission service unavailable")

    owner.check = dispatch_check_unavailable
    captured = []
    projection = {
        "protocol": "iqs.answer-semantic-resources/1.0.0",
        "context_sha256": "a" * 64,
        "module_package_id": None,
        "resource_digests": {"schema": "b" * 64},
        "format_resources": {"schema": {"type": "object"}},
    }
    monkeypatch.setattr(
        answer_validation,
        "project_original_semantic_resources",
        lambda observed, *, release_root: captured.append((observed, release_root)) or projection,
    )

    result = port.original_semantic_resources(context)

    assert captured == [(context, release_root.resolve())]
    assert result == projection and result is not projection
    # The public dispatch check remains live, independently of frozen reads.
    with pytest.raises(RuntimeError, match="current dispatch permission service unavailable"):
        port.check()
    assert str(iqs_root) not in json.dumps(result)
    assert str(release_root) not in json.dumps(result)


def test_semantic_resources_reject_context_from_another_owner(monkeypatch, tmp_path):
    iqs_root = _original_iqs_root_or_skip(monkeypatch)
    import answer_validation

    owner = _approved_owner(object())
    owner.native.client.config = {
        "iqs_code_root": str(iqs_root),
        "iqs_release_root": str(tmp_path.resolve()),
    }
    port = adapt_owner_refresh(owner)
    with pytest.raises(ValueError, match="original_semantic_context_mismatch"):
        port.original_semantic_resources({"schema": "other", "questions": {}})


def test_transport_context_keeps_the_port_not_the_session_bound_method():
    from src.utils.quick_scan_work_transport import (
        _check_owner_refresh_guard,
        bind_owner_refresh_guard,
        bind_quick_scan_work,
        current_quick_scan_owner_refresh_guard,
    )

    calls = []
    store = SimpleNamespace(
        get_item=lambda work_item_id: {
            "work_item_id": work_item_id,
            "question_id": "Q1",
        },
        get_owner_refresh_binding=lambda work_item_id: {"binding": {}},
    )
    owner = _approved_owner(store)
    owner.check = lambda: calls.append("checked")
    port = adapt_owner_refresh(owner)
    with bind_quick_scan_work(store, "W1", object()), bind_owner_refresh_guard(port):
        current = current_quick_scan_owner_refresh_guard()
        assert current is port
        assert getattr(current, "__self__", None) is None
        _check_owner_refresh_guard()
    assert calls == ["checked"]


@pytest.mark.parametrize(
    "bound_store,work_item_id,error",
    [
        ("different", "W1", "owner_refresh_store_mismatch"),
        ("same", "W_FOREIGN", "owner_refresh_work_scope_mismatch"),
    ],
)
def test_transport_guard_is_bound_to_the_current_store_and_work(bound_store, work_item_id, error):
    from src.utils.quick_scan_work_transport import (
        QuickScanWorkPersistenceError,
        _check_owner_refresh_guard,
        bind_owner_refresh_guard,
        bind_quick_scan_work,
    )
    from tests.unit.owner_refresh_port_test_support import approved_owner_refresh_port

    store = object()
    port = approved_owner_refresh_port(store=store, work_item_ids=("W1",))
    active_store = object() if bound_store == "different" else store
    with bind_quick_scan_work(active_store, work_item_id, object()), bind_owner_refresh_guard(port):
        with pytest.raises(QuickScanWorkPersistenceError, match="owner_refresh_guard_rejected"):
            _check_owner_refresh_guard()


def test_transport_rejects_arbitrary_callable_as_owner_guard():
    from src.utils.quick_scan_work_transport import bind_owner_refresh_guard

    with pytest.raises(ValueError, match="owner_refresh_port_required"):
        with bind_owner_refresh_guard(lambda: None):
            pytest.fail("an arbitrary callback cannot stand in for an owner capability")


def test_transport_rejects_owner_capability_without_concrete_work_scope(tmp_path):
    from src.utils.quick_scan_work_store import QuickScanWorkStore
    from src.utils.quick_scan_work_transport import (
        QuickScanWorkPersistenceError,
        _check_owner_refresh_guard,
        bind_owner_refresh_guard,
        bind_quick_scan_budget,
    )
    from tests.unit.owner_refresh_port_test_support import approved_owner_refresh_port
    from tests.unit.test_quick_scan_budget import _policy

    store = QuickScanWorkStore(tmp_path / "unscoped-owner.sqlite")
    owner_port = approved_owner_refresh_port(store=store, work_item_ids=())
    with bind_quick_scan_budget(store, _policy()), bind_owner_refresh_guard(owner_port):
        with pytest.raises(QuickScanWorkPersistenceError, match="owner_refresh_guard_rejected"):
            _check_owner_refresh_guard()


def test_transport_guard_cannot_authorize_an_unbound_request():
    from src.utils.quick_scan_work_transport import (
        QuickScanWorkPersistenceError,
        _check_owner_refresh_guard,
        bind_owner_refresh_guard,
    )

    store = SimpleNamespace()
    owner = _approved_owner(store)
    port = adapt_owner_refresh(owner)
    with bind_owner_refresh_guard(port):
        with pytest.raises(QuickScanWorkPersistenceError, match="owner_refresh_guard_rejected"):
            _check_owner_refresh_guard()


@pytest.mark.parametrize("send_kind", ["external_search", "mcp_control"])
def test_owner_change_blocks_external_permit_before_network_consumption(tmp_path, send_kind):
    from src.utils.quick_scan_work_store import QuickScanWorkStore, SendPermit
    from src.utils.quick_scan_work_transport import (
        QuickScanBudgetBinding,
        QuickScanSendAttempt,
        QuickScanWorkPersistenceError,
        _check_owner_refresh_guard,
        bind_owner_refresh_guard,
        bind_quick_scan_work,
    )
    from tests.unit.owner_refresh_port_test_support import approved_owner_refresh_port
    from tests.unit.test_quick_scan_budget import _policy
    from tests.unit.test_quick_scan_work_store import _spec

    store = QuickScanWorkStore(tmp_path / f"{send_kind}.sqlite")
    row = store.create_or_attach(**_spec(), run_id="RUN_OWNER_SEND", scan_id="SCAN_OWNER_SEND")
    lease = store.claim(row["work_item_id"], lease_seconds=120)
    state = ["approved"]
    checks = []

    def check_owner():
        checks.append(state[0])
        if state[0] != "approved":
            raise ValueError("fixture_owner_changed")

    owner_port = approved_owner_refresh_port(
        store=store,
        check=check_owner,
        work_item_ids=(row["work_item_id"],),
        question_ids=(row["question_id"],),
    )
    attempt_id = "ATTEMPT_" + send_kind
    operation = (
        {"external_operation_id": "EXTERNAL_OPERATION"}
        if send_kind == "external_search"
        else {"mcp_stage_id": "MCP_STAGE"}
    )
    attempt = QuickScanSendAttempt(
        None,
        attempt_id,
        SendPermit(attempt_id, 0.0, 0),
        budget_binding=QuickScanBudgetBinding(store, _policy()),
        budget_attempt_id=attempt_id,
        **operation,
    )

    with bind_quick_scan_work(store, row["work_item_id"], lease), bind_owner_refresh_guard(
        owner_port
    ):
        _check_owner_refresh_guard()
        state[0] = "changed"
        with pytest.raises(QuickScanWorkPersistenceError, match="owner_refresh_guard_rejected"):
            attempt.consume_for_post()

    assert checks == ["approved", "changed"]
    assert attempt._consumed is False
