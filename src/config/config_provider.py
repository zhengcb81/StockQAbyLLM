"""配置提供者抽象基类。

该模块定义了配置管理器的统一接口，支持不同的配置文件格式。
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import List

from ..core.exceptions import ConfigError


class ConfigProvider(ABC):
    """配置提供者抽象基类。

    定义了配置管理器的统一接口，支持不同的配置文件格式。
    """

    def __init__(self, config_path: str):
        """初始化配置提供者。

        Args:
            config_path: 配置文件的路径
        """
        self.config_path = Path(config_path)
        self._questions: List[str] = []

    @abstractmethod
    def load_questions(self) -> List[str]:
        """从配置文件加载问题列表。

        Returns:
            问题文本列表

        Raises:
            具体的配置加载异常
        """
        pass

    @abstractmethod
    def validate_questions(self, questions: List[str]) -> bool:
        """验证问题列表。

        Args:
            questions: 要验证的问题列表

        Returns:
            验证通过返回 True

        Raises:
            具体的验证异常
        """
        pass

    def get_questions(self) -> List[str]:
        """获取已加载的问题列表。

        Returns:
            问题文本列表

        Raises:
            ConfigError: 如果尚未加载配置
        """
        if not self._questions:
            raise ConfigError("配置尚未加载，请先调用 load_questions()")
        return self._questions

    def get_question_count(self) -> int:
        """获取问题数量。

        Returns:
            问题数量
        """
        return len(self._questions)

    def __repr__(self) -> str:
        """返回配置提供者的字符串表示。"""
        return (
            f"{self.__class__.__name__}(path='{self.config_path}', "
            f"questions={len(self._questions)})"
        )


def create_config_provider(config_path: str) -> ConfigProvider:
    """根据配置文件自动创建合适的配置提供者。

    支持自动检测文件格式：
    1. .json 扩展名 → JSONConfigManager
    2. .txt 或其他扩展名 → ConfigManager
    3. 也可以根据文件内容自动检测

    Args:
        config_path: 配置文件路径

    Returns:
        合适的配置提供者实例
    """
    path = Path(config_path)

    # 基于文件扩展名的检测
    if path.suffix.lower() == ".json":
        from .json_config_manager import JSONConfigManager

        return JSONConfigManager(config_path)
    else:
        # 默认使用文本配置管理器
        from .config_manager import ConfigManager

        return ConfigManager(config_path)
