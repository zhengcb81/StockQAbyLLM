"""测试配置管理器。

该模块测试 ConfigManager 类的功能。
"""

import pytest
from pathlib import Path

from src.config.config_manager import ConfigManager
from src.core.exceptions import ProjectFileNotFoundError, EmptyConfigError, ConfigError


class TestConfigManager:
    """测试 ConfigManager 类。"""

    def test_init(self):
        """测试初始化。"""
        manager = ConfigManager("config.txt")
        assert manager.config_path == Path("config.txt")
        assert manager.get_question_count() == 0

    def test_load_valid_config(self, temp_config_file):
        """测试加载有效配置。"""
        manager = ConfigManager(temp_config_file)
        questions = manager.load_questions()

        assert len(questions) == 3
        assert "如何学习Python编程？" in questions
        assert manager.get_question_count() == 3

    def test_load_nonexistent_file(self):
        """测试加载不存在的文件。"""
        manager = ConfigManager("nonexistent.txt")

        with pytest.raises(ProjectFileNotFoundError):
            manager.load_questions()

    def test_load_empty_config(self, empty_config_file):
        """测试加载空配置文件。"""
        manager = ConfigManager(empty_config_file)

        with pytest.raises(EmptyConfigError):
            manager.load_questions()

    def test_load_invalid_config(self, invalid_config_file):
        """测试加载只包含空白行的配置文件。"""
        manager = ConfigManager(invalid_config_file)

        with pytest.raises(EmptyConfigError):
            manager.load_questions()

    def test_validate_valid_questions(self, temp_config_file):
        """测试验证有效问题列表。"""
        manager = ConfigManager(temp_config_file)
        questions = manager.load_questions()

        assert manager.validate_questions(questions) is True

    def test_validate_empty_questions(self, temp_config_file):
        """测试验证空问题列表。"""
        manager = ConfigManager(temp_config_file)

        with pytest.raises(ConfigError, match="问题列表不能为空"):
            manager.validate_questions([])

    def test_validate_non_list_questions(self, temp_config_file):
        """测试验证非列表类型。"""
        manager = ConfigManager(temp_config_file)

        with pytest.raises(ConfigError, match="问题必须是列表类型"):
            manager.validate_questions("not a list")

    def test_validate_question_with_non_string(self, temp_config_file):
        """测试验证包含非字符串的问题。"""
        manager = ConfigManager(temp_config_file)

        with pytest.raises(ConfigError, match="不是字符串类型"):
            manager.validate_questions([123, 456])

    def test_validate_question_with_empty_string(self, temp_config_file):
        """测试验证包含空字符串的问题。"""
        manager = ConfigManager(temp_config_file)

        with pytest.raises(ConfigError, match="问题 .* 为空"):
            manager.validate_questions(["", "valid question"])

    def test_get_questions_before_loading(self, temp_config_file):
        """测试在加载前获取问题。"""
        manager = ConfigManager(temp_config_file)

        with pytest.raises(ConfigError, match="配置尚未加载"):
            manager.get_questions()

    def test_get_questions_after_loading(self, temp_config_file):
        """测试在加载后获取问题。"""
        manager = ConfigManager(temp_config_file)
        manager.load_questions()

        questions = manager.get_questions()
        assert len(questions) == 3

    def test_config_manager_repr(self, temp_config_file):
        """测试字符串表示。"""
        manager = ConfigManager(temp_config_file)
        manager.load_questions()

        repr_str = repr(manager)
        assert "ConfigManager" in repr_str
        assert "questions=3" in repr_str
