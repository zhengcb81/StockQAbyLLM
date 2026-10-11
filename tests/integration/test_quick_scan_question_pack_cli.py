"""Public CLI/runner option contracts in owned temp dirs, no API or real approval."""

import sys
from unittest.mock import Mock

import pytest

import main_with_llm
from src.runners.llm_runner import LLMRunner


@pytest.mark.parametrize(
    "extra,expected",
    [
        (["--question-pack-size", "3"], {"question_pack_size": 3}),
        (
            ["--thinking-enabled", "true", "--max-output-tokens", "9000"],
            {"thinking_enabled": True, "max_output_tokens": 9000},
        ),
        (["--thinking-enabled", "false"], {"thinking_enabled": False}),
        ([], {}),
    ],
)
def test_public_cli_forwards_only_explicit_options(tmp_path, monkeypatch, extra, expected):
    monkeypatch.chdir(tmp_path)
    runner = Mock()
    runner.run.return_value = 0
    monkeypatch.setattr("src.runners.llm_runner.LLMRunner", lambda **_: runner)
    monkeypatch.setattr(sys, "argv", ["main_with_llm.py", "--company", "Synthetic Co"] + extra)
    assert main_with_llm.main() == 0
    kwargs = runner.run.call_args.kwargs
    for key in ("question_pack_size", "thinking_enabled", "max_output_tokens"):
        assert (key in kwargs) == (key in expected)
        if key in expected:
            assert kwargs[key] == expected[key]


@pytest.mark.parametrize(
    "extra",
    [
        ["--question-pack-size", "0"],
        ["--question-pack-size", "31"],
        ["--thinking-enabled", "yes"],
        ["--max-output-tokens", "0"],
        ["--max-output-tokens", "32769"],
    ],
)
def test_public_cli_invalid_controls_cannot_construct_runner(tmp_path, monkeypatch, extra):
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr("src.runners.llm_runner.LLMRunner", lambda **_: calls.append("runner"))
    monkeypatch.setattr(sys, "argv", ["main_with_llm.py", "--company", "Synthetic Co"] + extra)
    with pytest.raises(SystemExit):
        main_with_llm.main()
    assert not calls


@pytest.mark.parametrize(
    "options",
    [
        {"question_pack_size": 3},
        {"question_pack_size": 3, "require_search": True, "entity_id": "ENT_TEST"},
        {"thinking_enabled": "true"},
        {"max_output_tokens": True},
        {"max_output_tokens": 0},
        {"thinking_enabled": True},
        {"question_pack_size": True},
    ],
)
def test_runner_preflight_controls_and_pack_dependencies_precede_file_reads(
    tmp_path, monkeypatch, options
):
    monkeypatch.chdir(tmp_path)
    runner = LLMRunner()
    run = Mock()
    monkeypatch.setattr(runner, "_run_single_company", run)
    with pytest.raises((ValueError, TypeError)):
        runner.run(company="Synthetic Co", **options)
    run.assert_not_called()
    assert not list(tmp_path.glob("*.sqlite"))


def test_runner_forwards_explicit_single_thinking_to_actual_company_entry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = LLMRunner()
    entry = Mock(return_value=0)
    monkeypatch.setattr(runner, "_run_single_company", entry)
    monkeypatch.setattr("src.runners.llm_runner._spend_authorization_preflight", lambda _: None)
    assert (
        runner.run(
            company="Synthetic Co",
            require_search=True,
            entity_id="ENT_TEST",
            thinking_enabled=True,
            max_output_tokens=9000,
        )
        == 0
    )
    assert entry.call_args.kwargs["thinking_enabled"] is True
    assert entry.call_args.kwargs["max_output_tokens"] == 9000


def test_route_provider_factory_preserves_actual_thinking_on_hot_reload(tmp_path, monkeypatch):
    constructor = Mock()
    provider = Mock()
    constructor.return_value = provider
    monkeypatch.setattr("src.runners.llm_runner.LLMProvider", constructor)
    policy = {
        "routes": [
            {"eligible": True, "provider_config_ref": "mimo-fixture", "model": "mimo-v2.6-pro"}
        ]
    }
    LLMRunner._build_route_providers(
        policy,
        company="Synthetic Co",
        entity_id="ENT_TEST",
        config_file=tmp_path / "absent.json",
        thinking_enabled=True,
        max_output_tokens=9000,
    )
    assert constructor.call_args.kwargs["thinking_enabled"] is True
    assert constructor.call_args.kwargs["max_output_tokens"] == 9000
