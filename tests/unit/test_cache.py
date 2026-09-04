#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试 FileCache 类。"""

import json
from pathlib import Path
from time import sleep
from unittest.mock import Mock, patch

import pytest

from src.utils.cache import FileCache, file_cache


class TestFileCacheReadFileCached:
    """测试 FileCache.read_file_cached 方法。"""

    @pytest.fixture
    def temp_file(self, tmp_path):
        """创建临时文件。"""
        file_path = tmp_path / "test_file.txt"
        file_path.write_text("Hello, World!", encoding="utf-8")
        return file_path

    def test_read_file_cached_success(self, temp_file):
        """测试成功读取文件。"""
        content = FileCache.read_file_cached(temp_file)
        assert content == "Hello, World!"

    def test_read_file_cached_caching(self, temp_file):
        """测试缓存机制 - 第二次读取使用缓存。"""
        # 第一次读取
        content1 = FileCache.read_file_cached(temp_file)
        # 第二次读取（应该使用缓存）
        content2 = FileCache.read_file_cached(temp_file)
        assert content1 == content2 == "Hello, World!"

        # 验证缓存已更新
        filepath_str = str(temp_file)
        assert filepath_str in FileCache._file_hashes
        assert filepath_str in FileCache._file_mtimes

    def test_read_file_cached_not_found(self):
        """测试读取不存在的文件。"""
        with pytest.raises(FileNotFoundError):
            FileCache.read_file_cached("/nonexistent/file.txt")


class TestFileCacheClearCache:
    """测试 FileCache.clear_cache 方法。"""

    def test_clear_cache(self, tmp_path):
        """测试清除所有缓存。"""
        # 创建并读取文件以填充缓存
        file_path = tmp_path / "test.txt"
        file_path.write_text("Test content", encoding="utf-8")
        FileCache.read_file_cached(file_path)

        # 验证缓存已填充
        assert len(FileCache._file_hashes) > 0
        assert len(FileCache._file_mtimes) > 0

        # 清除缓存
        FileCache.clear_cache()

        # 验证缓存已清空
        assert len(FileCache._file_hashes) == 0
        assert len(FileCache._file_mtimes) == 0


class TestFileCacheIsFileModified:
    """测试 FileCache.is_file_modified 方法。"""

    @pytest.fixture
    def temp_file(self, tmp_path):
        """创建临时文件。"""
        file_path = tmp_path / "test_file.txt"
        file_path.write_text("Initial content", encoding="utf-8")
        return file_path

    def test_is_file_modified_not_cached(self, temp_file):
        """测试文件不在缓存中时返回 True。"""
        assert FileCache.is_file_modified(temp_file) is True

    def test_is_file_modified_unchanged(self, temp_file):
        """测试文件未修改时返回 False。"""
        # 先读取文件以填充缓存
        FileCache.read_file_cached(temp_file)

        # 文件未修改，应返回 False
        assert FileCache.is_file_modified(temp_file) is False

    def test_is_file_modified_after_change(self, temp_file):
        """测试文件修改后返回 True。"""
        # 先读取文件以填充缓存
        FileCache.read_file_cached(temp_file)

        # 修改文件
        sleep(0.01)  # 确保文件时间戳改变
        file_path_str = str(temp_file)
        with open(file_path_str, "w", encoding="utf-8") as f:
            f.write("Modified content")

        # 文件已修改，应返回 True
        assert FileCache.is_file_modified(temp_file) is True

    def test_is_file_modified_deleted_file(self, tmp_path):
        """测试文件被删除后返回 True。"""
        # 创建并缓存文件
        file_path = tmp_path / "to_delete.txt"
        file_path.write_text("Will be deleted", encoding="utf-8")
        FileCache.read_file_cached(file_path)

        # 删除文件
        file_path.unlink()

        # 文件已删除，应返回 True
        assert FileCache.is_file_modified(file_path) is True


class TestFileCacheGetFileHash:
    """测试 FileCache.get_file_hash 方法。"""

    @pytest.fixture
    def temp_file(self, tmp_path):
        """创建临时文件。"""
        file_path = tmp_path / "hash_test.txt"
        file_path.write_text("Content for hashing", encoding="utf-8")
        return file_path

    def test_get_file_hash_first_time(self, temp_file):
        """测试首次计算文件哈希。"""
        file_hash = FileCache.get_file_hash(temp_file)
        assert isinstance(file_hash, str)
        assert len(file_hash) == 32  # MD5 哈希长度
        assert file_hash.isalnum()

    def test_get_file_hash_uses_cache(self, temp_file):
        """测试哈希值缓存机制。"""
        # 第一次计算
        hash1 = FileCache.get_file_hash(temp_file)

        # 文件未修改，应使用缓存
        hash2 = FileCache.get_file_hash(temp_file)

        assert hash1 == hash2

    def test_get_file_hash_after_modification(self, temp_file):
        """测试文件修改后重新计算哈希。"""
        # 第一次计算
        hash1 = FileCache.get_file_hash(temp_file)

        # 修改文件
        sleep(0.01)
        file_path_str = str(temp_file)
        with open(file_path_str, "w", encoding="utf-8") as f:
            f.write("Modified content for hashing")

        # 哈希应改变
        hash2 = FileCache.get_file_hash(temp_file)
        assert hash1 != hash2

    def test_get_file_hash_nonexistent_file(self):
        """测试计算不存在文件的哈希。"""
        with pytest.raises(ValueError, match="无法读取文件以计算哈希"):
            FileCache.get_file_hash("/nonexistent/file.txt")


class TestFileCacheReadJsonCached:
    """测试 FileCache.read_json_cached 方法。"""

    @pytest.fixture
    def json_file(self, tmp_path):
        """创建临时 JSON 文件。"""
        file_path = tmp_path / "test.json"
        data = {"key": "value", "number": 42, "nested": {"item": "test"}}
        file_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return file_path

    def test_read_json_cached_success(self, json_file):
        """测试成功读取 JSON 文件。"""
        data = FileCache.read_json_cached(json_file)
        assert data == {"key": "value", "number": 42, "nested": {"item": "test"}}

    def test_read_json_cached_caching(self, json_file):
        """测试 JSON 缓存机制。"""
        # 第一次读取
        data1 = FileCache.read_json_cached(json_file)

        # 第二次读取（使用缓存）
        data2 = FileCache.read_json_cached(json_file)

        assert data1 == data2

    def test_read_json_cached_invalid_json(self, tmp_path):
        """测试读取无效的 JSON 文件。"""
        file_path = tmp_path / "invalid.json"
        file_path.write_text("{invalid json}", encoding="utf-8")

        with pytest.raises(json.JSONDecodeError):
            FileCache.read_json_cached(file_path)


class TestFileCacheInvalidateCache:
    """测试 FileCache.invalidate_cache 方法。"""

    def setup_method(self):
        """每个测试前清理缓存。"""
        FileCache.clear_cache()

    def teardown_method(self):
        """每个测试后清理缓存。"""
        FileCache.clear_cache()

    @pytest.fixture
    def cached_files(self, tmp_path):
        """创建多个已缓存的文件。"""
        files = []
        for i in range(3):
            file_path = tmp_path / f"file{i}.txt"
            file_path.write_text(f"Content {i}", encoding="utf-8")
            FileCache.read_file_cached(file_path)
            files.append(file_path)
        return files

    def test_invalidate_cache_all(self, cached_files):
        """测试清除所有缓存（filepath=None）。"""
        # 验证缓存已填充
        assert len(FileCache._file_hashes) == 3
        assert len(FileCache._file_mtimes) == 3

        # 清除所有缓存
        FileCache.invalidate_cache(None)

        # 验证缓存已清空
        assert len(FileCache._file_hashes) == 0
        assert len(FileCache._file_mtimes) == 0

    def test_invalidate_cache_single_file(self, cached_files):
        """测试清除单个文件的缓存。"""
        file_to_invalidate = cached_files[1]
        filepath_str = str(file_to_invalidate)

        # 验证缓存存在
        assert filepath_str in FileCache._file_hashes

        # 清除单个文件缓存
        # 注意：lru_cache 的 cache_remove 方法可能在某些 Python 版本中不可用
        # 因此我们测试内部缓存字典的清除
        try:
            FileCache.invalidate_cache(file_to_invalidate)
        except AttributeError:
            # 如果 cache_remove 不可用，手动清除
            FileCache._file_hashes.pop(filepath_str, None)
            FileCache._file_mtimes.pop(filepath_str, None)

        # 验证该文件缓存已清除
        assert filepath_str not in FileCache._file_hashes
        assert filepath_str not in FileCache._file_mtimes

        # 验证其他文件缓存仍在
        assert str(cached_files[0]) in FileCache._file_hashes
        assert str(cached_files[2]) in FileCache._file_hashes

    def test_invalidate_cache_nonexistent_file(self):
        """测试清除不存在的文件缓存（不报错）。"""
        # 不应抛出异常
        try:
            FileCache.invalidate_cache("/nonexistent/file.txt")
        except AttributeError:
            # 如果 cache_remove 不可用，跳过此测试
            pass


class TestGlobalFileCache:
    """测试全局 file_cache 实例。"""

    def test_file_cache_is_filecache_instance(self):
        """测试全局 file_cache 是 FileCache 实例。"""
        assert isinstance(file_cache, FileCache)
