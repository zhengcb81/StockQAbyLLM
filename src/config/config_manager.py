"""配置管理模块。

该模块负责加载和验证配置文件。
支持从文本文件读取问题列表，并进行数据验证。
"""

from typing import List

from ..core.exceptions import ConfigError, EmptyConfigError, ProjectFileNotFoundError
from ..utils.cache import file_cache
from ..utils.logger import get_logger
from .config_provider import ConfigProvider

logger = get_logger(__name__)


class ConfigManager(ConfigProvider):
    """配置管理器。

    负责从配置文件加载问题列表，并进行验证和错误处理。
    """

    def load_questions(self) -> List[str]:
        """从配置文件加载问题列表。

        Returns:
            问题文本列表

        Raises:
            ProjectFileNotFoundError: 配置文件不存在
            EmptyConfigError: 配置文件为空
            ConfigError: 配置文件读取或解析失败
        """
        logger.info("正在加载配置文件: %s", self.config_path)

        # 检查文件是否存在
        if not self.config_path.exists():
            logger.error("配置文件不存在: %s", self.config_path)
            raise ProjectFileNotFoundError(str(self.config_path))

        # 检查是否为文件
        if not self.config_path.is_file():
            logger.error("路径不是文件: %s", self.config_path)
            raise ConfigError(f"路径不是文件: {self.config_path}")

        try:
            # 读取文件内容（使用缓存）
            content = file_cache.read_file_cached(self.config_path)
            lines = content.splitlines(keepends=True)

            # 解析问题列表
            questions = [line.strip() for line in lines if line.strip()]

            # 检查是否为空
            if not questions:
                logger.warning("配置文件为空或只包含空白行: %s", self.config_path)
                raise EmptyConfigError(str(self.config_path))

            # 保存问题列表
            self._questions = questions
            logger.info(f"成功加载 {len(questions)} 个问题")

            return questions

        except EmptyConfigError:
            raise
        except ProjectFileNotFoundError:
            raise
        except UnicodeDecodeError as e:
            logger.error("文件编码错误: %s - %s", self.config_path, e)
            raise ConfigError(
                message=f"文件编码错误，请确保文件为 UTF-8 编码",
                file_path=str(self.config_path),
                details={"encoding_error": str(e)},
            )
        except PermissionError as e:
            logger.error("文件权限错误: %s - %s", self.config_path, e)
            raise ConfigError(
                message=f"无权限读取配置文件",
                file_path=str(self.config_path),
                details={"permission_error": str(e)},
            )
        except OSError as e:
            logger.error("读取配置文件时发生未知错误: %s - %s", self.config_path, e)
            raise ConfigError(
                message=f"读取配置文件失败: {str(e)}", file_path=str(self.config_path)
            )

    def __repr__(self) -> str:
        """返回配置管理器的字符串表示。"""
        return f"ConfigManager(path='{self.config_path}', questions={len(self._questions)})"
