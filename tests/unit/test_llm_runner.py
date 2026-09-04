#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试 LLM 运行器。"""

from pathlib import Path
from unittest.mock import MagicMock, Mock, call, patch

import pytest

from src.runners.llm_runner import (
    LLMRunner,
    load_stock_list,
    log_final_results,
    process_batch_stocks,
)


class TestLLMRunner:
    """测试 LLMRunner 类。"""

    @pytest.fixture
    def runner(self):
        return LLMRunner(verbose=False)

    @patch("src.runners.llm_runner.JSONConfigManager")
    @patch("src.runners.llm_runner.LLMProvider")
    @patch("src.runners.llm_runner.QAEngine")
    def test_run_single_company(self, mock_qa_engine, mock_llm_provider, mock_json_config, runner):
        """测试单公司运行模式。"""
        # 模拟配置加载
        mock_config_instance = mock_json_config.return_value
        mock_config_instance.load_questions.return_value = ["问题1"]

        # 模拟 QA 引擎结果
        mock_engine_instance = mock_qa_engine.return_value
        mock_batch_result = Mock()
        mock_batch_result.results = []
        mock_engine_instance.process_questions.return_value = mock_batch_result
        mock_engine_instance.get_statistics.return_value = {
            "total_questions": 0,
            "success_count": 0,
        }

        result = runner.run(company="海康威视", provider="deepseek")

        assert result == 0
        mock_llm_provider.assert_called_once()
        mock_engine_instance.process_questions.assert_called_once()

    @patch("src.runners.llm_runner.load_stock_list")
    @patch("src.runners.llm_runner.process_batch_stocks")
    def test_run_batch_mode(self, mock_process_batch, mock_load_stocks, runner):
        """测试批量运行模式。"""
        mock_load_stocks.return_value = ["公司1", "公司2"]
        mock_process_batch.return_value = {"total_stocks": 2, "success_count": 2, "failed_count": 0}

        result = runner.run(batch_file="stocks.txt", provider="deepseek")

        assert result == 0
        mock_load_stocks.assert_called_once_with("stocks.txt")
        mock_process_batch.assert_called_once()

    @patch("src.runners.llm_runner.JSONConfigManager")
    def test_run_single_company_exception(self, mock_json_config, runner):
        """测试单公司运行异常处理。"""
        mock_config_instance = mock_json_config.return_value
        mock_config_instance.load_questions.side_effect = OSError("File error")

        result = runner.run(company="公司", provider="deepseek")
        assert result == 1

    @patch("src.runners.llm_runner.load_stock_list")
    def test_run_batch_mode_exception(self, mock_load_stocks, runner):
        """测试批量模式异常处理。"""
        mock_load_stocks.side_effect = OSError("File error")

        result = runner.run(batch_file="stocks.txt", provider="deepseek")
        assert result == 1

    def test_run_validation_error(self, runner):
        """测试参数验证错误。"""
        # 同时提供 company 和 batch_file
        with pytest.raises(ValueError, match="不能同时使用"):
            runner.run(company="公司", batch_file="stocks.txt")

        # 都不提供
        with pytest.raises(ValueError, match="必须指定"):
            runner.run()

    @patch("src.runners.llm_runner.JSONConfigManager")
    @patch("src.runners.llm_runner.LLMProvider")
    @patch("src.runners.llm_runner.QAEngine")
    def test_run_single_company_with_results(
        self, mock_qa_engine, mock_llm_provider, mock_json_config, runner
    ):
        """测试带结果的单公司运行。"""
        # 模拟配置加载
        mock_config_instance = mock_json_config.return_value
        mock_config_instance.load_questions.return_value = ["问题1", "问题2"]

        # 模拟 QA 引擎结果
        mock_engine_instance = mock_qa_engine.return_value
        mock_batch_result = Mock()
        mock_q1_result = Mock()
        mock_q1_result.question.text = "这是一个测试问题"
        mock_q1_result.answer.score = 8
        mock_q1_result.answer.text = "这是详细答案内容" * 30  # 超过200字符
        mock_q2_result = Mock()
        mock_q2_result.question.text = "短问题"
        mock_q2_result.answer.score = 7
        mock_q2_result.answer.text = "短答案"

        mock_batch_result.results = [mock_q1_result, mock_q2_result]
        mock_engine_instance.process_questions.return_value = mock_batch_result
        mock_engine_instance.get_statistics.return_value = {
            "total_questions": 2,
            "success_count": 2,
        }

        result = runner.run(company="海康威视", provider="deepseek")

        assert result == 0
        mock_engine_instance.output_results.assert_called_once()

    @patch("src.runners.llm_runner.ConfigManager")
    @patch("src.runners.llm_runner.LLMProvider")
    @patch("src.runners.llm_runner.QAEngine")
    def test_run_single_company_txt_format(
        self, mock_qa_engine, mock_llm_provider, mock_config_manager, runner
    ):
        """测试使用txt格式的单公司运行。"""
        mock_config_instance = mock_config_manager.return_value
        mock_config_instance.load_questions.return_value = ["问题1"]

        mock_engine_instance = mock_qa_engine.return_value
        mock_batch_result = Mock()
        mock_batch_result.results = []
        mock_engine_instance.process_questions.return_value = mock_batch_result
        mock_engine_instance.get_statistics.return_value = {
            "total_questions": 0,
            "success_count": 0,
        }

        result = runner.run(company="海康威视", provider="deepseek", config_format="txt")

        assert result == 0
        mock_config_manager.assert_called_once()

    def test_run_single_company_provider_none(self, runner):
        """测试provider为None时抛出错误。"""
        # 当provider为None时，代码在加载问题后会抛出ValueError
        # 这不应该被except块捕获，因为ValueError不是ConnectionError/TimeoutError/OSError
        with pytest.raises(ValueError, match="provider 参数不能为 None"):
            runner.run(company="海康威视", provider=None)

    @patch("src.runners.llm_runner.JSONConfigManager")
    @patch("src.runners.llm_runner.LLMProvider")
    @patch("src.runners.llm_runner.QAEngine")
    def test_run_single_company_connection_error(
        self, mock_qa_engine, mock_llm_provider, mock_json_config, runner
    ):
        """测试连接错误处理。"""
        mock_config_instance = mock_json_config.return_value
        mock_config_instance.load_questions.side_effect = ConnectionError("Network error")

        result = runner.run(company="海康威视", provider="deepseek")

        assert result == 1

    @patch("src.runners.llm_runner.load_stock_list")
    @patch("src.runners.llm_runner.process_batch_stocks")
    def test_run_batch_mode_with_failures(self, mock_process_batch, mock_load_stocks, runner):
        """测试批量模式有失败的情况。"""
        mock_load_stocks.return_value = ["公司1", "公司2"]
        mock_process_batch.return_value = {
            "total_stocks": 2,
            "success_count": 1,
            "failed_count": 1,
            "failed_stocks": [{"stock": "公司2", "error": "API错误"}],
        }

        result = runner.run(batch_file="stocks.txt", provider="deepseek")

        assert result == 1  # 有失败返回1

    @patch("src.runners.llm_runner.load_stock_list")
    @patch("src.runners.llm_runner.process_batch_stocks")
    def test_run_batch_mode_value_error(self, mock_process_batch, mock_load_stocks, runner):
        """测试批量模式ValueError处理。"""
        mock_load_stocks.side_effect = ValueError("Invalid format")

        result = runner.run(batch_file="stocks.txt", provider="deepseek")

        assert result == 1

    @patch("src.runners.llm_runner.load_stock_list")
    @patch("src.runners.llm_runner.process_batch_stocks")
    def test_run_batch_mode_with_override(self, mock_process_batch, mock_load_stocks, runner):
        """测试批量模式with override参数。"""
        mock_load_stocks.return_value = ["公司1"]
        mock_process_batch.return_value = {
            "total_stocks": 1,
            "success_count": 1,
            "failed_count": 0,
            "failed_stocks": [],
        }

        result = runner.run(batch_file="stocks.txt", provider="deepseek", override=True)

        assert result == 0
        # 验证override参数被传递
        call_args = mock_process_batch.call_args
        assert call_args.kwargs.get("override") is True

    def test_verbose_mode(self):
        """测试verbose模式。"""
        runner_verbose = LLMRunner(verbose=True)
        assert runner_verbose.logger is not None


def test_load_stock_list(tmp_path):
    """测试加载股票列表函数。"""
    stock_file = tmp_path / "stocks.txt"
    stock_file.write_text("公司1\n  公司2  \n\n公司3", encoding="utf-8")

    stocks = load_stock_list(str(stock_file))
    assert stocks == ["公司1", "公司2", "公司3"]


def test_load_stock_list_not_found():
    """测试文件未找到。"""
    with pytest.raises(FileNotFoundError):
        load_stock_list("non_existent.txt")


def test_load_stock_list_empty_file(tmp_path):
    """测试空股票列表文件。"""
    stock_file = tmp_path / "empty_stocks.txt"
    stock_file.write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="股票列表文件为空"):
        load_stock_list(str(stock_file))


def test_load_stock_list_os_error(tmp_path):
    """测试OSError处理。"""
    # 使用mock来模拟OSError
    with patch("builtins.open", side_effect=OSError("Permission denied")):
        with pytest.raises(OSError):
            load_stock_list("some_file.txt")


class TestProcessBatchStocks:
    """测试批量处理股票函数。"""

    @patch("src.runners.llm_runner.load_questions")
    @patch("src.runners.llm_runner.calculate_questions_to_process")
    @patch("src.runners.llm_runner.validate_and_repair_existing_file")
    @patch("src.runners.llm_runner.process_single_stock_with_retry")
    @patch("src.runners.llm_runner.load_existing_answers")
    def test_process_batch_all_success(
        self, mock_load_answers, mock_process_single, mock_validate, mock_calc, mock_load_questions
    ):
        """测试所有股票成功处理。"""
        mock_load_questions.return_value = ["q1", "q2"]
        mock_load_answers.return_value = {}
        mock_calc.return_value = (["q1", "q2"], [], True)
        mock_process_single.return_value = (True, None)

        results = process_batch_stocks(
            stocks=["公司1", "公司2"],
            config_file="config.json",
            provider_name="deepseek",
            output_dir="outputs",
        )

        assert results["success_count"] == 2
        assert results["failed_count"] == 0
        assert results["total_stocks"] == 2

    @patch("src.runners.llm_runner.load_questions")
    @patch("src.runners.llm_runner.calculate_questions_to_process")
    @patch("src.runners.llm_runner.process_single_stock_with_retry")
    @patch("src.runners.llm_runner.load_existing_answers")
    def test_process_batch_with_failure(
        self, mock_load_answers, mock_process_single, mock_calc, mock_load_questions
    ):
        """测试部分股票处理失败。"""
        mock_load_questions.return_value = ["q1"]
        mock_load_answers.return_value = {}
        mock_calc.return_value = (["q1"], [], True)
        # 第一次成功，第二次失败
        mock_process_single.side_effect = [(True, None), (False, "API错误")]

        results = process_batch_stocks(
            stocks=["公司1", "公司2"],
            config_file="config.json",
            provider_name="deepseek",
            output_dir="outputs",
        )

        assert results["success_count"] == 1
        assert results["failed_count"] == 1
        assert len(results["failed_stocks"]) == 1

    @patch("src.runners.llm_runner.load_questions")
    @patch("src.runners.llm_runner.calculate_questions_to_process")
    @patch("src.runners.llm_runner.validate_and_repair_existing_file")
    @patch("src.runners.llm_runner.load_existing_answers")
    def test_process_batch_skip_existing(
        self, mock_load_answers, mock_validate, mock_calc, mock_load_questions
    ):
        """测试跳过已存在的输出文件。"""
        mock_load_questions.return_value = ["q1"]
        mock_load_answers.return_value = {"q1": {"text": "答案"}}
        mock_calc.return_value = ([], ["q1"], False)

        results = process_batch_stocks(
            stocks=["公司1"],
            config_file="config.json",
            provider_name="deepseek",
            output_dir="outputs",
        )

        assert results["skipped_count"] == 1
        assert results["success_count"] == 0
        mock_validate.assert_called_once()

    @patch("src.runners.llm_runner.load_questions")
    @patch("src.runners.llm_runner.load_existing_answers")
    def test_process_batch_provider_none(self, mock_load_answers, mock_load_questions):
        """测试provider为None的情况。"""
        mock_load_questions.return_value = ["q1"]
        mock_load_answers.return_value = {}

        results = process_batch_stocks(
            stocks=["公司1"],
            config_file="config.json",
            provider_name=None,
            output_dir="outputs",
        )

        assert results["failed_count"] == 1
        assert results["failed_stocks"][0]["error"] == "provider_name is None"


class TestLogFinalResults:
    """测试最终结果日志。"""

    @patch("src.runners.llm_runner.logger")
    def test_log_final_results_basic(self, mock_logger):
        """测试基本结果日志。"""
        results = {
            "total_stocks": 10,
            "success_count": 8,
            "failed_count": 2,
            "failed_stocks": [{"stock": "公司1", "error": "API错误"}],
        }
        log_final_results(results, 100.0, "outputs")

        # 验证日志被调用
        assert mock_logger.info.called

    @patch("src.runners.llm_runner.logger")
    def test_log_final_results_with_skipped(self, mock_logger):
        """测试包含跳过数量的结果日志。"""
        results = {
            "total_stocks": 10,
            "success_count": 5,
            "failed_count": 2,
            "skipped_count": 3,
            "failed_stocks": [],
        }
        log_final_results(results, 100.0, "outputs")

        # 验证skipped_count被记录
        calls = [str(call) for call in mock_logger.info.call_args_list]
        assert any("skipped_count" in str(c) or "已跳过" in str(c) for c in calls)
