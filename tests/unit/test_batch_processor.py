"""Regression tests for nullable legacy result imports and summaries."""

from src.cli.batch_processor import log_success_result, merge_existing_answers
from src.core.models import QABatchResult


def test_importing_legacy_answer_without_score_does_not_invent_five():
    batch = QABatchResult(total_questions=1)
    merged = merge_existing_answers(
        batch,
        {"Question without score": {"description": "Older answer"}},
        qa_engine=None,
    )

    assert merged.results[0].answer.score is None
    assert merged.results[0].answer.status == "unknown"


def test_success_summary_handles_all_null_scores(caplog, tmp_path):
    batch = QABatchResult(total_questions=1)
    merge_existing_answers(
        batch,
        {"Question without score": {"description": "Older answer"}},
        qa_engine=None,
    )

    log_success_result("Fixture", tmp_path / "out.json", batch, [])

    assert "无有效评分" in caplog.text
