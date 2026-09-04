"""JSON配置管理模块。

该模块负责从JSON配置文件加载问题列表。
支持从JSON格式的配置文件中提取所有questions列表中的问题。
"""

import json
from typing import Any, Dict, List

from ..core.exceptions import ConfigError, EmptyConfigError, ProjectFileNotFoundError
from ..utils.cache import file_cache
from ..utils.logger import get_logger
from .config_provider import ConfigProvider

logger = get_logger(__name__)


class JSONConfigManager(ConfigProvider):
    """JSON配置管理器。

    负责从JSON配置文件加载问题列表，并进行验证和错误处理。
    从所有categories的questions列表中提取问题。
    """

    def load_questions(self) -> List[str]:
        """从JSON配置文件加载问题列表。

        Returns:
            问题文本列表

        Raises:
            ProjectFileNotFoundError: 配置文件不存在
            EmptyConfigError: 配置文件为空或没有问题
            ConfigError: 配置文件读取或解析失败
        """
        logger.info("正在加载JSON配置文件: %s", self.config_path)

        # 检查文件是否存在
        if not self.config_path.exists():
            logger.error("配置文件不存在: %s", self.config_path)
            raise ProjectFileNotFoundError(str(self.config_path))

        # 检查是否为文件
        if not self.config_path.is_file():
            logger.error("路径不是文件: %s", self.config_path)
            raise ConfigError(f"路径不是文件: {self.config_path}")

        try:
            # 读取JSON文件内容（使用缓存）
            config_data = file_cache.read_json_cached(self.config_path)

            # 解析问题列表
            questions = self._extract_questions_from_json(config_data)

            # 检查是否为空
            if not questions:
                logger.warning("配置文件中没有找到问题: %s", self.config_path)
                raise EmptyConfigError(str(self.config_path))

            # 保存问题列表
            self._questions = questions
            logger.info(f"成功从JSON配置加载 {len(questions)} 个问题")

            return questions

        except json.JSONDecodeError as e:
            logger.error("JSON解析错误: %s - %s", self.config_path, e)
            raise ConfigError(
                message=f"JSON格式错误，请检查文件语法",
                file_path=str(self.config_path),
                details={"json_error": str(e)},
            )
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
            logger.error("读取JSON配置文件时发生未知错误: %s - %s", self.config_path, e)
            raise ConfigError(
                message=f"读取配置文件失败: {str(e)}", file_path=str(self.config_path)
            )

    def _extract_questions_from_json(self, config_data: Any) -> List[str]:
        """从JSON数据中提取所有问题。

        Args:
            config_data: JSON配置数据

        Returns:
            问题列表

        Raises:
            ConfigError: 数据格式不符合预期
        """
        questions = []

        # 支持两种格式：
        # 1. 单个对象：{"category": "...", "questions": [...]}
        # 2. 对象数组：[{"category": "...", "questions": [...]}, ...]
        # 3. 包含categories字段的对象：{"categories": [...]}

        if isinstance(config_data, dict):
            # 格式3: {"categories": [...]}
            if "categories" in config_data:
                categories = config_data["categories"]
                if not isinstance(categories, list):
                    raise ConfigError("categories字段必须是数组")
                for category in categories:
                    if isinstance(category, dict) and "questions" in category:
                        questions.extend(self._extract_questions_from_category(category))

            # 格式1: 单个对象
            elif "questions" in config_data:
                questions.extend(self._extract_questions_from_category(config_data))

            else:
                raise ConfigError("JSON配置必须包含'questions'字段或'categories'字段")

        elif isinstance(config_data, list):
            # 格式2: 对象数组
            for item in config_data:
                if isinstance(item, dict) and "questions" in item:
                    questions.extend(self._extract_questions_from_category(item))
                else:
                    logger.warning("跳过无效的配置项: %s", item)

        else:
            raise ConfigError(f"不支持的JSON数据类型: {type(config_data)}")

        return questions

    def _extract_questions_from_category(self, category: Dict[str, Any]) -> List[str]:
        """从单个分类对象中提取问题。

        Args:
            category: 分类对象

        Returns:
            问题列表
        """
        questions: List[str] = []

        questions_data = category.get("questions", [])
        if not isinstance(questions_data, list):
            logger.warning(
                f"分类的questions字段不是数组，跳过: {category.get('category', 'unknown')}"
            )
            return questions

        for q in questions_data:
            if isinstance(q, str) and q.strip():
                questions.append(q.strip())
            else:
                logger.warning("跳过无效的问题: %s", q)

        return questions

    def __repr__(self) -> str:
        """返回配置管理器的字符串表示。"""
        return f"JSONConfigManager(path='{self.config_path}', questions={len(self._questions)})"
