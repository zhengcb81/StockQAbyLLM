"""Future-claim lease policy: public CLI and isolated real SDK/SQLite, no live calls."""

import sys
from unittest.mock import Mock

import pytest

import main_with_llm
from src.runners.llm_runner import LLMRunner


@pytest.mark.parametrize("value", ["1800", "3600"])
def test_public_cli_forwards_explicit_work_lease(tmp_path, monkeypatch, value):
    monkeypatch.chdir(tmp_path)
    runner = Mock()
    runner.run.return_value = 0
    monkeypatch.setattr("src.runners.llm_runner.LLMRunner", lambda **_: runner)
    monkeypatch.setattr(
        sys, "argv", ["main_with_llm.py", "--company", "Synthetic", "--work-lease-seconds", value]
    )
    assert main_with_llm.main() == 0
    assert runner.run.call_args.kwargs["work_lease_seconds"] == float(value)


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf", "86401", "true"])
def test_public_cli_rejects_bad_work_lease_before_runner(tmp_path, monkeypatch, value):
    monkeypatch.chdir(tmp_path)
    constructor = Mock()
    monkeypatch.setattr("src.runners.llm_runner.LLMRunner", constructor)
    monkeypatch.setattr(
        sys, "argv", ["main_with_llm.py", "--company", "Synthetic", "--work-lease-seconds", value]
    )
    with pytest.raises(SystemExit):
        main_with_llm.main()
    constructor.assert_not_called()


@pytest.mark.parametrize(
    "value", [True, False, None, 0, -1, float("nan"), float("inf"), 86401, "1800"]
)
def test_runner_rejects_bad_work_lease_before_entry_or_files(tmp_path, monkeypatch, value):
    monkeypatch.chdir(tmp_path)
    runner = LLMRunner()
    entry = Mock()
    monkeypatch.setattr(runner, "_run_single_company", entry)
    with pytest.raises((ValueError, TypeError)):
        runner.run(company="Synthetic", work_lease_seconds=value)
    entry.assert_not_called()
    assert not list(tmp_path.glob("*.sqlite"))


def test_default_work_lease_is_not_added_to_legacy_cli_kwargs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = Mock()
    runner.run.return_value = 0
    monkeypatch.setattr("src.runners.llm_runner.LLMRunner", lambda **_: runner)
    monkeypatch.setattr(sys, "argv", ["main_with_llm.py", "--company", "Synthetic"])
    assert main_with_llm.main() == 0 and "work_lease_seconds" not in runner.run.call_args.kwargs


def test_runner_forwards_valid_work_lease_without_changing_default_kwargs(tmp_path, monkeypatch):
    import src.runners.llm_runner as module

    monkeypatch.chdir(tmp_path)
    runner = LLMRunner()
    entry = Mock(return_value=0)
    monkeypatch.setattr(runner, "_run_single_company", entry)
    monkeypatch.setattr(module, "_spend_authorization_preflight", lambda _: None)
    monkeypatch.setattr(
        module, "load_identity_snapshot", lambda *_, **__: {"synthetic": "identity"}
    )
    assert (
        runner.run(
            company="Synthetic",
            require_search=True,
            entity_id="ENT_TEST",
            identity_snapshot="synthetic-unused",
            work_lease_seconds=1800,
        )
        == 0
    )
    assert entry.call_args.kwargs["work_lease_seconds"] == 1800


@pytest.mark.parametrize("lease,delay", [(600, 650), (1800, 650), (1800, 1900)])
def test_actual_sdk_delayed_response_obeys_new_claim_policy_without_renewal(
    tmp_path, monkeypatch, lease, delay
):
    import copy
    import time
    from datetime import datetime, timezone

    import tests.unit.test_known_pack_continuation as fixtures
    from src.core.qa_engine import QAEngine
    from src.providers import llm_client
    from src.providers.base_llm_provider import standard_answer_transport
    from src.utils import quick_scan_work_transport as transport

    original_lifecycle = fixtures.QuickScanWorkLifecycle

    class ConfiguredLifecycle(original_lifecycle):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, lease_seconds=lease, **kwargs)

    monkeypatch.setattr(fixtures, "QuickScanWorkLifecycle", ConfiguredLifecycle)
    store, provider, lifecycle, questions, session, policy = fixtures.fixture(
        tmp_path, monkeypatch, "answer_generator"
    )
    assert lifecycle._lease_seconds == lease
    clock = [
        float(int(store._now()) + 1)
    ]  # exact seconds avoid ISO microsecond rounding in test-only owner timestamps
    monkeypatch.setattr(store, "clock", lambda: clock[0])
    monkeypatch.setattr(
        lifecycle._owner_refresh,
        "register_subject_dispatch",
        lambda request: fixtures._owner_receipt(request, clock[0]),
    )
    monkeypatch.setattr(
        llm_client,
        "_utc_now",
        lambda: datetime.fromtimestamp(clock[0], timezone.utc).isoformat().replace("+00:00", "Z"),
    )
    observed_errors = []
    original_search = provider.search_question_pack

    def trace(*args, **kwargs):
        try:
            return original_search(*args, **kwargs)
        except Exception as error:
            observed_errors.append((type(error).__name__, str(error), repr(error.__cause__)))
            raise

    monkeypatch.setattr(provider, "search_question_pack", trace)
    original_post = session.post.side_effect

    def delayed_post(*args, **kwargs):
        clock[0] += delay
        return original_post(*args, **kwargs)

    session.post.side_effect = delayed_post

    def execute():
        with standard_answer_transport(True), transport.bind_quick_scan_budget(
            store,
            policy,
            cost_resolver=lambda _: {
                "actual_cost": 0.03,
                "pricing_ref": "fixture-price",
                "source_ref": "synthetic-rate",
            },
        ):
            return QAEngine(
                provider, progress_reporter=Mock(), work_item_lifecycle=lifecycle
            ).process_questions(questions[:2], question_pack_size=2)

    batch = execute()
    assert session.post.call_count == 1, (
        [(row.answer.text, row.metadata) for row in batch.results],
        observed_errors,
    )
    rows = store.find_work_items(
        entity_id="ENT_CONTEXT_FIXTURE", question_ids=[q.question_id for q in questions[:2]]
    )
    items = [store.get_item(rows[q.question_id][0]["work_item_id"]) for q in questions[:2]]
    checkpoints = [store.get_answer_checkpoint(item["work_item_id"]) for item in items]
    late = delay >= lease
    assert all(item["status"] == ("uncertain" if late else "result_ready") for item in items)
    assert all((cp is None) == late for cp in checkpoints)
    assert all((row.answer.score is None) == late for row in batch.results)
    budget = store.get_quick_scan_budget_status("pack-transport")
    assert budget["requests"] == 1 and budget["spent_micros"] == 30000 and budget["in_flight"] == 0
    before = copy.deepcopy(items)
    execute()
    assert session.post.call_count == 1
    assert [store.get_item(item["work_item_id"]) for item in items] == before
