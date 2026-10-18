"""Strict rc2 native-wire validation; hashes are bindings, not agent identity.

Only the existing fixed native StockWiki subprocess supplies this envelope.
Current revocation is checked there on every inspection, including pre-POST.
No user/consumer JSON can invoke this helper as permission admission.
"""

import hashlib
import re
from datetime import datetime, timedelta
from pathlib import Path

from .quick_scan_result_outbox import canonical_sha256

GRANT_FIELDS = {
    "protocol",
    "grant_id",
    "owner_principal",
    "workspace_root",
    "agent",
    "agent_assurance",
    "authorization_basis",
    "authorization_reference",
    "binding",
    "created_at",
    "expires_at",
    "record_sha256",
}
CONFIRMATION_FIELDS = {
    "protocol",
    "confirmation_id",
    "owner_principal",
    "consumer_id",
    "idempotency_key_sha256",
    "preview_id",
    "preview_record_sha256",
    "request_sha256",
    "scope_sha256",
    "owner_state_sha256",
    "policy_sha256",
    "confirmed_at",
    "method",
    "record_sha256",
    "delegation",
}


def _utc(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("delegation UTC required")
    return datetime.fromisoformat(value[:-1] + "+00:00")


def validate_delegated_native(result, *, workspace_root, budget_policy_file, now):
    receipt = result["confirmation"]
    if (
        set(receipt) != CONFIRMATION_FIELDS
        or receipt["protocol"] != "stockwiki.refresh_confirmation_record/2.0.0"
        or receipt["method"] != "native_owner_delegated_agent"
    ):
        raise ValueError("explicit delegated confirmation required")
    grant = receipt["delegation"]
    if (
        not isinstance(grant, dict)
        or set(grant) != GRANT_FIELDS
        or grant["protocol"] != "stockwiki.refresh_delegation/1.0.0"
    ):
        raise ValueError("explicit delegated grant required")
    if (
        grant["record_sha256"]
        != canonical_sha256({k: v for k, v in grant.items() if k != "record_sha256"})
        or grant["owner_principal"] != result["owner_policy"]["owner_principal"]
        or grant["owner_principal"] != receipt["owner_principal"]
        or grant["workspace_root"] != str(Path(workspace_root).resolve())
    ):
        raise ValueError("native delegation namespace mismatch")
    agent = grant["agent"]
    if (
        not isinstance(agent, dict)
        or set(agent) != {"profile_id", "profile_sha256", "harness_ref"}
        or not isinstance(agent["profile_sha256"], str)
        or re.fullmatch("[a-f0-9]{64}", agent["profile_sha256"]) is None
        or any(
            not isinstance(agent[k], str)
            or not 1 <= len(agent[k]) <= (160 if k == "profile_id" else 512)
            or any(ord(char) < 32 for char in agent[k])
            for k in ("profile_id", "harness_ref")
        )
        or grant["agent_assurance"] != "local_owner_namespace"
        or grant["authorization_basis"] != "recorded_user_authorization"
        or not isinstance(grant["authorization_reference"], str)
        or not 1 <= len(grant["authorization_reference"]) <= 512
        or any(ord(char) < 32 for char in grant["authorization_reference"])
    ):
        raise ValueError("unproved agent identity claim forbidden")
    preview, request, state = result["preview"], result["request"], result["owner_state"]
    record = dict(
        protocol="stockwiki.refresh_record/1.0.0",
        request=request,
        preview=preview,
        owner_state=state,
        owner_principal=receipt["owner_principal"],
    )
    expected = dict(
        preview_id=preview["preview_id"],
        preview_record_sha256=canonical_sha256(record),
        **{
            k: preview[k]
            for k in ("request_sha256", "scope_sha256", "owner_state_sha256", "policy_sha256")
        },
        consumer_id=request["consumer_id"],
        store_id=state["store_id"],
        subject_refs=request["subject_refs"],
        field_ids=request["field_ids"],
        items=state["items"],
        anchors=state["anchors"],
        provider=preview["provider"],
        model=preview["model"],
        cohort_id=request.get("cohort_id"),
        quote=preview["quote"],
        qa=result["owner_policy"]["qa"],
        budget_policy_sha256=hashlib.sha256(Path(budget_policy_file).read_bytes()).hexdigest(),
    )
    if (
        grant["binding"] != expected
        or receipt["preview_record_sha256"] != expected["preview_record_sha256"]
    ):
        raise ValueError("native delegation exact scope mismatch")
    grant_id = "dg1_" + canonical_sha256(
        dict(
            owner_principal=grant["owner_principal"],
            workspace_root=grant["workspace_root"],
            consumer_id=request["consumer_id"],
            preview_id=preview["preview_id"],
        )
    )
    expected_id = "rc2_" + canonical_sha256(
        dict(
            owner_principal=grant["owner_principal"],
            consumer_id=request["consumer_id"],
            grant_id=grant_id,
        )
    )
    if grant["grant_id"] != grant_id or receipt["confirmation_id"] != expected_id:
        raise ValueError("delegated single action identifier mismatch")
    if not _utc(grant["created_at"]) <= _utc(receipt["confirmed_at"]) <= _utc(now) < _utc(
        grant["expires_at"]
    ) or _utc(grant["expires_at"]) - _utc(grant["created_at"]) > timedelta(hours=24):
        raise ValueError("delegated action expired or future")
