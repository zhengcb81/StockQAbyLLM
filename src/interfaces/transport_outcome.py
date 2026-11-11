"""Provider-neutral transport outcome contracts consumed by the Q&A core."""

from __future__ import annotations

import copy
from typing import Any, Dict


class UncertainTransportOutcome(RuntimeError):
    """A transport outcome that may have reached a provider.

    Concrete transports must pass an already-sanitized receipt. The core only
    reads this stable contract and never imports a storage or provider adapter.
    """

    def __init__(self, attempt_id: str, sanitized_receipt: Dict[str, Any] | None = None):
        if not isinstance(attempt_id, str) or not attempt_id or len(attempt_id) > 160:
            raise ValueError("uncertain_transport_attempt_id_invalid")
        if sanitized_receipt is not None and not isinstance(sanitized_receipt, dict):
            raise ValueError("uncertain_transport_receipt_invalid")
        self.attempt_id = attempt_id
        self._sanitized_attempt_receipt = copy.deepcopy(sanitized_receipt or {})
        super().__init__("transport outcome is uncertain")

    @property
    def attempt_receipt(self) -> Dict[str, Any]:
        """Return a detached receipt so consumers cannot mutate its safe snapshot."""
        return copy.deepcopy(self._sanitized_attempt_receipt)


__all__ = ["UncertainTransportOutcome"]
