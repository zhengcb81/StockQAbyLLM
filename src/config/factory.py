#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置提供者工厂。

负责根据文件路径创建合适的配置提供者实例。
"""

from pathlib import Path
from .config_provider import ConfigProvider
from .config_manager import ConfigManager
from .json_config_manager import JSONConfigManager


def create_config_provider(config_path: str) -> ConfigProvider:
    """根据配置文件自动创建合适的配置提供者。

    支持自动检测文件格式：
    1. .json 扩展名 → JSONConfigManager
    2. .txt 或其他扩展名 → ConfigManager

    Args:
        config_path: 配置文件路径

    Returns:
        合适的配置提供者实例
    """
    path = Path(config_path)

    # 基于文件扩展名的检测
    if path.suffix.lower() == ".json":
        return JSONConfigManager(config_path)

    # 默认使用文本配置管理器
    return ConfigManager(config_path)
