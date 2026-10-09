"""Versioned, exact model identity permissions shared by every HTTP protocol.

The projection contains no credentials and is detached from mutable config.
Callers freeze that projection with their route/attempt before dispatch; this
module never looks up current configuration while accepting a response.
"""

import hashlib
import json
import unicodedata
from collections.abc import Mapping
from typing import Any, Dict

SCHEMA_VERSION = "1.0.0"
MAX_ALIASES = 256
MAX_MODEL_LENGTH = 160
_ALIAS_KEYS = frozenset({"provider", "protocol", "requested_model", "resolved_model"})
_PROVIDER_PROTOCOLS = frozenset(
    {
        ("openai", "responses"),
        ("minimax", "responses"),
        ("minimax", "anthropic_messages"),
        ("mimo", "mimo_chat_completions"),
    }
)


def _valid_model_name(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_MODEL_LENGTH
        and value == value.strip()
        and not any(
            char in "*?[]" or unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value
        )
    )


def normalize_model_resolution(value: Any = None) -> Dict[str, Any]:
    """Validate and return a fresh canonical projection for a frozen snapshot.

    None means an absent configuration. An explicit object must contain the
    entire versioned contract, even when its alias list is empty. No identifier
    is case folded, trimmed, guessed, or otherwise rewritten.
    """
    path = "quick_scan_model_resolution"
    if value is None:
        return {"schema_version": SCHEMA_VERSION, "aliases": []}
    if not isinstance(value, Mapping) or set(value) != {"schema_version", "aliases"}:
        raise ValueError(f"{path} requires exactly schema_version and aliases")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"{path}.schema_version must be {SCHEMA_VERSION}")
    aliases = value["aliases"]
    if not isinstance(aliases, list) or len(aliases) > MAX_ALIASES:
        raise ValueError(f"{path}.aliases must be an array of at most {MAX_ALIASES} mappings")

    seen = set()
    canonical = []
    for index, alias in enumerate(aliases):
        alias_path = f"{path}.aliases[{index}]"
        if not isinstance(alias, Mapping) or set(alias) != _ALIAS_KEYS:
            raise ValueError(f"{alias_path} has missing or unknown fields")
        provider, protocol = alias["provider"], alias["protocol"]
        if (
            not isinstance(provider, str)
            or not isinstance(protocol, str)
            or (provider, protocol) not in _PROVIDER_PROTOCOLS
        ):
            raise ValueError(f"{alias_path} has an unsupported canonical provider/protocol pair")
        requested, resolved = alias["requested_model"], alias["resolved_model"]
        if not _valid_model_name(requested) or not _valid_model_name(resolved):
            raise ValueError(
                f"{alias_path} model identifiers must be finite exact strings without controls or wildcards"
            )
        identity = (provider, protocol, requested, resolved)
        if identity in seen:
            raise ValueError(f"{alias_path} duplicates an exact mapping")
        seen.add(identity)
        canonical.append(identity)
    canonical.sort()
    return {
        "schema_version": SCHEMA_VERSION,
        "aliases": [
            dict(zip(("provider", "protocol", "requested_model", "resolved_model"), identity))
            for identity in canonical
        ],
    }


def model_resolution_sha256(policy: Any = None) -> str:
    """Hash only the validated canonical non-secret permission projection."""
    canonical = json.dumps(
        normalize_model_resolution(policy),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def model_resolution_allowed(
    provider: str,
    protocol: str,
    requested_model: str,
    actual_model: Any,
    policy: Any = None,
) -> bool:
    """Accept exact HTTP identity or one explicitly registered four-part alias.

    Invalid policy sources always raise, including for an exact response, so a
    malformed configured permission cannot disappear through the exact branch.
    Missing/invalid HTTP identity and unsupported provider/protocol return False.
    """
    normalized = normalize_model_resolution(policy)
    if (
        not isinstance(provider, str)
        or not isinstance(protocol, str)
        or (provider, protocol) not in _PROVIDER_PROTOCOLS
        or not _valid_model_name(requested_model)
        or not _valid_model_name(actual_model)
    ):
        return False
    if actual_model == requested_model:
        return True
    return {
        "provider": provider,
        "protocol": protocol,
        "requested_model": requested_model,
        "resolved_model": actual_model,
    } in normalized["aliases"]
