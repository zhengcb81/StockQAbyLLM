"""JSON 配置集成测试。

该模块测试 JSON 配置文件的完整工作流。
"""

import json
import sys
import tempfile
from pathlib import Path

import pytest

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from src.config.json_config_manager import JSONConfigManager


class TestJSONConfigFormats:
    """测试不同的 JSON 配置格式。"""

    def test_single_category_format(self, tmp_path):
        """测试单个 category 格式。

        Args:
            tmp_path: 临时目录
        """
        config_file = tmp_path / "config.json"
        config_content = {
            "category": "投资逻辑",
            "questions": ["公司的核心竞争优势是什么？", "公司的财务状况如何？"],
        }

        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        manager = JSONConfigManager(str(config_file))
        questions = manager.load_questions()

        assert len(questions) == 2
        assert questions[0] == "公司的核心竞争优势是什么？"
        assert questions[1] == "公司的财务状况如何？"

    def test_categories_array_format(self, tmp_path):
        """测试 categories 数组格式。

        Args:
            tmp_path: 临时目录
        """
        config_file = tmp_path / "config.json"
        config_content = {
            "categories": [
                {"category": "投资逻辑", "questions": ["公司的核心竞争优势是什么？"]},
                {"category": "财务状况", "questions": ["公司的财务状况如何？"]},
            ]
        }

        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        manager = JSONConfigManager(str(config_file))
        questions = manager.load_questions()

        assert len(questions) == 2
        assert "公司的核心竞争优势是什么？" in questions
        assert "公司的财务状况如何？" in questions

    def test_nested_object_array_format(self, tmp_path):
        """测试嵌套对象数组格式。

        Note: 当前实现不支持嵌套对象格式，测试预期行为。
        """
        config_file = tmp_path / "config.json"
        config_content = {
            "categories": [
                {
                    "category": "投资逻辑",
                    "questions": [
                        {"question": "公司的核心竞争优势是什么？"},
                        {"question": "公司的护城河是什么？"},
                    ],
                }
            ]
        }

        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        manager = JSONConfigManager(str(config_file))

        # 当前实现不支持嵌套对象格式，会跳过这些问题
        from src.core.exceptions import ConfigError, EmptyConfigError

        with pytest.raises((EmptyConfigError, ConfigError)):
            manager.load_questions()

    def test_multiple_categories_format(self, tmp_path):
        """测试多个 categories 格式。

        Args:
            tmp_path: 临时目录
        """
        config_file = tmp_path / "config.json"
        config_content = {
            "categories": [
                {"category": "投资逻辑", "questions": ["问题1", "问题2"]},
                {"category": "财务状况", "questions": ["问题3", "问题4"]},
                {"category": "风险因素", "questions": ["问题5", "问题6"]},
            ]
        }

        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        manager = JSONConfigManager(str(config_file))
        questions = manager.load_questions()

        assert len(questions) == 6
        assert "问题1" in questions
        assert "问题6" in questions


class TestJSONConfigErrorScenarios:
    """测试 JSON 配置错误场景。"""

    def test_config_file_not_exists(self, tmp_path):
        """测试配置文件不存在。

        Args:
            tmp_path: 临时目录
        """
        config_file = tmp_path / "nonexistent.json"

        from src.core.exceptions import ProjectFileNotFoundError

        with pytest.raises(ProjectFileNotFoundError):
            manager = JSONConfigManager(str(config_file))
            manager.load_questions()

    def test_invalid_json_syntax(self, tmp_path):
        """测试无效的 JSON 语法。

        Args:
            tmp_path: 临时目录
        """
        config_file = tmp_path / "config.json"
        config_file.write_text("{invalid json}", encoding="utf-8")

        from src.core.exceptions import ConfigError

        with pytest.raises(ConfigError):
            manager = JSONConfigManager(str(config_file))
            manager.load_questions()

    def test_empty_categories(self, tmp_path):
        """测试空的 categories 数组。

        Args:
            tmp_path: 临时目录
        """
        config_file = tmp_path / "config.json"
        config_content = {"categories": []}

        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        from src.core.exceptions import EmptyConfigError

        with pytest.raises(EmptyConfigError):
            manager = JSONConfigManager(str(config_file))
            manager.load_questions()

    def test_missing_questions_field(self, tmp_path):
        """测试缺少 questions 字段。

        Args:
            tmp_path: 临时目录
        """
        config_file = tmp_path / "config.json"
        config_content = {"category": "投资逻辑"}

        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        # 当配置既没有 'questions' 也没有 'categories' 时，会抛出 ConfigError
        from src.core.exceptions import ConfigError

        with pytest.raises(ConfigError):
            manager = JSONConfigManager(str(config_file))
            manager.load_questions()

    def test_unicode_characters(self, tmp_path):
        """测试 Unicode 字符（中文）。

        Args:
            tmp_path: 临时目录
        """
        config_file = tmp_path / "config.json"
        config_content = {
            "category": "投资分析",
            "questions": [
                "公司的核心竞争力是什么？🎯",
                "公司2024年的营收增长情况如何？📊",
                "公司在ESG方面的表现如何？🌱",
            ],
        }

        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        manager = JSONConfigManager(str(config_file))
        questions = manager.load_questions()

        assert len(questions) == 3
        assert "🎯" in questions[0]
        assert "📊" in questions[1]
        assert "🌱" in questions[2]

    def test_very_long_question(self, tmp_path):
        """测试超长问题。

        Args:
            tmp_path: 临时目录
        """
        config_file = tmp_path / "config.json"
        long_question = "问题" * 300  # 600个字符

        config_content = {"category": "测试", "questions": [long_question]}

        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        manager = JSONConfigManager(str(config_file))
        questions = manager.load_questions()

        assert len(questions) == 1
        assert len(questions[0]) == len(long_question)

    def test_extra_fields_ignored(self, tmp_path):
        """测试额外字段被忽略。

        Args:
            tmp_path: 临时目录
        """
        config_file = tmp_path / "config.json"
        config_content = {
            "category": "投资逻辑",
            "questions": ["问题1", "问题2"],
            "version": "1.0",
            "created_at": "2026-01-01",
            "author": "test",
        }

        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        manager = JSONConfigManager(str(config_file))
        questions = manager.load_questions()

        assert len(questions) == 2
        assert "问题1" in questions
        assert "问题2" in questions


class TestJSONConfigValidation:
    """测试 JSON 配置验证功能。"""

    def test_validate_valid_questions(self, tmp_path):
        """测试验证有效的问题列表。

        Args:
            tmp_path: 临时目录
        """
        config_file = tmp_path / "config.json"
        config_content = {"category": "测试", "questions": ["问题1", "问题2", "问题3"]}

        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        manager = JSONConfigManager(str(config_file))
        questions = manager.load_questions()

        assert manager.validate_questions(questions) is True

    def test_validate_empty_question(self, tmp_path):
        """测试验证包含空问题。

        Args:
            tmp_path: 临时目录
        """
        config_file = tmp_path / "config.json"
        config_content = {"category": "测试", "questions": ["有效问题", "", "  ", "另一个有效问题"]}

        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        manager = JSONConfigManager(str(config_file))

        from src.core.exceptions import ConfigError

        with pytest.raises(ConfigError):
            manager.validate_questions(["有效问题", "", "  "])

    def test_validate_non_string_question(self, tmp_path):
        """测试验证非字符串问题。

        Args:
            tmp_path: 临时目录
        """
        config_file = tmp_path / "config.json"
        config_content = {"category": "测试", "questions": ["问题1", 123, "问题3"]}

        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        manager = JSONConfigManager(str(config_file))

        from src.core.exceptions import ConfigError

        with pytest.raises(ConfigError):
            manager.validate_questions(["问题1", 123, "问题3"])

    def test_get_question_count(self, tmp_path):
        """测试获取问题数量。

        Args:
            tmp_path: 临时目录
        """
        config_file = tmp_path / "config.json"
        config_content = {"category": "测试", "questions": ["问题1", "问题2", "问题3"]}

        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        manager = JSONConfigManager(str(config_file))
        manager.load_questions()

        assert manager.get_question_count() == 3
