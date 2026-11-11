"""Versioned public API for owner refresh capabilities and detached snapshots."""

from __future__ import annotations

from importlib import import_module

from src.interfaces.quick_scan_owner_refresh import (
    QUICK_SCAN_OWNER_REFRESH_PORT_PROTOCOL,
    OwnerRefreshSnapshot,
    QuickScanOwnerRefreshPort,
)

PUBLIC_API_PROTOCOL = "stockqa.quick_scan_owner_refresh_public_api/1.1.0"


def _implementation():
    return import_module("src.utils._quick_scan_owner_refresh_port")


def adapt_owner_refresh(owner):
    """Adapt a concrete owner session to its opaque, versioned public port."""
    return _implementation().adapt_owner_refresh(owner)


def is_owner_refresh_port(value) -> bool:
    return bool(_implementation().is_owner_refresh_port(value))


def build_refresh_result(batch, owner_port, quick_scan_context):
    """Render an owner result without exposing its concrete session to QAEngine."""
    return _implementation().build_refresh_result(batch, owner_port, quick_scan_context)


def original_semantic_resources(owner_port, context):
    """Return frozen answer semantics bound to this admitted owner."""
    return _implementation().original_semantic_resources(owner_port, context)


__all__ = [
    "PUBLIC_API_PROTOCOL",
    "QUICK_SCAN_OWNER_REFRESH_PORT_PROTOCOL",
    "OwnerRefreshSnapshot",
    "QuickScanOwnerRefreshPort",
    "adapt_owner_refresh",
    "is_owner_refresh_port",
    "build_refresh_result",
    "original_semantic_resources",
]
