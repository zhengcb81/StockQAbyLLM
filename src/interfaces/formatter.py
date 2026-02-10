"""输出格式化器接口。

该模块定义了输出格式化器的抽象接口。
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Type

from src.core.models import QABatchResult


class OutputFormatter(ABC):
    """输出格式化器抽象基类。

    所有输出格式化器都应该继承此类并实现 format 方法。
    """

    @abstractmethod
    def format(self, result: QABatchResult) -> str:
        """格式化输出结果。

        Args:
            result: 批量问答结果

        Returns:
            格式化后的字符串
        """
        pass

    @abstractmethod
    def get_file_extension(self) -> str:
        """获取输出文件扩展名。

        Returns:
            文件扩展名（例如：'.json', '.yaml'）
        """
        pass

    @abstractmethod
    def get_format_name(self) -> str:
        """获取格式名称。

        Returns:
            格式名称（例如：'JSON', 'YAML'）
        """
        pass


class JSONFormatter(OutputFormatter):
    """JSON 格式化器。"""

    def __init__(self, indent: int = 4, ensure_ascii: bool = False):
        """初始化 JSON 格式化器。

        Args:
            indent: 缩进空格数
            ensure_ascii: 是否确保 ASCII 编码
        """
        self.indent = indent
        self.ensure_ascii = ensure_ascii

    def format(self, result: QABatchResult) -> str:
        """格式化为 JSON。

        Args:
            result: 批量问答结果

        Returns:
            JSON 格式的字符串
        """
        import json

        return json.dumps(
            result.to_dict(),
            ensure_ascii=self.ensure_ascii,
            indent=self.indent,
        )

    def get_file_extension(self) -> str:
        """获取文件扩展名。"""
        return ".json"

    def get_format_name(self) -> str:
        """获取格式名称。"""
        return "JSON"


class YAMLFormatter(OutputFormatter):
    """YAML 格式化器。"""

    def __init__(self, default_flow_style: bool = False):
        """初始化 YAML 格式化器。

        Args:
            default_flow_style: 是否使用流式风格
        """
        self.default_flow_style = default_flow_style

    def format(self, result: QABatchResult) -> str:
        """格式化为 YAML。

        Args:
            result: 批量问答结果

        Returns:
            YAML 格式的字符串

        Raises:
            ImportError: 如果 PyYAML 未安装
        """
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError("PyYAML 未安装，请运行: pip install pyyaml") from exc

        return str(
            yaml.dump(
                result.to_dict(),
                allow_unicode=True,
                default_flow_style=self.default_flow_style,
            )
        )

    def get_file_extension(self) -> str:
        """获取文件扩展名。"""
        return ".yaml"

    def get_format_name(self) -> str:
        """获取格式名称。"""
        return "YAML"


class CSVFormatter(OutputFormatter):
    """CSV 格式化器。"""

    def __init__(self, include_header: bool = True):
        """初始化 CSV 格式化器。

        Args:
            include_header: 是否包含表头
        """
        self.include_header = include_header

    def format(self, result: QABatchResult) -> str:
        """格式化为 CSV。

        Args:
            result: 批量问答结果

        Returns:
            CSV 格式的字符串
        """
        import csv
        import io

        output = io.StringIO()
        writer = csv.writer(output)

        if self.include_header:
            writer.writerow(["Question", "Score", "Description", "Source"])

        for qa_result in result.results:
            question = str(qa_result.question)
            answer = qa_result.answer
            writer.writerow([question, answer.score, answer.text, answer.source])

        return output.getvalue()

    def get_file_extension(self) -> str:
        """获取文件扩展名。"""
        return ".csv"

    def get_format_name(self) -> str:
        """获取格式名称。"""
        return "CSV"


class FormatterFactory:
    """格式化器工厂。

    用于创建不同格式的格式化器。
    """

    _formatters: Dict[str, Type[OutputFormatter]] = {
        "json": JSONFormatter,
        "yaml": YAMLFormatter,
        "csv": CSVFormatter,
    }

    @classmethod
    def create(cls, format_name: str, **kwargs: Any) -> OutputFormatter:
        """创建格式化器。

        Args:
            format_name: 格式名称（json, yaml, csv）
            **kwargs: 传递给格式化器的参数

        Returns:
            格式化器实例

        Raises:
            ValueError: 如果格式名称无效
        """
        format_name_lower = format_name.lower()
        if format_name_lower not in cls._formatters:
            raise ValueError(
                f"不支持的格式: {format_name}。支持的格式: {list(cls._formatters.keys())}"
            )

        formatter_class = cls._formatters[format_name_lower]
        return formatter_class(**kwargs)

    @classmethod
    def get_supported_formats(cls) -> List[str]:
        """获取支持的格式列表。

        Returns:
            支持的格式名称列表
        """
        return list(cls._formatters.keys())

    @classmethod
    def register_formatter(cls, format_name: str, formatter_class: Type[OutputFormatter]) -> None:
        """注册自定义格式化器。

        Args:
            format_name: 格式名称
            formatter_class: 格式化器类
        """
        cls._formatters[format_name.lower()] = formatter_class
