#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""控制台进度报告器。

实现 ProgressReporter 接口，将进度输出到控制台。
"""

from src.interfaces.progress_reporter import ProgressReporter
from src.config.settings import DISPLAY_LINE_WIDTH

class ConsoleReporter(ProgressReporter):
    """控制台进度报告器实现。"""

    def start_batch(self, total_count: int, message: str = "") -> None:
        print(f"\n{'=' * DISPLAY_LINE_WIDTH}")
        if message:
            print(message)
        else:
            print(f"开始处理 {total_count} 个任务...")
        print(f"{'=' * DISPLAY_LINE_WIDTH}\n")

    def update_progress(self, current_idx: int, total_count: int, message: str = "") -> None:
        progress_pct = (current_idx) / total_count * 100
        print(
            f"\r[{current_idx}/{total_count}] 进度: {progress_pct:.1f}% - {message}",
            end="",
            flush=True,
        )

    def finish_batch(self, message: str = "") -> None:
        print(f"\n\n{'=' * DISPLAY_LINE_WIDTH}")
        if message:
            print(message)
        else:
            print("处理完成！")
        print(f"{'=' * DISPLAY_LINE_WIDTH}\n")

    def log_message(self, message: str, level: str = "info") -> None:
        # 记录消息，确保不破坏进度条
        print(f"\n[{level.upper()}] {message}")
