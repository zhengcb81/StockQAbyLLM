#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文件缓存模块。

提供文件内容缓存功能，减少重复文件 I/O 操作。
"""

import hashlib
import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional, Union


class FileCache:
    """文件内容缓存类。

    提供基于内存的文件内容缓存，支持缓存失效机制。
    """

    # 文件修改时间缓存，用于检测文件变更
    _file_mtimes: Dict[str, float] = {}
    # 文件哈希缓存，用于内容变更检测
    _file_hashes: Dict[str, str] = {}

    @staticmethod
    @lru_cache(maxsize=32)
    def read_file_cached(filepath: Union[str, Path]) -> str:
        """读取文件内容，使用 LRU 缓存。

        Args:
            filepath: 文件路径

        Returns:
            文件内容字符串

        Raises:
            FileNotFoundError: 文件不存在
            IOError: 文件读取错误
        """
        filepath_str = str(filepath)
        with open(filepath_str, "r", encoding="utf-8") as f:
            content = f.read()

        # 更新文件修改时间缓存
        FileCache._file_mtimes[filepath_str] = os.path.getmtime(filepath_str)
        # 更新文件哈希缓存
        FileCache._file_hashes[filepath_str] = hashlib.md5(
            content.encode(), usedforsecurity=False
        ).hexdigest()

        return content

    @staticmethod
    def clear_cache() -> None:
        """清除所有缓存。"""
        FileCache.read_file_cached.cache_clear()
        FileCache._file_mtimes.clear()
        FileCache._file_hashes.clear()

    @staticmethod
    def is_file_modified(filepath: Union[str, Path]) -> bool:
        """检查文件是否自上次缓存后被修改。

        Args:
            filepath: 文件路径

        Returns:
            True 如果文件被修改，否则 False
        """
        filepath_str = str(filepath)

        # 如果文件不在缓存中，视为已修改
        if filepath_str not in FileCache._file_mtimes:
            return True

        try:
            current_mtime = os.path.getmtime(filepath_str)
            cached_mtime = FileCache._file_mtimes[filepath_str]
            return current_mtime != cached_mtime
        except (OSError, FileNotFoundError):
            # 文件可能被删除，视为已修改
            return True

    @staticmethod
    def get_file_hash(filepath: Union[str, Path]) -> str:
        """计算文件内容的哈希值。

        Args:
            filepath: 文件路径

        Returns:
            MD5 哈希字符串
        """
        filepath_str = str(filepath)

        # 如果已有缓存哈希且文件未修改，返回缓存哈希
        if not FileCache.is_file_modified(filepath_str):
            return FileCache._file_hashes.get(filepath_str, "")

        # 计算新哈希
        try:
            with open(filepath_str, "r", encoding="utf-8") as f:
                content = f.read()
            file_hash = hashlib.md5(content.encode(), usedforsecurity=False).hexdigest()

            # 更新缓存
            FileCache._file_hashes[filepath_str] = file_hash
            FileCache._file_mtimes[filepath_str] = os.path.getmtime(filepath_str)

            return file_hash
        except (OSError, FileNotFoundError) as e:
            raise ValueError(f"无法读取文件以计算哈希: {filepath_str}") from e

    @staticmethod
    def read_json_cached(filepath: Union[str, Path]) -> Any:
        """读取 JSON 文件并缓存内容。

        Args:
            filepath: JSON 文件路径

        Returns:
            解析后的 JSON 数据

        Raises:
            json.JSONDecodeError: JSON 解析错误
        """
        import json

        content = FileCache.read_file_cached(filepath)
        return json.loads(content)

    @staticmethod
    def invalidate_cache(filepath: Optional[Union[str, Path]] = None) -> None:
        """使缓存失效。

        Args:
            filepath: 可选的文件路径，如果为 None 则清除所有缓存
        """
        if filepath is None:
            FileCache.clear_cache()
        else:
            filepath_str = str(filepath)
            # 从 LRU 缓存中删除特定文件
            try:
                FileCache.read_file_cached.cache_remove(filepath_str)  # type: ignore[attr-defined]
            except KeyError:
                pass  # 文件不在缓存中

            # 从内部缓存中删除
            FileCache._file_mtimes.pop(filepath_str, None)
            FileCache._file_hashes.pop(filepath_str, None)


# 全局缓存实例
file_cache = FileCache()
