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


def test_paid_runner_uses_original_receipt_metadata_without_today_model_or_budget(
    tmp_path, monkeypatch
):
    import src.runners.llm_runner as runner
    from src.utils import quick_scan_shared_routes as shared

    q = [Question("first", question_id="Q1"), Question("second", question_id="Q2")]
    route = dict(
        id="original-account-route",
        provider_config_ref="original-account",
        model="original-model",
        quota_group=None,
    )
    store = SimpleNamespace(
        list_attempts=lambda wid: [
            dict(
                attempt_id="A1",
                phase="response_available",
                route_id=route["id"],
                provider=route["provider_config_ref"],
                model_requested=route["model"],
            )
        ],
        get_attempt_response=lambda aid: dict(receipt=dict(provider="mimo")),
    )
    owner = SimpleNamespace(
        record={"inputs": {"run_id": "ORIGINAL", "scan_id": "ORIGINAL"}},
        service=SimpleNamespace(store=store),
        plan=dict(provider="mimo", model="original-model"),
        proof={"owner_policy": {"qa": {"provider_config_ref": "original-account"}}},
        entries={
            "Q1": {"kind": "work", "work_item_id": "W1"},
            "Q2": {"kind": "work", "work_item_id": "W2"},
        },
        requested_question_ids=["Q1", "Q2"],
        scope_bindings={},
        bundle={"authority": {"schema_version": "3.0.0"}},
        native=SimpleNamespace(observation_context={}),
        execution_routes=lambda policy: [route],
        existing_work=lambda qid: dict(generation=1),
        dispatch_scope_report=lambda batch: dict(original_question_count=2),
    )
    cp = dict(
        payload=dict(
            answer=dict(status="scored", score=7, description="original"),
            provenance=dict(response_completed_at="2026-10-01T00:00:00Z"),
        ),
        work_item_id="W1",
        attempt_id="A1",
    )
    life = SimpleNamespace(
        reference_question=lambda q: None,
        hydrate_question=lambda question: cp if question.question_id == "Q1" else None,
    )
    calls = []

    def no_fresh(*a, **k):
        raise AssertionError("paid save must not initialize today's configuration")

    monkeypatch.setattr(runner, "LLMConfig", no_fresh)
    monkeypatch.setattr(runner, "QuickScanCostResolver", no_fresh)
    monkeypatch.setattr(runner, "QuickScanWorkLifecycle", lambda *a, **k: life)

    def replay(lifecycle, pairs, **kw):
        assert kw["recovery_only"] is True
        assert pairs[0][1].api_key == "" and pairs[0][1].base_url == ""
        assert pairs[0][1].model == "original-model" and pairs[0][1].provider_name == "mimo"
        return dict(
            status="partial",
            saved_question_ids=["Q1"],
            remaining_question_ids=["Q2"],
            API_requests=0,
        )

    monkeypatch.setattr(shared, "run_ordered_company_scan", replay)
    monkeypatch.setattr(QAEngine, "output_results", lambda batch, *a, **k: calls.append(batch))
    code = runner.LLMRunner()._recover_shared_company_answers(
        owner,
        {},
        q,
        identity={},
        company="Synthetic",
        entity_id="ENT_TEST",
        output=str(tmp_path / "result.json"),
    )
    assert code == 0 and calls[0].total_questions == 2 and calls[0].processed_count == 1
    import json

    scope = json.loads((tmp_path / "result.json.scope.json").read_text(encoding="utf8"))
    assert scope["status"] == "partial" and scope["remaining_question_ids"] == ["Q2"]


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
