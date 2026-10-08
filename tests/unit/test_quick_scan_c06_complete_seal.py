"""QA-C06-02: complete-observation sealing, side tables and the revision chain.

Covers the integration rows the CLI E2E cannot reach cheaply: durable context /
standard-answer side tables, a missing input that blocks and is later supplied
with ZERO model calls, a compact package superseded by a complete revision,
and the ACK / ``send_uncertain`` negatives around a moving head.

Every identity, prompt and answer here is SYNTHETIC and derived from
``tests/fixtures/quick_scan_c06_*`` — never an owner golden.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from pathlib import Path

import pytest

from src.utils.quick_scan_c06_authority import load_c06_authority
from src.utils.quick_scan_delivery_seal import (
    BLOCK_CONTEXT,
    BLOCK_STANDARD_ANSWER,
    HISTORICAL_READONLY,
    seal_pending_deliveries,
    seal_result_delivery,
)
from src.utils.quick_scan_question_manifest import load_question_manifest
from src.utils.quick_scan_result_outbox import (
    canonical_bytes,
    canonical_sha256,
    validate_exchange_package,
)
from src.utils.quick_scan_work_store import (
    SCHEMA_VERSION,
    QuickScanWorkStore,
    WorkConflictError,
    quick_scan_receipt_sha256,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
AUTHORITY_V2 = json.loads(
    (FIXTURES / "quick_scan_c06_authority_v2_fixture.json").read_text(encoding="utf-8")
)
CONTEXT = AUTHORITY_V2["observation_context"]
MANIFEST = load_question_manifest(FIXTURES / "quick_scan_c06_manifest_v2_fixture.json")
QID = MANIFEST["questions"][0]["id"]
ENTITY = CONTEXT["questions"][QID]["metadata"]["entity_id"]
SOURCE_URL = "https://example.invalid/company"
# 2026-09-27T11:00:00Z — before the receipt's completed_at, after nothing else
CLOCK = [1_790_506_800.0]
MODEL = "mimo-v2.6-flash"

V1_AUTHORITY = {
    "schema_version": "1.0.0",
    "contract_versions": {
        "identity_schema": "2.1.0",
        "answer_schema": "quick-scan-answer-v1",
        "observation_schema": "1.0.0",
        "question_catalog": "iqs-2026-10",
        "model_policy_schema": "2.0.0",
    },
    "capabilities": [
        "entity_security_identity_v1",
        "standard_observation_v1",
        "score_v1",
    ],
    "producer_component_version": "1.0.0",
    "producer_build_id": "stockqa-offline-complete-seal",
}


def _store(tmp_path) -> QuickScanWorkStore:
    return QuickScanWorkStore(tmp_path / "quick_scan_work.sqlite", clock=lambda: CLOCK[0])


def _v2_authority(tmp_path) -> dict:
    path = tmp_path / "authority_v2.json"
    path.write_text(json.dumps(AUTHORITY_V2, ensure_ascii=False), encoding="utf-8")
    return load_c06_authority(path)


def _standard_body() -> dict:
    return {
        "question_id": QID,
        "response_kind": "score",
        "status": "scored",
        "score": 8,
        "summary": "Synthetic standard answer; not a real company conclusion.",
        "information_as_of": "2026-09-01",
        "period_start": "2026-01-01",
        "period_end": "2026-06-30",
        "basis": "current",
        "trend": "stable",
        "confidence": "medium",
        "metrics": [],
        "items": [],
        "evidence": [
            {
                "id": "e1",
                "title": "Synthetic test source",
                "url": SOURCE_URL,
                "published_at": "2026-09-01",
                "claim": "Synthetic test claim only.",
            }
        ],
        "counterevidence": "Synthetic missing detail.",
        "watch_triggers": ["Synthetic trigger"],
        "missing_fields": ["customer concentration"],
        "coverage": {
            "status": "partial",
            "reason": "Synthetic fixture has incomplete company coverage.",
        },
    }


def _checkpointed(tmp_path, *, with_context: bool = True, with_answer: bool = True) -> tuple:
    store = _store(tmp_path)
    item = store.create_or_attach(
        entity_id=ENTITY,
        question_id=QID,
        generation=1,
        scope="entity",
        scope_id=ENTITY,
        identity_revision=1,
        source_binding_version=1,
        identity_state="verified",
        source_binding_ref="BND_COMPLETE_SEAL",
        source_binding_refs=("BND_COMPLETE_SEAL",),
        identity_snapshot_sha256=CONTEXT["identity_snapshot_sha256"],
        question_fingerprint=CONTEXT["questions"][QID]["work_prompt_sha256"],
        routing_fingerprint="d" * 64,
        run_id="RUN_COMPLETE",
        scan_id="SCAN_COMPLETE",
    )
    work_id = item["work_item_id"]
    lease = store.claim(work_id, lease_seconds=60)
    assert lease is not None
    attempt = store.prepare_attempt(
        work_id,
        lease,
        route_id="route-1",
        provider="mimo",
        model_requested=MODEL,
        request_cache_key="REQ_" + hashlib.sha256(b"complete-seal").hexdigest(),
        prompt_sha256="e" * 64,
    )
    store.mark_send_intent(work_id, lease, attempt["attempt_id"])
    receipt = {
        "provider": "mimo",
        "request_id": "req_complete_01",
        "response_id": "resp_complete_01",
        "actual_model": MODEL,
        "search_status": "executed",
        "response_status": "completed",
        "http_status_code": 200,
        "attempt_id": "provider_attempt_complete_01",
        "prompt_sha256": hashlib.sha256(b"provider prompt only").hexdigest(),
        "search_receipt_id": "search_complete_01",
        "completed_at": "2026-09-27T12:00:00Z",
        "source_urls": [SOURCE_URL],
    }
    store.record_attempt_outcome(
        work_id,
        lease,
        attempt["attempt_id"],
        outcome="response_available",
        http_status_code=200,
        receipt_sha256=quick_scan_receipt_sha256(receipt),
        request_id=receipt["request_id"],
    )
    body = _standard_body()
    answer = {
        "entity_id": ENTITY,
        "question_id": QID,
        "status": "scored",
        "score": 8,
        "description": body["summary"],
    }
    checkpoint = store.save_answer_checkpoint(
        work_id,
        lease,
        attempt["attempt_id"],
        answer=answer,
        execution_receipt=receipt,
        observation_context=CONTEXT if with_context else None,
        standard_answer=body if with_answer else None,
    )
    return store, item, checkpoint, body


def test_complete_standard_answer_seals_one_revision_and_replays_idempotently(tmp_path):
    store, item, checkpoint, body = _checkpointed(tmp_path)
    work_id = item["work_item_id"]
    authority = _v2_authority(tmp_path)

    assert store.get_standard_answer(work_id)["answer"] == body
    assert store.get_observation_context(work_id)["context_sha256"] == canonical_sha256(CONTEXT)

    sealed = seal_result_delivery(store, work_id, authority=authority)
    assert sealed["action"] == "sealed"
    assert sealed["revision"] == 1
    delivery = store.get_result_delivery(work_id)
    assert delivery["state"] == "ready"
    validate_exchange_package(delivery["package"])
    observation = delivery["package"]["items"][0]["observation"]
    assert (
        observation["information_cutoff"]
        == CONTEXT["questions"][QID]["metadata"]["information_cutoff"]
    )
    assert observation["execution"]["started_at"] == "2026-09-27T11:00:00Z"
    assert observation["answer"] == body
    assert store.list_delivery_revisions(work_id)[0]["supersedes_revision"] is None

    # replaying the same inputs is byte-stable and changes nothing
    again = seal_result_delivery(store, work_id, authority=authority)
    assert again["action"] == "already_sealed"
    assert store.get_result_delivery(work_id)["package_bytes"] == delivery["package_bytes"]
    report = seal_pending_deliveries(store, authority=authority)
    assert report["model_calls"] == 0
    assert [entry["work_item_id"] for entry in report["already_sealed"]] == [work_id]
    assert store.list_delivery_revisions(work_id)[-1]["revision"] == 1


def test_a_second_frozen_context_for_the_same_task_is_refused(tmp_path):
    store, item, checkpoint, body = _checkpointed(tmp_path)
    work_id = item["work_item_id"]
    conflicting = json.loads(json.dumps(CONTEXT))
    for question in conflicting["questions"].values():
        question["metadata"]["run_id"] = "RUN_A_DIFFERENT_EXECUTION"
    with pytest.raises(WorkConflictError, match="different observation context"):
        store.attach_standard_inputs(work_id, observation_context=conflicting, standard_answer=body)
    stored = store.get_observation_context(work_id)
    assert stored["context_sha256"] == canonical_sha256(CONTEXT)
    # the immutable context row itself is not rewritten either
    with pytest.raises(Exception):
        with store._connect() as connection:
            connection.execute(
                "UPDATE quick_scan_observation_context SET context_json='{}' "
                "WHERE context_sha256=?",
                (stored["context_sha256"],),
            )


def test_missing_standard_answer_blocks_then_supplements_with_zero_model_calls(
    tmp_path,
):
    store, item, checkpoint, _ = _checkpointed(tmp_path, with_answer=False)
    work_id = item["work_item_id"]
    authority = _v2_authority(tmp_path)

    blocked = seal_result_delivery(store, work_id, authority=authority)
    assert blocked["action"] == "blocked"
    assert blocked["block_code"] == BLOCK_STANDARD_ANSWER
    assert store.get_item(work_id)["status"] == "result_ready"
    assert store.get_result_delivery(work_id)["package"] is None

    # the original frozen inputs arrive later: packaging only, model calls stay 0
    supplemented = store.attach_standard_inputs(
        work_id, observation_context=CONTEXT, standard_answer=_standard_body()
    )
    assert supplemented["context_sha256"] == canonical_sha256(CONTEXT)
    report = seal_pending_deliveries(store, authority=authority)
    assert report["model_calls"] == 0
    assert [entry["work_item_id"] for entry in report["sealed"]] == [work_id]
    assert store.get_result_delivery(work_id)["state"] == "ready"


def test_compact_package_is_superseded_and_the_old_head_ack_cannot_settle_it(tmp_path):
    store, item, checkpoint, body = _checkpointed(tmp_path)
    work_id = item["work_item_id"]
    v2 = _v2_authority(tmp_path)

    # an earlier build sealed the compact package from the same checkpoint
    compact = seal_result_delivery(store, work_id, authority=dict(V1_AUTHORITY))
    assert compact["action"] == "sealed"
    original = store.get_result_delivery(work_id)
    original_bytes = original["package_bytes"]
    original_ack = _ack(original, ack_id="ack_old_head_00001")
    assert store.list_delivery_revisions(work_id)[-1]["revision"] == 1

    superseded = seal_result_delivery(store, work_id, authority=v2)
    assert superseded["action"] == "superseded"
    assert superseded["revision"] == 2
    assert superseded["supersedes_revision"] == 1
    assert superseded["supersedes_package_id"] == original["package_id"]

    revisions = store.list_delivery_revisions(work_id)
    assert [row["revision"] for row in revisions] == [1, 2]
    current = store.get_result_delivery(work_id)
    assert current["package_bytes"] != original_bytes
    assert current["package_id"] == revisions[1]["package_id"]
    assert revisions[0]["supersedes_revision"] is None
    assert revisions[1]["supersedes_revision"] == 1

    # the OLD head's ACK must not settle the NEW head
    with pytest.raises(ValueError):
        store.apply_result_delivery_ack(work_id, original_ack)

    # dispatch and settle the CURRENT head only
    dispatch = store.begin_result_delivery(work_id)
    assert dispatch["request_bytes"] == canonical_bytes(current["package"])
    settled = store.apply_result_delivery_ack(work_id, _ack(current, ack_id="ack_new_head_00001"))
    assert settled["state"] == "delivered"
    assert store.get_item(work_id)["status"] == "delivered"
    replay = store.apply_result_delivery_ack(work_id, _ack(current, ack_id="ack_new_head_00001"))
    assert replay["ack_sha256"] == settled["ack_sha256"]
    assert [event["event_type"] for event in store.list_result_delivery_events(work_id)] == [
        "package_prepared",
        "package_prepared",
        "send_intent",
        "accepted",
    ]


def test_send_uncertain_is_reconciled_before_a_new_head_is_written(tmp_path):
    store, item, checkpoint, body = _checkpointed(tmp_path)
    work_id = item["work_item_id"]
    v2 = _v2_authority(tmp_path)
    assert seal_result_delivery(store, work_id, authority=dict(V1_AUTHORITY))["action"] == "sealed"
    store.begin_result_delivery(work_id)

    in_flight = seal_result_delivery(store, work_id, authority=v2)
    assert in_flight["action"] == "left_in_flight"
    assert store.get_result_delivery(work_id)["state"] == "send_uncertain"
    assert store.list_delivery_revisions(work_id)[-1]["revision"] == 1

    store.confirm_result_delivery_not_sent(work_id, "connection_refused_before_write")
    reconciled = seal_result_delivery(store, work_id, authority=v2)
    assert reconciled["action"] == "superseded"
    assert reconciled["revision"] == 2


def test_legacy_checkpoint_without_inputs_keeps_its_historical_package_read_only(
    tmp_path,
):
    store, item, checkpoint, _ = _checkpointed(tmp_path, with_context=False, with_answer=False)
    work_id = item["work_item_id"]
    v2 = _v2_authority(tmp_path)
    assert seal_result_delivery(store, work_id, authority=dict(V1_AUTHORITY))["action"] == "sealed"
    before = store.get_result_delivery(work_id)["package_bytes"]

    outcome = seal_result_delivery(store, work_id, authority=v2)
    assert outcome["action"] == "left_in_flight"
    assert outcome["reason"] == HISTORICAL_READONLY
    assert outcome["block_code"] == BLOCK_CONTEXT
    assert store.get_result_delivery(work_id)["package_bytes"] == before
    assert len(store.list_delivery_revisions(work_id)) == 1


def test_schema_and_side_tables_are_the_v6_shapes(tmp_path):
    assert SCHEMA_VERSION == 6
    store, item, _, _ = _checkpointed(tmp_path)
    with closing(store._connect()) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'quick_scan_%'"
            )
        }
    assert version == SCHEMA_VERSION
    assert {
        "quick_scan_observation_context",
        "quick_scan_work_context",
        "quick_scan_standard_answer",
        "quick_scan_delivery_revision",
    } <= tables


def _ack(delivery: dict, *, ack_id: str) -> dict:
    return {
        "schema_version": "1.0.0",
        "ack_id": ack_id,
        "package_id": delivery["package_id"],
        "item_id": delivery["item_id"],
        "observation_id": delivery["observation_id"],
        "payload_sha256": delivery["payload_sha256"],
        "status": "accepted",
        "error_code": None,
        "received_at": "2026-09-27T12:05:00Z",
        "consumer": {
            "component": "StockWiki",
            "namespace": "quick_scan",
            "store_id": "stockwiki-complete-seal-store",
        },
    }
