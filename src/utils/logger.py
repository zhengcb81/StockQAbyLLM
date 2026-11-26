"""日志配置工具。

该模块提供统一的日志配置和获取功能。
支持文件和控制台双输出，使用结构化格式。

Q05（LLM-08）内容边界：所有处理器经过 :class:`ContentBoundaryFormatter`，
落盘/输出前对凭据类内容脱敏并限制单条记录长度；调用方只应传入允许的
结构化字段与短依据（问题/答案文本按既有约定截断）。
"""

import errno
import hashlib
import json
import logging
import os
import re
import sqlite3
import stat
import sys
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Protocol

from src.utils.execution_feedback import (
    DEFAULT_LOG_DIR,
    PHASE_MESSAGE,
    RECEIPT_MESSAGE,
    emit_execution_observation,
    execution_phase,
    execution_references,
    warn_feedback_unavailable,
)

_FAILURE_CONTEXT: ContextVar[dict[str, Any]] = ContextVar("stockqa_failure_context", default={})
_REFERENCE_FIELDS = frozenset(
    {
        "question_id",
        "work_item_id",
        "work_attempt_id",
        "attempt_id",
        "run_id",
        "invocation_id",
        "request_id",
        "provider_attempt_id",
        "provider",
        "model",
    }
)


def _failure_references(values):
    references = {}
    for key, value in values.items():
        if key not in _REFERENCE_FIELDS or type(value) is not str or not value:
            continue
        if (
            re.fullmatch(r"[A-Za-z0-9_.:/@-]{1,200}", value)
            and "://" not in value
            and sanitize_log_text(value, truncate=False) == value
        ):
            references[key] = value
        elif key in {"request_id", "provider_attempt_id"}:
            # A provider header can be arbitrary text. Keep a correlation hash
            # when its value cannot safely be published as an opaque identifier.
            references[key] = (
                "sha256-" + hashlib.sha256(value.encode("utf8", "surrogatepass")).hexdigest()
            )
    return references


def configure_failure_diagnostics(*, output):
    """Associate this real CLI invocation with its existing output, not an HTTP attempt."""
    context = execution_references({}, os.environ)
    try:
        if output is not None and not os.environ.get("RESEARCH_FEEDBACK_RUN_ID"):
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


def current_execution_references():
    return execution_references(_FAILURE_CONTEXT.get(), os.environ)


@contextmanager
def execution_scope(entry):
    """Create one attempt for a public non-CLI operation or inherit its caller."""
    if _FAILURE_CONTEXT.get():
        yield
        return
    token = configure_failure_diagnostics(output=None)
    try:
        _FAILURE_CONTEXT.get()["entry"] = entry
        yield
    finally:
        reset_failure_diagnostics(token)


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
    context = _FAILURE_CONTEXT.get()
    identity = context
    try:
        identity = execution_references(
            context or _failure_references(references or {}), os.environ
        )
        references = {**identity, **(references or {})}
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
    if context:
        diagnostic["entry"] = context.get("entry")
        diagnostic["options"] = context.get("options", [])
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
        if context and explains_invocation:
            context["has_failure"] = True
        emit_execution_observation(diagnostic, context=identity, fallback=_log_failure_receipt)
    except Exception:
        _tracking_incomplete()
    return diagnostic


def _log_failure_receipt(receipt):
    _log_execution_record(RECEIPT_MESSAGE, receipt, logging.ERROR)


def _log_execution_record(kind, body, level):
    # The existing journal rotation and formatter also serve machine-safe history.
    get_logger("stockqa.execution", log_to_console=False).log(
        level,
        "%s %s",
        kind,
        json.dumps(body, ensure_ascii=True, sort_keys=True),
        extra={"_stockqa_structural_failure": True},
    )


def record_execution_phase(stage, *, references=None, exit_code=None):
    context = _FAILURE_CONTEXT.get()
    if not context.get("record_lifecycle"):
        return
    try:
        phase = execution_phase(
            stage, context, _failure_references(references or {}), exit_code=exit_code
        )
        _log_execution_record(PHASE_MESSAGE, phase, logging.INFO)
    except Exception:
        _tracking_incomplete()


def observe_arguments(parser, argv=None, *, record_lifecycle=False):
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
        if record_lifecycle and not {"--help", "-h"}.intersection(args):
            context["record_lifecycle"] = True
            record_execution_phase("cli.started")
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
                record_execution_phase(
                    "cli.exited", exit_code=error.code if type(error.code) is int else 1
                )
                raise
            except KeyboardInterrupt as error:
                record_execution_failure(None, error, stage="cli.cancelled")
                raise
            except Exception as error:
                if not _FAILURE_CONTEXT.get().get("has_failure"):
                    record_execution_failure(None, error, stage="cli.run")
                record_execution_phase("cli.exited", exit_code=1)
                raise
            if result and not _FAILURE_CONTEXT.get().get("has_failure"):
                record_execution_failure(None, SystemExit(result), stage="cli.return")
            if type(result) is int:
                record_execution_phase("cli.exited", exit_code=result)
            return result
        finally:
            reset_failure_diagnostics(token)

    return observed


# 日志目录
LOG_DIR = DEFAULT_LOG_DIR
_DAILY_LOG_MAX_BYTES = 2 * 1024 * 1024

# Q05（LLM-08）单条日志记录最大长度（含 traceback），超出即截断。
LOG_RECORD_MAX_CHARS = 2000

# Q05（LLM-08）凭据类内容脱敏模式（落盘前应用）。
_CREDENTIAL_VALUE = r"""(?:"[^"\r\n]*"?|'[^'\r\n]*'?|[^\s,;"'&]+)"""
_FOLDED_AUTH_VALUE = re.compile(r"[A-Za-z0-9._~+/\-]+=*")
LOG_REDACTION_PATTERNS = (
    (
        re.compile(
            rf"""(?im)(\b(?:proxy[-_])?authorization["']?[ \t]*[:=][ \t]*)"""
            rf"(?:(?:bearer|basic)[ \t]*\r?\n[ \t]*{_FOLDED_AUTH_VALUE.pattern}(?=[ \t]*\r?$)"
            rf"|(?:(?:bearer|basic)[ \t]+)?{_CREDENTIAL_VALUE})"
        ),
        r"\1***",
    ),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"), "sk-***"),
    (re.compile(r"(?i)\bbearer[ \t:=]+[A-Za-z0-9._~+/\-=]{6,}"), "Bearer ***"),
    (
        re.compile(
            r"""(?i)(\b(?:api[_-]?key|(?:access[_-]?|refresh[_-]?)?token|"""
            r"""(?:client[_-]?)?secret|password)["']?[ \t]*[=:][ \t]*)""" + _CREDENTIAL_VALUE
        ),
        r"\1***",
    ),
)

_SUBPROCESS_STDERR_WINDOW = 8192
# Leave space for the logging prefix without losing the exception at the end.
_SUBPROCESS_STDERR_CHARS = LOG_RECORD_MAX_CHARS - 256


def sanitize_log_text(text: str, *, truncate: bool = True) -> str:
    """对日志文本执行凭据脱敏与限长（Q05 / LLM-08）。"""
    for pattern, replacement in LOG_REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    if truncate and len(text) > LOG_RECORD_MAX_CHARS:
        text = text[:LOG_RECORD_MAX_CHARS] + "...[truncated]"
    return text


class _StderrStream(Protocol):
    def seek(self, offset: int, whence: int = 0, /) -> int: ...

    def read(self, size: int = -1, /) -> bytes: ...


def subprocess_stderr_excerpt(stderr: bytes | str | _StderrStream | None) -> str:
    """Return a short, redacted stderr tail from an owner subprocess."""
    if stderr is None:
        return "<empty>"
    if isinstance(stderr, (bytes, str)):
        cropped = len(stderr) > _SUBPROCESS_STDERR_WINDOW
        raw = stderr[-_SUBPROCESS_STDERR_WINDOW:]
    else:
        try:
            size = stderr.seek(0, 2)
            cropped = size > _SUBPROCESS_STDERR_WINDOW
            stderr.seek(max(0, size - _SUBPROCESS_STDERR_WINDOW))
            raw = stderr.read(_SUBPROCESS_STDERR_WINDOW)
        except (OSError, ValueError):
            return "<unavailable>"

    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    if cropped:
        # A window may start inside a credential or its folded continuation.
        # Skip that partial line and any leading continuation lines.
        text = text.partition("\n")[2]
        lines = text.splitlines(keepends=True)
        first_complete = next(
            (index for index, line in enumerate(lines) if line.strip() and not line[0].isspace()),
            len(lines),
        )
        if first_complete < len(lines) and _FOLDED_AUTH_VALUE.fullmatch(
            lines[first_complete].strip()
        ):
            # A malformed fold may start with an unindented credential after
            # a header that was truncated at the start of the window.
            lines[first_complete] = "***\n"
        text = "".join(lines[first_complete:])
    safe = sanitize_log_text(text, truncate=False).strip()
    return safe[-_SUBPROCESS_STDERR_CHARS:] or "<empty>"


class ContentBoundaryFormatter(logging.Formatter):
    """落盘/控制台内容边界：先按常规格式化，再脱敏与限长。"""

    def format(self, record: logging.LogRecord) -> str:
        return sanitize_log_text(
            super().format(record),
            truncate=not record.__dict__.get("_stockqa_structural_failure", False),
        )


@contextmanager
def _log_writer_lock(path: Path) -> Iterator[None]:
    """Serialize size, rotation and append across all writers of a daily file."""
    with path.open("a+b") as handle:
        handle.seek(0)
        deadline = time.monotonic() + 2
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        # Closing the descriptor releases the lock, including on process exit.
        yield


class _DailyFileHandler(logging.Handler):
    """Keep the active log and one recent segment for each existing log date."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._active = path.absolute()
        self._archive = self._active.with_suffix(".log.1")
        self._lock = self._active.with_suffix(".log.lock")
        # Detect an unavailable sink at setup so get_logger keeps its console fallback.
        with _log_writer_lock(self._lock), self._active.open("ab"):
            pass

    def _rotate(self, incoming_bytes: int) -> None:
        # An external reader or an unavailable archive can prevent the
        # atomic replace. Keep both files, append if possible, and retry.
        with suppress(OSError):
            current = self._active.lstat()
            if (
                stat.S_ISREG(current.st_mode)
                and current.st_size
                and current.st_size + incoming_bytes > _DAILY_LOG_MAX_BYTES
            ):
                self._active.replace(self._archive)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            body = (self.format(record) + "\n").replace("\n", os.linesep).encode("utf-8")
            with _log_writer_lock(self._lock):
                self._rotate(len(body))
                with self._active.open("ab") as sink:
                    sink.write(body)
        except Exception:
            # Logging's default error handler can echo the original, unredacted record.
            warn_feedback_unavailable("logging tracking incomplete")


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
            file_handler = _DailyFileHandler(log_file)
            file_handler.setLevel(level)
            file_handler.setFormatter(file_formatter)
            logger.addHandler(file_handler)
        except (OSError, ValueError):
            file_unavailable = True
            warn_feedback_unavailable("logging tracking incomplete")

    # 添加控制台处理器
    console_stream = sys.stderr if file_unavailable or sys.stdout is None else sys.stdout
    if (log_to_console or file_unavailable) and console_stream is not None:
        console_handler = logging.StreamHandler(console_stream)
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
