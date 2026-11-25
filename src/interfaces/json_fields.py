"""Shared object-pair semantics for local wire records and provider responses."""

import json
from typing import Any, Dict


class DuplicateJSONKeyError(ValueError):
    """Raised when a response object contains an ambiguous repeated key."""


def reject_duplicate_json_keys(pairs):
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            # Identical repeated wire fields add no competing claim. Compare
            # canonical JSON, not Python equality (True must not equal 1).
            if json.dumps(result[key], sort_keys=True, allow_nan=False) != json.dumps(
                value, sort_keys=True, allow_nan=False
            ):
                raise DuplicateJSONKeyError("冲突JSON字段")
        else:
            result[key] = value
    return result
