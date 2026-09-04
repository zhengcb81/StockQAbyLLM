#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安全工具模块。

提供文件名清洗、输入验证和速率限制等安全相关功能。
"""

import re
import time
from pathlib import Path
from threading import Lock
from typing import Any, Optional

from src.utils.logger import get_logger

logger = get_logger(__name__)


# ============================================================================
# 文件名清洗 - 防止路径遍历攻击
# ============================================================================

_DANGEROUS_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
_PATH_TRAVERSAL = re.compile(r"\.\.|[\\/]")
_DANGEROUS_EXTENSIONS = {".exe", ".bat", ".cmd", ".scr", ".pif", ".com", ".vbs", ".js", ".jar"}


def sanitize_filename(filename: str, max_length: int = 255) -> str:
    """清洗文件名，防止路径遍历和文件系统攻击。

    Args:
        filename: 原始文件名
        max_length: 最大长度限制（默认255，适用于大多数文件系统）

    Returns:
        清洗后的安全文件名

    Raises:
        ValueError: 如果文件名包含危险字符或为空

    Examples:
        >>> sanitize_filename("test.txt")
        'test.txt'
        >>> sanitize_filename("../etc/passwd")
        'etcpasswd'
        >>> sanitize_filename("file<script>.txt")
        'file.txt'
    """
    if not filename:
        raise ValueError("文件名不能为空")

    # 移除路径分隔符和路径遍历序列
    # 只取文件名部分，忽略任何目录路径
    clean_name = Path(filename).name

    # 如果清洗后为空，使用默认名称
    if not clean_name or clean_name == ".":
        clean_name = "unnamed"

    # 移除危险字符
    clean_name = _DANGEROUS_CHARS.sub("", clean_name)

    # 移除路径遍历模式
    clean_name = _PATH_TRAVERSAL.sub("", clean_name)

    # 移除控制字符
    clean_name = "".join(char for char in clean_name if ord(char) >= 32)

    # 限制长度
    if len(clean_name) > max_length:
        clean_name = clean_name[:max_length]
        logger.warning("文件名超过最大长度，已截断: %s", filename)

    # 检查是否为空
    if not clean_name:
        raise ValueError(f"文件名 '{filename}' 包含非法字符，清洗后为空")

    # 检查危险扩展名
    name_lower = clean_name.lower()
    for ext in _DANGEROUS_EXTENSIONS:
        if name_lower.endswith(ext):
            logger.warning("文件名包含潜在危险扩展名: %s", ext)
            # 保留文件名但更改扩展名为 .txt
            clean_name = str(Path(clean_name).with_suffix(".txt"))

    logger.debug("文件名已清洗: %s -> %s", filename, clean_name)
    return clean_name


def sanitize_path(path_str: str, base_dir: Optional[str] = None) -> str:
    """清洗并验证路径，确保不逃逸基础目录。

    Args:
        path_str: 输入路径字符串
        base_dir: 基础目录路径（如果提供，确保结果路径在此目录下）

    Returns:
        安全的绝对路径

    Raises:
        ValueError: 如果路径尝试逃逸基础目录
    """
    # 先清洗文件名部分
    path_obj = Path(path_str)

    # 清洗路径中的每个组件
    clean_components = []
    for component in path_obj.parts:
        try:
            clean_component = sanitize_filename(component)
            clean_components.append(clean_component)
        except ValueError:
            # 跳过非法组件
            continue

    if not clean_components:
        clean_components = ["unnamed"]

    clean_path = Path(*clean_components)

    # 如果提供了基础目录，确保路径在其下
    if base_dir:
        base = Path(base_dir).resolve()
        try:
            resolved = (base / clean_path).resolve()
            # 验证是否在基础目录下
            resolved.relative_to(base)
            return str(resolved)
        except ValueError:
            raise ValueError(f"路径 '{path_str}' 尝试逃逸基础目录 '{base_dir}'")

    return str(clean_path.resolve())


# ============================================================================
# 输入验证 - 防止过载攻击
# ============================================================================

MAX_QUESTION_LENGTH = 10000  # 最大问题长度（字符数）
MAX_ANSWER_LENGTH = 50000  # 最大答案长度（字符数）


def validate_question(question: str, max_length: int = MAX_QUESTION_LENGTH) -> None:
    """验证问题输入是否合法。

    Args:
        question: 问题文本
        max_length: 最大允许长度

    Raises:
        ValueError: 如果问题过长或为空
    """
    if not question or not question.strip():
        raise ValueError("问题不能为空")

    if len(question) > max_length:
        raise ValueError(f"问题过长: {len(question)} 字符，最大允许 {max_length} 字符")

    # 检查是否只包含空白字符
    if not question.strip():
        raise ValueError("问题不能只包含空白字符")

    logger.debug("问题验证通过: 长度=%d", len(question))


def validate_answer(answer: str, max_length: int = MAX_ANSWER_LENGTH) -> None:
    """验证答案是否合法。

    Args:
        answer: 答案文本
        max_length: 最大允许长度

    Raises:
        ValueError: 如果答案过长
    """
    if not answer:
        raise ValueError("答案不能为空")

    if len(answer) > max_length:
        raise ValueError(f"答案过长: {len(answer)} 字符，最大允许 {max_length} 字符")

    logger.debug("答案验证通过: 长度=%d", len(answer))


# ============================================================================
# 速率限制器 - 防止 API 滥用
# ============================================================================


class RateLimiter:
    """基于令牌桶算法的速率限制器。

    用于防止 API 调用过于频繁，避免配额耗尽或被封禁。

    Attributes:
        rate: 每秒允许的请求数
        capacity: 桶容量（最大突发请求数）
    """

    def __init__(self, rate: float, capacity: int = 10):
        """初始化速率限制器。

        Args:
            rate: 每秒允许的请求数（例如：2.0 表示每秒2个请求）
            capacity: 令牌桶容量（默认10），允许突发请求
        """
        self.rate = rate
        self.capacity = capacity
        self._tokens = float(capacity)
        self._last_update = time.time()
        self._lock = Lock()
        logger.debug("速率限制器已初始化: rate=%f/s, capacity=%d", rate, capacity)

    def acquire(self, tokens: int = 1, timeout: Optional[float] = None) -> bool:
        """获取令牌。

        Args:
            tokens: 需要的令牌数量（默认1）
            timeout: 最大等待时间（秒），None 表示不等待

        Returns:
            是否成功获取令牌

        Examples:
            >>> limiter = RateLimiter(rate=2.0)  # 每秒2个请求
            >>> limiter.acquire()  # 获取1个令牌
            True
            >>> limiter.acquire(tokens=5)  # 需要5个令牌
            False  # 可能失败，取决于当前可用令牌
        """
        with self._lock:
            now = time.time()

            # 计算新增令牌
            elapsed = now - self._last_update
            new_tokens = elapsed * self.rate
            self._tokens = min(self.capacity, self._tokens + new_tokens)
            self._last_update = now

            # 检查是否有足够令牌
            if self._tokens >= tokens:
                self._tokens -= tokens
                logger.debug("获取令牌成功: 需要=%d, 剩余=%.2f", tokens, self._tokens)
                return True

            logger.debug("获取令牌失败: 需要=%d, 可用=%.2f", tokens, self._tokens)
            return False

    def wait_for_token(self, tokens: int = 1) -> None:
        """等待直到可以获取令牌。

        会阻塞当前线程直到有足够的令牌可用。

        Args:
            tokens: 需要的令牌数量（默认1）
        """
        while not self.acquire(tokens):
            # 计算需要等待的时间
            needed = tokens - self._tokens
            wait_time = needed / self.rate
            logger.debug("等待令牌: 需要等待 %.2f 秒", wait_time)
            time.sleep(wait_time)

    def get_available_tokens(self) -> float:
        """获取当前可用令牌数。

        Returns:
            可用令牌数量
        """
        with self._lock:
            now = time.time()
            elapsed = now - self._last_update
            new_tokens = elapsed * self.rate
            return min(self.capacity, self._tokens + new_tokens)


# ============================================================================
# API 密钥安全
# ============================================================================


def mask_api_key(api_key: str, visible_chars: int = 4) -> str:
    """遮蔽 API 密钥用于日志输出。

    Args:
        api_key: 原始 API 密钥
        visible_chars: 显示的字符数（从开头）

    Returns:
        遮蔽后的密钥字符串

    Examples:
        >>> mask_api_key("sk-1234567890abcdef")
        'sk-12****'
    """
    if not api_key:
        return "***"

    if len(api_key) <= visible_chars:
        return api_key[0] + "*" * (len(api_key) - 1)

    return api_key[:visible_chars] + "*" * min(8, len(api_key) - visible_chars)


# ============================================================================
# JSON Schema 验证辅助
# ============================================================================


def validate_json_structure(data: dict[str, Any], required_keys: list[str]) -> None:
    """验证 JSON 数据结构是否包含必需的键。

    Args:
        data: 要验证的字典数据
        required_keys: 必需的键列表

    Raises:
        ValueError: 如果缺少必需的键
    """
    missing_keys = [key for key in required_keys if key not in data]
    if missing_keys:
        raise ValueError(f"JSON 数据缺少必需的键: {missing_keys}")

    logger.debug("JSON 结构验证通过: 所有必需键都存在")
