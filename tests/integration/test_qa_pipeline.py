"""端到端集成测试。

该模块测试完整的问答流程。
"""

import json
import sys
from pathlib import Path

import pytest

# 添加 src 到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from src.config.config_manager import ConfigManager
from src.core.models import Answer, QAResult, Question
from src.core.qa_engine import QAEngine
from src.services.answer_generator import AnswerGenerator
from src.services.search_service import SearchService


class TestQAPipeline:
    """测试完整的问答流程。"""

    def test_end_to_end_pipeline(self, temp_config_file):
        """测试从配置到结果的完整流程。"""
        # 1. 加载配置
        config_manager = ConfigManager(temp_config_file)
        questions = config_manager.load_questions()

        # 2. 验证配置
        config_manager.validate_questions(questions)

        # 3. 初始化 QAEngine
        search_service = SearchService()
        answer_generator = AnswerGenerator()
        qa_engine = QAEngine(search_service, answer_generator)

        # 4. 处理问题
        batch_result = qa_engine.process_questions(questions)

        # 5. 验证结果
        assert batch_result.total_questions == len(questions)
        assert batch_result.processed_count == len(questions)
        assert batch_result.is_complete()

        # 6. 转换为字典格式
        result_dict = batch_result.to_dict()

        # 7. 验证输出格式
        assert len(result_dict) == 3
        assert "如何学习Python编程？" in result_dict
        assert "人工智能的发展趋势是什么？" in result_dict
        assert "量子计算机的工作原理是什么？" in result_dict

        # 8. 验证 JSON 序列化
        json_output = json.dumps(result_dict, ensure_ascii=False, indent=4)
        assert "如何学习Python编程？" in json_output
        assert "这是关于" in json_output

        # 9. 验证统计信息
        stats = qa_engine.get_statistics(batch_result)
        assert stats["total_questions"] == 3
        assert stats["success_count"] == 3
        assert stats["error_count"] == 0

    def test_backward_compatibility(self, temp_config_file):
        """测试向后兼容性 - 确保输出格式与新版实现一致。"""
        # 使用新的架构
        config_manager = ConfigManager(temp_config_file)
        questions = config_manager.load_questions()

        search_service = SearchService()
        answer_generator = AnswerGenerator()
        qa_engine = QAEngine(search_service, answer_generator)

        batch_result = qa_engine.process_questions(questions)
        result_dict = batch_result.to_dict()

        # 验证输出格式
        assert isinstance(result_dict, dict)
        assert all(isinstance(k, str) for k in result_dict.keys())
        # 实际格式: values 是包含 score 和 description 的字典
        assert all(isinstance(v, dict) for v in result_dict.values())

        # 验证格式与新版实现一致
        for question_text in questions:
            assert question_text in result_dict
            # 验证包含 score 和 description 字段
            assert "score" in result_dict[question_text]
            assert "description" in result_dict[question_text]
            assert "这是关于" in result_dict[question_text]["description"]

    def test_pipeline_with_single_question(self, tmp_path):
        """测试处理单个问题。"""
        # 创建只有一个问题的配置文件
        config_file = tmp_path / "single_question.txt"
        with open(config_file, "w", encoding="utf-8") as f:
            f.write("单个问题测试\n")

        config_manager = ConfigManager(str(config_file))
        questions = config_manager.load_questions()

        assert len(questions) == 1
        assert questions[0] == "单个问题测试"

    def test_pipeline_with_many_questions(self, tmp_path):
        """测试处理大量问题。"""
        # 创建包含 100 个问题的配置文件
        config_file = tmp_path / "many_questions.txt"
        with open(config_file, "w", encoding="utf-8") as f:
            for i in range(100):
                f.write(f"问题 {i}\n")

        config_manager = ConfigManager(str(config_file))
        questions = config_manager.load_questions()

        assert len(questions) == 100

        # 验证所有问题都正确加载
        for i, question in enumerate(questions):
            assert question == f"问题 {i}"

    def test_pipeline_with_unicode_questions(self, tmp_path):
        """测试处理包含 Unicode 字符的问题。"""
        config_file = tmp_path / "unicode_questions.txt"
        questions = [
            "如何学习 Python？",
            "What is AI?",
            "测试中文、English、そして日本語",
            "表情符号测试 🚀 🎉",
        ]

        with open(config_file, "w", encoding="utf-8") as f:
            for q in questions:
                f.write(q + "\n")

        config_manager = ConfigManager(str(config_file))
        loaded_questions = config_manager.load_questions()

        assert len(loaded_questions) == len(questions)
        assert loaded_questions == questions

    def test_pipeline_preserves_question_order(self, tmp_path):
        """测试问题顺序是否保持不变。"""
        config_file = tmp_path / "ordered_questions.txt"
        questions = ["问题1", "问题2", "问题3", "问题4", "问题5"]

        with open(config_file, "w", encoding="utf-8") as f:
            for q in questions:
                f.write(q + "\n")

        config_manager = ConfigManager(str(config_file))
        loaded_questions = config_manager.load_questions()

        assert loaded_questions == questions
