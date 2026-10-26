"""Exact registered-scope mapping; these pure fixtures do not authenticate."""

import copy
import hashlib

import pytest

from src.core.models import Question
from src.runners.llm_runner import LLMRunner
from src.utils.quick_scan_refresh_admission import AdmissionRejected
from src.utils.quick_scan_refresh_execution import (
    ApprovedRefreshSession,
    registered_scope,
)
from src.utils.quick_scan_result_outbox import canonical_sha256
from tests.unit.test_quick_scan_refresh_admission import scope


@pytest.mark.parametrize(
    "arguments",
    [
        {"refresh_admission_config": "missing.json"},
        {"refresh_confirmation_id": "rc1_" + "1" * 64},
        {"refresh_admission_config": "missing.json", "refresh_confirmation_id": "rc1_" + "1" * 64},
    ],
)
def test_incomplete_admission_arguments_fail_before_model_config(monkeypatch, arguments):
    import src.runners.llm_runner as runner

    monkeypatch.setattr(
        runner, "LLMConfig", lambda *a, **k: pytest.fail("must not read model config")
    )
    with pytest.raises(ValueError):
        LLMRunner().run(
            company="Synthetic", require_search=True, entity_id="ENT_SYNTHETIC", **arguments
        )


def materials():
    item, bundle = scope()
    qid = item["question_id"]
    question = Question("Synthetic question", question_id=qid)
    bundle["questions"] = {qid: question}
    bundle["manifest"]["manifest_sha256"] = "b" * 64
    bundle["identity"].update(
        identity_state="verified",
        source_binding_version=1,
        source_binding_ref="BND_SYNTHETIC",
        source_binding_refs=["BND_SYNTHETIC"],
        identity_snapshot_sha256="c" * 64,
    )
    binding = dict(
        protocol="stockqa.owner_refresh_binding/2.0.0",
        subject_context_schema="stockqa.quick_scan_observation_context/2.0.0",
        subject_key=item["subject_ref"]["analysis_subject_id"] + "@1",
        perimeter_sha256=item["subject_ref"]["perimeter_sha256"],
        decision_id="decision-synthetic",
        anchor_version=1,
        manifest_raw_sha256="b" * 64,
        provider="openai",
        model="fixture-model",
        information_cutoff="2026-10-10",
        request_identity_key="d" * 64,
        target_generation=1,
        question_id=qid,
        scope="entity",
        scope_id="ENT_SYNTHETIC",
        entity_id="ENT_SYNTHETIC",
        identity_revision=1,
    )
    spec = dict(
        bundle["identity"],
        question_id=qid,
        generation=1,
        scope="entity",
        scope_id="ENT_SYNTHETIC",
        question_fingerprint=hashlib.sha256(question.text.encode()).hexdigest(),
        routing_fingerprint=canonical_sha256(
            dict(
                subject_key=binding["subject_key"],
                request_identity_key=binding["request_identity_key"],
            )
        ),
        owner_refresh_binding=binding,
    )
    record = dict(
        inputs=dict(specifications=[spec], references=[]),
        receipt=dict(
            items=[
                dict(
                    work_item_id="WORK_SYNTHETIC",
                    entity_id="ENT_SYNTHETIC",
                    question_id=qid,
                    generation=1,
                    scope="entity",
                    scope_id="ENT_SYNTHETIC",
                )
            ]
        ),
    )
    return item, bundle, record


def test_registered_scope_selects_original_work_and_keeps_full_manifest():
    item, bundle, record = materials()
    full = copy.deepcopy(bundle["manifest"])
    entries = registered_scope([item], bundle, record, information_cutoff="2026-10-10")
    assert list(entries) == ["IQS_05"]
    assert entries["IQS_05"]["work_item_id"] == "WORK_SYNTHETIC"
    assert bundle["manifest"] == full


@pytest.mark.parametrize(
    "fault",
    [
        "entity_id",
        "scope_id",
        "generation",
        "identity_revision",
        "question_fingerprint",
        "routing_fingerprint",
        "perimeter",
        "subject",
        "context_version",
        "duplicate",
    ],
)
def test_registered_scope_rejects_resealed_wrong_work_or_binding(fault):
    item, bundle, record = materials()
    spec = record["inputs"]["specifications"][0]
    if fault in {"perimeter", "subject", "context_version"}:
        key = {
            "perimeter": "perimeter_sha256",
            "subject": "subject_key",
            "context_version": "subject_context_schema",
        }[fault]
        spec["owner_refresh_binding"][key] = "wrong"
    elif fault == "duplicate":
        record["inputs"]["specifications"].append(copy.deepcopy(spec))
        record["receipt"]["items"].append(copy.deepcopy(record["receipt"]["items"][0]))
    else:
        spec[fault] = 2 if fault in {"generation", "identity_revision"} else "wrong"
    with pytest.raises(AdmissionRejected):
        registered_scope([item], bundle, record, information_cutoff="2026-10-10")


def report_session():
    session = ApprovedRefreshSession.__new__(ApprovedRefreshSession)
    session.entries = {"IQS_05": {}}
    session.bundle = {"manifest": {"questions": [{"id": "IQS_05"}, {"id": "IQS_10"}]}}
    session.proof = dict(
        confirmation={"confirmation_id": "synthetic-only"}, preview={"scope_sha256": "a" * 64}
    )
    return session


@pytest.mark.parametrize(
    "status,complete",
    [("scored", True), ("unknown", True), ("insufficient_evidence", True), ("error", False)],
)
def test_subset_output_cannot_claim_whole_company_or_accuracy(status, complete):
    from types import SimpleNamespace as NS

    report = report_session().dispatch_scope_report(
        NS(results=[NS(question=NS(question_id="IQS_05"), answer=NS(status=status))])
    )
    assert report["requested_question_count"] == 1 and report["manifest_question_count"] == 2
    assert report["requested_scope_answered"] is complete
    assert report["whole_manifest_answered"] is False
    assert report["answer_completion_is_not_accuracy_or_qualification"] is True


@pytest.mark.parametrize("qids", [[], ["IQS_05", "IQS_05"], ["IQS_10"]])
def test_missing_duplicate_and_unapproved_output_are_not_scope_success(qids):
    from types import SimpleNamespace as NS

    batch = NS(
        results=[NS(question=NS(question_id=qid), answer=NS(status="scored")) for qid in qids]
    )
    if qids:
        with pytest.raises(AdmissionRejected, match="output_scope_mismatch"):
            report_session().dispatch_scope_report(batch)
    else:
        assert report_session().dispatch_scope_report(batch)["requested_scope_answered"] is False


def state_session(store, work_id, *, projected_work_id=None):
    """Component fixture only: native consent is NOT authenticated here."""
    from types import SimpleNamespace as NS

    item = store.get_item(work_id)
    qid = item["question_id"]
    row = dict(
        field_id=qid,
        decision="dispatch_new_generation",
        generation=999,
        reuse_observation_id=None,
        request_identity_key="d" * 64,
    )
    native = NS(
        plan={"fields": [row], "reused_observations": {}},
        fields={qid: row},
        scope_bindings={qid: {"scope": "entity", "scope_id": item["entity_id"]}},
        projection={qid: {"work_item_id": projected_work_id or work_id}},
    )
    session = ApprovedRefreshSession(
        NS(store=store),
        report_session().proof,
        {},
        {"manifest": {"questions": [{"id": qid}, {"id": "IQS_10"}]}, "authority": {}},
        {},
        {
            qid: {
                "kind": "work",
                "work_item_id": work_id,
                "specification": {
                    "owner_refresh_binding": {"protocol": "stockqa.owner_refresh_binding/2.0.0"}
                },
            }
        },
        native,
        {},
        {},
    )
    session.check = lambda: None  # Pure state tests, not a native permission proof.
    return session


@pytest.mark.parametrize("state", ["active_lease", "expired_unsent", "expired_send_intent"])
def test_original_lease_recovery_cannot_turn_unknown_send_into_new_work(tmp_path, state):
    from src.utils.quick_scan_work_store import QuickScanWorkStore
    from tests.unit.test_quick_scan_work_store import _created, _prepared

    clock = [1800000000.0]
    store = QuickScanWorkStore(tmp_path / "original.sqlite", clock=lambda: clock[0])
    item = _created(store)
    work_id = item["work_item_id"]
    lease = store.claim(work_id, lease_seconds=30)
    if state == "expired_send_intent":
        attempt = _prepared(store, work_id, lease)
        store.mark_send_intent(work_id, lease, attempt["attempt_id"])
    if state != "active_lease":
        clock[0] += 31
    session = state_session(store, work_id)
    expected = {
        "active_lease": "leased",
        "expired_unsent": "pending",
        "expired_send_intent": "uncertain",
    }[state]
    assert store.get_item(work_id)["status"] == expected
    assert session.existing_work("IQS_05")["work_item_id"] == work_id
    assert session.fields["IQS_05"]["generation"] == item["generation"] != 999
    assert session.needs_new_dispatch is (state == "expired_unsent")
    if state != "expired_unsent":
        report = session.recovery_report()
        assert report["API_requests"] == 0 and report["model_dispatch_started"] is False
        assert report["requested_scope_completed"] is False
        assert report["deferred_work"][0]["status"] == expected
    assert len(store.list_attempts(work_id)) == (1 if state == "expired_send_intent" else 0)


def test_old_approval_cannot_dispatch_latest_generation(tmp_path):
    from src.utils.quick_scan_work_store import QuickScanWorkStore
    from tests.unit.test_quick_scan_work_store import _created

    store = QuickScanWorkStore(tmp_path / "original.sqlite")
    old = _created(store)
    new = _created(store, generation=2, run_id="OTHER_ACTION")
    session = state_session(store, old["work_item_id"], projected_work_id=new["work_item_id"])
    assert session.needs_new_dispatch is False
    report = session.recovery_report()
    assert report["deferred_work"][0]["work_item_id"] == old["work_item_id"]
    assert report["deferred_work"][0]["reason"] == "registered_work_superseded"
    assert (
        store.list_attempts(old["work_item_id"]) == store.list_attempts(new["work_item_id"]) == []
    )


def test_recovery_reference_keeps_original_public_descriptor_and_time():
    from types import SimpleNamespace as NS

    payload = dict(
        observed_at="2025-04-01T00:00:00Z",
        execution={"model_resolved": "original-model"},
        answer={"score": 7},
    )
    original = dict(
        observation_id="obs_" + "a" * 64,
        payload_sha256=canonical_sha256(payload),
        payload=payload,
        qualification_status="historical_original",
    )
    session = report_session()
    session.check = lambda: None
    session.fields = {"IQS_05": {"decision": "reuse"}}
    session.bundle["questions"] = {"IQS_05": NS(question_id="IQS_05")}
    session.reference = lambda q: NS(answer=NS(metadata={"owner_observation_reference": original}))
    report = session.recovery_report()
    reference = report["observation_references"]["IQS_05"]
    assert reference["original_observation"] == payload
    assert reference["payload_sha256"] == original["payload_sha256"]
    assert reference["original_qualification_status"] == "historical_original"
    assert reference["new_observation_created"] is False
    assert payload["observed_at"] == "2025-04-01T00:00:00Z"


def executor_policy_session(store, policy):
    """Pure policy/ledger component fixture; this does not authenticate consent."""
    from types import SimpleNamespace as NS

    session = ApprovedRefreshSession.__new__(ApprovedRefreshSession)
    session.service = NS(store=store, budget=copy.deepcopy(policy))
    session._policy_check = None
    return session


def test_explicit_original_executor_store_allows_registered_nondefault_layout(tmp_path):
    from src.utils.quick_scan_work_store import QuickScanWorkStore
    from tests.unit.test_quick_scan_budget import _policy

    policy = _policy()
    store = QuickScanWorkStore(tmp_path / "approved-workspace" / "work.sqlite")
    config = tmp_path / "runtime" / "llm_apis.json"
    original = store.path.read_bytes()
    session = executor_policy_session(store, policy)

    session.assert_executor_policy(config, policy, executor_store=store)

    assert store.path.read_bytes() == original
    assert not (config.parent / "quick_scan_work.sqlite").exists()


def test_explicit_different_executor_store_still_refused_without_mutation(tmp_path):
    from src.utils.quick_scan_work_store import QuickScanWorkStore
    from tests.unit.test_quick_scan_budget import _policy

    policy = _policy()
    original = QuickScanWorkStore(tmp_path / "original.sqlite")
    different = QuickScanWorkStore(tmp_path / "different.sqlite")
    hashes = [store.path.read_bytes() for store in (original, different)]
    session = executor_policy_session(original, policy)

    with pytest.raises(AdmissionRejected, match="refresh_execution_original_ledger_required"):
        session.assert_executor_policy(tmp_path / "llm_apis.json", policy, executor_store=different)

    assert [store.path.read_bytes() for store in (original, different)] == hashes


def test_legacy_executor_store_omission_keeps_default_path_contract(tmp_path):
    from src.utils.quick_scan_work_store import QuickScanWorkStore
    from tests.unit.test_quick_scan_budget import _policy

    policy = _policy()
    default = QuickScanWorkStore(tmp_path / "quick_scan_work.sqlite")
    session = executor_policy_session(default, policy)
    session.assert_executor_policy(tmp_path / "llm_apis.json", policy)
    session.service.store = QuickScanWorkStore(tmp_path / "elsewhere.sqlite")
    with pytest.raises(AdmissionRejected, match="refresh_execution_original_ledger_required"):
        session.assert_executor_policy(tmp_path / "llm_apis.json", policy)


def test_explicit_original_executor_store_does_not_bypass_confirmed_policy(tmp_path):
    from src.utils.quick_scan_work_store import QuickScanWorkStore
    from tests.unit.test_quick_scan_budget import _policy

    policy = _policy()
    store = QuickScanWorkStore(tmp_path / "approved.sqlite")
    session = executor_policy_session(store, policy)
    altered = copy.deepcopy(policy)
    altered["budget"]["max_cost"] += 1
    with pytest.raises(
        AdmissionRejected, match="refresh_execution_confirmed_model_policy_required"
    ):
        session.assert_executor_policy(tmp_path / "llm_apis.json", altered, executor_store=store)


def test_explicit_original_executor_store_keeps_live_policy_change_guard(tmp_path):
    from src.utils.quick_scan_work_store import QuickScanWorkStore
    from tests.unit.test_quick_scan_budget import _policy

    policy = _policy()
    store = QuickScanWorkStore(tmp_path / "approved.sqlite")
    session = executor_policy_session(store, policy)
    current = copy.deepcopy(policy)
    session.assert_executor_policy(
        tmp_path / "llm_apis.json", policy, executor_store=store, policy_reader=lambda: current
    )
    session._policy_check()
    current["routes"][0]["model"] = "different-model"
    with pytest.raises(AdmissionRejected, match="refresh_execution_model_policy_changed"):
        session._policy_check()


def reactivated_session(tmp_path):
    """Real original SQLite/ORF; fake native transport is NOT consent gold."""
    from types import SimpleNamespace as NS
    from unittest.mock import Mock

    from src.runners.llm_runner import QuickScanWorkLifecycle
    from src.utils.quick_scan_work_store import QuickScanWorkStore

    item, bundle, record = materials()
    spec = record["inputs"]["specifications"][0]
    binding = spec["owner_refresh_binding"]
    binding.update(decision_id="route_" + "a" * 64, anchor_version=2)
    item["decision"] = "resume_existing_work"
    store = QuickScanWorkStore(tmp_path / "original.sqlite")
    row = store.create_or_attach(**spec, run_id="original-run", scan_id="original-scan")
    qid = item["question_id"]
    record["receipt"]["items"][0]["work_item_id"] = row["work_item_id"]
    confirmation = "rc2_" + "e" * 64
    record["inputs"]["action"] = {"confirmation_id": confirmation}
    proof = {
        "confirmation": {"confirmation_id": confirmation},
        "request": {
            "protocol": "iqs.refresh_request/1.1.0",
            "source_query": {"payload": {"information_cutoff": "2026-10-10"}},
        },
        "preview": {
            "items": [copy.deepcopy(item)],
            "provider": binding["provider"],
            "model": binding["model"],
        },
        "owner_policy": {"provider": binding["provider"], "model": binding["model"]},
    }
    native = NS(
        plan={**copy.deepcopy(binding), "anchor_version": 4},
        current={"anchor_version": 4, "expected_decision_id": binding["decision_id"]},
        check=Mock(),
    )
    session = ApprovedRefreshSession.__new__(ApprovedRefreshSession)
    session.proof = proof
    session.record = copy.deepcopy(record)
    session.bundle = bundle
    session.bundles = {}
    session.input_raw = {}
    session.input_paths = {}
    session._policy_check = None
    session.native = native
    session.plan = copy.deepcopy(native.plan)
    session.deferred = {}
    session.fields = {qid: {"decision": "resume_existing_work"}}
    session.entries = registered_scope([item], bundle, record, information_cutoff="2026-10-10")
    session.service = NS(
        store=store,
        _inputs_unchanged=Mock(),
        _record=Mock(return_value=copy.deepcopy(record)),
        bridge=NS(inspect=Mock(return_value=copy.deepcopy(proof))),
    )
    lifecycle = QuickScanWorkLifecycle(
        store,
        entity_id="ENT_SYNTHETIC",
        run_id="fresh-run",
        scan_id="fresh-scan",
        identity=bundle["identity"],
        transport_managed=True,
        owner_refresh_session=session,
    )
    return lifecycle, session, store, row, bundle["questions"][qid]


def test_reactivated_epoch_resumes_exact_original_binding_without_rewrite(tmp_path):
    lifecycle, session, store, row, question = reactivated_session(tmp_path)
    before = store.get_owner_refresh_binding(row["work_item_id"])
    old, binding, reason = lifecycle._owner_work(question.question_id)
    assert reason is None and old["work_item_id"] == row["work_item_id"]
    assert binding == before["binding"] and binding["anchor_version"] == 2
    assert session.plan["anchor_version"] == session.native.current["anchor_version"] == 4
    assert store.get_owner_refresh_binding(row["work_item_id"]) == before
    session.service.bridge.inspect.assert_called_once_with(
        session.proof["confirmation"]["confirmation_id"], mode="current_permissions"
    )
    assert store.list_attempts(row["work_item_id"]) == []
    handle = lifecycle.before_question(question)
    assert handle["claimed"] is True
    assert store.get_owner_refresh_binding(row["work_item_id"]) == before
    assert store.list_attempts(row["work_item_id"]) == []


@pytest.mark.parametrize(
    "fault",
    [
        "missing_action",
        "entry_binding",
        "foreign_item",
        "scope",
        "generation",
        "manifest",
        "decision",
        "perimeter",
        "provider",
        "model",
        "cutoff",
        "native_epoch",
        "backwards_epoch",
        "boolean_epoch",
        "foreign_store",
        "no_permission",
    ],
)
def test_epoch_resume_requires_current_exact_registered_action_and_native_context(tmp_path, fault):
    from unittest.mock import Mock

    from src.utils.quick_scan_work_store import QuickScanWorkStore

    lifecycle, session, store, row, question = reactivated_session(tmp_path)
    before = store.get_owner_refresh_binding(row["work_item_id"])
    qid = question.question_id
    if fault == "missing_action":
        session.service._record.return_value = None
    elif fault == "entry_binding":
        session.entries[qid]["specification"]["owner_refresh_binding"][
            "request_identity_key"
        ] = "changed"
    elif fault == "foreign_item":
        session.entries[qid]["work_item_id"] = "WORK_foreign"
    elif fault == "scope":
        session.entries[qid]["specification"]["scope_id"] = "ENT_foreign"
    elif fault == "generation":
        session.entries[qid]["specification"]["generation"] = 2
    elif fault in {"manifest", "decision", "perimeter", "provider", "model", "cutoff"}:
        key = {
            "manifest": "manifest_raw_sha256",
            "decision": "decision_id",
            "perimeter": "perimeter_sha256",
            "cutoff": "information_cutoff",
        }.get(fault, fault)
        session.plan[key] = "changed"
    elif fault == "native_epoch":
        session.native.current["anchor_version"] = 5
    elif fault == "backwards_epoch":
        session.plan["anchor_version"] = session.native.current["anchor_version"] = 1
    elif fault == "boolean_epoch":
        session.plan["anchor_version"] = session.native.current["anchor_version"] = True
    elif fault == "foreign_store":
        lifecycle._store = QuickScanWorkStore(tmp_path / "foreign.sqlite")
    else:
        session.service.bridge.inspect = Mock(side_effect=ValueError("synthetic revoked"))
    result = lifecycle.before_question(question)
    assert result["claimed"] is False
    assert store.get_owner_refresh_binding(row["work_item_id"]) == before
    assert store.get_item(row["work_item_id"])["status"] == "pending"
    assert store.list_attempts(row["work_item_id"]) == []


def test_generic_owner_session_never_gets_epoch_compatibility(tmp_path):
    from types import SimpleNamespace as NS

    lifecycle, session, store, row, question = reactivated_session(tmp_path)
    lifecycle._owner_refresh = NS(
        check=session.check,
        fields=session.fields,
        plan=session.plan,
        existing_work=session.existing_work,
    )
    assert lifecycle.before_question(question)["claimed"] is False
    assert store.get_item(row["work_item_id"])["status"] == "pending"


def test_epoch_reapproval_does_not_replay_uncertain_work_projection(tmp_path, monkeypatch):
    """Negative read-port state only; never forge a native send or edit a row.

    Original real expired-send -> uncertain fencing is independently covered
    by test_original_lease_recovery_cannot_turn_unknown_send_into_new_work.
    """
    lifecycle, session, store, row, question = reactivated_session(tmp_path)
    original_get = store.get_item
    before = store.path.read_bytes()
    monkeypatch.setattr(
        store, "get_item", lambda work_id: dict(original_get(work_id), status="uncertain")
    )
    monkeypatch.setattr(
        store, "claim", lambda *args, **kwargs: pytest.fail("Unknown work must not be claimed")
    )
    result = lifecycle.before_question(question)
    assert result["claimed"] is False and "delivery_or_reconciliation" in result["reason"]
    assert original_get(row["work_item_id"])["status"] == "pending"
    assert store.list_attempts(row["work_item_id"]) == [] and store.path.read_bytes() == before


def original_ready_session(tmp_path, monkeypatch):
    """Original SQLite/client/checkpoints; current owner port remains synthetic."""
    import json
    from types import SimpleNamespace as NS
    from unittest.mock import Mock

    from src.runners.llm_runner import QuickScanWorkLifecycle

    # The pack fixture has a deliberately generic subject key. Bind its real
    # synthetic subject BEFORE native work creation, never graft an ORF later.
    from src.utils.quick_scan_owner_refresh import OwnerRefreshSession
    from src.utils.quick_scan_question_manifest import load_question_manifest
    from src.utils.quick_scan_work_store import QuickScanWorkStore
    from tests.unit.test_qa_question_pack import (
        test_actual_lifecycle_client_group_store_and_checkpoints_only_valid_members as seed,
    )
    from tests.unit.test_quick_scan_question_pack_transport import FIX, _v2_context

    context = _v2_context()
    original_binding = OwnerRefreshSession.binding

    def subject_binding(owner, qid):
        result = original_binding(owner, qid)
        ref = context["questions"][qid]["subject_binding"]["subject_ref"]
        result["subject_key"] = (
            ref["analysis_subject_id"] + "@" + str(ref["analysis_subject_revision"])
        )
        return result

    from src.utils import quick_scan_owner_refresh as original_owner

    original_fingerprints = original_owner.routing_fingerprints

    def fingerprints(owner):
        ref = context["questions"]["IQS_01"]["subject_binding"]["subject_ref"]
        owner.plan["subject_key"] = (
            ref["analysis_subject_id"] + "@" + str(ref["analysis_subject_revision"])
        )
        return original_fingerprints(owner)

    monkeypatch.setattr(OwnerRefreshSession, "binding", subject_binding)
    monkeypatch.setattr(original_owner, "routing_fingerprints", fingerprints)
    seed(tmp_path, monkeypatch, False)
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    manifest = load_question_manifest(FIX / "quick_scan_c06_manifest_v2_fixture.json")
    qid = "IQS_01"
    rows = store.find_work_items(entity_id="ENT_CONTEXT_FIXTURE", question_ids=[qid])
    work = store.get_item(rows[qid][0]["work_item_id"])
    binding = store.get_owner_refresh_binding(work["work_item_id"])["binding"]
    identity = {
        k: work[k]
        for k in (
            "entity_id",
            "identity_revision",
            "source_binding_version",
            "identity_state",
            "source_binding_ref",
            "identity_snapshot_sha256",
        )
    }
    identity["source_binding_refs"] = json.loads(work["source_binding_refs_json"])
    question = Question(
        next(q["prompt"] for q in manifest["questions"] if q["id"] == qid), question_id=qid
    )
    spec = {
        **identity,
        "question_id": qid,
        "generation": work["generation"],
        "scope": work["scope"],
        "scope_id": work["scope_id"],
        "question_fingerprint": work["question_fingerprint"],
        "routing_fingerprint": work["routing_fingerprint"],
        "owner_refresh_binding": binding,
    }
    fresh = copy.deepcopy(context)
    for row in fresh["questions"].values():
        row["metadata"].update(run_id="SYNTHETIC_NEW_RUN", scan_id="SYNTHETIC_NEW_SCAN")
    bundle = {
        "identity": identity,
        "manifest": manifest,
        "questions": {qid: question},
        "authority": {"schema_version": "3.0.0", "observation_context": fresh},
    }
    item = {
        "question_id": qid,
        "field_id": context["questions"][qid]["metadata"]["field_id"],
        "subject_ref": context["questions"][qid]["subject_binding"]["subject_ref"],
        "generation": work["generation"],
        "decision": "resume_existing_work",
        "reuse_observation_id": None,
        "existing_work_status": "result_ready",
    }
    confirmation = "rc2_" + "f" * 64
    record = {
        "inputs": {
            "action": {"confirmation_id": confirmation},
            "specifications": [spec],
            "references": [],
        },
        "receipt": {
            "items": [
                {
                    k: work[k]
                    for k in (
                        "work_item_id",
                        "entity_id",
                        "question_id",
                        "generation",
                        "scope",
                        "scope_id",
                    )
                }
            ]
        },
    }
    proof = {
        "confirmation": {"confirmation_id": confirmation},
        "request": {
            "protocol": "iqs.refresh_request/1.1.0",
            "source_query": {"payload": {"information_cutoff": binding["information_cutoff"]}},
        },
        "preview": {
            "items": [item],
            "provider": binding["provider"],
            "model": binding["model"],
            "scope_sha256": "a" * 64,
        },
        "owner_policy": {"provider": binding["provider"], "model": binding["model"]},
    }
    session = ApprovedRefreshSession.__new__(ApprovedRefreshSession)
    session.plan = {**binding, "anchor_version": binding["anchor_version"] + 2}
    session.proof = proof
    session.record = record
    session.bundle = bundle
    session.bundles = {}
    session.input_paths = {}
    session.input_raw = {}
    session._policy_check = None
    session.deferred = {}
    session.fields = {qid: {"decision": "resume_existing_work"}}
    session.entries = registered_scope(
        [item], bundle, record, information_cutoff=binding["information_cutoff"]
    )
    session.native = NS(
        plan=copy.deepcopy(session.plan),
        current={
            "anchor_version": session.plan["anchor_version"],
            "expected_decision_id": binding["decision_id"],
        },
        check=Mock(),
    )
    session.service = NS(
        store=store,
        _inputs_unchanged=Mock(),
        _record=Mock(return_value=copy.deepcopy(record)),
        bridge=NS(inspect=Mock(return_value=copy.deepcopy(proof))),
    )
    lifecycle = QuickScanWorkLifecycle(
        store,
        entity_id=identity["entity_id"],
        run_id="SYNTHETIC_NEW_RUN",
        scan_id="SYNTHETIC_NEW_SCAN",
        identity=identity,
        owner_refresh_session=session,
        transport_managed=True,
    )
    return lifecycle, session, store, work, question


def test_approved_ready_checkpoint_uses_original_context_receipt_and_never_new_claim(
    tmp_path, monkeypatch
):
    from unittest.mock import Mock

    from src.core.qa_engine import QAEngine

    lifecycle, session, store, work, question = original_ready_session(tmp_path, monkeypatch)
    wid = work["work_item_id"]
    before = store.path.read_bytes()
    saved = store.get_answer_checkpoint(wid)
    context = store.get_observation_context(wid)
    standard = store.get_standard_answer(wid)
    delivery = store.get_result_delivery(wid)
    attempts = store.list_attempts(wid)
    restored = lifecycle.hydrate_question(question)
    assert restored["payload"] == saved["payload"] and restored["attempt_id"] == saved["attempt_id"]
    assert (
        restored["original_observation_context"] == context
        and restored["original_standard_answer"] == standard
    )
    assert restored["original_attempt_response"] == store.get_attempt_response(saved["attempt_id"])
    assert restored["new_C06_package_created"] is False
    assert context["context"] != session.bundle["authority"]["observation_context"]
    provider = Mock()
    provider.get_provider_name.return_value = "no-new-provider"
    provider.search_question.side_effect = AssertionError("Cached answer must not call provider")
    monkeypatch.setattr(
        lifecycle, "before_question", lambda *args: pytest.fail("Cached answer must not claim")
    )
    monkeypatch.setattr(
        lifecycle,
        "after_question",
        lambda *args: pytest.fail("Cached answer must not be re-sealed"),
    )
    batch = QAEngine(
        provider, progress_reporter=Mock(), work_item_lifecycle=lifecycle
    ).process_questions([question])
    assert batch.results[0].answer.source == "answer_checkpoint"
    assert batch.results[0].answer.score == saved["payload"]["answer"]["score"]
    assert batch.results[0].metadata["answer_checkpoint"]["attempt_id"] == saved["attempt_id"]
    assert (
        store.get_observation_context(wid) == context and store.get_standard_answer(wid) == standard
    )
    assert store.get_result_delivery(wid) == delivery and store.list_attempts(wid) == attempts
    assert store.path.read_bytes() == before
    provider.search_question.assert_not_called()


@pytest.mark.parametrize(
    "fault",
    [
        "context_hash",
        "answer_hash",
        "response_missing",
        "wrong_model",
        "wrong_question",
        "pending",
        "leased",
        "uncertain",
    ],
)
def test_ready_recovery_refuses_changed_original_material_or_unreceived_work(
    tmp_path, monkeypatch, fault
):
    lifecycle, session, store, work, question = original_ready_session(tmp_path, monkeypatch)
    before = store.path.read_bytes()
    wid = work["work_item_id"]
    if fault in {"pending", "leased", "uncertain"}:
        original = store.get_item
        monkeypatch.setattr(
            store, "get_item", lambda work_id: dict(original(work_id), status=fault)
        )
        monkeypatch.setattr(
            store,
            "get_answer_checkpoint",
            lambda *args: pytest.fail("Unreceived work cannot hydrate"),
        )
        assert lifecycle.hydrate_question(question) is None
    else:
        if fault == "context_hash":
            original = store.get_observation_context
            monkeypatch.setattr(
                store,
                "get_observation_context",
                lambda *args: {**original(*args), "context_sha256": "f" * 64},
            )
        elif fault == "answer_hash":
            original = store.get_standard_answer
            monkeypatch.setattr(
                store,
                "get_standard_answer",
                lambda *args: {**original(*args), "answer_sha256": "f" * 64},
            )
        elif fault == "response_missing":
            monkeypatch.setattr(store, "get_attempt_response", lambda *args: None)
        elif fault == "wrong_model":
            original = store.get_answer_checkpoint

            def changed(*args):
                saved = original(*args)
                saved["payload"]["provenance"]["model_requested"] = "different-model"
                return saved

            monkeypatch.setattr(store, "get_answer_checkpoint", changed)
        else:
            question = Question("Wrong question meaning", question_id=question.question_id)
        assert lifecycle.hydrate_question(question) is None
    assert store.path.read_bytes() == before
