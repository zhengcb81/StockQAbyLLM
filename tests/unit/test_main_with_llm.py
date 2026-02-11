"""测试 main_with_llm.py 的批量处理和覆盖功能。

该模块测试 main_with_llm.py 中的批量股票处理功能，特别是 override 参数的行为。
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path
import tempfile
import sys
import os
import json

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

# 导入主程序中的函数（需要添加父目录到路径）
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from main_with_llm import load_stock_list, process_batch_stocks
from src.cli import batch_processor


class TestLoadStockList:
    """测试 load_stock_list 函数。"""

    def test_load_stock_list_success(self, tmp_path):
        """测试成功加载股票列表。"""
        stock_file = tmp_path / "stocks.txt"
        stock_file.write_text("华锐精密\n苏试试验\n中密控股\n", encoding="utf-8")

        stocks = load_stock_list(str(stock_file))
        assert stocks == ["华锐精密", "苏试试验", "中密控股"]

    def test_load_stock_list_with_empty_lines(self, tmp_path):
        """测试加载包含空行的股票列表。"""
        stock_file = tmp_path / "stocks.txt"
        stock_file.write_text("华锐精密\n\n苏试试验\n  \n中密控股\n", encoding="utf-8")

        stocks = load_stock_list(str(stock_file))
        assert stocks == ["华锐精密", "苏试试验", "中密控股"]

    def test_load_stock_list_file_not_found(self):
        """测试文件不存在的情况。"""
        with pytest.raises(FileNotFoundError):
            load_stock_list("nonexistent_file.txt")

    def test_load_stock_list_empty_file(self, tmp_path):
        """测试空文件的情况。"""
        stock_file = tmp_path / "empty_stocks.txt"
        stock_file.touch()

        with pytest.raises(ValueError, match="股票列表文件为空"):
            load_stock_list(str(stock_file))


class TestProcessBatchStocksOverride:
    """测试 process_batch_stocks 的 override 功能。"""

    @pytest.fixture
    def temp_output_dir(self, tmp_path):
        """创建临时输出目录。"""
        output_dir = tmp_path / "outputs"
        output_dir.mkdir()
        return str(output_dir)

    @pytest.fixture
    def temp_config_file(self, tmp_path):
        """创建临时配置文件。"""
        config_file = tmp_path / "test_config.txt"
        config_file.write_text("测试问题1\n测试问题2\n", encoding="utf-8")
        return str(config_file)

    @pytest.fixture
    def mock_llm_provider(self):
        """模拟 LLM 提供者。"""
        with patch("src.cli.batch_processor.LLMProvider") as mock:
            provider_instance = Mock()
            provider_instance.get_provider_name.return_value = "mock_provider"
            mock.return_value = provider_instance
            yield mock

    @pytest.fixture
    def mock_qa_engine(self):
        """模拟 QA 引擎。"""
        # 只需要在 batch_processor 中进行 patch
        with patch("src.cli.batch_processor.QAEngine") as mock1:
            engine_instance = Mock()

            # 创建单个问题的结果，用于 process_question
            def create_single_question_result(question_text):
                """创建单个问题处理结果，用于修复场景。"""
                mock_result = Mock()
                mock_result.question = Mock()
                mock_result.question.text = question_text
                mock_result.answer = Mock()
                mock_result.answer.score = 7
                mock_result.answer.text = f"这是{question_text}的答案"
                # 模拟 to_dict() 方法，返回正确的字典格式
                result_dict = {
                    question_text: {"score": 7, "description": f"这是{question_text}的答案"}
                }
                mock_result.to_dict.return_value = result_dict
                return mock_result

            # 让 process_question 返回单个问题结果
            engine_instance.process_question.side_effect = lambda q: create_single_question_result(
                q
            )

            # 创建一个函数来动态生成结果，基于输入的问题数量
            def create_dynamic_result(questions):
                mock_result = Mock()
                mock_result.results = []

                for i, question_text in enumerate(questions):
                    question_mock = Mock()
                    question_mock.text = question_text
                    answer_mock = Mock()
                    answer_mock.score = 7 + i
                    answer_mock.text = f"这是{question_text}的答案"

                    result_mock = Mock()
                    result_mock.question = question_mock
                    result_mock.answer = answer_mock
                    mock_result.results.append(result_mock)

                return mock_result

            # 让 process_questions 返回基于输入的动态结果
            engine_instance.process_questions.side_effect = create_dynamic_result

            # get_statistics 需要基于实际结果动态计算
            def get_dynamic_stats(batch_result):
                return {
                    "total_questions": len(batch_result.results),
                    "success_count": len(batch_result.results),
                    "error_count": 0,
                    "processed_count": len(batch_result.results),
                    "is_complete": True,
                    "created_at": "2026-01-04T10:00:00",
                }

            engine_instance.get_statistics.side_effect = get_dynamic_stats

            # 配置 output_results 方法来实际创建输出文件
            def mock_output_results(batch_result, output_path_str):
                """模拟 output_results 方法，实际创建输出文件。"""
                output_path = Path(output_path_str)
                output_path.parent.mkdir(parents=True, exist_ok=True)

                # 从 batch_result 中提取结果并创建输出字典
                output_data = {}
                for result in batch_result.results:
                    question_text = result.question.text
                    answer = result.answer
                    output_data[question_text] = {"score": answer.score, "description": answer.text}

                # 写入 JSON 文件
                with open(output_path, "w", encoding="utf-8") as f:
                    json.dump(output_data, f, ensure_ascii=False, indent=4)

            engine_instance.output_results = Mock(side_effect=mock_output_results)

            engine_instance.add_result = Mock()

            mock1.return_value = engine_instance
            yield mock1

    @pytest.fixture
    def mock_answer_generator(self):
        """模拟答案生成器。"""
        with patch("src.cli.batch_processor.AnswerGenerator") as mock:
            mock.return_value = Mock()
            yield mock

    def test_override_false_question_level_skip(
        self,
        temp_output_dir,
        temp_config_file,
        mock_llm_provider,
        mock_qa_engine,
        mock_answer_generator,
    ):
        """测试 override=False 时问题级别的跳过功能。"""
        # 创建已存在的输出文件，包含所有问题的答案
        existing_data = {
            "测试问题1": {"score": 7, "description": "这是测试问题1的现有答案"},
            "测试问题2": {"score": 8, "description": "这是测试问题2的现有答案"},
        }
        existing_file = Path(temp_output_dir) / "QALLM_华锐精密.json"
        existing_file.write_text(json.dumps(existing_data, ensure_ascii=False), encoding="utf-8")

        stocks = ["华锐精密"]

        # 调用函数，override=False
        results = process_batch_stocks(
            stocks=stocks,
            config_file=temp_config_file,
            provider_name="mock",
            output_dir=temp_output_dir,
            override=False,
            config_format="txt",
        )

        # 验证结果 - 所有问题都已存在，应该跳过整个股票
        assert results["total_stocks"] == 1
        assert results["success_count"] == 0  # 没有新问题需要处理
        assert results.get("skipped_count", 0) == 1  # 整个股票被跳过

        # 验证 QA 引擎没有被调用（因为所有问题都已存在）
        assert mock_qa_engine.return_value.process_questions.call_count == 0

    def test_override_true_processes_all_questions(
        self,
        temp_output_dir,
        temp_config_file,
        mock_llm_provider,
        mock_qa_engine,
        mock_answer_generator,
    ):
        """测试 override=True 时处理所有问题，包括已存在的。"""
        # 创建已存在的输出文件，包含部分问题的答案
        existing_data = {
            "测试问题1": {"score": 7, "description": "这是测试问题1的旧答案"},
            "测试问题2": {"score": 8, "description": "这是测试问题2的旧答案"},
        }
        existing_file = Path(temp_output_dir) / "QALLM_华锐精密.json"
        existing_file.write_text(json.dumps(existing_data, ensure_ascii=False), encoding="utf-8")

        stocks = ["华锐精密"]

        # 调用函数，override=True
        results = process_batch_stocks(
            stocks=stocks,
            config_file=temp_config_file,
            provider_name="mock",
            output_dir=temp_output_dir,
            override=True,
            config_format="txt",
        )

        # 验证结果 - 应该处理所有2个问题
        assert results["total_stocks"] == 1
        assert results["success_count"] == 1  # 1个股票处理成功
        assert results.get("skipped_count", 0) == 0  # 没有跳过股票
        assert results["failed_count"] == 0

        # 验证 QA 引擎被调用了一次（处理2个问题）
        assert mock_qa_engine.return_value.process_questions.call_count == 1
        # 验证调用时传入了所有2个问题
        call_args = mock_qa_engine.return_value.process_questions.call_args
        assert len(call_args[0][0]) == 2  # 第一个参数是问题列表

    def test_mixed_scenario_partial_questions_exist(
        self,
        temp_output_dir,
        temp_config_file,
        mock_llm_provider,
        mock_qa_engine,
        mock_answer_generator,
    ):
        """测试混合场景：部分问题已存在，部分不存在。"""
        # 为华锐精密创建输出文件，只包含部分问题
        existing_data = {
            "测试问题1": {"score": 7, "description": "这是测试问题1的现有答案"}
            # 测试问题2不存在
        }
        existing_file = Path(temp_output_dir) / "QALLM_华锐精密.json"
        existing_file.write_text(json.dumps(existing_data, ensure_ascii=False), encoding="utf-8")

        stocks = ["华锐精密", "苏试试验"]

        # override=False
        results = process_batch_stocks(
            stocks=stocks,
            config_file=temp_config_file,
            provider_name="mock",
            output_dir=temp_output_dir,
            override=False,
            config_format="txt",
        )

        # 验证结果
        assert results["total_stocks"] == 2
        assert results["success_count"] == 2  # 两个股票都处理成功
        assert results.get("skipped_count", 0) == 0  # 没有股票被完全跳过
        assert results["failed_count"] == 0

        # 验证 QA 引擎被调用了2次
        # 第一次：华锐精密，处理1个新问题（测试问题2）
        # 第二次：苏试试验，处理2个问题
        assert mock_qa_engine.return_value.process_questions.call_count == 2

        # 验证第一次调用只传入1个问题（测试问题2）
        first_call_args = mock_qa_engine.return_value.process_questions.call_args_list[0]
        assert len(first_call_args[0][0]) == 1
        assert first_call_args[0][0][0] == "测试问题2"

        # 验证第二次调用传入2个问题
        second_call_args = mock_qa_engine.return_value.process_questions.call_args_list[1]
        assert len(second_call_args[0][0]) == 2

    def test_no_override_default_behavior(
        self,
        temp_output_dir,
        temp_config_file,
        mock_llm_provider,
        mock_qa_engine,
        mock_answer_generator,
    ):
        """测试默认行为（不指定 override 参数）。"""
        # 创建已存在的输出文件，包含部分问题
        existing_data = {"测试问题1": {"score": 7, "description": "现有答案1"}}
        existing_file = Path(temp_output_dir) / "QALLM_华锐精密.json"
        existing_file.write_text(json.dumps(existing_data, ensure_ascii=False), encoding="utf-8")

        stocks = ["华锐精密", "苏试试验"]

        # 不指定 override 参数，应该默认为 False
        results = process_batch_stocks(
            stocks=stocks,
            config_file=temp_config_file,
            provider_name="mock",
            output_dir=temp_output_dir,
            config_format="txt",
            # override 参数使用默认值 False
        )

        # 验证结果
        assert results["total_stocks"] == 2
        assert results["success_count"] == 2  # 两个股票都处理成功
        assert results.get("skipped_count", 0) == 0  # 没有股票被完全跳过

        # 验证 QA 引擎调用次数
        # 第一次：华锐精密，处理1个新问题（测试问题2）
        # 第二次：苏试试验，处理2个问题
        assert mock_qa_engine.return_value.process_questions.call_count == 2

        # 验证第一次调用只传入1个问题（测试问题2）
        first_call_args = mock_qa_engine.return_value.process_questions.call_args_list[0]
        assert len(first_call_args[0][0]) == 1
        assert first_call_args[0][0][0] == "测试问题2"

        # 验证第二次调用传入2个问题（苏试试验没有现有文件）
        second_call_args = mock_qa_engine.return_value.process_questions.call_args_list[1]
        assert len(second_call_args[0][0]) == 2

    def test_override_false_no_existing_files(
        self,
        temp_output_dir,
        temp_config_file,
        mock_llm_provider,
        mock_qa_engine,
        mock_answer_generator,
    ):
        """测试 override=False 但没有现有文件的情况。"""
        stocks = ["华锐精密", "苏试试验"]

        results = process_batch_stocks(
            stocks=stocks,
            config_file=temp_config_file,
            provider_name="mock",
            output_dir=temp_output_dir,
            override=False,
            config_format="txt",
        )

        # 验证结果 - 应该处理所有股票
        assert results["total_stocks"] == 2
        assert results["success_count"] == 2
        assert results.get("skipped_count", 0) == 0


class TestProcessBatchStocksIntegration:
    """测试 process_batch_stocks 的集成场景。"""

    def test_process_batch_stocks_creates_output_files(self, tmp_path):
        """测试函数是否正确创建输出文件。"""
        # 创建临时配置文件
        config_file = tmp_path / "config.txt"
        config_file.write_text("测试问题1\n测试问题2\n", encoding="utf-8")

        # 创建临时输出目录
        output_dir = tmp_path / "outputs"
        output_dir.mkdir()

        # 使用 mock 来避免真实的 LLM 调用
        with patch("src.cli.batch_processor.LLMProvider"), patch(
            "src.cli.batch_processor.AnswerGenerator"
        ), patch("src.cli.batch_processor.QAEngine") as mock_engine:

            # 配置 mock
            engine_instance = Mock()
            mock_result = Mock()
            mock_result.results = []

            for i in range(2):
                question_mock = Mock()
                question_mock.text = f"问题{i+1}"
                answer_mock = Mock()
                answer_mock.score = 8
                answer_mock.text = f"答案{i+1}"

                result_mock = Mock()
                result_mock.question = question_mock
                result_mock.answer = answer_mock
                mock_result.results.append(result_mock)

            engine_instance.process_questions.return_value = mock_result
            engine_instance.get_statistics.return_value = {
                "total_questions": 2,
                "success_count": 2,
                "error_count": 0,
                "processed_count": 2,
                "is_complete": True,
                "created_at": "2026-01-04T10:00:00",
            }

            # 配置 process_question 用于修复场景
            def create_single_question_result(question_text):
                """创建单个问题处理结果，用于修复场景。"""
                mock_result = Mock()
                mock_result.question = Mock()
                mock_result.question.text = question_text
                mock_result.answer = Mock()
                mock_result.answer.score = 7
                mock_result.answer.text = f"这是{question_text}的答案"
                # 模拟 to_dict() 方法，返回正确的字典格式
                result_dict = {
                    question_text: {"score": 7, "description": f"这是{question_text}的答案"}
                }
                mock_result.to_dict.return_value = result_dict
                return mock_result

            engine_instance.process_question.side_effect = lambda q: create_single_question_result(
                q
            )

            # 捕获 output_results 调用的参数
            output_files_written = []

            def capture_output(result, path):
                output_files_written.append(path)
                # 创建实际的输出文件内容
                output_data = {}
                for r in result.results:
                    question_text = r.question.text
                    answer = r.answer
                    output_data[question_text] = {"score": answer.score, "description": answer.text}
                Path(path).write_text(
                    json.dumps(output_data, ensure_ascii=False, indent=4), encoding="utf-8"
                )

            engine_instance.output_results = Mock(side_effect=capture_output)
            mock_engine.return_value = engine_instance

            # 执行测试
            stocks = ["测试股票1", "测试股票2"]
            results = process_batch_stocks(
                stocks=stocks,
                config_file=str(config_file),
                provider_name="test",
                output_dir=str(output_dir),
                override=False,
                config_format="txt",
            )

            # 验证输出文件被创建
            assert len(output_files_written) == 2
            assert any("QALLM_测试股票1.json" in path for path in output_files_written)
            assert any("QALLM_测试股票2.json" in path for path in output_files_written)

            # 验证文件确实存在
            assert (output_dir / "QALLM_测试股票1.json").exists()
            assert (output_dir / "QALLM_测试股票2.json").exists()
