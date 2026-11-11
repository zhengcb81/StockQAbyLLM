"""Versioned public facade for company-scanning integrations.

Consumers must depend on this module rather than the implementation modules
that project an admitted lifecycle into detached scan inputs and runtime
operations. Those implementation details remain owned by StockQA.
"""

from __future__ import annotations

import inspect
from importlib import import_module

PUBLIC_API_PROTOCOL = "stockqa.company-scan-public-api/1.8.0"


def supports_public_api(value, family, minimum_version):
    """Return whether a declared public API meets a same-major minimum version."""
    if (
        not isinstance(value, str)
        or not isinstance(family, str)
        or type(minimum_version) is not tuple
        or len(minimum_version) != 3
        or any(type(part) is not int or part < 0 for part in minimum_version)
        or value.count("/") != 1
    ):
        return False
    declared_family, version = value.split("/", 1)
    parts = version.split(".")
    if declared_family != family or len(parts) != 3 or any(not p.isdigit() for p in parts):
        return False
    parsed = tuple(int(part) for part in parts)
    return parsed[0] == minimum_version[0] and parsed >= minimum_version


def supports_public_api_operations(module, family, minimum_version, operations):
    """Require a compatible facade to declare and implement each consumed operation."""
    if not supports_public_api(
        getattr(module, "PUBLIC_API_PROTOCOL", None), family, minimum_version
    ):
        return False
    if not isinstance(operations, (list, tuple)) or any(
        not isinstance(name, str) or not name for name in operations
    ):
        return False
    exports = getattr(module, "__all__", None)
    if not isinstance(exports, (list, tuple)) or any(
        not isinstance(name, str) or not name for name in exports
    ):
        return False
    return all(name in exports and callable(getattr(module, name, None)) for name in operations)


class CompanyAgentPublicOperations:
    """Limited projection of the Company Agent public operations used here.

    The caller receives exactly the names it asked for. The owner module
    object, its implementation modules and its private helpers stay inside the
    Company Agent package.
    """

    __slots__ = ("_requested", "_values")

    def __init__(self, values):
        self._requested = frozenset(values)
        self._values = dict(values)

    def __getattr__(self, name):
        try:
            values = object.__getattribute__(self, "_values")
        except AttributeError:
            raise AttributeError(name) from None
        try:
            return values[name]
        except KeyError:
            raise AttributeError(
                f"company agent public operation {name!r} was not requested"
            ) from None

    def __dir__(self):
        return sorted(self._requested)

    def __repr__(self):
        return f"<company_agent_public_operations {sorted(self._requested)}>"


def company_agent_public_api(*operations):
    """Load only the versioned Company Agent operations used by this integration.

    The result is a limited projection of the requested public names; the
    owner module object never crosses the package boundary.
    """
    try:
        api = import_module("iqs_company_agent_lab.api")
    except ImportError as error:
        raise ImportError("company_agent_public_api_unavailable") from error
    if not supports_public_api(
        getattr(api, "PUBLIC_API_PROTOCOL", None),
        "iqs.company-agent-public-api",
        (2, 3, 0),
    ):
        raise ImportError("company_agent_public_api_version_unsupported")
    if not operations or any(not isinstance(name, str) or not name for name in operations):
        raise ValueError("company_agent_public_api_operations_required")
    advertised = getattr(api, "__all__", None)
    if not isinstance(advertised, (list, tuple)) or any(
        not isinstance(name, str) or not name for name in advertised
    ):
        raise ImportError("company_agent_public_api_exports_invalid")
    if any(name not in advertised for name in operations):
        raise ImportError("company_agent_public_api_operation_not_exported")
    missing = object()
    resolved = {}
    for name in operations:
        value = getattr(api, name, missing)
        # A declared public name must resolve to a public value or operation.
        # A module object would hand the owner implementation back to callers.
        if value is missing or inspect.ismodule(value):
            raise ImportError("company_agent_public_api_operation_missing")
        resolved[name] = value
    return CompanyAgentPublicOperations(resolved)


_EXPORTS = {
    "COMPANY_SCAN_CONTRACT_PROTOCOL": (
        "quick_scan_company_context",
        "COMPANY_SCAN_CONTRACT_PROTOCOL",
    ),
    "CompanyScanContract": ("quick_scan_company_context", "CompanyScanContract"),
    "COMPANY_SCAN_TASK_PORT_PROTOCOL": (
        "quick_scan_company_context",
        "COMPANY_SCAN_TASK_PORT_PROTOCOL",
    ),
    "CompanyScanTaskPort": ("quick_scan_company_context", "CompanyScanTaskPort"),
    "assert_company_scan_admission": (
        "quick_scan_company_context",
        "assert_company_scan_admission",
    ),
    "company_scan_inputs": ("quick_scan_company_context", "company_scan_inputs"),
    "company_scan_contract": ("quick_scan_company_context", "company_scan_contract"),
    "company_scan_capabilities": (
        "quick_scan_company_context",
        "company_scan_capabilities",
    ),
    "create_native_task": ("quick_scan_company_context", "create_native_task"),
    "create_company_scan_task": ("quick_scan_company_context", "create_company_scan_task"),
    "company_scan_pack_group_key": ("quick_scan_company_context", "company_scan_pack_group_key"),
    "validate_company_scan_task": ("quick_scan_company_context", "validate_company_scan_task"),
    "COMPANY_SCAN_RUNTIME_PORT_PROTOCOL": (
        "quick_scan_company_runtime",
        "COMPANY_SCAN_RUNTIME_PORT_PROTOCOL",
    ),
    "RUNTIME_SERVICES_PROTOCOL": ("quick_scan_company_runtime", "RUNTIME_SERVICES_PROTOCOL"),
    "create_company_research_runtime": (
        "quick_scan_company_runtime",
        "create_company_research_runtime",
    ),
}

__all__ = [
    "PUBLIC_API_PROTOCOL",
    "supports_public_api",
    "supports_public_api_operations",
    "company_agent_public_api",
    *_EXPORTS,
]


def __getattr__(name: str):
    """Resolve only names explicitly included in the public contract."""
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no public attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(f".{module_name}", __package__), attribute)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
