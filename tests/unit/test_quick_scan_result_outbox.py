import copy

import pytest

from src.utils.quick_scan_result_outbox import (
    canonical_bytes,
    canonical_sha256,
    validate_exchange_package,
    validate_import_ack,
)


def _package():
    observation_id = "obs_" + "1" * 64
    observation = {
        "observation_id": observation_id,
        "entity_id": "ENT_TEST",
        "answer": {"summary": "Synthetic"},
    }
    payload_sha256 = canonical_sha256(observation)
    item_id = "itm_" + canonical_sha256(
        {"observation_id": observation_id, "payload_sha256": payload_sha256}
    )
    package = {
        "schema_version": "1.0.0",
        "producer": {
            "component": "StockQAbyLLM",
            "component_version": "1.0.0",
            "build_id": "BUILD_123",
        },
        "consumer": {
            "component": "StockWiki",
            "namespace": "quick_scan",
            "minimum_schema_version": "1.0.0",
        },
        "created_at": "2026-09-27T12:00:00Z",
        "data_class": "lightweight_screening",
        "required_capabilities": ["standard_observation_v1"],
        "contract_versions": {
            "identity_schema": "1.0.0",
            "answer_schema": "standard-1",
            "observation_schema": "1.0.0",
            "question_catalog": "3.0.0",
            "model_policy_schema": "2.0.0",
        },
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
    package["package_sha256"] = canonical_sha256(package)
    package["package_id"] = "pkg_" + package["package_sha256"]
    return package


def _outbox(package):
    item = package["items"][0]
    return {
        "package_id": package["package_id"],
        "item_id": item["item_id"],
        "observation_id": item["observation_id"],
        "payload_sha256": item["payload_sha256"],
    }


def _ack(package, *, status="accepted", error_code=None):
    return {
        "schema_version": "1.0.0",
        "ack_id": "ack_12345678",
        **_outbox(package),
        "status": status,
        "error_code": error_code,
        "received_at": "2026-09-27T12:01:00Z",
        "consumer": {
            "component": "StockWiki",
            "namespace": "quick_scan",
            "store_id": "test-store",
        },
    }


def test_canonical_json_and_package_addresses_are_stable():
    package = _package()
    expected = copy.deepcopy(package)
    assert canonical_bytes(package) == canonical_bytes(expected)
    assert validate_exchange_package(package) == package


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(required_capabilities={}),
        lambda p: p["required_capabilities"].append("standard_observation_v1"),
        lambda p: p["contract_versions"].pop("observation_schema"),
        lambda p: p["producer"].update(build_id="x" * 161),
        lambda p: p["consumer"].pop("minimum_schema_version"),
        lambda p: p["items"][0]["observation"].update(raw_document="never store"),
        lambda p: p.update(document_payloads_included=True),
    ],
)
def test_package_rejects_incomplete_duplicate_or_document_bearing_envelopes(mutate):
    package = _package()
    mutate(package)
    with pytest.raises(ValueError):
        validate_exchange_package(package)


@pytest.mark.parametrize(
    "forged_key",
    ["source_manifest", "source_manifest_id", "raw_document"],
)
def test_llm16_package_rejects_forged_authority_fields(forged_key):
    """LLM-16：模型输出夹带的权威声称字段不得通过交换包边界。"""
    package = _package()
    package["items"][0]["observation"][forged_key] = {"forged": True}
    with pytest.raises(ValueError, match="forbidden"):
        validate_exchange_package(package)


@pytest.mark.parametrize(
    "status,error_code",
    [
        ("accepted", None),
        ("already_present", None),
        ("rejected", "invalid_payload"),
        ("conflict", "immutable_key_hash_conflict"),
    ],
)
def test_exact_ack_status_and_error_semantics_are_accepted(status, error_code):
    package = _package()
    ack = _ack(package, status=status, error_code=error_code)
    assert validate_import_ack(ack, _outbox(package)) == ack


@pytest.mark.parametrize(
    "changes",
    [
        {"package_id": "pkg_" + "0" * 64},
        {"item_id": "itm_" + "0" * 64},
        {"observation_id": "obs_" + "0" * 64},
        {"payload_sha256": "0" * 64},
        {"ack_id": "arbitrary"},
        {
            "consumer": {
                "component": "Other",
                "namespace": "quick_scan",
                "store_id": "s",
            }
        },
        {"status": "accepted", "error_code": "invalid_payload"},
        {"status": "conflict", "error_code": "invalid_payload"},
        {"status": "rejected", "error_code": "immutable_key_hash_conflict"},
    ],
)
def test_ack_rejects_identity_drift_and_invalid_error_semantics(changes):
    package = _package()
    ack = _ack(package)
    ack.update(changes)
    with pytest.raises(ValueError):
        validate_import_ack(ack, _outbox(package))


def test_package_payload_hash_detects_mutation():
    package = _package()
    package["items"][0]["observation"]["answer"]["summary"] = "changed"
    with pytest.raises(ValueError, match="payload hash"):
        validate_exchange_package(package)
