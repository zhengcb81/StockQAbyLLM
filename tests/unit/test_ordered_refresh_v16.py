"""Ordered owner permissions and real temporary ledgers, never paid model calls."""

import copy
import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.utils.quick_scan_refresh_admission import AdmissionRejected
from src.utils.quick_scan_refresh_execution import ApprovedRefreshSession
from src.utils.quick_scan_refresh_inspect import inspect_refresh
from src.utils.quick_scan_work_store import QuickScanWorkStore, WorkConflictError
from tests.unit.test_quick_scan_budget import _policy, _reserve
from tests.unit.test_quick_scan_refresh_inspect import setup
from tests.unit.test_quick_scan_work_store import _spec
from tests.unit.test_w15_owner_refresh import _binding
from tests.unit.test_w15_owner_refresh_transport import NOW, _fixture


@pytest.fixture
def tmp_path():
    with tempfile.TemporaryDirectory(prefix="qa-ordered-") as owned:
        yield Path(owned)


def targets(policy, providers=("mimo", "deepseek")):
    return [
        dict(
            provider=provider,
            model=route["model"],
            route_id=route["id"],
            provider_config_ref=route["provider_config_ref"],
            quota_group=route["quota_group"],
        )
        for provider, route in zip(providers, policy["routes"][:2])
    ]


def test_inspection_v16_shares_one_original_budget_ceiling_and_primary_work_without_writes(
    tmp_path,
):
    ledger, request, item, policy = setup(tmp_path)
    request["ordered_route_targets"] = targets(policy)
    before = ledger.path.read_bytes()
    result = inspect_refresh(ledger.path, request, policy=policy, schema_version="1.6.0")
    single = inspect_refresh(
        ledger.path,
        {k: v for k, v in request.items() if k != "ordered_route_targets"},
        policy=policy,
        schema_version="1.1.0",
    )
    assert result["protocol"] == "stockqa.refresh_inspection/1.6.0"
    assert result["quote"]["max_http_requests"] == single["quote"]["max_http_requests"]
    assert result["quote"]["max_cost_micros"] == single["quote"]["max_cost_micros"]
    assert result["work_items"] == single["work_items"]
    assert result["API_requests"] == 0 and result["reservation_created"] is False
    assert ledger.path.read_bytes() == before


def test_ordered_inspection_keeps_one_sqlite_snapshot_during_a_real_budget_writer(
    tmp_path, monkeypatch
):
    from src.utils.quick_scan_refresh_inspect import ReadOnlyWorkView

    ledger, request, item, policy = setup(tmp_path)
    with closing(sqlite3.connect(ledger.path)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
    request["ordered_route_targets"] = targets(policy)
    original = ReadOnlyWorkView.get_quick_scan_budget_status
    changed = []

    def read(self, policy_id):
        value = original(self, policy_id)
        if not changed:
            changed.append(True)
            _reserve(ledger, policy, "other-dispatch-during-ordered-inspection")
        return value

    monkeypatch.setattr(ReadOnlyWorkView, "get_quick_scan_budget_status", read)
    result = inspect_refresh(ledger.path, request, policy=policy, schema_version="1.6.0")
    assert result["quote"]["max_http_requests"] == policy["budget"]["max_requests"]
    assert result["work_items"]["IQS_05"]["work_item_id"] == item["work_item_id"]
    assert ledger.get_quick_scan_budget_status(policy["policy_id"])["requests"] == 1
    later = inspect_refresh(ledger.path, request, policy=policy, schema_version="1.6.0")
    assert later["quote"]["max_http_requests"] == result["quote"]["max_http_requests"] - 1
    assert result["ledger_budget_sha256"] != later["ledger_budget_sha256"]


@pytest.mark.parametrize(
    "fault", ["primary", "route", "alias", "model", "quota", "duplicate", "extra"]
)
def test_inspection_v16_rejects_changed_or_unlisted_exact_targets(tmp_path, fault):
    ledger, request, item, policy = setup(tmp_path)
    selected = targets(policy)
    if fault == "primary":
        selected.reverse()
    elif fault == "duplicate":
        selected.append(copy.deepcopy(selected[0]))
    else:
        key = dict(route="route_id", alias="provider_config_ref", quota="quota_group").get(
            fault, fault
        )
        selected[1][key] = "not-in-original-policy"
    request["ordered_route_targets"] = selected
    before = ledger.path.read_bytes()
    with pytest.raises(ValueError):
        inspect_refresh(ledger.path, request, policy=policy, schema_version="1.6.0")
    assert ledger.path.read_bytes() == before


def session_and_policy(tmp_path):
    policy = _policy(model="mimo-v2.6-pro")
    policy["routes"][1]["model"] = "deepseek-flash"
    selected = targets(policy)
    session = ApprovedRefreshSession.__new__(ApprovedRefreshSession)
    session.proof = dict(
        request=dict(protocol="iqs.refresh_request/1.6.0"),
        preview=dict(
            provider="mimo", model="mimo-v2.6-pro", ordered_route_targets=copy.deepcopy(selected)
        ),
        owner_policy=dict(
            provider="mimo",
            model="mimo-v2.6-pro",
            ordered_route_targets=copy.deepcopy(selected),
            qa=dict(
                policy_id=policy["policy_id"],
                route_id=selected[0]["route_id"],
                provider_config_ref=selected[0]["provider_config_ref"],
            ),
        ),
    )
    session.work, session.deferred, session.entries = {"Q": {"status": "pending"}}, {}, {}
    session.recovery_only = False
    session._policy_check = lambda: None
    session.service = SimpleNamespace(
        store=SimpleNamespace(path=tmp_path / "work.sqlite"), budget=copy.deepcopy(policy)
    )
    config = dict(
        providers={
            selected[0]["provider_config_ref"]: dict(base_url="https://api.xiaomimimo.com/v1"),
            selected[1]["provider_config_ref"]: dict(base_url="https://api.deepseek.com/responses"),
        }
    )
    path = tmp_path / "llm.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return session, policy, path, selected


def test_approved_ordered_targets_are_detached_and_vendor_is_not_account_alias(tmp_path):
    session, policy, config, selected = session_and_policy(tmp_path)
    projected = session.ordered_route_targets
    assert projected == selected
    projected.reverse()
    assert session.ordered_route_targets == selected
    session.assert_provider_model("mimo", "mimo-v2.6-pro")
    session.assert_provider_model("deepseek", "deepseek-flash")
    with pytest.raises(AdmissionRejected):
        session.assert_provider_model("provider-a2", "deepseek-flash")
    with pytest.raises(AdmissionRejected):
        session.assert_provider_model("deepseek", "another-model")
    session.assert_executor_policy(config, policy, executor_store=session.service.store)
    routes = session.execution_routes(policy)
    assert [route["id"] for route in routes] == [target["route_id"] for target in selected]
    assert policy == session.service.budget
    session.proof["request"]["protocol"] = "iqs.refresh_request/1.1.0"
    assert session.ordered_route_targets is None
    with pytest.raises(AdmissionRejected):
        session.assert_provider_model("deepseek", "deepseek-flash")


def test_approved_route_vendor_configuration_changes_are_refused(tmp_path):
    session, policy, config, selected = session_and_policy(tmp_path)
    session.assert_executor_policy(config, policy, executor_store=session.service.store)
    document = json.loads(config.read_text(encoding="utf-8"))
    document["providers"][selected[1]["provider_config_ref"]][
        "base_url"
    ] = "https://api.openai.com/v1/responses"
    config.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(AdmissionRejected):
        session._policy_check()


@pytest.mark.parametrize("fault", [None, "unknown_send", "vendor", "alias", "route"])
def test_paid_only_routes_use_original_safe_target_without_current_endpoint_or_key(tmp_path, fault):
    from src.utils.quick_scan_work_store import quick_scan_receipt_sha256
    from tests.unit.test_quick_scan_work_store import (
        _created,
        _prepared,
        _synthetic_search_receipt,
    )

    session, policy, config, selected = session_and_policy(tmp_path)
    selected[1].update(provider="openai", model="gpt-fixture")
    policy["routes"][1]["model"] = "gpt-fixture"
    session.proof["preview"]["ordered_route_targets"] = copy.deepcopy(selected)
    session.proof["owner_policy"]["ordered_route_targets"] = copy.deepcopy(selected)
    session.recovery_only = True
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    session.service.store = store
    session.service.budget = copy.deepcopy(policy)
    work = _created(store)
    lease = store.claim(work["work_item_id"], lease_seconds=60)
    target = selected[1]
    attempt = _prepared(
        store,
        work["work_item_id"],
        lease,
        model=target["model"],
        route_id="unlisted-route" if fault == "route" else target["route_id"],
        provider="unlisted-account" if fault == "alias" else target["provider_config_ref"],
    )
    store.mark_send_intent(work["work_item_id"], lease, attempt["attempt_id"])
    if fault != "unknown_send":
        receipt = _synthetic_search_receipt(
            provider="mimo" if fault == "vendor" else "openai", model=target["model"]
        )
        store.record_attempt_outcome(
            work["work_item_id"],
            lease,
            attempt["attempt_id"],
            outcome="response_available",
            http_status_code=200,
            receipt_sha256=quick_scan_receipt_sha256(receipt),
            execution_receipt=receipt,
        )
    session.entries = {"IQS_05": dict(kind="work", work_item_id=work["work_item_id"])}
    # prepare_execution normally supplies these from the immutable work rows.
    received = store.get_attempt_response(attempt["attempt_id"]) is not None
    session.recovery_work_item_ids = [work["work_item_id"]] if received else []
    session.remaining_work_item_ids = [] if received else [work["work_item_id"]]
    config.unlink()  # Only the disposable test's current model configuration.
    before = store.path.read_bytes()
    session.assert_executor_policy(config, policy, executor_store=store)
    if fault is None:
        routes = session.execution_routes(policy)
        assert len(routes) == 1
        assert routes[0]["id"] == policy["routes"][1]["id"]
        assert routes[0]["eligible"] is False
        assert routes[0]["unavailable_reason"] == "received_response_only"
        with pytest.raises(AdmissionRejected, match="paid_recovery_cannot_dispatch"):
            session.claim_registered_question_batch(
                ["IQS_05"], run_id="NEW", scan_id="NEW", lease_seconds=60
            )
    else:
        expected_error = (
            "paid_recovery_original_response_required"
            if fault == "unknown_send"
            else "paid_recovery_original_target_response_required"
        )
        with pytest.raises(AdmissionRejected, match=expected_error):
            session.execution_routes(policy)
    assert store.path.read_bytes() == before


def test_binding_v5_freezes_targets_in_original_journal_and_old_binding_cannot_gain_them(tmp_path):
    policy = _policy()
    selected = targets(policy, providers=("P1", "P2"))
    binding = _binding(
        protocol="stockqa.owner_refresh_binding/5.0.0",
        subject_context_schema="stockqa.quick_scan_observation_context/2.0.0",
        model=selected[0]["model"],
        ordered_route_targets=selected,
    )
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    spec = _spec(generation=2)
    work = store.create_or_attach(
        **spec, run_id="RUN", scan_id="SCAN", owner_refresh_binding=binding
    )
    assert store.get_owner_refresh_binding(work["work_item_id"])["binding"] == binding
    changed = copy.deepcopy(binding)
    changed["ordered_route_targets"][1]["quota_group"] = "other"
    with pytest.raises(WorkConflictError):
        store.create_or_attach(
            **spec, run_id="OTHER", scan_id="SCAN", owner_refresh_binding=changed
        )
    with pytest.raises(ValueError):
        store.create_or_attach(
            **_spec(generation=3),
            run_id="OLD",
            scan_id="SCAN",
            owner_refresh_binding=dict(
                binding, protocol="stockqa.owner_refresh_binding/2.0.0", target_generation=3
            ),
        )


def test_native_prepare_forwards_frozen_targets_and_clock_in_owned_temporary_cli_input(
    tmp_path, monkeypatch
):
    client, manifest, store, identity = _fixture(tmp_path)
    selected = [
        dict(
            provider="P1", model="M1", route_id="r1", provider_config_ref="alias1", quota_group="g1"
        ),
        dict(
            provider="P2", model="M2", route_id="r2", provider_config_ref="alias2", quota_group="g2"
        ),
    ]
    client.plan_fixture["cache_clock"] = "successful_scan"
    from src.utils.quick_scan_result_outbox import canonical_sha256

    client.plan_fixture["refresh_id"] = "refresh_" + canonical_sha256(
        {
            k: v
            for k, v in client.plan_fixture.items()
            if k not in {"refresh_id", "reused_observations"}
        }
    )
    original_call = client._call
    paths = []

    def call(operation, extra):
        if operation == "refresh":
            path = Path(extra[extra.index("--ordered-route-targets") + 1])
            paths.append(path)
            assert json.loads(path.read_text(encoding="utf-8")) == selected
            assert extra[extra.index("--cache-clock") + 1] == "successful_scan"
        return original_call(operation, extra)

    monkeypatch.setattr(client, "_call", call)
    session = client.prepare(
        manifest,
        store,
        identity=identity,
        provider="P1",
        model="M1",
        now=NOW,
        ordered_route_targets=selected,
        cache_clock="successful_scan",
    )
    assert session.ordered_route_targets == selected
    assert paths and not any(path.exists() for path in paths)


@pytest.mark.parametrize(
    "provider,model,fault",
    [
        ("P1", "M1", None),
        ("P2", "M2", None),
        ("P2", "M2", "expired"),
        ("P2", "M2", "unlisted"),
        ("P2", "M2", "missing_receipt"),
        ("P2", "M2", "answer_clock"),
        ("P1", "M1", "legacy_clock"),
    ],
)
def test_successful_scan_reference_retains_old_information_date_and_actual_selected_model(
    tmp_path, provider, model, fault
):
    import base64
    from datetime import datetime, timedelta, timezone

    from src.core.models import Question
    from src.utils.quick_scan_owner_refresh import OwnerRefreshSession
    from src.utils.quick_scan_result_outbox import canonical_sha256

    client, manifest, store, identity = _fixture(tmp_path)
    plan = copy.deepcopy(client.plan_fixture)
    question = json.loads(
        base64.b64decode(client.current_fixture["snapshot"]["manifest_raw_base64"])
    )["questions"][0]
    observed = datetime.now(timezone.utc).replace(microsecond=0)
    if fault == "expired":
        observed -= timedelta(hours=plan["ttl_hours"] + 1)
    stamp = observed.isoformat().replace("+00:00", "Z")
    original = dict(
        observation_id="obs_" + "a" * 64,
        question_id=question["id"],
        entity_id=identity["entity_id"],
        identity_revision=identity["identity_revision"],
        analysis_subject=client.current_fixture["snapshot"]["subject_binding"]["subject"],
        scope="entity",
        information_cutoff=plan["information_cutoff"],
        observed_at=stamp,
        question_semantic_sha256=question["semantic_sha256"],
        question_definition_sha256=question["definition_sha256"],
        question_version=question["rubric_version"],
        execution=dict(
            provider=provider,
            model_resolved=model,
            answered_at=stamp,
            search_status="executed",
            search_receipt_id="SEARCH_FIXTURE",
        ),
        answer=dict(
            status="scored",
            score=7,
            summary="Synthetic stored answer",
            information_as_of="2020-01-01",
        ),
    )
    qid = question["id"]
    plan.update(
        cache_clock="successful_scan",
        reused_observations={
            original["observation_id"]: dict(
                observation_id=original["observation_id"],
                payload=original,
                payload_sha256=canonical_sha256(original),
            )
        },
    )
    plan["fields"][0].update(
        decision="reuse",
        reuse_observation_id=original["observation_id"],
        cache_clock="successful_scan",
        cache_started_at=stamp,
        cache_valid_until=(observed + timedelta(hours=plan["ttl_hours"]))
        .isoformat()
        .replace("+00:00", "Z"),
    )
    session = OwnerRefreshSession(
        client,
        client.current_fixture,
        plan,
        {qid: dict(scope="entity", scope_id=identity["entity_id"])},
        {},
        identity,
        store,
    )
    session.ordered_route_targets = [
        dict(provider="P1", model="M1"),
        dict(provider="P2", model="M2"),
    ]
    if fault == "unlisted":
        session.ordered_route_targets.pop()
    elif fault == "missing_receipt":
        session.plan["reused_observations"][original["observation_id"]]["payload"]["execution"][
            "search_receipt_id"
        ] = ""
    elif fault == "answer_clock":
        session.plan["fields"][0]["cache_started_at"] = "2020-01-01T00:00:00Z"
    elif fault == "legacy_clock":
        session.plan.pop("cache_clock")
    if fault in {"missing_receipt"}:
        # Keep the original input hash coherent: the failure must be the absent
        # receipt clock, not a deliberately corrupted payload.
        row = session.plan["reused_observations"][original["observation_id"]]
        row["payload_sha256"] = canonical_sha256(row["payload"])
    if fault is not None:
        from src.utils.quick_scan_owner_refresh import OwnerRefreshRejected

        expected = (
            "binding_mismatch"
            if fault == "unlisted"
            else "expired" if fault in {"expired", "legacy_clock"} else "scan_clock_invalid"
        )
        with pytest.raises(OwnerRefreshRejected, match=expected):
            session.reference(Question("Synthetic", question_id=qid))
        return
    result = session.reference(Question("Synthetic", question_id=qid))
    assert result.answer.metadata["actual_model"] == model
    assert result.answer.metadata["actual_provider"] == provider
    assert (
        result.answer.metadata["owner_observation_reference"]["payload"]["answer"][
            "information_as_of"
        ]
        == "2020-01-01"
    )
