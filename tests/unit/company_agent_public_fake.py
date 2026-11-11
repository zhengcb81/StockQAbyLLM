"""A public fake of the Company Agent facade for tests that must not need it.

StockQA consumes the Company Agent through its versioned public facade only.
This module reproduces the part of that contract a consumer needs: the
protocol declaration, the exported names, and a task contract that is
detached, JSON-only and digest-verified.

It is deliberately not a task-behaviour or financial double. The real Company
Agent package owns task semantics; the real-package combination check lives in
``tests/integration/test_company_agent_facade_material.py``.
"""

from __future__ import annotations

import copy
import hashlib
import json

PUBLIC_API_PROTOCOL = "iqs.company-agent-public-api/2.3.0"
TASK_CONTRACT_PROTOCOL = "iqs.company-agent-task-contract/1.1.0"

CONTRACT_FIELDS = (
    "protocol",
    "task_sha256",
    "contract_sha256",
    "raw",
    "questions",
    "question_ids",
    "company_doc",
    "company_ref",
    "evidence_board_doc",
    "runtime_profile",
    "targets",
    "limits",
    "owner_binding",
    "owner_control_binding",
)


def canonical_sha256(value):
    """Return the canonical digest of a JSON-compatible value."""
    payload = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class FakeOwnerTask:
    """Opaque owner task handle; only snapshots leave it."""

    __slots__ = (
        "raw",
        "questions",
        "company_doc",
        "company_ref",
        "evidence_board_doc",
        "runtime_profile",
        "targets",
        "owner_binding",
        "owner_control_binding",
        "task_sha256",
    )

    def __init__(
        self,
        raw,
        questions,
        company_doc,
        evidence_board_doc,
        runtime_profile,
        targets,
        *,
        owner_binding=None,
        owner_control_binding=None,
    ):
        self.raw = copy.deepcopy(raw)
        self.questions = copy.deepcopy(list(questions))
        self.company_doc = copy.deepcopy(company_doc)
        self.company_ref = copy.deepcopy(raw["company_ref"])
        self.evidence_board_doc = copy.deepcopy(evidence_board_doc)
        self.runtime_profile = copy.deepcopy(runtime_profile)
        self.targets = copy.deepcopy(targets)
        self.owner_binding = copy.deepcopy(owner_binding)
        self.owner_control_binding = copy.deepcopy(owner_control_binding)
        self.task_sha256 = canonical_sha256(self.raw)

    @property
    def question_ids(self):
        return [row["question_id"] for row in self.questions]

    @property
    def limits(self):
        return self.raw["limits"]


def create_owner_task(
    raw,
    questions,
    company_doc,
    evidence_board_doc,
    runtime_profile,
    targets,
    *,
    owner_binding=None,
    owner_control_binding=None,
):
    """Create an owner-bound task without exposing constructor field layout."""
    values = (raw, questions, company_doc, evidence_board_doc, runtime_profile, targets)
    if any(not isinstance(value, (dict, list)) for value in values):
        raise ValueError("company_task_contract_payload_required")
    return FakeOwnerTask(
        raw,
        questions,
        company_doc,
        evidence_board_doc,
        runtime_profile,
        targets,
        owner_binding=owner_binding,
        owner_control_binding=owner_control_binding,
    )


def task_snapshot(task):
    """Return a detached JSON-only contract; never expose handle state."""
    if not isinstance(task, FakeOwnerTask):
        raise ValueError("company_task_owner_handle_required")
    result = {
        "protocol": TASK_CONTRACT_PROTOCOL,
        "task_sha256": task.task_sha256,
        "raw": copy.deepcopy(task.raw),
        "questions": copy.deepcopy(task.questions),
        "question_ids": list(task.question_ids),
        "company_doc": copy.deepcopy(task.company_doc),
        "company_ref": copy.deepcopy(task.company_ref),
        "evidence_board_doc": copy.deepcopy(task.evidence_board_doc),
        "runtime_profile": copy.deepcopy(task.runtime_profile),
        "targets": copy.deepcopy(task.targets),
        "limits": copy.deepcopy(task.limits),
        "owner_binding": copy.deepcopy(task.owner_binding),
        "owner_control_binding": copy.deepcopy(task.owner_control_binding),
    }
    result["contract_sha256"] = canonical_sha256(result)
    json.dumps(result, ensure_ascii=False, allow_nan=False)
    return result


def task_from_snapshot(contract):
    """Rebuild an opaque owner handle from a detached public task document."""
    if not isinstance(contract, dict) or contract.get("protocol") != TASK_CONTRACT_PROTOCOL:
        raise ValueError("company_task_contract_version_unsupported")
    if set(contract) != set(CONTRACT_FIELDS):
        raise ValueError("company_task_contract_shape_invalid")
    without_digest = copy.deepcopy(contract)
    supplied_digest = without_digest.pop("contract_sha256")
    if not isinstance(supplied_digest, str) or supplied_digest != canonical_sha256(without_digest):
        raise ValueError("company_task_contract_digest_mismatch")
    raw = contract["raw"]
    questions = contract["questions"]
    if not isinstance(raw, dict) or not isinstance(questions, list):
        raise ValueError("company_task_contract_payload_invalid")
    return FakeOwnerTask(
        raw,
        questions,
        contract["company_doc"],
        contract["evidence_board_doc"],
        contract["runtime_profile"],
        contract["targets"],
        owner_binding=contract["owner_binding"],
        owner_control_binding=contract["owner_control_binding"],
    )


__all__ = [
    "PUBLIC_API_PROTOCOL",
    "TASK_CONTRACT_PROTOCOL",
    "create_owner_task",
    "task_from_snapshot",
    "task_snapshot",
]
