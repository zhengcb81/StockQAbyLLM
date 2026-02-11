#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""进度报告器接口。

定义进度报告器的抽象基类，用于解耦核心逻辑与 UI/控制台输出。
"""

from abc import ABC, abstractmethod


class ProgressReporter(ABC):
    """进度报告器抽象基类。"""

    @abstractmethod
    def start_batch(self, total_count: int, message: str = "") -> None:
        """开始批处理。"""
        pass

    @abstractmethod
    def update_progress(self, current_idx: int, total_count: int, message: str = "") -> None:
        """更新进度。"""
        pass

    @abstractmethod
    def finish_batch(self, message: str = "") -> None:
        """完成批处理。"""
        pass

    @abstractmethod
    def log_message(self, message: str, level: str = "info") -> None:
        """记录通用消息。"""
        pass
