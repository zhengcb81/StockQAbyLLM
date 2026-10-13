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


@pytest.mark.parametrize("status,complete", [("scored", True), ("unknown", True), ("error", False)])
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
