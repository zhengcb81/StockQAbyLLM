"""日志配置工具。

该模块提供统一的日志配置和获取功能。
支持文件和控制台双输出，使用结构化格式。
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

# 日志目录
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)


def get_logger(
    name: str,
    level: int = logging.INFO,
    log_to_file: bool = True,
    log_to_console: bool = True,
    verbose: bool = False,
) -> logging.Logger:
    """获取配置好的日志记录器。

    Args:
        name: 日志记录器名称，通常使用 __name__
        level: 日志级别（默认为 INFO）
        log_to_file: 是否输出到文件（默认为 True）
        log_to_console: 是否输出到控制台（默认为 True）
        verbose: 是否启用详细模式（DEBUG 级别）

    Returns:
        配置好的日志记录器实例

    Example:
        >>> logger = get_logger(__name__)
        >>> logger.info("处理开始")
        >>> logger.error("处理失败")
    """
    # 如果启用详细模式，设置为 DEBUG 级别
    if verbose:
        level = logging.DEBUG

    # 创建或获取日志记录器
    logger = logging.getLogger(name)

    # 如果已经配置过，直接返回
    if logger.handlers:
        return logger

    logger.setLevel(level)
    logger.propagate = False  # 不传播到父日志记录器

    # 定义日志格式
    file_formatter = logging.Formatter(
        fmt="%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_formatter = logging.Formatter(fmt="%(levelname)s - %(message)s", datefmt="%H:%M:%S")

    # 添加文件处理器
    if log_to_file:
        log_file = LOG_DIR / f"stock_qa_{datetime.now().strftime('%Y%m%d')}.log"
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)

    # 添加控制台处理器
    if log_to_console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)

    return logger


def set_log_level(logger: logging.Logger, level: int) -> None:
    """动态设置日志级别。

    Args:
        logger: 日志记录器实例
        level: 新的日志级别
    """
    logger.setLevel(level)
    for handler in logger.handlers:
        handler.setLevel(level)


def close_logger(logger: logging.Logger) -> None:
    """关闭日志记录器及其所有处理器。

    Args:
        logger: 要关闭的日志记录器
    """
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)
