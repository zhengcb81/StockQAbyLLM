"""Read-only answer hydration must not initialize search or model services."""

from types import SimpleNamespace

import pytest

from src.core.models import Question
from src.core.qa_engine import QAEngine


def test_partial_hydration_keeps_original_denominator_and_never_needs_search():
    q = [Question("first", question_id="Q1"), Question("second", question_id="Q2")]
    cp = dict(
        payload=dict(
            answer=dict(status="scored", score=7, description="original"),
            provenance=dict(
                actual_model="original-model", response_completed_at="2026-10-01T00:00:00Z"
            ),
        ),
        work_item_id="W1",
        attempt_id="A1",
    )
    life = SimpleNamespace(
        reference_question=lambda q: None,
        hydrate_question=lambda question: cp if question.question_id == "Q1" else None,
    )
    batch = QAEngine.hydrate_saved_questions(q, life, allow_partial=True)
    assert batch.total_questions == 2 and batch.processed_count == 1 and not batch.is_complete()
    assert batch.results[0].answer.score == 7
    assert batch.results[0].answer.created_at.isoformat().startswith("2026-10-01")
    with pytest.raises(ValueError, match="checkpoint_missing"):
        QAEngine.hydrate_saved_questions(q, life)


def test_output_can_be_called_without_an_engine_or_provider(tmp_path):
    from src.core.models import QABatchResult

    batch = QABatchResult(total_questions=1)
    out = tmp_path / "original.json"
    QAEngine.output_results(batch, str(out))
    assert out.is_file()


def test_result_proof_reads_a_narrow_evidence_port_without_a_sql_connection():
    from src.utils.quick_scan_result_proof import result_proof_port

    expected = {"request_id": "req-fixture"}
    proof = {"work_item_id": "WORK"}

    class EvidenceReader:
        def get_item(self, work_item_id):
            raise AssertionError("checkpoint projection already has the bound item")

        def list_attempts(self, work_item_id):
            raise AssertionError("no history required for checkpoint projection")

        def get_attempt_response(self, attempt_id):
            raise AssertionError("the owner reviews the response in its own read")

        def read_external_checkpoint_search(self, attempt_id, receipt, supplied):
            assert attempt_id == "ATTEMPT"
            assert receipt == expected
            assert supplied is proof
            return {
                "proof": proof,
                "source_urls": ["https://example.com"],
                "search_receipt_id": "USE",
            }

    metadata = {
        "external_context_use": proof,
        "work_transport": {"work_attempt_id": "ATTEMPT", "final_receipt": expected},
    }
    projection = result_proof_port(EvidenceReader()).project_checkpoint_search(
        metadata, work_item_id="WORK"
    )
    assert projection["external_context_use"] is proof
    assert projection["search_status"] == "executed"


def test_paid_runner_uses_original_receipt_metadata_without_today_model_or_budget(
    tmp_path, monkeypatch
):
    import json

    import requests

    import src.runners.llm_runner as runner
    from src.interfaces.quick_scan_owner_refresh import OwnerRefreshSnapshot
    from src.interfaces.quick_scan_result_proof import QuickScanResultProofPort
    from src.providers.llm_client import LLMClient
    from src.utils import quick_scan_owner_refresh_api as owner_api
    from src.utils import quick_scan_shared_routes as shared
    from src.utils import quick_scan_work_store as ledger
    from src.utils.quick_scan_result_proof import result_proof_port_for_store
    from tests.unit import test_quick_scan_work_store as ledger_fixture
    from tests.unit.test_q07_checkpoint import UUID_ENTITY

    questions = [Question("first", question_id="Q1"), Question("second", question_id="Q2")]
    route_id, account, model = "original-account-route", "original-account", "original-model"
    store = ledger.QuickScanWorkStore(tmp_path / "original.sqlite")
    works = {
        question.question_id: ledger_fixture._created(
            store,
            entity_id=UUID_ENTITY,
            scope_id=UUID_ENTITY,
            question_id=question.question_id,
            run_id="ORIGINAL",
            scan_id="ORIGINAL",
        )["work_item_id"]
        for question in questions
    }
    paid_id = works["Q1"]
    lease = store.claim(paid_id, lease_seconds=60)
    attempt_id = ledger_fixture._prepared(
        store, paid_id, lease, provider=account, model=model, route_id=route_id
    )["attempt_id"]
    store.mark_send_intent(paid_id, lease, attempt_id)
    receipt = ledger_fixture._synthetic_search_receipt(provider="mimo", model=model)
    store.record_attempt_outcome(
        paid_id,
        lease,
        attempt_id,
        outcome="response_available",
        http_status_code=200,
        request_id=receipt["request_id"],
        receipt_sha256=ledger.quick_scan_receipt_sha256(receipt),
        execution_receipt=receipt,
        actual_cost=0.25,
        cost_source_ref="fixture-original-invoice",
    )
    store.save_answer_checkpoint(
        paid_id,
        lease,
        attempt_id,
        answer={
            "entity_id": UUID_ENTITY,
            "question_id": "Q1",
            "status": "scored",
            "score": 7,
            "description": "original",
        },
        execution_receipt=receipt,
    )
    store = ledger.QuickScanWorkStore.for_existing_ledger(store.path)
    ledger_before = store.path.read_bytes()

    # The runner only gets the public capabilities of the real opened ledger.
    readback = SimpleNamespace(
        list_attempts=store.list_attempts,
        get_attempt_response=store.get_attempt_response,
        get_attempt_transmission=store.get_attempt_transmission,
        get_subject_dispatch_receipt=store.get_subject_dispatch_receipt,
    )
    snapshot = OwnerRefreshSnapshot(
        kind="approved",
        entries={qid: {"kind": "work", "work_item_id": work} for qid, work in works.items()},
        record={"inputs": {"run_id": "ORIGINAL", "scan_id": "ORIGINAL"}},
        plan={"model": model},
        proof={
            "request": {"protocol": "iqs.refresh_request/1.1.0"},
            "owner_policy": {
                "provider": "mimo",
                "model": model,
                "qa": {"provider_config_ref": account, "route_id": route_id},
            },
        },
        bundle={"authority": {"schema_version": "3.0.0"}},
        observation_context={},
        scope_bindings={},
        fields={},
        work={},
        deferred={},
        ordered_route_targets=None,
        needs_new_dispatch=False,
        recovery_only=True,
        requested_question_ids=("Q1", "Q2"),
        recovery_work_item_ids=(paid_id,),
        remaining_work_item_ids=(works["Q2"],),
    )
    owner = SimpleNamespace(
        snapshot=lambda: snapshot,
        existing_work=lambda qid: store.get_item(works[qid]),
        dispatch_scope_report=lambda batch: {"original_question_count": batch.total_questions},
    )
    monkeypatch.setattr(owner_api, "is_owner_refresh_port", lambda candidate: candidate is owner)
    lifecycle_view = SimpleNamespace(
        hydrate_question=lambda question: store.get_answer_checkpoint(works[question.question_id])
    )

    def no_fresh(*args, **kwargs):
        pytest.fail("paid readback initialized configuration, budget, or a new request")

    for module, name in (
        (runner, "LLMConfig"),
        (runner, "QuickScanCostResolver"),
        (runner, "QuickScanWorkLifecycle"),
        (LLMClient, "send_search_request"),
        (requests.Session, "request"),
    ):
        monkeypatch.setattr(module, name, no_fresh)
    replayed = []

    def replay(lifecycle, pairs, **kwargs):
        assert lifecycle is lifecycle_view and kwargs["recovery_only"] is True
        ((route, client),) = pairs
        assert route["id"] == route_id and route["provider_config_ref"] == account
        assert (client.api_key, client.base_url, client.model, client.provider_name) == (
            "",
            "",
            model,
            receipt["provider"],
        )
        replayed.append(route_id)

    def export(batch, candidate, context):
        assert candidate is owner
        assert isinstance(context["result_proof_port"], QuickScanResultProofPort)
        assert batch.total_questions == 2 and batch.processed_count == 1
        return batch.to_quick_scan_dict(**context)

    monkeypatch.setattr(shared, "run_ordered_company_scan", replay)
    monkeypatch.setattr(owner_api, "build_refresh_result", export)
    output = tmp_path / "result.json"
    status = runner.LLMRunner()._recover_shared_company_answers(
        owner,
        {},
        questions,
        ledger=readback,
        result_proof=result_proof_port_for_store(store),
        lifecycle_factory=lambda **kwargs: lifecycle_view,
        identity={},
        company="Synthetic",
        entity_id=UUID_ENTITY,
        output=str(output),
    )
    assert status == 0 and replayed == [route_id]
    result = json.loads(output.read_bytes())
    assert list(result["answers"]) == ["Q1"] and result["answers"]["Q1"]["score"] == 7
    original = result["execution_receipts"]["Q1"]
    for key in ("request_id", "response_id", "actual_model", "provider", "search_receipt_id"):
        assert original[key] == receipt[key]
    assert original["answered_at"] == receipt["completed_at"]
    scope = json.loads(output.with_suffix(".json.scope.json").read_bytes())
    assert scope["status"] == "partial" and scope["remaining_question_ids"] == ["Q2"]
    assert scope["original_question_count"] == 2 and scope["restored_question_count"] == 1
    assert scope["API_requests"] == 0 and scope["dispatch_started"] is False
    assert store.path.read_bytes() == ledger_before


@pytest.mark.parametrize("control", ["disabled", "missing"])
def test_existing_saved_answer_directory_does_not_require_capture_to_still_be_enabled(
    tmp_path, control
):
    import json

    from src.providers.llm_provider import _final_answer_diagnostic_policy

    directory = tmp_path / "final-answer-diagnostics"
    directory.mkdir()
    if control == "disabled":
        (tmp_path / "quick_scan_final_answer_diagnostics.json").write_text(
            json.dumps(dict(schema_version="1.0.0", enabled=False)), encoding="utf8"
        )
    assert _final_answer_diagnostic_policy(tmp_path / "llm_apis.json") is None
    saved = _final_answer_diagnostic_policy(tmp_path / "llm_apis.json", recovery_only=True)
    assert saved["directory"] == directory and saved["max_final_answer_bytes"] == 1024 * 1024
