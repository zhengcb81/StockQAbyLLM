"""Q10: deterministic C06 v1 exchange-package construction bound to a Q07
answer checkpoint.

The adapter NEVER invents facts: every identity/answer/execution field comes
from the checkpoint payload (answer/work/provenance — already validated by
``save_answer_checkpoint``), and every authority field (contract versions,
capabilities, producer identity) must be SUPPLIED by the caller. A missing
authority field, an unknown answer status, or a missing execution fact raises
:class:`MissingC06Fields` — the caller marks the work item durably BLOCKED
(work stays result_ready, no re-ask, nothing fabricated) exactly as Q10
step 1 / DB-07 require.
"""

from __future__ import annotations

from typing import Any

from src.utils.quick_scan_result_outbox import (
    _CAPABILITIES,
    _CONTRACT_VERSION_KEYS,
    canonical_sha256,
)

__all__ = ["MissingC06Fields", "build_c06_package", "build_complete_c06_package"]

_STATUS_MAP = {
    "scored": "scored",
    "insufficient_evidence": "insufficient_evidence",
    "not_applicable": "not_applicable",
}


class MissingC06Fields(ValueError):
    """Authority/execution facts required for a complete C06 package are
    missing — durably block the work item instead of fabricating fields."""


def _require_text(container: dict, key: str, what: str) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MissingC06Fields(f"{what} is missing: {key}")
    return value


def build_c06_package(
    checkpoint_payload: dict[str, Any], *, authority: dict[str, Any]
) -> dict[str, Any]:
    """Build one complete C06 v1 package bound to the checkpoint payload.

    ``authority`` supplies the non-derivable envelope facts:
    ``contract_versions`` (the exact five-key set), ``capabilities`` (must
    include ``standard_observation_v1`` and stay within the known set), and
    ``producer_component_version`` / ``producer_build_id``.
    """
    if not isinstance(checkpoint_payload, dict):
        raise MissingC06Fields("checkpoint payload must be a dict")
    if checkpoint_payload.get("checkpoint_schema") != "quick-scan-answer" or (
        checkpoint_payload.get("checkpoint_schema_version") != 1
    ):
        raise MissingC06Fields("unsupported checkpoint schema")
    work = checkpoint_payload.get("work")
    if not isinstance(work, dict):
        raise MissingC06Fields("checkpoint payload is missing work")
    answer = checkpoint_payload.get("answer")
    if not isinstance(answer, dict):
        raise MissingC06Fields("checkpoint payload is missing answer")
    provenance = checkpoint_payload.get("provenance")
    if not isinstance(provenance, dict):
        raise MissingC06Fields("checkpoint payload is missing provenance")

    saved_status = answer.get("status")
    if saved_status not in _STATUS_MAP:
        # unknown answers cannot be packaged — durable block, never fabricated
        raise MissingC06Fields(f"answer status is not packageable: {saved_status!r}")
    if not isinstance(answer.get("entity_id"), str) or not answer["entity_id"]:
        raise MissingC06Fields("answer entity_id is missing")
    if not isinstance(answer.get("question_id"), str) or not answer["question_id"]:
        raise MissingC06Fields("answer question_id is missing")
    if not isinstance(answer.get("description"), str) or not answer["description"]:
        raise MissingC06Fields("answer description is missing")
    question_id = answer["question_id"]
    if work.get("question_id") != question_id:
        raise MissingC06Fields("work and answer question ids disagree")
    scope = work.get("scope")
    if scope not in {"entity", "security", "segment"}:
        raise MissingC06Fields("work scope is invalid")
    scope_id = work.get("scope_id")
    if not isinstance(scope_id, str) or not scope_id:
        raise MissingC06Fields("work scope_id is missing")

    # authority validation — every non-derivable envelope fact must be supplied
    contract_versions, capabilities, producer_component_version, producer_build_id = (
        _envelope_facts(authority)
    )

    # execution facts — all nine must exist as non-empty provenance strings
    execution = {
        "provider": _require_text(provenance, "actual_provider", "execution"),
        "model_requested": _require_text(provenance, "model_requested", "execution"),
        "model_resolved": _require_text(provenance, "actual_model", "execution"),
        "request_id": _require_text(provenance, "request_id", "execution"),
        "attempt_id": _require_text(provenance, "provider_attempt_id", "execution"),
        "search_status": _require_text(provenance, "search_status", "execution"),
        "search_receipt_id": _require_text(provenance, "search_receipt_id", "execution"),
        "prompt_sha256": _require_text(provenance, "provider_prompt_sha256", "execution"),
        "answered_at": _require_text(provenance, "response_completed_at", "execution"),
    }

    source_urls = provenance.get("source_urls")
    source_urls = (
        [url for url in source_urls if isinstance(url, str) and url.strip()]
        if isinstance(source_urls, list)
        else []
    )
    evidence = [{"url": url} for url in source_urls]
    if saved_status == "scored" and not evidence:
        raise MissingC06Fields("scored answer requires evidence bound to provenance URLs")

    observation_id = "obs_" + canonical_sha256(
        {
            "entity_id": answer["entity_id"],
            "question_id": question_id,
            "scope": scope,
        }
    )
    observation = {
        "observation_id": observation_id,
        "entity_id": answer["entity_id"],
        "question_id": question_id,
        "scope": scope,
        "security_id": scope_id if scope == "security" else None,
        "segment_id": scope_id if scope == "segment" else None,
        "answer": {
            "question_id": question_id,
            "response_kind": "score",
            "score": answer["score"],
            "summary": answer["description"],
            "status": _STATUS_MAP[saved_status],
            "evidence": evidence,
        },
        "execution": execution,
    }
    return _package_from_observation(
        contract_versions,
        capabilities,
        producer_component_version,
        producer_build_id,
        observation,
    )


def _envelope_facts(
    authority: dict[str, Any] | None,
) -> tuple[dict[str, str], list[str], str, str]:
    """Validate the non-derivable envelope facts every package must carry."""
    if not isinstance(authority, dict):
        raise MissingC06Fields("authority must be a dict")
    contract_versions = authority.get("contract_versions")
    if not isinstance(contract_versions, dict) or set(contract_versions) != set(
        _CONTRACT_VERSION_KEYS
    ):
        raise MissingC06Fields("authority contract_versions must carry the exact five-key set")
    if any(not isinstance(value, str) or not value.strip() for value in contract_versions.values()):
        raise MissingC06Fields("authority contract_versions values must be non-empty")
    capabilities = authority.get("capabilities")
    if (
        not isinstance(capabilities, list)
        or not capabilities
        or any(cap not in _CAPABILITIES for cap in capabilities)
        or "standard_observation_v1" not in capabilities
    ):
        raise MissingC06Fields(
            "authority capabilities must be known and include standard_observation_v1"
        )
    producer_component_version = authority.get("producer_component_version")
    producer_build_id = authority.get("producer_build_id")
    if not isinstance(producer_component_version, str) or not producer_component_version.strip():
        raise MissingC06Fields("authority producer_component_version is missing")
    if not isinstance(producer_build_id, str) or not producer_build_id.strip():
        raise MissingC06Fields("authority producer_build_id is missing")
    return (
        dict(contract_versions),
        list(capabilities),
        producer_component_version,
        producer_build_id,
    )


def _package_from_observation(
    contract_versions: dict[str, str],
    capabilities: list[str],
    producer_component_version: str,
    producer_build_id: str,
    observation: dict[str, Any],
) -> dict[str, Any]:
    """Wrap ONE already-validated observation in the fixed C06 1.0.0 envelope."""
    observation_id = observation["observation_id"]
    payload_sha256 = canonical_sha256(observation)
    item_id = "itm_" + canonical_sha256(
        {"observation_id": observation_id, "payload_sha256": payload_sha256}
    )
    package_body = {
        "schema_version": "1.0.0",
        "producer": {
            "component": "StockQAbyLLM",
            "component_version": producer_component_version,
            "build_id": producer_build_id,
        },
        "consumer": {
            "component": "StockWiki",
            "namespace": "quick_scan",
            "minimum_schema_version": "1.0.0",
        },
        "created_at": observation["execution"]["answered_at"],
        "data_class": "lightweight_screening",
        "required_capabilities": list(capabilities),
        "contract_versions": dict(contract_versions),
        "document_payloads_included": False,
        "items": [
            {
                "item_id": item_id,
                "observation_id": observation_id,
                "payload_sha256": payload_sha256,
                "observation": observation,
            }
        ],
        "extensions": [],
    }
    package_sha256 = canonical_sha256(package_body)
    return {
        **package_body,
        "package_sha256": package_sha256,
        "package_id": "pkg_" + package_sha256,
    }


def build_complete_c06_package(
    checkpoint_payload: dict[str, Any],
    *,
    authority: dict[str, Any],
    context: dict[str, Any],
    standard_answer: dict[str, Any],
    started_at: str,
) -> dict[str, Any]:
    """Build the complete-observation C06 package (authority 2.0.0).

    NOTHING is derived from the compact checkpoint: the frozen bound context
    supplies identity/scope/metadata, the stored standard answer supplies the
    body, and the durable attempt supplies the execution instants
    (``started_at`` is the original send-intent instant, ``answered_at`` the
    original response completion). The observation ID is the IQS canonical
    digest of the full content, never a re-derived qid or a random id.
    """
    if not isinstance(checkpoint_payload, dict):
        raise MissingC06Fields("checkpoint payload must be a dict")
    if checkpoint_payload.get("checkpoint_schema") != "quick-scan-answer" or (
        checkpoint_payload.get("checkpoint_schema_version") != 1
    ):
        raise MissingC06Fields("unsupported checkpoint schema")
    if (
        not isinstance(context, dict)
        or context.get("schema") != "stockqa.quick_scan_question_context/1.0.0"
    ):
        raise MissingC06Fields("complete sealing needs a bound frozen context")
    if not isinstance(standard_answer, dict):
        raise MissingC06Fields("complete sealing needs the full standard answer")
    if standard_answer.get("status") not in _STATUS_MAP:
        raise MissingC06Fields(
            f"answer status is not packageable: {standard_answer.get('status')!r}"
        )
    contract_versions, capabilities, component_version, build_id = _envelope_facts(authority)

    from src.utils.quick_scan_observation_context import build_observation

    try:
        observation = build_observation(
            checkpoint_payload,
            context=context,
            standard_answer=standard_answer,
            started_at=started_at,
        )
    except ValueError as error:
        raise MissingC06Fields(f"complete observation refused: {error}") from error
    return _package_from_observation(
        contract_versions, capabilities, component_version, build_id, observation
    )
