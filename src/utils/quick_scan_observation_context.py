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

from src.core.models import (
    QUICK_SCAN_ANSWER_DESCRIPTION_MAX_CHARS,
    SUCCESSFUL_ANSWER_STATUSES,
)

from .quick_scan_metric_coordinates import validate_metric_coordinates
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


def _subject_version(context, *, bound=False):
    from .quick_scan_subject_context import CONTEXT_SCHEMA as V2
    from .quick_scan_subject_context import QUESTION_SCHEMA as Q2

    return context.get("schema") == (Q2 if bound else V2)


def _visible_answer_schema(schema):
    """A compatible read view; frozen schema bytes and answer data stay intact."""
    view = copy.deepcopy(schema)
    view["additionalProperties"] = True
    # Fact-list cardinality and prose lengths are generation/display preferences. The
    # transport's byte ceiling bounds resources; these must not discard a paid
    # answer. Identifier bounds, types, enums and financial coordinates remain.
    prose = {
        "summary",
        "unit_detail",
        "definition",
        "description",
        "claim",
        "counterevidence",
        "reason",
        "title",
        "name",
        "segment",
    }

    def relax(node, field=None):
        if isinstance(node, dict):
            schema_type = node.get("type")
            is_array = schema_type == "array" or (
                isinstance(schema_type, list) and "array" in schema_type
            )
            if is_array:
                node.pop("maxItems", None)
            if field in prose:
                node.pop("maxLength", None)
            for key, value in node.items():
                if key == "properties" and isinstance(value, dict):
                    for name, child in value.items():
                        relax(child, name)
                else:
                    relax(value, field)
        elif isinstance(node, list):
            for child in node:
                relax(child, field)

    relax(view)
    return view


# These previously published paths are the immutable answer-format 1.0.0
# resources, not mutable "latest" aliases. A new format gets separate files and
# an explicit observation-profile mapping; existing work keeps its original map.
_ANSWER_FORMATS = {
    "1.0.0": ("quick_scan_answer_content.schema.json", "quick_scan_metric_registry.json"),
}
_OBSERVATION_FORMATS = {
    "1.1.0": ("quick_scan_observation.schema.json", "1.0.0"),
    "1.2.0": ("quick_scan_observation.v1.2.schema.json", "1.0.0"),
}


@lru_cache(maxsize=4)
def _resources(version="1.1.0"):
    if not isinstance(version, str) or version not in _OBSERVATION_FORMATS:
        raise ValueError("unsupported observation execution profile")
    root = Path(__file__).resolve().parents[1] / "config"
    observation_name, answer_version = _OBSERVATION_FORMATS[version]
    answer_name, metrics_name = _ANSWER_FORMATS[answer_version]
    observation = strict_json_loads((root / observation_name).read_text(encoding="utf-8"))
    answer = strict_json_loads((root / answer_name).read_text(encoding="utf-8"))
    metrics = strict_json_loads((root / metrics_name).read_text(encoding="utf-8"))
    registry = Registry().with_resources(
        [
            (schema["$id"], Resource.from_contents(schema))
            for schema in (observation, _visible_answer_schema(answer))
        ]
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
    if not isinstance(metadata, dict):
        raise ValueError("observation metadata must be an object")
    observation, _, _, registry = _resources(metadata.get("schema_version"))
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
    if metadata["evidence_review_status"] != "unreviewed":
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
    if "metadata" in document:
        metadata_rows = [document["metadata"]]
    else:
        questions = document.get("questions")
        if not isinstance(questions, dict) or not 1 <= len(questions) <= 1000:
            raise ValueError("bounded nonempty question map required")
        metadata_rows = [
            row.get("metadata") if isinstance(row, dict) else None for row in questions.values()
        ]
    if any(not isinstance(row, dict) for row in metadata_rows):
        raise ValueError("observation metadata must be an object")
    versions = [row.get("schema_version") for row in metadata_rows]
    version = versions[0]
    if any(candidate != version for candidate in versions):
        raise ValueError("mixed observation execution profiles")
    observation, _, _, _ = _resources(version)
    # The execution envelope is an actual supported wire profile. Whole answer
    # schema/metric-registry hashes describe the original format resources;
    # adding an unrelated metric or visible explanation must not block a paid
    # response. Their digest spelling and complete context binding are still
    # checked, while original question/subject and metric semantics are checked
    # independently by their normal validators.
    if document["observation_schema_sha256"] != canonical_sha256(observation):
        raise ValueError("unsupported frozen observation execution schema hash")
    for field in ("answer_schema_sha256", "metric_registry_sha256"):
        _hash(document[field], field)


def validate_context_document(context: dict[str, Any], *, expected_sha256: str) -> dict[str, Any]:
    """Validate the owner-issued context shape/hash, never infer missing facts."""
    if not isinstance(context, dict) or set(context) != _CONTEXT_FIELDS:
        raise ValueError("observation context field set mismatch")
    if context["schema"] not in {
        CONTEXT_SCHEMA,
        "stockqa.quick_scan_observation_context/2.0.0",
    }:
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
            or set(question)
            != (
                {"metadata", "frozen_prompt_sha256", "work_prompt_sha256"}
                | ({"subject_binding"} if _subject_version(context) else set())
            )
        ):
            raise ValueError("invalid frozen question context")
        for key in ("frozen_prompt_sha256", "work_prompt_sha256"):
            _hash(question[key], key)
        _metadata(question["metadata"])
        if question["metadata"]["question_id"] != qid:
            raise ValueError("question map ID differs from metadata")
        if _subject_version(context):
            from .quick_scan_subject_context import validate_subject_binding

            validate_subject_binding(question["subject_binding"], question["metadata"])
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


def validate_resumed_context_compatibility(
    original: dict[str, Any],
    current: dict[str, Any],
    *,
    original_sha256: str,
    current_sha256: str,
) -> None:
    """Allow only execution-attribution labels to differ, not stable contracts.

    Both documents and their own hashes are validated before comparison. This
    pure check grants no send, owner, lease, receipt or publication permission;
    callers must retain those original guards and the original stored context.
    """
    validate_context_document(original, expected_sha256=original_sha256)
    validate_context_document(current, expected_sha256=current_sha256)
    stable_hashes = []
    for document in (original, current):
        stripped = copy.deepcopy(document)
        for row in stripped["questions"].values():
            for key in ("run_id", "scan_id", "inputset_id"):
                row["metadata"].pop(key, None)
        # Canonical JSON distinguishes 1/1.0/true; Python equality does not.
        stable_hashes.append(canonical_sha256(stripped))
    if stable_hashes[0] != stable_hashes[1]:
        raise ValueError("resume_original_observation_context_stable_inputs_changed")


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
        if question.get("response_kind") == "fact" and not _subject_version(context):
            raise ValueError("fact question requires its frozen subject question contract")
        if (
            _subject_version(context)
            and selected["subject_binding"]["question_contract"] != question
        ):
            raise ValueError("subject context question differs from the frozen manifest")
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
    bound["schema"] = (
        "stockqa.quick_scan_question_context/2.0.0"
        if _subject_version(context)
        else QUESTION_CONTEXT_SCHEMA
    )
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
    if _subject_version(context):
        from .quick_scan_subject_context import validate_subject_binding

        if type(work_item.get("identity_revision")) is not int:
            raise ValueError("subject context requires durable identity revision")
        validate_subject_binding(
            selected["subject_binding"],
            metadata,
            identity_revision=work_item.get("identity_revision"),
        )
    bound["schema"] = (
        "stockqa.quick_scan_question_context/2.0.0"
        if _subject_version(context)
        else QUESTION_CONTEXT_SCHEMA
    )
    return copy.deepcopy(bound)


def parse_standard_answer(text: Any) -> dict[str, Any] | None:
    """Return the standard body a description carries, or None.

    A description that is not a JSON object with the standard answer identity
    keys is plain prose — the caller keeps its historical compact handling. A
    description that IS shaped like a standard answer is never silently
    downgraded: the caller must validate it or refuse the answer. A JSON-shaped
    body with conflicting repeated keys or non-finite numbers is refused
    loudly (``ValueError``). Identical repeated fields and one complete JSON
    fence use the same visible-answer parser as the original model transport.
    """

    if not isinstance(text, str):
        return None
    candidate = text.strip()
    if not ((candidate.startswith("{") and candidate.endswith("}")) or candidate.startswith("```")):
        return None
    from src.providers.llm_response_parser import load_visible_answer_json

    try:
        body = load_visible_answer_json(candidate)
    except json.JSONDecodeError:
        return None  # not JSON at all: plain prose keeps legacy handling
    except ValueError as error:
        raise ValueError(f"standard answer body is not strict JSON: {error}") from error
    if not isinstance(body, dict):
        return None
    if "question_id" in body and "response_kind" in body:
        return body
    return None


def split_standard_answer_transport(description: str) -> tuple[str, dict[str, Any] | None]:
    """Return the original compact summary and complete standard answer body.

    This pure transport split preserves the original description and body;
    schema/content validation and saving still belong to their original owner.
    """
    body = parse_standard_answer(description)
    if body is None:
        return description, None
    summary = body.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("standard answer does not carry a usable summary")
    if len(summary) > QUICK_SCAN_ANSWER_DESCRIPTION_MAX_CHARS:
        raise ValueError("standard answer summary exceeds the safe answer transport bound")
    return summary, body


def _evidence_url_identity(url: str) -> str:
    """Use the receipt's URL identity without laundering stripped credentials."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    from .quick_scan_work_store import (
        api_work_store_canonical_source_urls as _canonical_source_urls,
    )
    from .quick_scan_work_store import (
        api_work_store_sensitive_query_parameter as _sensitive_query_parameter,
    )

    try:
        query = urlsplit(url).query
        if any(
            _sensitive_query_parameter(key) for key, _ in parse_qsl(query, keep_blank_values=True)
        ):
            raise ValueError("sensitive evidence source URL")
        canonical = _canonical_source_urls([url])[0]
        parts = urlsplit(canonical)
        # Only a complete fixed article path makes these known referral fields
        # irrelevant to resource selection. All other query keys stay intact.
        tracking = set()
        if parts.netloc == "finance.sina.com.cn" and re.fullmatch(
            r"/wm/[0-9]{4}-[0-9]{2}-[0-9]{2}/doc-[A-Za-z0-9_-]+\.shtml", parts.path
        ):
            tracking = {"cre", "mod", "loc", "r", "rfunc", "tj", "tr"}
        elif parts.netloc == "caifuhao.eastmoney.com" and re.fullmatch(r"/news/[0-9]+", parts.path):
            tracking = {"from", "name", "gubaurl"}
        quote_navigation = parts.netloc == "quote.eastmoney.com" and re.fullmatch(
            r"/(?:sh|sz|bj)[0-9]{6}\.html", parts.path
        )
        if tracking or quote_navigation:
            query_items = parse_qsl(parts.query, keep_blank_values=True)
            canonical = urlunsplit(
                (
                    parts.scheme,
                    parts.netloc,
                    parts.path,
                    urlencode(
                        [
                            (key, value)
                            for key, value in query_items
                            if key not in tracking
                            and not (quote_navigation and key == "jump_to_web" and value == "true")
                        ]
                    ),
                    parts.fragment,
                )
            )
        return canonical
    except (TypeError, ValueError) as error:
        raise ValueError("invalid or sensitive evidence source URL") from error


def _matching_evidence_source_url(url: str, source_urls: list[str]) -> str:
    """Return receipt spelling; only explicit resource referrals may differ."""
    identity = _evidence_url_identity(url)
    matches = [source for source in source_urls if _evidence_url_identity(source) == identity]
    if url in matches:
        return url
    if matches:
        return matches[0]
    raise ValueError("evidence URL/date is not bound to search/cutoff")


def frozen_question_contract(context: dict[str, Any]) -> dict[str, Any]:
    """Return the independently bound question definition when the context has one."""
    return (context.get("subject_binding") or {}).get("question_contract") or {}


def validate_standard_answer(
    content: dict[str, Any],
    *,
    metadata: dict[str, Any],
    normalized_answer: dict[str, Any],
    source_urls: list[str],
    question_contract: dict[str, Any] | None = None,
    research_only: bool = False,
) -> dict[str, Any]:
    """Validate the model's actual body and provenance; add no answer defaults."""
    from src.providers.llm_response_parser import load_visible_answer_json

    _, schema, metrics, registry = _resources(metadata.get("schema_version", "1.1.0"))
    _validate(content, _visible_answer_schema(schema), registry)
    load_visible_answer_json(json.dumps(content, ensure_ascii=False, allow_nan=False))
    if "raw_html" in content:
        raise ValueError("full document body is not a lightweight answer")
    for field in ("entity_id", "security_id", "segment_id"):
        if field in content and content[field] != metadata.get(field):
            raise ValueError("standard answer subject mismatch: " + field)
    question = question_contract or {}
    kind = question.get("response_kind", "score")
    if (
        content["response_kind"] != kind
        or content["question_id"] != metadata["question_id"]
        or (question and question.get("id") != metadata["question_id"])
        or normalized_answer.get("entity_id") != metadata["entity_id"]
        or any(
            content[key] != normalized_answer.get(key) for key in ("question_id", "status", "score")
        )
        or content["summary"] != normalized_answer.get("description")
    ):
        raise ValueError("standard answer does not match normalized checkpoint")
    status = content["status"]
    items = content["items"]
    successful = status in SUCCESSFUL_ANSWER_STATUSES
    if kind == "score":
        if items or status == "answered" or (status == "scored") != (content["score"] is not None):
            raise ValueError("standard score answer contract conflict")
    elif (
        content["score"] is not None
        or status == "scored"
        or successful != bool(items)
        or any(item["relation"] not in question.get("relations", ()) for item in items)
    ):
        raise ValueError("standard fact answer contract conflict")
    if research_only and successful:
        raise ValueError("research-only publication cannot assert a score or fact")
    if not successful and content["metrics"] and not research_only:
        raise ValueError("unavailable answer cannot assert metrics")
    cutoff = metadata["information_cutoff"]
    if content["information_as_of"] is not None and content["information_as_of"] > cutoff:
        raise ValueError("answer information after cutoff")
    if content["basis"] != "forward" and any(
        content[key] is not None and content[key] > cutoff for key in ("period_start", "period_end")
    ):
        raise ValueError("answer information after cutoff")
    if (
        content["period_start"]
        and content["period_end"]
        and content["period_start"] > content["period_end"]
    ):
        raise ValueError("reversed answer period")
    ids = [item["id"] for item in content["evidence"]]
    if successful and not ids:
        raise ValueError("successful answer needs evidence")
    evidence_by_id: dict[str, dict[str, Any]] = {}
    for entry in content["evidence"]:
        previous = evidence_by_id.get(entry["id"])
        if previous is not None and canonical_sha256(previous) != canonical_sha256(entry):
            raise ValueError("conflicting duplicate evidence id")
        evidence_by_id[entry["id"]] = entry
    for evidence in content["evidence"]:
        _matching_evidence_source_url(evidence["url"], source_urls)
        if evidence["published_at"] is not None and evidence["published_at"] > cutoff:
            raise ValueError("evidence URL/date is not bound to search/cutoff")
    for item in items:
        if not item["evidence_ids"] or not set(item["evidence_ids"]) <= evidence_by_id.keys():
            raise ValueError("fact row lacks evidence binding")
        if item["valid_from"] and item["valid_to"] and item["valid_from"] > item["valid_to"]:
            raise ValueError("reversed relationship validity")
    metric_groups = [content["metrics"], *(item["metrics"] for item in items)]
    for group in metric_groups:
        validate_metric_coordinates(group)
    for metric in (entry for group in metric_groups for entry in group):
        known = metrics["metrics"].get(metric["metric_id"])
        # Registration governs canonical comparison, not whether received
        # content may be retained. Never guess aliases or rewrite model IDs.
        if known is not None and metric["unit"] != known["unit"]:
            raise ValueError("incorrect canonical metric unit")
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
            or metric["basis"] != "forward"
            and any(
                metric[key] is not None and metric[key] > cutoff
                for key in ("period_start", "period_end")
            )
        ):
            raise ValueError("invalid metric period")
    return copy.deepcopy(content)


def _vendor_receipt_execution(checkpoint: dict, response: dict | None) -> dict:
    """Project a verified durable response, never repair a historical checkpoint.

    The caller reads this record from QuickScanWorkStore, and the store rebuilds
    it again inside its sealing transaction. Hash consistency alone does not
    authenticate an arbitrary caller's record or grant owner/financial status.
    """
    from .quick_scan_work_store import quick_scan_receipt_sha256

    if not isinstance(response, dict) or not isinstance(response.get("receipt"), dict):
        raise ValueError("vendor identity profile requires the durable response")
    provenance = checkpoint["provenance"]
    receipt = response["receipt"]
    if (
        provenance.get("model_resolution_binding") != "durable-response-v1"
        or response.get("attempt_id") != checkpoint.get("attempt_id")
        or "request_id" not in receipt
        or receipt.get("response_json_basis") not in {"strict_http_json", "parsed_payload"}
        or receipt.get("response_status") != "completed"
        or type(receipt.get("http_status_code")) is not int
        or not 200 <= receipt["http_status_code"] < 300
    ):
        raise ValueError("durable vendor response does not bind the checkpoint")
    digest = quick_scan_receipt_sha256(receipt)
    if response.get("receipt_sha256") != digest or provenance.get("receipt_sha256") != digest:
        raise ValueError("durable vendor receipt hash mismatch")
    triples = (
        ("provider", "provider", "actual_provider"),
        ("model_requested", "requested_model", "model_requested"),
        ("model_resolved", "actual_model", "actual_model"),
        ("response_id", "response_id", "response_id"),
        ("provider_attempt_id", "attempt_id", "provider_attempt_id"),
        ("response_sha256", "response_sha256", "response_sha256"),
    )
    if any(
        response.get(column) != receipt.get(key) or provenance.get(field) != receipt.get(key)
        for column, key, field in triples
    ):
        raise ValueError("mixed durable vendor response metadata")
    if (
        provenance.get("request_id") != receipt["request_id"]
        or provenance.get("provider_prompt_sha256") != receipt.get("prompt_sha256")
        or provenance.get("response_completed_at") != receipt.get("completed_at")
    ):
        raise ValueError("checkpoint differs from original vendor execution")
    for key in ("request_payload_sha256", "response_sha256"):
        _hash(receipt.get(key), key)
    if (
        "request_payload_sha256" in provenance
        and provenance["request_payload_sha256"] != receipt["request_payload_sha256"]
    ):
        raise ValueError("checkpoint request hash differs from the durable receipt")
    return {
        "response_id": receipt["response_id"],
        "request_payload_sha256": receipt["request_payload_sha256"],
        "response_sha256": receipt["response_sha256"],
        "receipt_sha256": digest,
    }


def build_observation(
    checkpoint_payload: dict[str, Any],
    *,
    context: dict[str, Any],
    standard_answer: dict[str, Any],
    started_at: str,
    durable_response: dict[str, Any] | None = None,
    answer_source_urls: list[str] | None = None,
) -> dict[str, Any]:
    """Assemble one immutable snapshot only from frozen inputs and actual execution."""
    if (
        not isinstance(context, dict)
        or set(context)
        != (
            _BOUND_FIELDS
            | ({"subject_binding"} if _subject_version(context, bound=True) else set())
        )
        or context.get("schema")
        not in {QUESTION_CONTEXT_SCHEMA, "stockqa.quick_scan_question_context/2.0.0"}
    ):
        raise ValueError("unsupported bound question context")
    for key in _BOUND_FIELDS - {"schema", "metadata"}:
        _hash(context[key], key)
    _resource_hashes(context)
    metadata = context["metadata"]
    _metadata(metadata)
    if _subject_version(context, bound=True):
        from .quick_scan_subject_context import validate_subject_binding

        if type(checkpoint_payload.get("work", {}).get("identity_revision")) is not int:
            raise ValueError("subject context requires checkpoint identity revision")
        validate_subject_binding(
            context["subject_binding"],
            metadata,
            identity_revision=checkpoint_payload.get("work", {}).get("identity_revision"),
        )
    if checkpoint_payload.get("checkpoint_schema") != "quick-scan-answer" or checkpoint_payload.get(
        "checkpoint_schema_version"
    ) not in {
        1,
        2,
        3,
    }:
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
    validation_sources = (
        provenance["source_urls"] if answer_source_urls is None else answer_source_urls
    )
    body = validate_standard_answer(
        standard_answer,
        metadata=metadata,
        normalized_answer=checkpoint_payload["answer"],
        source_urls=validation_sources,
        question_contract=frozen_question_contract(context),
        research_only=checkpoint_payload["checkpoint_schema_version"] == 3,
    )
    # The detached observation uses the authenticated receipt's exact spelling.
    # Keep the model's standard answer, checkpoint and receipt bytes unchanged.
    for evidence in body["evidence"]:
        evidence["url"] = _matching_evidence_source_url(evidence["url"], validation_sources)
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
    if metadata["schema_version"] == "1.2.0":
        execution.update(_vendor_receipt_execution(checkpoint_payload, durable_response))
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
    if body["status"] in SUCCESSFUL_ANSWER_STATUSES and (
        execution["search_status"] != "executed" or not execution["search_receipt_id"]
    ):
        raise ValueError(f"{body['status']} answer requires actual search proof")
    observation["observation_id"] = "obs_" + canonical_sha256(observation)
    schema, _, _, registry = _resources(metadata["schema_version"])
    _validate(observation, schema, registry)
    return observation
