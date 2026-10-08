"""Load and validate the versioned C06 delivery authority document.

Q10/DB-07: contract versions, capabilities and producer identity are NOT
derivable from an answer checkpoint, so they must be supplied by an explicit,
versioned document. Anything missing or malformed raises
:class:`AuthorityUnavailable` and the caller records a durable delivery block
instead of inventing a field.

Two published versions coexist without one replacing the other:

* ``1.0.0`` keeps exactly its historical field set and capability checks —
  old authority files and the compact packages they sealed stay readable.
* ``2.0.0`` additionally carries the frozen IQS ``observation_context`` and
  its content digest. It is validated against the published v2 schema
  artifact plus the context contract (per-question metadata, both manifest
  hashes, identity bytes, schema/metric hashes, prompts). An unknown
  ``schema_version`` is refused — never downgraded to v1.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from src.utils.quick_scan_c06_adapter import MissingC06Fields
from src.utils.quick_scan_result_outbox import (
    _CAPABILITIES,
    _CONTRACT_VERSION_KEYS,
    canonical_sha256,
)

__all__ = [
    "AUTHORITY_SCHEMA_ID",
    "AUTHORITY_SCHEMA_ID_V2",
    "AUTHORITY_SCHEMA_VERSION",
    "AUTHORITY_SCHEMA_VERSION_V2",
    "AuthorityUnavailable",
    "authority_adapter_input",
    "load_c06_authority",
]

AUTHORITY_SCHEMA_ID = "stockqa.quick_scan_c06_authority/1.0.0"
AUTHORITY_SCHEMA_VERSION = "1.0.0"
AUTHORITY_SCHEMA_ID_V2 = "stockqa.quick_scan_c06_authority/2.0.0"
AUTHORITY_SCHEMA_VERSION_V2 = "2.0.0"
DEFAULT_AUTHORITY_FILENAME = "quick_scan_c06_authority.json"
_ROOT = Path(__file__).resolve().parent.parent
_SCHEMA_PATH = _ROOT / "config" / "quick_scan_c06_authority.schema.json"
_SCHEMA_V2_PATH = _ROOT / "config" / "quick_scan_c06_authority.v2.schema.json"
_MANIFEST_EXCLUDED = {"manifest_sha256", "manifest_path", "contract_id"}


class AuthorityUnavailable(MissingC06Fields):
    """The authority document is absent or invalid — durable block, never a guess."""


def _schema_document(path: Path) -> dict[str, Any]:
    """The published, versioned schema artifact shipped next to this loader."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise AuthorityUnavailable("c06 authority schema artifact is invalid")
    return document


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 160:
        raise AuthorityUnavailable(f"c06 authority {label} must be a short string")
    return value


def _envelope(document: dict[str, Any], allowed: set[str]) -> None:
    if set(document) != allowed:
        raise AuthorityUnavailable("c06 authority field set mismatch")
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


def _validate(document: Any) -> dict[str, Any]:
    """Hand-rolled check mirroring quick_scan_c06_authority.schema.json.

    StockQA does not depend on a JSON Schema runtime for the v1 envelope, so
    the loader enforces the same constraints directly — a schema violation and
    a loader refusal must agree on the bounded reason.
    """
    if not isinstance(document, dict):
        raise AuthorityUnavailable("c06 authority must be a JSON object")
    _envelope(document, {"schema_version", "contract_versions", "capabilities", "producer"})
    if document["schema_version"] != AUTHORITY_SCHEMA_VERSION:
        raise AuthorityUnavailable("c06 authority schema_version mismatch")
    return document


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _crosscheck_manifest(context: dict[str, Any], manifest: dict[str, Any]) -> None:
    """Bind both manifest hashes and every frozen/stripped prompt to the file."""
    if not isinstance(manifest, dict) or manifest.get("answer_format") != "standard-1":
        raise AuthorityUnavailable("c06 authority context needs a standard-1 manifest")
    original = {key: value for key, value in manifest.items() if key not in _MANIFEST_EXCLUDED}
    if context["manifest_file_sha256"] != manifest.get("manifest_sha256") or context[
        "manifest_content_sha256"
    ] != canonical_sha256(original):
        raise AuthorityUnavailable("c06 authority manifest hashes do not match the manifest file")
    questions = manifest.get("questions")
    known = (
        {question.get("id"): question for question in questions}
        if isinstance(questions, list)
        else {}
    )
    if set(known) != set(context["questions"]):
        raise AuthorityUnavailable("c06 authority question set differs from the manifest")
    for question_id, selected in context["questions"].items():
        question = known[question_id]
        prompt = question.get("prompt")
        if (
            not isinstance(prompt, str)
            or selected["frozen_prompt_sha256"] != question.get("prompt_sha256")
            or selected["frozen_prompt_sha256"] != _sha256_text(prompt)
            or selected["work_prompt_sha256"] != _sha256_text(prompt.strip())
        ):
            raise AuthorityUnavailable(f"c06 authority prompt hash mismatch for {question_id}")


def _validate_v2(
    document: Any,
    *,
    manifest: dict[str, Any] | None,
    identity_snapshot_sha256: str | None,
) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise AuthorityUnavailable("c06 authority must be a JSON object")
    _envelope(
        document,
        {
            "schema_version",
            "contract_versions",
            "capabilities",
            "producer",
            "observation_context",
            "observation_context_sha256",
        },
    )
    if document["schema_version"] != AUTHORITY_SCHEMA_VERSION_V2:
        raise AuthorityUnavailable("c06 authority schema_version mismatch")

    artifact = _schema_document(_SCHEMA_V2_PATH)
    if artifact.get("$id") != AUTHORITY_SCHEMA_ID_V2:
        raise AuthorityUnavailable("c06 authority v2 schema artifact id mismatch")

    from src.utils.quick_scan_observation_context import validate_context_document

    context = document["observation_context"]
    expected = document["observation_context_sha256"]
    if not isinstance(expected, str) or len(expected) != 64:
        raise AuthorityUnavailable("c06 authority observation_context_sha256 is malformed")
    try:
        validated = validate_context_document(context, expected_sha256=expected)
    except ValueError as error:
        raise AuthorityUnavailable(
            f"c06 authority observation_context is invalid: {error}"
        ) from error

    if identity_snapshot_sha256 is not None and (
        validated["identity_snapshot_sha256"] != identity_snapshot_sha256
    ):
        raise AuthorityUnavailable(
            "c06 authority identity bytes differ from the identity snapshot in use"
        )
    if manifest is not None:
        try:
            _crosscheck_manifest(validated, manifest)
        except AuthorityUnavailable:
            raise
        except ValueError as error:
            raise AuthorityUnavailable(
                f"c06 authority manifest cross-check failed: {error}"
            ) from error
    return document


def load_c06_authority(
    path: str | Path,
    *,
    manifest: dict[str, Any] | None = None,
    identity_snapshot_sha256: str | None = None,
) -> dict[str, Any]:
    """Read one authority document and return its validated adapter input.

    Raises :class:`AuthorityUnavailable` for a missing file, invalid JSON, an
    unknown schema version or any field the C06 adapter would otherwise have
    to invent. ``manifest`` / ``identity_snapshot_sha256`` bind a v2 context
    to the exact files the run actually loaded — supply them whenever the
    caller has them, before any key is read or HTTP request is sent.
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

    version = document.get("schema_version") if isinstance(document, dict) else None
    if version == AUTHORITY_SCHEMA_VERSION:
        validated = _validate(document)
        schema_path = _SCHEMA_PATH
        schema_id = AUTHORITY_SCHEMA_ID
        schema_version = AUTHORITY_SCHEMA_VERSION
        extra: dict[str, Any] = {}
    elif version == AUTHORITY_SCHEMA_VERSION_V2:
        validated = _validate_v2(
            document,
            manifest=manifest,
            identity_snapshot_sha256=identity_snapshot_sha256,
        )
        schema_path = _SCHEMA_V2_PATH
        schema_id = AUTHORITY_SCHEMA_ID_V2
        schema_version = AUTHORITY_SCHEMA_VERSION_V2
        extra = {
            "observation_context": validated["observation_context"],
            "observation_context_sha256": validated["observation_context_sha256"],
        }
    else:
        # Unknown versions fail closed; a v2 document must never be read as v1
        # and a v1 document must never be re-labelled to reach newer code.
        raise AuthorityUnavailable(f"unsupported c06 authority schema_version: {version!r}")

    schema_bytes = schema_path.read_bytes()
    schema_document = _schema_document(schema_path)
    if schema_document.get("$id") != schema_id:
        raise AuthorityUnavailable("c06 authority schema artifact id mismatch")
    return {
        "schema_id": schema_id,
        "schema_version": schema_version,
        "schema_sha256": hashlib.sha256(schema_bytes).hexdigest(),
        "authority_sha256": hashlib.sha256(raw).hexdigest(),
        "contract_versions": dict(validated["contract_versions"]),
        "capabilities": list(validated["capabilities"]),
        "producer_component_version": validated["producer"]["component_version"],
        "producer_build_id": validated["producer"]["build_id"],
        **extra,
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
