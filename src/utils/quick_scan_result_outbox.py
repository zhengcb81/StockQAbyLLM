"""Versioned validation helpers for immutable quick-scan result delivery.

The owner project validates the complete C06 Observation schema on import.
StockQA independently verifies package addressing, checkpoint bindings, and
the exact allowlisted ACK fields; it never synthesizes missing Observation
fields from the compact answer checkpoint.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from typing import Any

OUTBOX_SCHEMA = "quick-scan-result-outbox"
OUTBOX_SCHEMA_VERSION = 1
ACK_SCHEMA_VERSION = "1.0.0"

_HEX_256 = re.compile(r"^[a-f0-9]{64}$")
_PACKAGE_ID = re.compile(r"^pkg_[a-f0-9]{64}$")
_ITEM_ID = re.compile(r"^itm_[a-f0-9]{64}$")
_OBSERVATION_ID = re.compile(r"^obs_[a-f0-9]{64}$")
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_ACK_ID = re.compile(r"^ack_[A-Za-z0-9_-]{8,160}$")
_FORBIDDEN_KEYS = {
    "accepted_ids",
    "document_body",
    "document_payload",
    "document_text",
    "evidence_span",
    "evidence_span_id",
    "evidence_span_ids",
    "raw_document",
    "raw_html",
    "source_manifest",
    "source_manifest_id",
    "source_manifest_ids",
}
_CAPABILITIES = {
    "entity_security_identity_v1",
    "standard_observation_v1",
    "score_v1",
    "fact_v1",
}
_CONTRACT_VERSION_KEYS = {
    "identity_schema",
    "answer_schema",
    "observation_schema",
    "question_catalog",
    "model_policy_schema",
}
_ACK_ERRORS = {
    "immutable_key_hash_conflict",
    "unsupported_schema",
    "missing_entity",
    "unknown_question",
    "invalid_payload",
    "lineage_violation",
}


def canonical_bytes(value: object) -> bytes:
    """Serialize values using the C06 UTF-8 canonical JSON rule."""
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _text(
    value: Any,
    label: str,
    *,
    pattern: re.Pattern[str] | None = None,
    maximum: int = 500,
) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"invalid {label}")
    if pattern is not None and not pattern.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _utc_timestamp(value: Any, label: str) -> str:
    text = _text(value, label)
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        raise ValueError(f"invalid {label}") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"invalid {label}")
    return text


def _contains_forbidden_key(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).casefold() in _FORBIDDEN_KEYS or _contains_forbidden_key(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_key(child) for child in value)
    return False


def validate_exchange_package(package: object) -> dict:
    """Validate one complete C06 v1 package's envelope and content addresses.

    The receiving owner remains responsible for full JSON Schema and semantic
    validation. A work item gets one package/item so ACK and retry are atomic.
    """
    if not isinstance(package, dict):
        raise ValueError("exchange package must be an object")
    required = {
        "schema_version",
        "producer",
        "consumer",
        "created_at",
        "data_class",
        "required_capabilities",
        "contract_versions",
        "document_payloads_included",
        "items",
        "extensions",
        "package_sha256",
        "package_id",
    }
    if set(package) != required:
        raise ValueError("exchange package envelope fields mismatch")
    if package["schema_version"] != ACK_SCHEMA_VERSION:
        raise ValueError("unsupported exchange package version")
    producer = package["producer"]
    consumer = package["consumer"]
    if (
        not isinstance(producer, dict)
        or set(producer) != {"component", "component_version", "build_id"}
        or producer.get("component") != "StockQAbyLLM"
        or not isinstance(consumer, dict)
        or set(consumer) != {"component", "namespace", "minimum_schema_version"}
        or consumer.get("component") != "StockWiki"
        or consumer.get("namespace") != "quick_scan"
    ):
        raise ValueError("exchange package producer or consumer mismatch")
    _text(producer["component_version"], "producer component_version", maximum=80)
    _text(producer["build_id"], "producer build_id", maximum=160)
    _text(
        consumer["minimum_schema_version"],
        "consumer minimum_schema_version",
        pattern=re.compile(r"^\d+\.\d+\.\d+$"),
    )
    _utc_timestamp(package["created_at"], "package created_at")
    capabilities = package["required_capabilities"]
    contract_versions = package["contract_versions"]
    if not isinstance(capabilities, list) or any(
        not isinstance(capability, str) or capability not in _CAPABILITIES
        for capability in capabilities
    ):
        raise ValueError("exchange package capabilities are invalid")
    if len(set(capabilities)) != len(capabilities):
        raise ValueError("exchange package capabilities must be unique")
    if not isinstance(contract_versions, dict) or set(contract_versions) != _CONTRACT_VERSION_KEYS:
        raise ValueError("exchange package contract versions are incomplete")
    for key, value in contract_versions.items():
        if key == "answer_schema" or key == "question_catalog":
            _text(value, f"contract version {key}", maximum=80)
        else:
            _text(value, f"contract version {key}", pattern=re.compile(r"^\d+\.\d+\.\d+$"))
    if (
        package["data_class"] != "lightweight_screening"
        or package["document_payloads_included"] is not False
        or package["extensions"] != []
        or "standard_observation_v1" not in capabilities
        or not isinstance(package["items"], list)
        or len(package["items"]) != 1
    ):
        raise ValueError("exchange package is incomplete or outside the Q10 envelope")
    item = package["items"][0]
    if not isinstance(item, dict) or set(item) != {
        "item_id",
        "observation_id",
        "payload_sha256",
        "observation",
    }:
        raise ValueError("exchange package item fields mismatch")
    observation = item["observation"]
    if not isinstance(observation, dict) or _contains_forbidden_key(observation):
        raise ValueError("exchange package contains forbidden document payload fields")
    observation_id = _text(item["observation_id"], "observation_id", pattern=_OBSERVATION_ID)
    if observation.get("observation_id") != observation_id:
        raise ValueError("exchange item observation id mismatch")
    payload_sha256 = _text(item["payload_sha256"], "payload_sha256", pattern=_HEX_256)
    if canonical_sha256(observation) != payload_sha256:
        raise ValueError("exchange item payload hash mismatch")
    expected_item_id = "itm_" + canonical_sha256(
        {"observation_id": observation_id, "payload_sha256": payload_sha256}
    )
    if _text(item["item_id"], "item_id", pattern=_ITEM_ID) != expected_item_id:
        raise ValueError("exchange item id mismatch")
    package_sha256 = _text(package["package_sha256"], "package_sha256", pattern=_HEX_256)
    package_body = {
        key: value for key, value in package.items() if key not in {"package_id", "package_sha256"}
    }
    if canonical_sha256(package_body) != package_sha256:
        raise ValueError("exchange package hash mismatch")
    if _text(package["package_id"], "package_id", pattern=_PACKAGE_ID) != "pkg_" + package_sha256:
        raise ValueError("exchange package id mismatch")
    return copy.deepcopy(package)


def validate_checkpoint_binding(package: dict, checkpoint: dict) -> None:
    """Bind exchange fields present in Q07's checkpoint without inventing others."""
    payload = checkpoint.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("checkpoint payload is missing")
    work = payload.get("work")
    saved_answer = payload.get("answer")
    provenance = payload.get("provenance")
    observation = package["items"][0]["observation"]
    if (
        payload.get("checkpoint_schema") != "quick-scan-answer"
        or payload.get("checkpoint_schema_version") != 1
        or not isinstance(work, dict)
        or not isinstance(saved_answer, dict)
        or not isinstance(provenance, dict)
    ):
        raise ValueError("checkpoint binding fields are missing")
    if (
        observation.get("entity_id") != saved_answer.get("entity_id")
        or observation.get("question_id") != work.get("question_id")
        or saved_answer.get("question_id") != work.get("question_id")
        or observation.get("scope") != work.get("scope")
    ):
        raise ValueError("exchange package identity/question does not match checkpoint")
    expected_security = work.get("scope_id") if work.get("scope") == "security" else None
    expected_segment = work.get("scope_id") if work.get("scope") == "segment" else None
    if (
        observation.get("security_id") != expected_security
        or observation.get("segment_id") != expected_segment
    ):
        raise ValueError("exchange package scope does not match checkpoint")
    answer = observation.get("answer")
    if not isinstance(answer, dict) or (
        answer.get("question_id") != saved_answer.get("question_id")
        or answer.get("response_kind") != "score"
        or answer.get("score") != saved_answer.get("score")
        or answer.get("summary") != saved_answer.get("description")
    ):
        raise ValueError("exchange package answer does not match checkpoint")
    if saved_answer.get("status") == "scored" and (
        type(answer.get("score")) is not int or not 1 <= answer["score"] <= 10
    ):
        raise ValueError("exchange package score does not match checkpoint contract")
    status_map = {
        "scored": "scored",
        "insufficient_evidence": "insufficient_evidence",
        "not_applicable": "not_applicable",
    }
    saved_status: Any = saved_answer.get("status")
    expected_status = status_map.get(saved_status)
    if expected_status is None or answer.get("status") != expected_status:
        raise ValueError("checkpoint answer status requires a verified adapter")
    source_urls = provenance.get("source_urls")
    evidence = answer.get("evidence")
    if not isinstance(source_urls, list) or not isinstance(evidence, list):
        raise ValueError("checkpoint evidence provenance is missing")
    if saved_answer.get("status") == "scored" and not evidence:
        raise ValueError("scored exchange answer requires evidence")
    if any(not isinstance(source_url, str) or not source_url.strip() for source_url in source_urls):
        raise ValueError("checkpoint evidence provenance is invalid")
    for entry in evidence:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("url"), str)
            or entry["url"] not in source_urls
        ):
            raise ValueError("exchange package evidence URL is not bound to checkpoint")
    execution = observation.get("execution")
    expected_execution = {
        "provider": provenance.get("actual_provider"),
        "model_requested": provenance.get("model_requested"),
        "model_resolved": provenance.get("actual_model"),
        "request_id": provenance.get("request_id"),
        "attempt_id": provenance.get("provider_attempt_id"),
        "search_status": provenance.get("search_status"),
        "search_receipt_id": provenance.get("search_receipt_id"),
        "prompt_sha256": provenance.get("provider_prompt_sha256"),
        "answered_at": provenance.get("response_completed_at"),
    }
    if not isinstance(execution, dict) or any(
        not isinstance(value, str) or not value.strip() for value in expected_execution.values()
    ):
        raise ValueError("checkpoint lacks required C06 execution metadata")
    if any(execution.get(key) != value for key, value in expected_execution.items()):
        raise ValueError("exchange package execution does not match checkpoint")


def validate_import_ack(ack: object, outbox: dict) -> dict:
    """Validate the exact C06 per-item ACK against one durable outbox item."""
    if not isinstance(outbox, dict):
        raise ValueError("outbox item must be an object")
    if not isinstance(ack, dict) or set(ack) != {
        "schema_version",
        "ack_id",
        "package_id",
        "item_id",
        "observation_id",
        "payload_sha256",
        "status",
        "error_code",
        "received_at",
        "consumer",
    }:
        raise ValueError("import ACK fields mismatch")
    if ack["schema_version"] != ACK_SCHEMA_VERSION:
        raise ValueError("unsupported import ACK version")
    for field in ("package_id", "item_id", "observation_id", "payload_sha256"):
        if ack[field] != outbox[field]:
            raise ValueError(f"import ACK {field} does not match outbox")
    _text(ack["ack_id"], "ack_id", pattern=_ACK_ID)
    _utc_timestamp(ack["received_at"], "ACK received_at")
    if not isinstance(ack["status"], str):
        raise ValueError("invalid import ACK status")
    if ack["status"] not in {"accepted", "already_present", "conflict", "rejected"}:
        raise ValueError("invalid import ACK status")
    error_code = ack["error_code"]
    if ack["status"] in {"accepted", "already_present"}:
        if error_code is not None:
            raise ValueError("successful import ACK cannot have an error code")
    else:
        if not isinstance(error_code, str) or error_code not in _ACK_ERRORS:
            raise ValueError("invalid import ACK error code")
        if ack["status"] == "conflict" and error_code != "immutable_key_hash_conflict":
            raise ValueError("conflict ACK has an invalid error code")
        if ack["status"] == "rejected" and error_code not in {
            "unsupported_schema",
            "missing_entity",
            "unknown_question",
            "invalid_payload",
            "lineage_violation",
        }:
            raise ValueError("rejected ACK has an invalid error code")
    consumer = ack["consumer"]
    if (
        not isinstance(consumer, dict)
        or set(consumer) != {"component", "namespace", "store_id"}
        or consumer.get("component") != "StockWiki"
        or consumer.get("namespace") != "quick_scan"
    ):
        raise ValueError("import ACK consumer mismatch")
    _text(consumer.get("store_id"), "consumer store_id", maximum=160)
    return copy.deepcopy(ack)


def delivery_key(package_id: str, item_id: str, payload_sha256: str) -> str:
    """Stable idempotency key for all deliveries of the same immutable item."""
    return hashlib.sha256(f"{package_id}\n{item_id}\n{payload_sha256}".encode("utf-8")).hexdigest()
