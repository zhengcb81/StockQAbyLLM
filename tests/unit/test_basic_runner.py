#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试 BasicRunner 类。"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from src.core.exceptions import StockQAError
from src.runners.basic_runner import BasicRunner


class TestBasicRunnerInit:
    """测试 BasicRunner 初始化。"""

    def test_init_default(self):
        """测试默认初始化。"""
        runner = BasicRunner()
        assert runner.logger is not None

    def test_init_verbose(self):
        """测试启用详细日志的初始化。"""
        runner = BasicRunner(verbose=True)
        assert runner.logger is not None


class TestBasicRunnerRun:
    """测试 BasicRunner.run 方法。"""

    @pytest.fixture
    def temp_config_file(self, tmp_path):
        """创建临时配置文件。"""
        config_file = tmp_path / "test_config.txt"
        config_file.write_text("什么是股票？\n如何购买股票？\n", encoding="utf-8")
        return str(config_file)

    @pytest.fixture
    def temp_output_file(self, tmp_path):
        """创建临时输出文件路径。"""
        return str(tmp_path / "test_output.json")

    @patch("src.runners.basic_runner.ConfigManager")
    @patch("src.runners.basic_runner.SearchService")
    @patch("src.runners.basic_runner.AnswerGenerator")
    @patch("src.runners.basic_runner.QAEngine")
    def test_run_success(
        self, mock_qa_engine, mock_answer_gen, mock_search_svc, mock_config_mgr, temp_config_file
    ):
        """测试成功运行。"""
        # 配置 mocks
        mock_config_instance = Mock()
        mock_config_instance.load_questions.return_value = ["什么是股票？"]
        mock_config_instance.validate_questions.return_value = None
        mock_config_mgr.return_value = mock_config_instance

        mock_search_service = Mock()
        mock_search_svc.return_value = mock_search_service

        mock_answer_generator = Mock()
        mock_answer_gen.return_value = mock_answer_generator

        mock_qa_instance = Mock()
        mock_qa_instance.process_questions.return_value = Mock()
        mock_qa_instance.get_statistics.return_value = {"total": 1, "success": 1}
        mock_qa_engine.return_value = mock_qa_instance

        # 运行
        runner = BasicRunner(verbose=False)
        exit_code = runner.run(temp_config_file)

        # 验证
        assert exit_code == 0
        mock_config_instance.load_questions.assert_called_once()
        mock_config_instance.validate_questions.assert_called_once()
        mock_qa_instance.process_questions.assert_called_once()
        mock_qa_instance.output_results.assert_called_once()

    @patch("src.runners.basic_runner.ConfigManager")
    @patch("src.runners.basic_runner.SearchService")
    @patch("src.runners.basic_runner.AnswerGenerator")
    @patch("src.runners.basic_runner.QAEngine")
    def test_run_with_output_path(
        self,
        mock_qa_engine,
        mock_answer_gen,
        mock_search_svc,
        mock_config_mgr,
        temp_config_file,
        temp_output_file,
    ):
        """测试带输出路径的运行。"""
        # 配置 mocks
        mock_config_instance = Mock()
        mock_config_instance.load_questions.return_value = ["什么是股票？"]
        mock_config_instance.validate_questions.return_value = None
        mock_config_mgr.return_value = mock_config_instance

        mock_search_service = Mock()
        mock_search_svc.return_value = mock_search_service

        mock_answer_generator = Mock()
        mock_answer_gen.return_value = mock_answer_generator

        mock_qa_instance = Mock()
        mock_qa_instance.process_questions.return_value = Mock()
        mock_qa_instance.get_statistics.return_value = {"total": 1, "success": 1}
        mock_qa_engine.return_value = mock_qa_instance

        # 运行
        runner = BasicRunner(verbose=False)
        exit_code = runner.run(temp_config_file, temp_output_file)

        # 验证
        assert exit_code == 0
        mock_qa_instance.output_results.assert_called_once()
        # 验证输出路径参数
        call_args = mock_qa_instance.output_results.call_args
        assert (
            temp_output_file in call_args[0] or call_args[1].get("output_path") == temp_output_file
        )

    @patch("src.runners.basic_runner.ConfigManager")
    def test_run_config_error(self, mock_config_mgr, temp_config_file):
        """测试配置错误处理。"""
        # 配置 mock 抛出异常
        mock_config_mgr.side_effect = StockQAError("配置文件无效")

        runner = BasicRunner(verbose=False)
        exit_code = runner.run(temp_config_file)

        assert exit_code == 1

    @patch("src.runners.basic_runner.ConfigManager")
    def test_run_keyboard_interrupt(self, mock_config_mgr, temp_config_file):
        """测试用户中断处理。"""
        # 配置 mock 抛出 KeyboardInterrupt
        mock_config_mgr.side_effect = KeyboardInterrupt()

        runner = BasicRunner(verbose=False)
        exit_code = runner.run(temp_config_file)

        assert exit_code == 130  # 128 + SIGINT(2)

    @patch("src.runners.basic_runner.ConfigManager")
    def test_run_runtime_error(self, mock_config_mgr, temp_config_file):
        """测试未预期的运行时错误。"""
        # 配置 mock 抛出 RuntimeError
        mock_config_mgr.side_effect = RuntimeError("未预期的错误")

        runner = BasicRunner(verbose=False)
        exit_code = runner.run(temp_config_file)

        assert exit_code == 1


class TestBasicRunnerMain:
    """测试 BasicRunner.main 函数。"""

    @patch("sys.argv", ["basic_runner", "--config", "test_config.txt"])
    @patch("src.runners.basic_runner.BasicRunner")
    @patch("pathlib.Path.exists", return_value=True)
    def test_main_with_default_args(self, mock_exists, mock_runner_class):
        """测试 main 函数使用默认参数。"""
        mock_runner_instance = Mock()
        mock_runner_instance.run.return_value = 0
        mock_runner_class.return_value = mock_runner_instance

        from src.runners.basic_runner import main

        exit_code = main()

        assert exit_code == 0
        mock_runner_class.assert_called_once_with(verbose=False)
        mock_runner_instance.run.assert_called_once()

    @patch("sys.argv", ["basic_runner", "--verbose", "--config", "test_config.txt"])
    @patch("src.runners.basic_runner.BasicRunner")
    @patch("pathlib.Path.exists", return_value=True)
    def test_main_with_verbose(self, mock_exists, mock_runner_class):
        """测试 main 函数启用详细日志。"""
        mock_runner_instance = Mock()
        mock_runner_instance.run.return_value = 0
        mock_runner_class.return_value = mock_runner_instance

        from src.runners.basic_runner import main

        exit_code = main()

        assert exit_code == 0
        mock_runner_class.assert_called_once_with(verbose=True)

    @patch("sys.argv", ["basic_runner", "--config", "test_config.txt", "--output", "output.json"])
    @patch("src.runners.basic_runner.BasicRunner")
    @patch("pathlib.Path.exists", return_value=True)
    def test_main_with_output(self, mock_exists, mock_runner_class):
        """测试 main 函数指定输出文件。"""
        mock_runner_instance = Mock()
        mock_runner_instance.run.return_value = 0
        mock_runner_class.return_value = mock_runner_instance

        from src.runners.basic_runner import main

        exit_code = main()

        assert exit_code == 0
        # 验证 run 方法被调用并传入输出路径
        call_args = mock_runner_instance.run.call_args
        assert call_args[0][1] == "output.json"  # 第二个参数是 output_path
