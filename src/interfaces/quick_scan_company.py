"""Detached, versioned data contracts for StockQA company scanning."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Mapping, Tuple

COMPANY_SCAN_CONTRACT_PROTOCOL = "stockqa.company_scan_contract/1.0.0"
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_REQUIRED_NATIVE_INPUTS = frozenset(
    {"manifest.json", "identity.json", "authority.json", "owner-config.json", "questions.json"}
)


@dataclass(frozen=True)
class CompanyScanContract:
    """Safe owner projection used to prepare or validate a company scan task.

    The contract carries source hashes and semantic projections, never the
    original input bytes, a database handle, or a live owner session.
    """

    protocol: str
    requested_question_ids: Tuple[str, ...]
    eligible_question_ids: Tuple[str, ...]
    question_contracts: Mapping[str, Mapping[str, Any]]
    module_locks: Tuple[Mapping[str, Any], ...]
    identity_payload: Mapping[str, Any]
    native_input_references: Mapping[str, Mapping[str, Any]]
    observation_context: Mapping[str, Any]
    information_cutoff: str
    route_id: str
    route_provider: str
    route_model: str

    def __post_init__(self) -> None:
        if self.protocol != COMPANY_SCAN_CONTRACT_PROTOCOL:
            raise ValueError("company_scan_contract_protocol_unsupported")
        if (
            not isinstance(self.requested_question_ids, tuple)
            or any(not isinstance(value, str) or not value for value in self.requested_question_ids)
            or len(set(self.requested_question_ids)) != len(self.requested_question_ids)
        ):
            raise ValueError("company_scan_contract_question_scope_invalid")
        object.__setattr__(self, "requested_question_ids", tuple(self.requested_question_ids))
        object.__setattr__(self, "eligible_question_ids", tuple(self.eligible_question_ids))
        object.__setattr__(self, "question_contracts", copy.deepcopy(dict(self.question_contracts)))
        object.__setattr__(self, "module_locks", tuple(copy.deepcopy(self.module_locks)))
        object.__setattr__(self, "identity_payload", copy.deepcopy(dict(self.identity_payload)))
        object.__setattr__(
            self, "native_input_references", copy.deepcopy(dict(self.native_input_references))
        )
        object.__setattr__(
            self, "observation_context", copy.deepcopy(dict(self.observation_context))
        )
        if (
            not isinstance(self.eligible_question_ids, tuple)
            or any(value not in self.requested_question_ids for value in self.eligible_question_ids)
            or len(set(self.eligible_question_ids)) != len(self.eligible_question_ids)
            or not set(self.eligible_question_ids) <= set(self.question_contracts)
        ):
            raise ValueError("company_scan_contract_eligible_scope_invalid")
        if any(
            not isinstance(question, Mapping) or question.get("id") != question_id
            for question_id, question in self.question_contracts.items()
        ):
            raise ValueError("company_scan_contract_question_contract_invalid")
        if any(
            not isinstance(lock, Mapping)
            or not isinstance(lock.get("module_id"), str)
            or not lock["module_id"]
            or not isinstance(lock.get("version"), str)
            or not lock["version"]
            for lock in self.module_locks
        ) or len({lock["module_id"] for lock in self.module_locks}) != len(self.module_locks):
            raise ValueError("company_scan_contract_module_locks_invalid")
        if set(self.native_input_references) != _REQUIRED_NATIVE_INPUTS:
            raise ValueError("company_scan_contract_native_input_set_invalid")
        for name, reference in self.native_input_references.items():
            if (
                not isinstance(reference, Mapping)
                or set(reference) != {"path", "bytes", "sha256"}
                or reference["path"] != "native-input/" + name
                or type(reference["bytes"]) is not int
                or reference["bytes"] < 0
                or not isinstance(reference["sha256"], str)
                or not _SHA256.fullmatch(reference["sha256"])
            ):
                raise ValueError("company_scan_contract_native_input_reference_invalid")
        for value in (
            self.information_cutoff,
            self.route_id,
            self.route_provider,
            self.route_model,
        ):
            if not isinstance(value, str) or not value:
                raise ValueError("company_scan_contract_route_or_cutoff_invalid")
        if not isinstance(self.identity_payload, Mapping) or not isinstance(
            self.observation_context, Mapping
        ):
            raise ValueError("company_scan_contract_projection_invalid")
        if (
            not isinstance(self.identity_payload.get("entity_id"), str)
            or not self.identity_payload["entity_id"]
        ):
            raise ValueError("company_scan_contract_identity_invalid")
        if not isinstance(self.observation_context.get("questions"), Mapping):
            raise ValueError("company_scan_contract_observation_context_invalid")


__all__ = ["COMPANY_SCAN_CONTRACT_PROTOCOL", "CompanyScanContract"]
