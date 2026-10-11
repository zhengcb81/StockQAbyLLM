"""Versioned, independent subject context; never modifies standard metadata."""

from __future__ import annotations

import copy
import re

CONTEXT_SCHEMA = "stockqa.quick_scan_observation_context/2.0.0"
QUESTION_SCHEMA = "stockqa.quick_scan_question_context/2.0.0"
SUBJECT_FIELDS = {
    "entity_id",
    "analysis_subject_id",
    "analysis_subject_revision",
    "primary_issuer_id",
    "perimeter_sha256",
    "scope",
    "scope_id",
    "security_id",
    "segment_id",
    "listing_id",
}


def validate_subject_binding(binding, metadata, *, identity_revision=None):
    if (
        not isinstance(binding, dict)
        or set(binding) != {"protocol", "subject_ref", "identity_revision", "question_contract"}
        or binding["protocol"] != "stockqa.subject_context/1.0.0"
        or type(binding["identity_revision"]) is not int
        or binding["identity_revision"] < 1
        or identity_revision is not None
        and binding["identity_revision"] != identity_revision
    ):
        raise ValueError("subject_context_version_or_revision_mismatch")
    ref = binding["subject_ref"]
    if (
        not isinstance(ref, dict)
        or set(ref) != SUBJECT_FIELDS
        or type(ref["analysis_subject_revision"]) is not int
        or ref["analysis_subject_revision"] < 1
        or not isinstance(ref["perimeter_sha256"], str)
        or re.fullmatch("[a-f0-9]{64}", ref["perimeter_sha256"]) is None
        or not isinstance(ref["analysis_subject_id"], str)
        or re.fullmatch(
            "ASJ_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            ref["analysis_subject_id"],
        )
        is None
        or any(
            not isinstance(ref[k], str) or not ref[k] or len(ref[k]) > 160
            for k in ("entity_id", "primary_issuer_id", "scope_id")
        )
        or any(ref[k] != metadata[k] for k in ("entity_id", "scope", "security_id", "segment_id"))
    ):
        raise ValueError("subject_context_scope_mismatch")
    expected_id = (
        metadata["entity_id"]
        if metadata["scope"] == "entity"
        else (
            metadata["security_id"] if metadata["scope"] == "security" else metadata["segment_id"]
        )
    )
    if (
        ref["scope_id"] != expected_id
        or (
            metadata["scope"] == "security"
            and (not isinstance(ref["listing_id"], str) or not ref["listing_id"])
        )
        or (metadata["scope"] != "security" and ref["listing_id"] is not None)
    ):
        raise ValueError("subject_context_listing_or_scope_mismatch")
    q = binding["question_contract"]
    if (
        not isinstance(q, dict)
        or q.get("id") != metadata["question_id"]
        or q.get("response_kind", "score") not in {"score", "fact"}
    ):
        raise ValueError("subject_context_question_mismatch")
    return copy.deepcopy(binding)
