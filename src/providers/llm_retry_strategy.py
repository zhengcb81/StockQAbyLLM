#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 重试策略。

定义 LLM 请求的重试逻辑和退避算法。
"""

import random
import time
from typing import Any, Callable, Tuple, TypeVar, cast

from src.config.settings import DEFAULT_RETRY_DELAY
from src.utils.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


class LLMRetryStrategy:
    """LLM 重试策略类。"""

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = DEFAULT_RETRY_DELAY,
        exponential: bool = True,
        jitter: bool = True,
    ):
        """初始化重试策略。

        Args:
            max_retries: 最大重试次数
            base_delay: 基础延迟时间（秒）
            exponential: 是否使用指数退避
            jitter: 是否添加随机抖动
        """
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.exponential = exponential
        self.jitter = jitter

    def get_wait_time(self, retry_count: int) -> float:
        """计算等待时间。

        Args:
            retry_count: 当前已重试次数（从0开始）

        Returns:
            等待秒数
        """
        wait_time: float
        if self.exponential:
            # 指数退避: base * 2^retry
            wait_time = self.base_delay * (2**retry_count)
        else:
            # 线性退避
            wait_time = self.base_delay * (retry_count + 1)

        if self.jitter:
            # 添加 0-10% 的随机抖动（非加密用途，仅用于避免重试冲突）
            jitter = random.uniform(0, 0.1)  # nosec B311
            wait_time += wait_time * jitter

        return wait_time

    def should_retry(self, retry_count: int, exception: Exception) -> bool:
        """判断是否应该重试。

        Args:
            retry_count: 当前已重试次数
            exception: 发生的异常

        Returns:
            是否重试
        """
        if retry_count >= self.max_retries - 1:
            return False

        # 这里可以根据异常类型细化逻辑
        # 例如：对于 401 Unauthorized 不应该重试
        return True
