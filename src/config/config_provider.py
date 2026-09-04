"""配置提供者抽象基类。

该模块定义了配置管理器的统一接口，支持不同的配置文件格式。
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import List

from ..core.exceptions import ConfigError
from .settings import MAX_QUESTION_LENGTH


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

    def validate_questions(self, questions: List[str]) -> bool:
        """验证问题列表。

        Args:
            questions: 要验证的问题列表

        Returns:
            验证通过返回 True

        Raises:
            ConfigError: 验证失败
        """
        if not questions:
            raise ConfigError("问题列表不能为空")

        if not isinstance(questions, list):
            raise ConfigError("问题必须是列表类型")

        for i, question in enumerate(questions, 1):
            if not isinstance(question, str):
                raise ConfigError(
                    message=f"问题 {i} 不是字符串类型",
                    details={
                        "question_index": i,
                        "question_type": type(question).__name__,
                    },
                )

            if not question.strip():
                raise ConfigError(message=f"问题 {i} 为空", details={"question_index": i})

            if len(question) > MAX_QUESTION_LENGTH:
                import logging

                logging.getLogger(__name__).warning(
                    "问题 %d 长度超过 %d 字符", i, MAX_QUESTION_LENGTH
                )

        return True

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
