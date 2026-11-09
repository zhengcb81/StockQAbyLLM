"""Preserve original paid results without new dispatch authority or current controls."""

import pytest

from src.utils.quick_scan_refresh_admission import (
    AdmissionRejected,
    RefreshAdmissionService,
)


@pytest.mark.parametrize("changed_field", ["budget_raw", "config_raw"])
def test_received_recovery_uses_registered_action_when_current_control_bytes_change(changed_field):
    """Guard-only ports: no synthetic proof is claimed as native authorization."""
    import copy
    from types import SimpleNamespace

    service = object.__new__(RefreshAdmissionService)
    service.config_raw = b'{"workspace":"original"}'
    service.budget_raw = b'{"max_cost":10}'
    proof = {
        "confirmation": {"confirmation_id": "rc1_" + "a" * 64},
        "preview": {"preview_id": "original-preview", "scope_sha256": "s", "policy_sha256": "p"},
        "owner_policy": {},
        "owner_state": {"ttl_hours": 24},
    }
    record = {"inputs": {"action": service._action_binding(proof, {})}}
    service._ledger_ready = lambda: None
    service._record = lambda identifier: record
    service.bridge = SimpleNamespace(inspect=lambda *args, **kwargs: copy.deepcopy(proof))
    service._proof_inputs = lambda original: {}
    service._assert_received_response_scope = lambda original: ["W1"]
    service.read_registered(proof["confirmation"]["confirmation_id"], recovery_only=True)
    setattr(service, changed_field, b'{"current":"changed after original response"}')
    service.read_registered(proof["confirmation"]["confirmation_id"], recovery_only=True)
    # A new dispatch still needs its own unchanged deployment/budget binding.
    with pytest.raises(AdmissionRejected, match="idempotency_conflict"):
        service.read_registered(proof["confirmation"]["confirmation_id"])


def test_received_scope_preserves_paid_member_while_other_registered_work_is_unasked():
    from types import SimpleNamespace

    service = object.__new__(RefreshAdmissionService)

    def item(index, status):
        return dict(
            work_item_id="W" + str(index),
            entity_id="ENT",
            question_id="Q" + str(index),
            generation=1,
            scope="entity",
            scope_id="ENT",
            status=status,
        )

    work = {row["work_item_id"]: row for row in (item(1, "leased"), item(2, "pending"))}
    service.store = SimpleNamespace(
        get_item=lambda wid: work[wid],
        list_attempts=lambda wid: (
            [{"attempt_id": "A1", "phase": "response_available"}] if wid == "W1" else []
        ),
        get_attempt_response=lambda aid: {"original": "safe"},
    )
    record = {
        "receipt": {
            "items": [
                {key: value for key, value in row.items() if key != "status"}
                for row in work.values()
            ]
        }
    }
    service._assert_received_response_scope(record)
    work["W1"]["status"] = "pending"
    service.store.list_attempts = lambda wid: []
    with pytest.raises(AdmissionRejected, match="original_response_required"):
        service._assert_received_response_scope(record)


from tests.unit.test_shared_company_routes import coordinator_ports


def test_paid_partial_publication_keeps_original_task_scope_and_reports_remaining(
    coordinator_ports, monkeypatch
):
    from types import SimpleNamespace

    p = coordinator_ports
    p.state["rows"] = [
        dict(
            question_id="Q1",
            checkpoint_id=None,
            latest_attempt=dict(phase="response_available", route_id="a"),
        ),
        dict(question_id="Q2", checkpoint_id=None, latest_attempt=None),
    ]
    monkeypatch.setattr(p.entry, "original_task_checkpoint", lambda root: root / "checkpoint.json")
    monkeypatch.setattr(
        p.entry,
        "load_original_company_task",
        lambda path: SimpleNamespace(
            limits={"max_model_requests_per_session": 5},
            questions=[dict(question_id=q, question={"prompt": q}) for q in ("Q1", "Q2")],
        ),
    )

    def scan(life, client, **options):
        assert options["recovery_only"] is True
        assert options["ordered_scope"] == ["Q1", "Q2"]
        assert [q.question_id for q in options["questions"]] == ["Q1", "Q2"]
        p.state["rows"][0]["checkpoint_id"] = "CP-original"
        raise p.entry.SharedCompanyIncomplete()

    monkeypatch.setattr(p.entry, "run_shared_company_scan", scan)
    result = p.routes.run_ordered_company_scan(
        p.life, p.clients, config=p.config, questions=p.questions, recovery_only=True
    )
    assert result["status"] == "partial"
    assert result["saved_question_ids"] == ["Q1"]
    assert result["remaining_question_ids"] == ["Q2"]
    assert result["API_requests"] == 0 and result["dispatch_started"] is False


@pytest.mark.parametrize("missing_scratch", [False, True])
def test_paid_service_construction_does_not_open_missing_current_budget(
    tmp_path, monkeypatch, missing_scratch
):
    import json
    from types import SimpleNamespace

    from src.utils import quick_scan_refresh_admission as admission

    config = {key: str(tmp_path / key) for key in admission.PATH_KEYS}
    config.update(
        protocol="stockqa.refresh_admission_config/1.0.0", consumer_id="invest-quick-scan"
    )
    for key in admission.PATH_KEYS - {"work_store", "budget_policy_file"}:
        if key != "runs_dir" or not missing_scratch:
            (tmp_path / key).mkdir()
    (tmp_path / "work_store").write_bytes(b"synthetic file; ledger boundary is mocked")
    config_file = tmp_path / "admission.json"
    config_file.write_text(json.dumps(config), encoding="utf8")
    monkeypatch.setattr(admission.RefreshAdmissionService, "_ledger_ready", lambda self: None)
    monkeypatch.setattr(admission, "OwnerConsentEndpoint", lambda *args, **kw: SimpleNamespace())
    monkeypatch.setattr(admission, "RefreshConsentClient", lambda *args, **kw: SimpleNamespace())
    service = admission.RefreshAdmissionService(config_file, recovery_only=True)
    assert service.budget is None and service.budget_raw is None
    assert not (tmp_path / "budget_policy_file").exists()
    if missing_scratch:
        assert not (tmp_path / "runs_dir").exists()
    with pytest.raises(AdmissionRejected, match="input_unavailable"):
        admission.RefreshAdmissionService(config_file)


def test_old_exposure_control_is_not_reauthorized_when_preserving_original_task(monkeypatch):
    from types import SimpleNamespace

    from src.utils import quick_scan_owner_budget_continuation as exposure
    from src.utils.quick_scan_owner_research import OwnerResearchServices
    from src.utils.quick_scan_result_outbox import canonical_sha256

    contract = dict(id="Q1", module_id="original-module", prompt="Original question")
    subject = dict(entity_id="ENT-original")
    context = {
        "schema": "stockqa.quick_scan_observation_context/2.0.0",
        "questions": {
            "Q1": {"subject_binding": {"question_contract": contract, "subject_ref": subject}}
        },
    }
    owner = SimpleNamespace(
        recovery_only=True,
        entries={"Q1": {"kind": "work"}},
        native=SimpleNamespace(observation_context=context),
        bundle={
            "manifest": {"module_locks": [{"module_id": "original-module", "version": "1.0.0"}]}
        },
        plan={"information_cutoff": "2026-11-01"},
    )
    service = object.__new__(OwnerResearchServices)
    service.owner = owner
    service.lifecycle = SimpleNamespace(
        registered_research_work=lambda: SimpleNamespace(observation_context=context),
        question_pack_group_key=lambda q: ("original",),
    )
    service.search_service = None
    service.task = SimpleNamespace(
        company_doc={"origin": "owner_public", "subject": {"subject_ref": subject}},
        questions=[
            {
                "question_id": "Q1",
                "question": contract,
                "question_object_sha256": canonical_sha256(contract),
                "module_id": "original-module",
                "module_version": "1.0.0",
            }
        ],
        raw={"information_cutoff": "2026-11-01"},
    )

    def expired(*args, **kwargs):
        raise ValueError("original experiment control expired today")

    monkeypatch.setattr(exposure, "assert_research_task", expired)
    service._validate_task()
    owner.recovery_only = False
    with pytest.raises(ValueError, match="expired today"):
        service._validate_task()


from tests.unit.test_shared_company_entry import configured


def test_paid_shared_config_ignores_today_boards_limits_and_unrelated_fields(configured):
    import json

    from src.utils.quick_scan_shared_company import read_shared_company_config

    config, path = configured
    config.update(
        limits="changed irrelevant control",
        pack_by={"no": "longer used"},
        evidence_boards="today board unavailable",
        future_unrelated_setting=True,
    )
    path.write_text(json.dumps(config), encoding="utf8")
    loaded = read_shared_company_config(path, recovery_only=True)
    assert set(loaded) == {
        "schema_version",
        "iqs_root",
        "company_agent_root",
        "runtime_bridge_root",
        "state_root",
    }
    with pytest.raises(ValueError):
        read_shared_company_config(path)


def test_paid_native_view_uses_original_binding_and_never_asks_current_anchor():
    import copy
    from types import SimpleNamespace

    from src.utils.quick_scan_owner_refresh import (
        OwnerRefreshClient,
        OwnerRefreshRejected,
    )

    client = object.__new__(OwnerRefreshClient)
    client.config = dict(
        subject_key="ASJ-original@1", scope="entity", scope_id="ENT-original", ttl_hours=24
    )
    client._call = lambda *args, **kwargs: pytest.fail(
        "received history must not query current anchor"
    )
    binding = dict(
        subject_key="ASJ-original@1",
        decision_id="ROUTE-original",
        anchor_version=1,
        manifest_raw_sha256="a" * 64,
        perimeter_sha256="p" * 64,
        provider="mimo",
        model="original-model",
        entity_id="ENT-original",
        identity_revision=1,
        question_id="Q1",
        scope="entity",
        scope_id="ENT-original",
        target_generation=1,
        request_identity_key="original-key",
        information_cutoff="2026-11-01",
    )
    spec = dict(
        entity_id="ENT-original",
        question_id="Q1",
        scope="entity",
        scope_id="ENT-original",
        generation=1,
        owner_refresh_binding=binding,
    )
    entry = dict(kind="work", work_item_id="W1", specification=spec)
    store = SimpleNamespace(
        get_item=lambda wid: {
            "work_item_id": wid,
            **{k: v for k, v in spec.items() if k != "owner_refresh_binding"},
        },
        get_owner_refresh_binding=lambda wid: {"binding": copy.deepcopy(binding)},
    )
    manifest = dict(
        manifest_sha256="a" * 64,
        questions=[{"id": "Q1"}],
        module_package_id="original-package",
        module_release_id="original-release",
        module_locks=[],
    )
    identity = dict(entity_id="ENT-original", identity_revision=1)
    session = client.prepare(
        manifest,
        store,
        identity=identity,
        provider="mimo",
        model="original-model",
        now="2026-11-02T00:00:00Z",
        recovery_entries={"Q1": entry},
    )
    assert session.recovery_only is True and session.current is None
    assert (
        session.plan["decision_id"] == "ROUTE-original"
        and session.plan["new_execution_authorized"] is False
    )
    assert session.fields["Q1"]["decision"] == "resume_existing_work"
    session.check()
    manifest["manifest_sha256"] = "b" * 64
    with pytest.raises(OwnerRefreshRejected, match="original"):
        client.prepare(
            manifest,
            store,
            identity=identity,
            provider="mimo",
            model="original-model",
            now="2026-11-02T00:00:00Z",
            recovery_entries={"Q1": entry},
        )


@pytest.mark.parametrize("original_refresh_id", [None, "refresh_" + "a" * 64])
def test_real_paid_partial_output_preserves_history_without_inventing_refresh_id(
    original_refresh_id,
):
    from src.core.models import Answer, QABatchResult, QAResult, Question
    from src.utils.quick_scan_owner_refresh import (
        OwnerRefreshSession,
        build_refresh_result,
    )

    entity = "ENT_11111111-1111-4111-8111-111111111111"
    fields = [
        dict(
            field_id=q,
            decision="resume_existing_work",
            generation=1,
            request_identity_key="original-" + q,
            reuse_observation_id=None,
        )
        for q in ("Q1", "Q2")
    ]
    plan = dict(
        subject_key="ASJ-original@1",
        decision_id="route_" + "d" * 64,
        anchor_version=1,
        manifest_raw_sha256="b" * 64,
        fields=fields,
        reused_observations={},
    )
    if original_refresh_id is not None:
        plan["refresh_id"] = original_refresh_id
    session = OwnerRefreshSession(
        None, None, plan, {}, {}, {"entity_id": entity}, None, recovery_only=True
    )
    batch = QABatchResult(total_questions=2)
    batch.add_result(
        QAResult(
            Question("Original question", question_id="Q1"),
            Answer(
                "Original saved answer; serialization fixture only", status="unknown", score=None
            ),
        )
    )
    result = build_refresh_result(
        batch,
        session,
        dict(
            entity_id=entity,
            company_name="Synthetic serialization only",
            provider_name="mimo",
            requested_model="original-model",
        ),
    )
    assert result["refresh_id"] == original_refresh_id
    assert [row["field_id"] for row in result["question_actions"]] == ["Q1", "Q2"]
    assert set(result["dispatched_result"]["answers"]) == {"Q1"}
    assert result["original_observations_resealed"] is False
