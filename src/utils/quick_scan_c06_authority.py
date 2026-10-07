"""Load and validate the versioned C06 delivery authority document.

Q10/DB-07: contract versions, capabilities and producer identity are NOT
derivable from an answer checkpoint, so they must be supplied by an explicit,
versioned document. Anything missing or malformed raises
:class:`AuthorityUnavailable` and the caller records a durable delivery block
instead of inventing a field.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from src.utils.quick_scan_c06_adapter import MissingC06Fields
from src.utils.quick_scan_result_outbox import _CAPABILITIES, _CONTRACT_VERSION_KEYS

__all__ = [
    "AUTHORITY_SCHEMA_ID",
    "AUTHORITY_SCHEMA_VERSION",
    "AuthorityUnavailable",
    "authority_adapter_input",
    "load_c06_authority",
]

AUTHORITY_SCHEMA_ID = "stockqa.quick_scan_c06_authority/1.0.0"
AUTHORITY_SCHEMA_VERSION = "1.0.0"
DEFAULT_AUTHORITY_FILENAME = "quick_scan_c06_authority.json"
_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "config" / ("quick_scan_c06_authority.schema.json")
)


class AuthorityUnavailable(MissingC06Fields):
    """The authority document is absent or invalid — durable block, never a guess."""


def _schema_document() -> dict[str, Any]:
    """The published, versioned schema artifact shipped next to this loader."""
    document = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise AuthorityUnavailable("c06 authority schema artifact is invalid")
    return document


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 160:
        raise AuthorityUnavailable(f"c06 authority {label} must be a short string")
    return value


def _validate(document: Any) -> dict[str, Any]:
    """Hand-rolled check mirroring quick_scan_c06_authority.schema.json.

    StockQA does not depend on a JSON Schema runtime, so the loader enforces
    the same constraints directly — a schema violation and a loader refusal
    must agree on the bounded reason.
    """
    if not isinstance(document, dict):
        raise AuthorityUnavailable("c06 authority must be a JSON object")
    allowed = {"schema_version", "contract_versions", "capabilities", "producer"}
    if set(document) != allowed:
        raise AuthorityUnavailable("c06 authority field set mismatch")
    if document["schema_version"] != AUTHORITY_SCHEMA_VERSION:
        raise AuthorityUnavailable("c06 authority schema_version mismatch")

    contract_versions = document["contract_versions"]
    if not isinstance(contract_versions, dict) or set(contract_versions) != set(
        _CONTRACT_VERSION_KEYS
    ):
        raise AuthorityUnavailable("c06 authority contract_versions key set mismatch")
    for key in _CONTRACT_VERSION_KEYS:
        _text(contract_versions[key], f"contract_versions.{key}")

    capabilities = document["capabilities"]
    if not isinstance(capabilities, list) or not capabilities:
        raise AuthorityUnavailable("c06 authority capabilities must be a list")
    if len(set(capabilities)) != len(capabilities):
        raise AuthorityUnavailable("c06 authority capabilities must be unique")
    if any(not isinstance(capability, str) for capability in capabilities):
        raise AuthorityUnavailable("c06 authority capabilities must be strings")
    if any(cap not in _CAPABILITIES for cap in capabilities):
        raise AuthorityUnavailable("c06 authority declares an unknown capability")
    if "standard_observation_v1" not in capabilities:
        raise AuthorityUnavailable("c06 authority must declare standard_observation_v1")

    producer = document["producer"]
    if not isinstance(producer, dict) or set(producer) != {
        "component_version",
        "build_id",
    }:
        raise AuthorityUnavailable("c06 authority producer field set mismatch")
    _text(producer["component_version"], "producer.component_version")
    _text(producer["build_id"], "producer.build_id")
    return document


def load_c06_authority(path: str | Path) -> dict[str, Any]:
    """Read one authority document and return its validated adapter input.

    Raises :class:`AuthorityUnavailable` for a missing file, invalid JSON, an
    unknown schema version or any field the C06 adapter would otherwise have
    to invent.
    """
    resolved = Path(path)
    try:
        raw = resolved.read_bytes()
    except OSError as error:
        raise AuthorityUnavailable(
            f"c06 authority is unreadable: {error.__class__.__name__}"
        ) from error
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise AuthorityUnavailable("c06 authority is not valid UTF-8 JSON") from error
    validated = _validate(document)
    schema_bytes = _SCHEMA_PATH.read_bytes()
    schema_document = _schema_document()
    if schema_document.get("$id") != AUTHORITY_SCHEMA_ID:
        raise AuthorityUnavailable("c06 authority schema artifact id mismatch")
    return {
        "schema_id": AUTHORITY_SCHEMA_ID,
        "schema_version": AUTHORITY_SCHEMA_VERSION,
        "schema_sha256": hashlib.sha256(schema_bytes).hexdigest(),
        "authority_sha256": hashlib.sha256(raw).hexdigest(),
        "contract_versions": dict(validated["contract_versions"]),
        "capabilities": list(validated["capabilities"]),
        "producer_component_version": validated["producer"]["component_version"],
        "producer_build_id": validated["producer"]["build_id"],
    }


def authority_adapter_input(authority: dict[str, Any] | None) -> dict[str, Any]:
    """Project the loaded authority onto the fields ``build_c06_package`` takes.

    ``None`` (not configured) is an explicit block, never an empty dict that
    could be mistaken for a partially valid document.
    """
    if not isinstance(authority, dict) or not authority:
        raise AuthorityUnavailable("c06 authority is not configured")
    return {
        "contract_versions": dict(authority.get("contract_versions") or {}),
        "capabilities": list(authority.get("capabilities") or []),
        "producer_component_version": authority.get("producer_component_version"),
        "producer_build_id": authority.get("producer_build_id"),
    }
