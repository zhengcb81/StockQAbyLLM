"""Frozen IQS observation context and actual standard-answer validation.

No network, identity attestation, answer synthesis, or checkpoint rewriting.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource

from .quick_scan_result_outbox import canonical_sha256, strict_json_loads

CONTEXT_SCHEMA = "stockqa.quick_scan_observation_context/1.0.0"
QUESTION_CONTEXT_SCHEMA = "stockqa.quick_scan_question_context/1.0.0"
_HASH = re.compile(r"^[a-f0-9]{64}$")
_CONTEXT_FIELDS = {
    "schema",
    "manifest_content_sha256",
    "manifest_file_sha256",
    "identity_snapshot_sha256",
    "observation_schema_sha256",
    "answer_schema_sha256",
    "metric_registry_sha256",
    "questions",
}
_BOUND_FIELDS = (_CONTEXT_FIELDS - {"questions"}) | {
    "metadata",
    "frozen_prompt_sha256",
    "work_prompt_sha256",
}


@lru_cache(maxsize=1)
def _resources():
    root = Path(__file__).resolve().parents[1] / "config"
    observation = json.loads(
        (root / "quick_scan_observation.schema.json").read_text(encoding="utf-8")
    )
    answer = json.loads(
        (root / "quick_scan_answer_content.schema.json").read_text(encoding="utf-8")
    )
    metrics = json.loads((root / "quick_scan_metric_registry.json").read_text(encoding="utf-8"))
    registry = Registry().with_resources(
        [(schema["$id"], Resource.from_contents(schema)) for schema in (observation, answer)]
    )
    return observation, answer, metrics, registry


def _hash(value, label):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _validate(value, schema, registry):
    try:
        Draft202012Validator(schema, registry=registry, format_checker=FormatChecker()).validate(
            value
        )
        canonical_sha256(value)  # finite JSON only
    except (ValidationError, TypeError) as error:
        raise ValueError("value differs from the frozen standard schema") from error


def _metadata(metadata):
    observation, _, _, registry = _resources()
    absent = {"observation_id", "observed_at", "execution", "answer"}
    detached = sorted(absent & set(metadata))
    if detached:
        # A re-signed context digest never legitimises a smuggled answer or
        # execution: those four fields are produced by build_observation from
        # the real attempt, never frozen into the owner's metadata.
        raise ValueError("metadata carries detached observation fields: " + ",".join(detached))
    schema = {
        **observation,
        "required": [key for key in observation["required"] if key not in absent],
    }
    _validate(metadata, schema, registry)
    if metadata["schema_version"] != "1.1.0" or metadata["evidence_review_status"] != "unreviewed":
        raise ValueError("new model observations require released metadata and unreviewed evidence")
    if metadata["task_mode"] == "comparison":
        if not metadata["comparison_group_id"]:
            raise ValueError("comparison group is missing")
    elif metadata["comparison_group_id"] is not None:
        raise ValueError("primary scan cannot claim a comparison group")
    expected_security = metadata["security_id"] if metadata["scope"] == "security" else None
    expected_segment = metadata["segment_id"] if metadata["scope"] == "segment" else None
    if (
        metadata["security_id"] != expected_security
        or metadata["segment_id"] != expected_segment
        or (metadata["scope"] == "security" and not expected_security)
        or (metadata["scope"] == "segment" and not expected_segment)
    ):
        raise ValueError("metadata scope bindings are incomplete or conflicting")


def _resource_hashes(document):
    observation, answer, metrics, _ = _resources()
    for field, resource in [
        ("observation_schema_sha256", observation),
        ("answer_schema_sha256", answer),
        ("metric_registry_sha256", metrics),
    ]:
        if document[field] != canonical_sha256(resource):
            raise ValueError("unsupported frozen schema/metric registry hash")


def validate_context_document(context: dict[str, Any], *, expected_sha256: str) -> dict[str, Any]:
    """Validate the owner-issued context shape/hash, never infer missing facts."""
    if not isinstance(context, dict) or set(context) != _CONTEXT_FIELDS:
        raise ValueError("observation context field set mismatch")
    if context["schema"] != CONTEXT_SCHEMA:
        raise ValueError("unsupported observation context version")
    if _hash(expected_sha256, "context hash") != canonical_sha256(context):
        raise ValueError("observation context hash mismatch")
    for key in _CONTEXT_FIELDS - {"schema", "questions"}:
        _hash(context[key], key)
    _resource_hashes(context)
    questions = context["questions"]
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 1000:
        raise ValueError("bounded nonempty question map required")
    for qid, question in questions.items():
        if (
            not isinstance(qid, str)
            or not qid
            or not isinstance(question, dict)
            or set(question) != {"metadata", "frozen_prompt_sha256", "work_prompt_sha256"}
        ):
            raise ValueError("invalid frozen question context")
        for key in ("frozen_prompt_sha256", "work_prompt_sha256"):
            _hash(question[key], key)
        _metadata(question["metadata"])
        if question["metadata"]["question_id"] != qid:
            raise ValueError("question map ID differs from metadata")
    return copy.deepcopy(context)


def manifest_question_bindings(
    manifest: dict[str, Any], question: dict[str, Any]
) -> dict[str, Any]:
    """The ONE shared rule set: frozen per-question metadata == manifest bytes.

    field / construct / question version / template / cutoff / module / method /
    cohort / cycle sensitivity / definition / semantic — derived from the exact
    manifest the run loaded, never re-typed per call site.
    """
    profile = manifest["profile"]
    return {
        "question_id": question["id"],
        "field_id": question.get("field_id", question.get("metric_id")),
        "construct_id": question.get("construct_id"),
        "question_version": question["rubric_version"],
        "template_version": manifest["template_version"],
        "information_cutoff": profile["as_of"],
        "module_package_id": manifest["module_package_id"],
        "module_release_id": manifest["module_release_id"],
        "question_definition_sha256": question["definition_sha256"],
        "question_semantic_sha256": question["semantic_sha256"],
        "method_id": "module-locked-v1/"
        + manifest["method_id"]
        + "/"
        + question["semantic_sha256"][:16],
        "cohort": {
            "company_type": profile.get("company_type", "unclassified"),
            "industries": sorted(profile.get("industry_modules", [])),
            "stage": profile.get("stage", "unclassified"),
            "subtype": profile.get("business_subtype"),
        },
        "cycle_sensitive": bool(profile.get("cycle_sensitive", False)),
    }


def check_context_matches_manifest(context: dict[str, Any], manifest: dict[str, Any]) -> None:
    """Bind both manifest hashes and EVERY frozen question to the manifest file.

    Shared by the v2 authority loader (before any key read or HTTP) and by
    ``bind_question_context`` — one implementation, so no second,
    omission-prone validation copy can ever drift apart. Raises ``ValueError``
    on the first divergence.
    """
    if not isinstance(manifest, dict) or manifest.get("answer_format") != "standard-1":
        raise ValueError("context needs a standard-1 manifest")
    original = {
        key: value
        for key, value in manifest.items()
        if key not in {"manifest_sha256", "manifest_path", "contract_id"}
    }
    if context["manifest_file_sha256"] != manifest.get("manifest_sha256") or context[
        "manifest_content_sha256"
    ] != canonical_sha256(original):
        raise ValueError("manifest hashes do not match the manifest file")
    questions = manifest.get("questions")
    known = (
        {question.get("id"): question for question in questions}
        if isinstance(questions, list)
        else {}
    )
    if set(known) != set(context["questions"]):
        raise ValueError("context question set differs from the frozen manifest")
    profile = manifest["profile"]
    for question_id, selected in context["questions"].items():
        question = known[question_id]
        metadata = selected["metadata"]
        expected = manifest_question_bindings(manifest, question)
        if any(metadata.get(key) != value for key, value in expected.items()):
            raise ValueError(f"context metadata differs from the frozen manifest for {question_id}")
        expected_scope = (
            "segment"
            if profile.get("segment_id") and question["scope"] == "entity"
            else question["scope"]
        )
        if (
            metadata.get("entity_id") != profile.get("entity_id")
            or metadata.get("scope") != expected_scope
        ):
            raise ValueError(
                f"context entity/scope differs from the frozen manifest for {question_id}"
            )
        # QR1B: the specific listing ID must be bound at the entry too — a
        # security/segment question may only carry the listing frozen in the
        # manifest profile, so a re-signed SEC_FOREIGN/segment label is refused
        # before any key read, HTTP, budget reservation or checkpoint.
        if expected_scope == "security" and metadata.get("security_id") != profile.get(
            "security_id"
        ):
            raise ValueError(
                f"context security_id differs from the frozen manifest profile for {question_id}"
            )
        if expected_scope == "segment" and metadata.get("segment_id") != profile.get("segment_id"):
            raise ValueError(
                f"context segment_id differs from the frozen manifest profile for {question_id}"
            )
        prompt = question.get("prompt")
        if (
            not isinstance(prompt, str)
            or selected["frozen_prompt_sha256"] != question.get("prompt_sha256")
            or selected["frozen_prompt_sha256"]
            != hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            or selected["work_prompt_sha256"]
            != hashlib.sha256(prompt.strip().encode("utf-8")).hexdigest()
        ):
            raise ValueError(f"context prompt differs from the frozen manifest for {question_id}")


def bind_question_context(
    context: dict[str, Any],
    *,
    manifest: dict[str, Any],
    identity_snapshot_sha256: str,
    entity_id: str,
    question_id: str,
    scope: str,
    scope_id: str,
) -> dict[str, Any]:
    """Bind independently loaded files and the actual work scope before dispatch."""
    validate_context_document(context, expected_sha256=canonical_sha256(context))
    check_context_matches_manifest(context, manifest)
    if context["identity_snapshot_sha256"] != identity_snapshot_sha256:
        raise ValueError("context belongs to a different identity snapshot")
    known = {question["id"]: question for question in manifest["questions"]}
    if question_id not in known or question_id not in context["questions"]:
        raise ValueError("context question set differs from the frozen manifest")
    question = known[question_id]
    selected = context["questions"][question_id]
    metadata = selected["metadata"]
    profile = manifest["profile"]
    if profile.get("entity_id") != entity_id or metadata["entity_id"] != entity_id:
        raise ValueError("context entity does not match work")
    expected_id = (
        metadata["security_id"]
        if scope == "security"
        else (metadata["segment_id"] if scope == "segment" else entity_id)
    )
    expected_scope = (
        "segment"
        if profile.get("segment_id") and question["scope"] == "entity"
        else question["scope"]
    )
    if scope != expected_scope or metadata["scope"] != scope or scope_id != expected_id:
        raise ValueError("context scope does not match work")
    bound = {key: value for key, value in context.items() if key != "questions"}
    bound.update(selected)
    bound["schema"] = QUESTION_CONTEXT_SCHEMA
    return copy.deepcopy(bound)


def bind_context_to_work_item(
    context: dict[str, Any], *, work_item: dict[str, Any], question_id: str
) -> dict[str, Any]:
    """Re-derive the bound question context from a DURABLE store row.

    Seal/restart has no question file to re-read: the frozen context side
    table and the stored work row are the only inputs. Every binding the
    dispatcher checked (identity bytes, stripped prompt fingerprint, scope and
    scope id) is re-checked here, so a context stored for one task can never
    be replayed onto another.
    """
    validate_context_document(context, expected_sha256=canonical_sha256(context))
    if not isinstance(question_id, str) or question_id not in context["questions"]:
        raise ValueError("work item question is absent from the frozen context")
    selected = context["questions"][question_id]
    metadata = selected["metadata"]
    scope = work_item.get("scope")
    scope_id = work_item.get("scope_id")
    expected_scope_id = (
        metadata["security_id"]
        if scope == "security"
        else (metadata["segment_id"] if scope == "segment" else metadata["entity_id"])
    )
    if (
        work_item.get("entity_id") != metadata["entity_id"]
        or metadata["scope"] != scope
        or scope_id != expected_scope_id
        or work_item.get("identity_snapshot_sha256") != context["identity_snapshot_sha256"]
        or work_item.get("question_fingerprint") != selected["work_prompt_sha256"]
    ):
        raise ValueError("frozen context does not bind the stored work item")
    bound = {key: value for key, value in context.items() if key != "questions"}
    bound.update(selected)
    bound["schema"] = QUESTION_CONTEXT_SCHEMA
    return copy.deepcopy(bound)


def parse_standard_answer(text: Any) -> dict[str, Any] | None:
    """Return the standard body a description carries, or None.

    A description that is not a JSON object with the standard answer identity
    keys is plain prose — the caller keeps its historical compact handling. A
    description that IS shaped like a standard answer is never silently
    downgraded: the caller must validate it or refuse the answer. A JSON-shaped
    body with duplicate keys or non-finite numbers is CORRUPT — it is refused
    loudly (``ValueError``) instead of silently keeping the last value or
    falling back to the legacy compact path.
    """
    if not isinstance(text, str):
        return None
    candidate = text.strip()
    if not (candidate.startswith("{") and candidate.endswith("}")):
        return None
    try:
        body = strict_json_loads(candidate)
    except json.JSONDecodeError:
        return None  # not JSON at all: plain prose keeps legacy handling
    except ValueError as error:
        raise ValueError(f"standard answer body is not strict JSON: {error}") from error
    if not isinstance(body, dict):
        return None
    if "question_id" in body and "response_kind" in body:
        return body
    return None


def validate_standard_answer(
    content: dict[str, Any],
    *,
    metadata: dict[str, Any],
    normalized_answer: dict[str, Any],
    source_urls: list[str],
) -> dict[str, Any]:
    """Validate the model's actual body and provenance; add no answer defaults."""
    _, schema, metrics, registry = _resources()
    _validate(content, schema, registry)
    if (
        content["response_kind"] != "score"
        or content["items"]
        or content["question_id"] != metadata["question_id"]
        or normalized_answer.get("entity_id") != metadata["entity_id"]
        or any(
            content[key] != normalized_answer.get(key) for key in ("question_id", "status", "score")
        )
        or content["summary"] != normalized_answer.get("description")
    ):
        raise ValueError("standard answer does not match normalized checkpoint")
    successful = content["status"] == "scored"
    if successful != (content["score"] is not None):
        raise ValueError("standard answer score/status conflict")
    if not successful and content["metrics"]:
        raise ValueError("unavailable answer cannot assert metrics")
    cutoff = metadata["information_cutoff"]
    for key in ("information_as_of", "period_end"):
        if content[key] is not None and content[key] > cutoff:
            raise ValueError("answer information after cutoff")
    if (
        content["period_start"]
        and content["period_end"]
        and content["period_start"] > content["period_end"]
    ):
        raise ValueError("reversed answer period")
    ids = [item["id"] for item in content["evidence"]]
    if len(ids) != len(set(ids)) or (successful and not ids):
        raise ValueError("successful answer needs unique evidence")
    for evidence in content["evidence"]:
        if evidence["url"] not in source_urls or (
            evidence["published_at"] is not None and evidence["published_at"] > cutoff
        ):
            raise ValueError("evidence URL/date is not bound to search/cutoff")
    metric_ids = []
    for metric in content["metrics"]:
        metric_ids.append(metric["metric_id"])
        known = metrics["metrics"].get(metric["metric_id"])
        if (known is None and not metric["metric_id"].startswith("custom.")) or (
            known is not None and metric["unit"] != known["unit"]
        ):
            raise ValueError("unknown metric or incorrect canonical unit")
        if not set(metric["evidence_ids"]) <= set(ids) or (
            metric["value"] is not None and not metric["evidence_ids"]
        ):
            raise ValueError("numeric metric lacks evidence binding")
        if metric["unit"] in {"currency", "currency_per_share"} and metric["currency"] is None:
            raise ValueError("currency metric requires currency")
        if metric["unit"] == "other" and not metric["unit_detail"]:
            raise ValueError("other metric requires unit detail")
        if (
            metric["period_start"]
            and metric["period_end"]
            and metric["period_start"] > metric["period_end"]
            or metric["period_end"]
            and metric["period_end"] > cutoff
            and metric["basis"] != "forward"
        ):
            raise ValueError("invalid metric period")
    if len(metric_ids) != len(set(metric_ids)):
        raise ValueError("duplicate metric ID")
    return copy.deepcopy(content)


def build_observation(
    checkpoint_payload: dict[str, Any],
    *,
    context: dict[str, Any],
    standard_answer: dict[str, Any],
    started_at: str,
) -> dict[str, Any]:
    """Assemble one immutable snapshot only from frozen inputs and actual execution."""
    if (
        not isinstance(context, dict)
        or set(context) != _BOUND_FIELDS
        or context.get("schema") != QUESTION_CONTEXT_SCHEMA
    ):
        raise ValueError("unsupported bound question context")
    for key in _BOUND_FIELDS - {"schema", "metadata"}:
        _hash(context[key], key)
    _resource_hashes(context)
    metadata = context["metadata"]
    _metadata(metadata)
    if checkpoint_payload.get("checkpoint_schema") != "quick-scan-answer" or checkpoint_payload.get(
        "checkpoint_schema_version"
    ) not in {1, 2}:
        raise ValueError("unsupported answer checkpoint")
    work = checkpoint_payload["work"]
    scope_id = (
        metadata["security_id"]
        if metadata["scope"] == "security"
        else (metadata["segment_id"] if metadata["scope"] == "segment" else metadata["entity_id"])
    )
    if (
        any(work.get(key) != metadata[key] for key in ("entity_id", "question_id", "scope"))
        or work.get("scope_id") != scope_id
        or work.get("identity_snapshot_sha256") != context["identity_snapshot_sha256"]
        or work.get("question_fingerprint") != context["work_prompt_sha256"]
    ):
        raise ValueError("context does not bind the durable checkpoint")
    provenance = checkpoint_payload["provenance"]
    body = validate_standard_answer(
        standard_answer,
        metadata=metadata,
        normalized_answer=checkpoint_payload["answer"],
        source_urls=provenance["source_urls"],
    )
    execution = {
        key: provenance.get(source)
        for key, source in {
            "provider": "actual_provider",
            "model_requested": "model_requested",
            "model_resolved": "actual_model",
            "request_id": "request_id",
            "attempt_id": "provider_attempt_id",
            "search_status": "search_status",
            "search_receipt_id": "search_receipt_id",
            "prompt_sha256": "provider_prompt_sha256",
            "answered_at": "response_completed_at",
        }.items()
    }
    execution.update(
        started_at=started_at, model_revision=None
    )  # unknown, never a guessed revision
    observation = dict(
        copy.deepcopy(metadata),
        observed_at=execution["answered_at"],
        execution=execution,
        answer=body,
    )
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(execution["answered_at"].replace("Z", "+00:00"))
        if (
            start.utcoffset() is None
            or end.utcoffset() is None
            or end < start
            or metadata["information_cutoff"] > end.date().isoformat()
        ):
            raise ValueError("execution time/cutoff conflict")
    except (TypeError, AttributeError) as error:
        raise ValueError("missing actual execution timestamps") from error
    if body["status"] == "scored" and (
        execution["search_status"] != "executed" or not execution["search_receipt_id"]
    ):
        raise ValueError("scored answer requires actual search proof")
    observation["observation_id"] = "obs_" + canonical_sha256(observation)
    schema, _, _, registry = _resources()
    _validate(observation, schema, registry)
    return observation
