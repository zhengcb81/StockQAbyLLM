#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试安全工具模块。"""

import pytest
from time import sleep
from src.utils.security import (
    sanitize_filename,
    sanitize_path,
    validate_question,
    validate_answer,
    RateLimiter,
    mask_api_key,
    validate_json_structure,
)


class TestSanitizeFilename:
    """测试文件名清洗功能。"""

    def test_normal_filename(self):
        """测试正常文件名。"""
        assert sanitize_filename("test.txt") == "test.txt"
        assert sanitize_filename("data.json") == "data.json"

    def test_path_traversal_attack(self):
        """测试路径遍历攻击防护。"""
        assert ".." not in sanitize_filename("../../../etc/passwd")
        assert sanitize_filename("../test.txt") != "../test.txt"
        # "....txt" -> ".." removed twice, leaving ".txt" -> leading dot removed -> "txt"
        result = sanitize_filename("....txt")
        assert ".." not in result  # 确保没有路径遍历

    def test_dangerous_chars_removal(self):
        """测试危险字符移除。"""
        result1 = sanitize_filename("file<script>.txt")
        assert "<script>" not in result1  # 确保危险字符被移除
        assert "file" in result1 and ".txt" in result1

        result2 = sanitize_filename("file|name.txt")
        assert "|" not in result2

        result3 = sanitize_filename('file"name.txt')
        assert '"' not in result3

    def test_dangerous_extensions(self):
        """测试危险扩展名处理。"""
        assert sanitize_filename("malicious.exe").endswith(".txt")
        assert sanitize_filename("safe.csv") == "safe.csv"

    def test_empty_filename(self):
        """测试空文件名。"""
        with pytest.raises(ValueError):
            sanitize_filename("")

        # 全部是非法字符
        with pytest.raises(ValueError):
            sanitize_filename("<<<>>>")

    def test_max_length_truncation(self):
        """测试长度限制。"""
        long_name = "a" * 300
        result = sanitize_filename(long_name, max_length=100)
        assert len(result) == 100

    def test_unicode_filename(self):
        """测试 Unicode 文件名。"""
        assert sanitize_filename("测试文件.txt") == "测试文件.txt"


class TestSanitizePath:
    """测试路径清洗功能。"""

    def test_normal_path(self):
        """测试正常路径。"""
        result = sanitize_path("data/test.txt")
        assert "test.txt" in result

    def test_path_with_base_dir(self, tmp_path):
        """测试带基础目录的路径验证。"""
        base = str(tmp_path)
        result = sanitize_path("test.txt", base_dir=base)
        assert result.startswith(base)

    def test_path_traversal_with_base(self, tmp_path):
        """测试带基础目录的路径遍历防护。"""
        import os
        base = str(tmp_path)
        # 创建一个确实在base之外的路径
        # 使用父目录尝试逃逸
        parent_dir = os.path.dirname(base)
        test_path = os.path.join(parent_dir, "etc", "passwd")

        # 这应该被检测为逃逸尝试
        try:
            result = sanitize_path(test_path, base_dir=base)
            # 如果没有抛出异常，验证结果是否真的在base下
            # 如果sanitize_path正确地将其转换为base下的文件，那就没问题
            assert result.startswith(base)
        except ValueError as e:
            # 预期的行为
            assert "逃逸" in str(e)


class TestInputValidation:
    """测试输入验证功能。"""

    def test_valid_question(self):
        """测试有效问题。"""
        validate_question("这是一个测试问题？")

    def test_empty_question(self):
        """测试空问题。"""
        with pytest.raises(ValueError, match="不能为空"):
            validate_question("")

    def test_whitespace_only_question(self):
        """测试只包含空白的问题。"""
        with pytest.raises(ValueError):
            validate_question("   \n\t  ")

    def test_too_long_question(self):
        """测试过长问题。"""
        long_question = "a" * 20000
        with pytest.raises(ValueError, match="过长"):
            validate_question(long_question, max_length=1000)

    def test_valid_answer(self):
        """测试有效答案。"""
        validate_answer("这是一个测试答案。")

    def test_empty_answer(self):
        """测试空答案。"""
        with pytest.raises(ValueError, match="不能为空"):
            validate_answer("")


class TestRateLimiter:
    """测试速率限制器。"""

    def test_basic_rate_limiting(self):
        """测试基本速率限制。"""
        limiter = RateLimiter(rate=10.0, capacity=10)

        # 应该能立即获取10个令牌
        assert limiter.acquire(tokens=10) is True
        # 第11个应该失败
        assert limiter.acquire(tokens=1) is False

    def test_token_refill(self):
        """测试令牌补充。"""
        limiter = RateLimiter(rate=5.0, capacity=5)

        # 用掉所有令牌
        assert limiter.acquire(tokens=5) is True
        assert limiter.acquire(tokens=1) is False

        # 等待令牌补充
        sleep(0.25)  # 等待约1.25个令牌
        assert limiter.acquire(tokens=1) is True

    def test_wait_for_token(self):
        """测试等待令牌。"""
        limiter = RateLimiter(rate=10.0, capacity=5)

        # 用掉所有令牌
        limiter.acquire(tokens=5)

        # 应该会阻塞直到有令牌可用
        limiter.wait_for_token(tokens=1)

    def test_get_available_tokens(self):
        """测试获取可用令牌数。"""
        limiter = RateLimiter(rate=5.0, capacity=10)

        available = limiter.get_available_tokens()
        assert available == 10

        limiter.acquire(tokens=3)
        available = limiter.get_available_tokens()
        # 由于时间流逝，可能有少量令牌补充，所以用近似比较
        assert 6.9 <= available <= 7.1  # 允许微小的时间差异


class TestMaskApiKey:
    """测试 API 密钥遮蔽。"""

    def test_mask_normal_key(self):
        """测试正常密钥遮蔽。"""
        masked = mask_api_key("sk-1234567890abcdef")
        assert masked.startswith("sk-1")
        assert "*" in masked
        assert "7890" not in masked

    def test_mask_short_key(self):
        """测试短密钥遮蔽。"""
        assert mask_api_key("ab") == "a*"
        assert mask_api_key("a") == "a"

    def test_mask_empty_key(self):
        """测试空密钥。"""
        assert mask_api_key("") == "***"
        assert mask_api_key(None) == "***"


class TestValidateJsonStructure:
    """测试 JSON 结构验证。"""

    def test_valid_structure(self):
        """测试有效结构。"""
        data = {"name": "test", "value": 123}
        validate_json_structure(data, ["name", "value"])

    def test_missing_keys(self):
        """测试缺少必需键。"""
        data = {"name": "test"}
        with pytest.raises(ValueError, match="缺少必需的键"):
            validate_json_structure(data, ["name", "value", "missing"])

    def test_empty_required_list(self):
        """测试空必需列表。"""
        data = {"any": "thing"}
        validate_json_structure(data, [])  # 不应抛出异常
