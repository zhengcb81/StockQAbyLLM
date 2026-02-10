"""测试 Settings 模块。

该模块测试配置常量和默认设置。
"""

import pytest
from pathlib import Path
import sys

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from src.config import settings


class TestSettingsConstants:
    """测试 Settings 常量。"""

    def test_project_root_exists(self):
        """测试项目根目录存在。"""
        assert settings.PROJECT_ROOT.exists()
        assert settings.PROJECT_ROOT.is_dir()

    def test_project_root_name(self):
        """测试项目根目录名称。"""
        assert settings.PROJECT_ROOT.name in ["StockQAbyLLM", "projects"]

    def test_log_dir(self):
        """测试日志目录配置。"""
        assert settings.LOG_DIR.name == "logs"
        assert settings.LOG_DIR.parent == settings.PROJECT_ROOT

    def test_output_dir(self):
        """测试输出目录配置。"""
        assert settings.OUTPUT_DIR.name == "outputs"
        assert settings.OUTPUT_DIR.parent == settings.PROJECT_ROOT

    def test_default_config_file(self):
        """测试默认配置文件名。"""
        assert settings.DEFAULT_CONFIG_FILE == "config.txt"
        assert isinstance(settings.DEFAULT_CONFIG_FILE, str)

    def test_default_encoding(self):
        """测试默认编码。"""
        assert settings.DEFAULT_ENCODING == "utf-8"

    def test_max_question_length(self):
        """测试最大问题长度。"""
        assert settings.MAX_QUESTION_LENGTH == 1000
        assert settings.MAX_QUESTION_LENGTH > 0

    def test_min_question_length(self):
        """测试最小问题长度。"""
        assert settings.MIN_QUESTION_LENGTH == 1
        assert settings.MIN_QUESTION_LENGTH > 0

    def test_max_greater_than_min(self):
        """测试最大长度大于最小长度。"""
        assert settings.MAX_QUESTION_LENGTH > settings.MIN_QUESTION_LENGTH

    def test_json_indent(self):
        """测试 JSON 缩进配置。"""
        assert settings.JSON_INDENT == 4
        assert settings.JSON_INDENT > 0

    def test_ensure_ascii(self):
        """测试 ASCII 编码选项。"""
        assert isinstance(settings.ENSURE_ASCII, bool)
        assert settings.ENSURE_ASCII is False  # 支持中文

    def test_log_level(self):
        """测试默认日志级别。"""
        assert settings.LOG_LEVEL == "INFO"
        assert settings.LOG_LEVEL in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

    def test_log_format(self):
        """测试日志格式字符串。"""
        assert isinstance(settings.LOG_FORMAT, str)
        assert "%(asctime)s" in settings.LOG_FORMAT
        assert "%(name)s" in settings.LOG_FORMAT
        assert "%(levelname)s" in settings.LOG_FORMAT

    def test_log_date_format(self):
        """测试日志日期格式。"""
        assert isinstance(settings.LOG_DATE_FORMAT, str)
        assert "%Y" in settings.LOG_DATE_FORMAT  # 年份
        assert "%m" in settings.LOG_DATE_FORMAT  # 月份
        assert "%d" in settings.LOG_DATE_FORMAT  # 日期


class TestSettingsTypes:
    """测试 Settings 类型正确性。"""

    def test_project_root_is_path(self):
        """测试 PROJECT_ROOT 是 Path 对象。"""
        assert isinstance(settings.PROJECT_ROOT, Path)

    def test_log_dir_is_path(self):
        """测试 LOG_DIR 是 Path 对象。"""
        assert isinstance(settings.LOG_DIR, Path)

    def test_output_dir_is_path(self):
        """测试 OUTPUT_DIR 是 Path 对象。"""
        assert isinstance(settings.OUTPUT_DIR, Path)


class TestSettingsValues:
    """测试 Settings 配置值的合理性。"""

    def test_question_length_bounds(self):
        """测试问题长度边界合理。"""
        # 最小1个字符
        assert settings.MIN_QUESTION_LENGTH >= 1
        # 最大不超过10000个字符
        assert settings.MAX_QUESTION_LENGTH <= 10000
        # 最大至少是最小的10倍
        assert settings.MAX_QUESTION_LENGTH >= settings.MIN_QUESTION_LENGTH * 10

    def test_json_indent_reasonable(self):
        """测试 JSON 缩进合理。"""
        # 缩进应该是2或4
        assert settings.JSON_INDENT in [2, 4]
