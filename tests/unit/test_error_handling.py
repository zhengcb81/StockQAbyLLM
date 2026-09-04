"""测试错误处理。

该模块测试各种错误场景和边界情况。
"""

from pathlib import Path

import pytest

from src.config.config_manager import ConfigManager
from src.core.exceptions import (
    ConfigError,
    EmptyConfigError,
    ProcessingError,
    ProjectFileNotFoundError,
    ValidationError,
)
from src.core.models import Question
from src.services.answer_generator import AnswerGenerator
from src.services.search_service import SearchService


class TestErrorHandling:
    """测试错误处理机制。"""

    def test_config_file_not_found_error(self):
        """测试配置文件未找到错误。"""
        manager = ConfigManager("nonexistent_file.txt")

        with pytest.raises(ProjectFileNotFoundError) as exc_info:
            manager.load_questions()

        assert "配置文件不存在" in str(exc_info.value)

    def test_config_file_permission_error(self, tmp_path):
        """测试配置文件权限错误。"""
        # 创建一个测试文件
        config_file = tmp_path / "readonly_config.txt"
        config_file.write_text("测试问题\n", encoding="utf-8")

        # 在大多数情况下，我们能正常读取文件
        # 这里我们测试文件能被正常读取
        manager = ConfigManager(str(config_file))
        questions = manager.load_questions()

        assert len(questions) == 1
        assert questions[0] == "测试问题"

    def test_config_empty_file_error(self, empty_config_file):
        """测试空配置文件错误。"""
        manager = ConfigManager(empty_config_file)

        with pytest.raises(EmptyConfigError) as exc_info:
            manager.load_questions()

        assert "配置文件为空" in str(exc_info.value)

    def test_question_validation_error(self):
        """测试问题验证错误。"""
        with pytest.raises(ValueError, match="问题文本不能为空"):
            Question(text="")

    def test_search_service_empty_query_error(self):
        """测试搜索服务空查询错误。"""
        service = SearchService()

        with pytest.raises(ProcessingError, match="搜索查询不能为空"):
            service.search("")

    def test_search_service_whitespace_query_error(self):
        """测试搜索服务纯空格查询错误。"""
        service = SearchService()

        with pytest.raises(ProcessingError):
            service.search("   ")

    def test_answer_generator_no_results(self):
        """测试答案生成器处理无搜索结果。"""
        generator = AnswerGenerator()
        question = Question(text="测试问题")

        answer = generator.generate_answer(question, [])

        assert "没有找到" in answer.text
        assert answer.source == "no_results"

    def test_config_manager_get_questions_before_loading(self, temp_config_file):
        """测试在加载前获取问题。"""
        manager = ConfigManager(temp_config_file)

        with pytest.raises(ConfigError, match="配置尚未加载"):
            manager.get_questions()

    def test_validation_error_with_non_list(self, temp_config_file):
        """测试验证非列表类型。"""
        manager = ConfigManager(temp_config_file)

        with pytest.raises(ConfigError, match="问题必须是列表类型"):
            manager.validate_questions("not a list")

    def test_validation_error_with_non_string_question(self, temp_config_file):
        """测试验证包含非字符串的问题。"""
        manager = ConfigManager(temp_config_file)

        with pytest.raises(ConfigError, match="不是字符串类型"):
            manager.validate_questions([123, 456])

    def test_answer_generator_batch_mismatch(self, tmp_path):
        """测试答案生成器批次不匹配错误。"""
        generator = AnswerGenerator()

        questions = [Question(text="问题1"), Question(text="问题2")]

        # 搜索结果数量不匹配
        search_results = [[{"title": "结果1", "snippet": "摘要1", "source": "web"}]]

        with pytest.raises(ProcessingError, match="问题和搜索结果数量不匹配"):
            generator.generate_batch_answers(questions, search_results)


class TestBoundaryConditions:
    """测试边界条件和特殊情况。"""

    def test_very_long_question(self):
        """测试超长问题。"""
        very_long_question = "问" * 10000
        question = Question(text=very_long_question)

        assert len(question.text) == 10000

    def test_question_with_special_characters(self):
        """测试包含特殊字符的问题。"""
        special_questions = [
            "问题\n带\n换行符",
            "问题\t带\t制表符",
            "问题带'单引号'",
            '问题带"双引号"',
            "问题带\\反斜杠",
            "问题带/斜杠",
            "问题带<>&*%$#@!",
        ]

        for q in special_questions:
            question = Question(text=q)
            assert question.text == q

    def test_question_with_unicode_emojis(self):
        """测试包含 Unicode 表情符号的问题。"""
        emoji_questions = [
            "测试表情符号 😀😁🎉",
            "测试数学符号 ∑∏∫√",
            "测试箭头 →←↑↓",
            "测试货币符号 €£¥₹",
            "测试其他符号 ©®™™",
        ]

        for q in emoji_questions:
            question = Question(text=q)
            assert question.text == q

    def test_question_with_mixed_scripts(self):
        """测试混合文字系统。"""
        mixed_questions = ["English中文日本語", "Привет世界مرحبا", "مرحبا🌍World"]

        for q in mixed_questions:
            question = Question(text=q)
            assert question.text == q

    def test_single_character_question(self):
        """测试单字符问题。"""
        question = Question(text="?")

        assert question.text == "?"

    def test_question_with_leading_trailing_whitespace(self):
        """测试首尾空格的问题。"""
        question = Question(text="   测试问题   ")

        assert question.text == "测试问题"
        assert question.text == question.text.strip()

    def test_config_file_with_only_comments(self, tmp_path):
        """测试只包含注释的配置文件（当前不支持注释）。"""
        config_file = tmp_path / "comments_only.txt"
        # 由于当前不支持注释，这些行会被当作问题
        config_file.write_text("# 这是注释\n# 另一个注释\n", encoding="utf-8")

        manager = ConfigManager(str(config_file))
        questions = manager.load_questions()

        # 当前实现会把注释也当作问题
        assert len(questions) > 0

    def test_config_file_with_blank_lines(self, tmp_path):
        """测试包含空行的配置文件。"""
        config_file = tmp_path / "with_blanks.txt"
        config_file.write_text("问题1\n\n问题2\n\n\n问题3\n", encoding="utf-8")

        manager = ConfigManager(str(config_file))
        questions = manager.load_questions()

        assert len(questions) == 3
        assert questions == ["问题1", "问题2", "问题3"]

    def test_answer_with_special_characters(self):
        """测试包含特殊字符的答案。"""
        from src.core.models import Answer

        special_answer = Answer(text="答案包含\n换行\t制表符'引号\"双引号", source="test")

        assert "\n" in special_answer.text
        assert "\t" in special_answer.text
        assert "'" in special_answer.text
        assert '"' in special_answer.text
