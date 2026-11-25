"""日志配置工具。

该模块提供统一的日志配置和获取功能。
支持文件和控制台双输出，使用结构化格式。

Q05（LLM-08）内容边界：所有处理器经过 :class:`ContentBoundaryFormatter`，
落盘/输出前对凭据类内容脱敏并限制单条记录长度；调用方只应传入允许的
结构化字段与短依据（问题/答案文本按既有约定截断）。
"""

import hashlib
import json
import logging
import re
import sqlite3
import sys
from collections import deque
from contextvars import ContextVar
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.utils.execution_feedback import warn_feedback_unavailable

_FAILURE_CONTEXT: ContextVar[dict[str, Any]] = ContextVar("stockqa_failure_context", default={})
_REFERENCE_FIELDS = frozenset(
    {
        "question_id",
        "work_item_id",
        "work_attempt_id",
        "attempt_id",
        "run_id",
        "invocation_id",
        "provider",
        "model",
    }
)


def _failure_references(values):
    return {
        key: value
        for key, value in values.items()
        if key in _REFERENCE_FIELDS
        and type(value) is str
        and re.fullmatch(r"[A-Za-z0-9_.:/@-]{1,200}", value)
    }


def configure_failure_diagnostics(*, output):
    """Associate this real CLI invocation with its existing output, not an HTTP attempt."""
    invocation = "cli-invocation-" + uuid4().hex
    context = {
        "invocation_id": invocation,
        "run_id": "cli-run-" + invocation.removeprefix("cli-invocation-"),
    }
    try:
        if output is not None:
            path = Path(output).absolute()
            context["run_id"] = (
                "cli-output-" + hashlib.sha256(str(path).encode("utf8")).hexdigest()[:24]
            )
    except Exception:
        # Output validation still belongs to the normal runner. Keep this
        # invocation observable under its independent ID.
        warn_feedback_unavailable("execution invocation output reference unavailable")
    return _FAILURE_CONTEXT.set(context)


def reset_failure_diagnostics(token):
    _FAILURE_CONTEXT.reset(token)


def build_failure_diagnostic(error, *, stage, references=None):
    """Inspect native exception slots. Never format messages, locals or arguments."""
    pending: deque[tuple[BaseException | None, str, int | None, int]] = deque(
        [(error, "raised", None, 0)]
    )
    seen = set()
    chain: list[dict[str, Any]] = []
    root_type = type(error).__name__
    deepest = -1
    while pending:
        current, relation, parent, depth = pending.popleft()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        kind = type(current)
        node: dict[str, Any] = {
            "type": kind.__name__,
            "module": kind.__module__,
            "relation": relation,
            "parent": parent,
            "frames": [],
        }
        traceback = BaseException.__dict__["__traceback__"].__get__(current)
        while traceback is not None:
            frame_code = traceback.tb_frame.f_code
            node["frames"].append(
                {
                    "file": Path(frame_code.co_filename).name,
                    "function": frame_code.co_name,
                    "line": traceback.tb_lineno,
                }
            )
            traceback = traceback.tb_next
        if (
            kind.__module__ == "src.utils.quick_scan_work_store"
            and kind.__name__ == "BudgetAdmissionError"
        ):
            fields = BaseException.__dict__["__dict__"].__get__(current)
            code = fields.get("public_code")
            if type(code) is str and re.fullmatch(r"[a-z][a-z0-9_]*", code):
                node["code"] = code
        # Bypass user-defined __getattribute__ and descriptors, and only consult
        # native instance data for the two owner-approved diagnostic vocabularies.
        if isinstance(current, sqlite3.Error) or (
            kind.__module__ == "src.utils.quick_scan_refresh_admission"
            and kind.__name__ == "AdmissionRejected"
        ):
            attributes = BaseException.__dict__["__dict__"].__get__(current)
            if isinstance(current, sqlite3.Error):
                code, name = attributes.get("sqlite_errorcode"), attributes.get("sqlite_errorname")
                if (
                    type(code) is int
                    and type(name) is str
                    and name.startswith("SQLITE_")
                    and getattr(sqlite3, name, None) == code
                ):
                    node.update(sqlite_errorcode=code, sqlite_errorname=name)
            else:
                reason = attributes.get("reason")
                code = reason.partition(":")[0] if type(reason) is str else ""
                if re.fullmatch(
                    r"(?:refresh_admission|refresh_execution|paid_recovery)_[a-z0-9_]+", code
                ):
                    node["code"] = code
        index = len(chain)
        chain.append(node)
        if depth > deepest:
            deepest, root_type = depth, node["type"]
        context = BaseException.__dict__["__context__"].__get__(current)
        cause = BaseException.__dict__["__cause__"].__get__(current)
        suppressed = BaseException.__dict__["__suppress_context__"].__get__(current)
        pending.append((cause, "cause", index, depth + 1))
        pending.append(
            (context, "suppressed_context" if suppressed else "context", index, depth + 1)
        )
    refs = _failure_references({**_FAILURE_CONTEXT.get(), **(references or {})})
    return {
        "error_type": type(error).__name__,
        "root_type": root_type,
        "stage": stage,
        "references": refs,
        "chain": chain,
    }


def _tracking_incomplete():
    warn_feedback_unavailable("execution failure tracking incomplete")


def record_execution_failure(
    logger, error, *, stage, references=None, refusal_code=None, explains_invocation=True
):
    """A defective observer cannot replace the business outcome or its cleanup."""
    try:
        if error is None:
            diagnostic = {
                "stage": stage,
                "outcome_source": "returned_status",
                "chain": [],
                "error_type": "WorkClaimRefused",
                "root_type": "WorkClaimRefused",
                "references": _failure_references({**_FAILURE_CONTEXT.get(), **(references or {})}),
            }
        else:
            diagnostic = build_failure_diagnostic(error, stage=stage, references=references)
    except Exception:
        _tracking_incomplete()
        diagnostic = {
            "error_type": type(error).__name__,
            "root_type": type(error).__name__,
            "stage": stage,
            "references": {},
            "chain": [],
            "tracking_incomplete": True,
        }
    if refusal_code is not None:
        diagnostic["refusal_code"] = refusal_code
    if _FAILURE_CONTEXT.get():
        diagnostic["entry"] = _FAILURE_CONTEXT.get().get("entry")
        diagnostic["options"] = _FAILURE_CONTEXT.get().get("options", [])
    if logger is not None:
        try:
            logger.error(
                "execution_failure %s",
                json.dumps(diagnostic, ensure_ascii=False, sort_keys=True),
                extra={"_stockqa_structural_failure": True},
            )
        except Exception:
            _tracking_incomplete()
    try:
        from src.utils.execution_feedback import emit_execution_observation

        context = _FAILURE_CONTEXT.get()
        if context and explains_invocation:
            context["has_failure"] = True
        emit_execution_observation(diagnostic, context=context)
    except Exception:
        _tracking_incomplete()
    return diagnostic


def observe_arguments(parser, argv=None):
    """Keep enough public argument shape to reproduce a refusal without a secret-bearing argv."""
    context = _FAILURE_CONTEXT.get()
    if not context:
        return
    args = sys.argv[1:] if argv is None else list(argv)
    try:
        shapes = []
        for index, argument in enumerate(args):
            name, joined, inline = argument.partition("=")
            action = parser._option_string_actions.get(name)
            if action is None:
                continue
            item = {"option": action.option_strings[0]}
            value = inline if joined else (args[index + 1] if index + 1 < len(args) else "")
            if action.dest in {"work_lease_seconds", "question_pack_size", "max_output_tokens"}:
                item["number"] = (
                    value
                    if re.fullmatch(r"[-+]?\d{1,10}(?:\.\d{1,6})?", value)
                    else "invalid_or_absent"
                )
            elif action.choices:
                item["choice"] = value if value in action.choices else "invalid_or_absent"
            elif action.dest in {
                "batch",
                "config",
                "question_manifest",
                "identity_snapshot",
                "policy",
                "request",
                "ledger",
            }:
                # Fingerprint the supplied role without opening a path (including remote UNC paths).
                if value and not value.startswith("--"):
                    item["input_ref"] = hashlib.sha256(value.encode("utf8")).hexdigest()
            shapes.append(item)
        context["options"] = shapes
    except Exception:
        _tracking_incomplete()


def observed_command(function):
    """Pair CLI exits and early argument failure with the invocation they belong to."""

    @wraps(function)
    def observed(*args, **kwargs):
        token = configure_failure_diagnostics(output=None)
        try:
            _FAILURE_CONTEXT.get()["entry"] = Path(function.__code__.co_filename).name
            try:
                result = function(*args, **kwargs)
            except SystemExit as error:
                if error.code not in (None, 0) and not _FAILURE_CONTEXT.get().get("has_failure"):
                    record_execution_failure(None, error, stage="cli.arguments")
                raise
            except Exception as error:
                if not _FAILURE_CONTEXT.get().get("has_failure"):
                    record_execution_failure(None, error, stage="cli.run")
                raise
            if result and not _FAILURE_CONTEXT.get().get("has_failure"):
                record_execution_failure(None, SystemExit(result), stage="cli.return")
            return result
        finally:
            reset_failure_diagnostics(token)

    return observed


# 日志目录
LOG_DIR = Path("logs")

# Q05（LLM-08）单条日志记录最大长度（含 traceback），超出即截断。
LOG_RECORD_MAX_CHARS = 2000

# Q05（LLM-08）凭据类内容脱敏模式（落盘前应用）。
LOG_REDACTION_PATTERNS = (
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"), "sk-***"),
    (re.compile(r"(?i)\bbearer[\s:=]+[A-Za-z0-9\.\-_]{6,}"), "Bearer ***"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)"
            r"(\s*[=:]\s*)\S+"
        ),
        r"\1\2***",
    ),
)


def sanitize_log_text(text: str, *, truncate: bool = True) -> str:
    """对日志文本执行凭据脱敏与限长（Q05 / LLM-08）。"""
    for pattern, replacement in LOG_REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    if truncate and len(text) > LOG_RECORD_MAX_CHARS:
        text = text[:LOG_RECORD_MAX_CHARS] + "...[truncated]"
    return text


class ContentBoundaryFormatter(logging.Formatter):
    """落盘/控制台内容边界：先按常规格式化，再脱敏与限长。"""

    def format(self, record: logging.LogRecord) -> str:
        return sanitize_log_text(
            super().format(record),
            truncate=not record.__dict__.get("_stockqa_structural_failure", False),
        )


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

    # 定义日志格式（Q05：文件与控制台均经过内容边界 formatter）
    file_formatter = ContentBoundaryFormatter(
        fmt="%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_formatter = ContentBoundaryFormatter(
        fmt="%(levelname)s - %(message)s", datefmt="%H:%M:%S"
    )

    # 添加文件处理器
    file_unavailable = False
    if log_to_file:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_file = LOG_DIR / f"stock_qa_{datetime.now().strftime('%Y%m%d')}.log"
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setLevel(level)
            file_handler.setFormatter(file_formatter)
            logger.addHandler(file_handler)
        except (OSError, ValueError):
            file_unavailable = True
            warn_feedback_unavailable("logging tracking incomplete")

    # 添加控制台处理器
    if log_to_console or file_unavailable:
        console_handler = logging.StreamHandler(sys.stderr if file_unavailable else sys.stdout)
        console_handler.setLevel(level)
        console_handler.addFilter(
            lambda record: not record.__dict__.get("_stockqa_structural_failure")
        )
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
