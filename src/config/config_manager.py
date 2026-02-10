"""配置管理模块。

该模块负责加载和验证配置文件。
支持从文本文件读取问题列表，并进行数据验证。
"""

from pathlib import Path
from typing import List

from ..core.exceptions import ConfigError, EmptyConfigError, FileNotFoundError
from ..utils.logger import get_logger
from ..utils.cache import file_cache
from .config_provider import ConfigProvider

logger = get_logger(__name__)


class ConfigManager(ConfigProvider):
    """配置管理器。

    负责从配置文件加载问题列表，并进行验证和错误处理。
    """

    def __init__(self, config_path: str):
        """初始化配置管理器。

        Args:
            config_path: 配置文件的路径
        """
        super().__init__(config_path)

    def load_questions(self) -> List[str]:
        """从配置文件加载问题列表。

        Returns:
            问题文本列表

        Raises:
            FileNotFoundError: 配置文件不存在
            EmptyConfigError: 配置文件为空
            ConfigError: 配置文件读取或解析失败
        """
        logger.info(f"正在加载配置文件: {self.config_path}")

        # 检查文件是否存在
        if not self.config_path.exists():
            logger.error(f"配置文件不存在: {self.config_path}")
            raise FileNotFoundError(str(self.config_path))

        # 检查是否为文件
        if not self.config_path.is_file():
            logger.error(f"路径不是文件: {self.config_path}")
            raise ConfigError(f"路径不是文件: {self.config_path}")

        try:
            # 读取文件内容（使用缓存）
            content = file_cache.read_file_cached(self.config_path)
            lines = content.splitlines(keepends=True)

            # 解析问题列表
            questions = [line.strip() for line in lines if line.strip()]

            # 检查是否为空
            if not questions:
                logger.warning(f"配置文件为空或只包含空白行: {self.config_path}")
                raise EmptyConfigError(str(self.config_path))

            # 保存问题列表
            self._questions = questions
            logger.info(f"成功加载 {len(questions)} 个问题")

            return questions

        except EmptyConfigError:
            raise
        except FileNotFoundError:
            raise
        except UnicodeDecodeError as e:
            logger.error(f"文件编码错误: {self.config_path} - {e}")
            raise ConfigError(
                message=f"文件编码错误，请确保文件为 UTF-8 编码",
                file_path=str(self.config_path),
                details={"encoding_error": str(e)},
            )
        except PermissionError as e:
            logger.error(f"文件权限错误: {self.config_path} - {e}")
            raise ConfigError(
                message=f"无权限读取配置文件",
                file_path=str(self.config_path),
                details={"permission_error": str(e)},
            )
        except OSError as e:
            logger.error(f"读取配置文件时发生未知错误: {self.config_path} - {e}")
            raise ConfigError(
                message=f"读取配置文件失败: {str(e)}", file_path=str(self.config_path)
            )

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
                    details={"question_index": i, "question_type": type(question).__name__},
                )

            if not question.strip():
                raise ConfigError(message=f"问题 {i} 为空", details={"question_index": i})

            if len(question) > 1000:
                logger.warning(f"问题 {i} 长度超过 1000 字符")

        logger.info(f"所有 {len(questions)} 个问题验证通过")
        return True

    def __repr__(self) -> str:
        """返回配置管理器的字符串表示。"""
        return f"ConfigManager(path='{self.config_path}', questions={len(self._questions)})"
