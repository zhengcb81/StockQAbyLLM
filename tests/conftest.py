"""Pytest 配置和共享 fixtures。

该模块提供测试所需的共享 fixtures 和配置。
"""

import sys
from pathlib import Path

import pytest

# 添加 src 到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def sample_questions():
    """提供示例问题列表。"""
    return ["如何学习Python编程？", "人工智能的发展趋势是什么？", "量子计算机的工作原理是什么？"]


@pytest.fixture
def temp_config_file(tmp_path, sample_questions):
    """创建临时配置文件。

    Args:
        tmp_path: pytest 提供的临时目录
        sample_questions: 问题列表

    Returns:
        临时配置文件路径
    """
    config_file = tmp_path / "test_config.txt"
    with open(config_file, "w", encoding="utf-8") as f:
        for question in sample_questions:
            f.write(question + "\n")
    return str(config_file)


@pytest.fixture
def empty_config_file(tmp_path):
    """创建空配置文件。

    Args:
        tmp_path: pytest 提供的临时目录

    Returns:
        空配置文件路径
    """
    config_file = tmp_path / "empty_config.txt"
    config_file.touch()
    return str(config_file)


@pytest.fixture
def invalid_config_file(tmp_path):
    """创建只包含空白行的配置文件。

    Args:
        tmp_path: pytest 提供的临时目录

    Returns:
        无效配置文件路径
    """
    config_file = tmp_path / "invalid_config.txt"
    with open(config_file, "w", encoding="utf-8") as f:
        f.write("\n\n\n")
    return str(config_file)
