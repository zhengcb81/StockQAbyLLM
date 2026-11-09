"""Shared stdin and network constraints for StockQA-owned local commands."""

import sys
from typing import Any

MAX_REQUEST_BYTES = 8 * 1024 * 1024


def read_request() -> dict[str, Any]:
    from src.utils.quick_scan_result_outbox import strict_json_loads

    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("owner_request_too_large")
    value = strict_json_loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("owner_request_object_required")
    return value


def deny_network(event: str, _args: tuple[object, ...]) -> None:
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
        raise RuntimeError("network_forbidden")
