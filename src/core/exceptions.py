"""自定义异常类。


该模块定义了 StockQAbyLLM 系统中使用的所有自定义异常。
所有异常都继承自基类 StockQAError，便于统一错误处理。
"""

from typing import Any, Optional


class StockQAError(Exception):
    """StockQAbyLLM 系统的基类异常。

    所有自定义异常都应该继承自此类。
    提供了统一的错误消息格式和上下文信息。
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        """初始化异常。

        Args:
            message: 错误消息
            details: 额外的错误详情（可选）
        """
        self.message = message
        self.details = details or {}
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        """格式化错误消息。

        Returns:
            格式化后的错误消息
        """
        msg = f"[{self.__class__.__name__}] {self.message}"
        if self.details:
            details_str = ", ".join(f"{k}={v}" for k, v in self.details.items())
            msg += f" ({details_str})"
        return msg


class ConfigError(StockQAError):
    """配置相关的错误。

    当配置文件加载、解析或验证失败时抛出。
    """

    def __init__(
        self,
        message: str,
        file_path: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ):
        """初始化配置错误。

        Args:
            message: 错误消息
            file_path: 配置文件路径（可选）
            details: 额外的错误详情（可选）
        """
        if details is None:
            details = {}
        if file_path:
            details["file"] = file_path
        super().__init__(message, details)


class ValidationError(StockQAError):
    """数据验证相关的错误。

    当输入数据验证失败时抛出。
    """

    def __init__(
        self, message: str, field: Optional[str] = None, details: Optional[dict[str, Any]] = None
    ):
        """初始化验证错误。

        Args:
            message: 错误消息
            field: 验证失败的字段名（可选）
            details: 额外的错误详情（可选）
        """
        if details is None:
            details = {}
        if field:
            details["field"] = field
        super().__init__(message, details)


class ProcessingError(StockQAError):
    """问题处理相关的错误。

    当问题处理或答案生成失败时抛出。
    """

    def __init__(
        self, message: str, question: Optional[str] = None, details: Optional[dict[str, Any]] = None
    ):
        """初始化处理错误。

        Args:
            message: 错误消息
            question: 相关的问题文本（可选）
            details: 额外的错误详情（可选）
        """
        if details is None:
            details = {}
        if question:
            details["question"] = question[:50] + "..." if len(question) > 50 else question
        super().__init__(message, details)


class FileNotFoundError(ConfigError):
    """配置文件未找到错误。

    当指定的配置文件不存在时抛出。
    """

    def __init__(self, file_path: str):
        """初始化文件未找到错误。

        Args:
            file_path: 未找到的文件路径
        """
        super().__init__(message=f"配置文件不存在: {file_path}", file_path=file_path)


class EmptyConfigError(ConfigError):
    """配置文件为空错误。

    当配置文件不包含任何有效数据时抛出。
    """

    def __init__(self, file_path: str):
        """初始化空配置错误。

        Args:
            file_path: 空配置文件的路径
        """
        super().__init__(message=f"配置文件为空或只包含空白行: {file_path}", file_path=file_path)


class OutputValidationError(StockQAError):
    """输出文件验证错误。

    当输出文件验证失败时抛出（例如：JSON 格式错误、文件损坏等）。
    """

    def __init__(
        self,
        message: str,
        output_path: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ):
        """初始化输出验证错误。

        Args:
            message: 错误消息
            output_path: 输出文件路径（可选）
            details: 额外的错误详情（可选）
        """
        if details is None:
            details = {}
        if output_path:
            details["output_file"] = output_path
        super().__init__(message, details)


class RepairError(StockQAError):
    """输出文件修复错误。

    当输出文件修复失败时抛出。
    """

    def __init__(
        self, message: str, question: Optional[str] = None, details: Optional[dict[str, Any]] = None
    ):
        """初始化修复错误。

        Args:
            message: 错误消息
            question: 相关的问题文本（可选）
            details: 额外的错误详情（可选）
        """
        if details is None:
            details = {}
        if question:
            short_q = question[:50] + "..." if len(question) > 50 else question
            details["question"] = short_q
        super().__init__(message, details)
