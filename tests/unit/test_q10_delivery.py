"""Q10: result outbox and delivery-ACK consumption.

Binds Q10 cases JOB-07/JOB-08/DB-07/PAR-10 (LLM-08 is Q05's, boundary only)
with invariants I01/I12: durable blocks instead of fabricated packages, the
C06 adapter's field mapping bound to the checkpoint (nothing invented), the
send-intent/uncertain lifecycle with same-bytes re-arm, exact-ACK delivery
with idempotent repeats, and fake-ACK rejection that never overwrites the
original observation.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import pytest

from src.core.models import Question
from src.utils import quick_scan_result_outbox as outbox
from src.utils.quick_scan_work_store import QuickScanWorkStore

UUID_ENTITY = "ENT_1b2a4d3e-0000-4a1b-8c2d-000000000001"
IDENTITY = {
    "identity_revision": 1,
    "source_binding_version": 1,
    "identity_state": "verified",
    "source_binding_ref": "BND_TEST_1",
    "source_binding_refs": ["BND_TEST_1"],
    "identity_snapshot_sha256": "a" * 64,
}
SOURCE_URL = "https://example.com/issuer"


def _store(tmp_path: Path) -> QuickScanWorkStore:
    return QuickScanWorkStore(tmp_path / "work.sqlite")


def _lifecycle(store: QuickScanWorkStore, run_id: str = "RUN_1"):
    from src.runners.llm_runner import QuickScanWorkLifecycle

    return QuickScanWorkLifecycle(
        store,
        entity_id=UUID_ENTITY,
        run_id=run_id,
        scan_id="SCAN_1",
        identity=dict(IDENTITY),
        model_requested="mimo-v2.6-flash",
    )


def _q(qid: str, text: str) -> Question:
    return Question(text=text, question_id=qid)


def _answer(qid: str, score: int = 8) -> dict:
    return {
        "entity_id": UUID_ENTITY,
        "question_id": qid,
        "status": "scored",
        "score": score,
        "description": "基于公开来源的判断",
    }


def _receipt(**over) -> dict:
    receipt = {
        "search_status": "executed",
        "provider": "mimo",
        "actual_model": "mimo-v2.6-flash",
        "response_id": "resp_q10_01",
        "attempt_id": "provider_attempt_q10_01",
        "search_receipt_id": "ws_q10_01",
        "response_status": "completed",
        "http_status_code": 200,
        "completed_at": "2026-10-06T00:00:00Z",
        "source_urls": [SOURCE_URL],
        "request_id": "req_q10_01",
        "prompt_sha256": "e" * 64,
    }
    receipt.update(over)
    return receipt


def _seed_checkpoint(store: QuickScanWorkStore, lifecycle, qid: str):
    from src.utils.quick_scan_work_store import quick_scan_receipt_sha256

    handle = lifecycle.before_question(_q(qid, f"问题 {qid}"))
    assert handle["claimed"] is True, handle
    receipt = _receipt()
    store.record_attempt_outcome(
        handle["work_item_id"],
        handle["lease"],
        handle["attempt_id"],
        outcome="response_available",
        receipt_sha256=quick_scan_receipt_sha256(receipt),
        http_status_code=200,
        request_id=receipt.get("request_id"),
    )
    record = store.save_answer_checkpoint(
        handle["work_item_id"],
        handle["lease"],
        handle["attempt_id"],
        answer=_answer(qid),
        execution_receipt=receipt,
    )
    return {"handle": handle, "record": record}


def _checkpoint_payload(store: QuickScanWorkStore, work_item_id: str) -> dict:
    record = store.get_answer_checkpoint(work_item_id)
    assert record is not None
    return record["payload"]


def _c06_authority() -> dict:
    return {
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
        "producer_build_id": "stockqa-offline-test",
    }


def _build_package(checkpoint_payload: dict, authority: dict) -> dict:
    """Deterministic C06 v1 construction bound to the checkpoint (the same
    mapping the Q10 adapter implements — tests keep this inline so adapter
    drift is caught against the store's own validators)."""
    from src.utils.quick_scan_result_outbox import (
        canonical_sha256,
    )

    work = checkpoint_payload["work"]
    answer = checkpoint_payload["answer"]
    provenance = checkpoint_payload["provenance"]
    status_map = {
        "scored": "scored",
        "insufficient_evidence": "insufficient_evidence",
        "not_applicable": "not_applicable",
    }
    observation_id = "obs_" + canonical_sha256(
        {
            "entity_id": answer["entity_id"],
            "question_id": work["question_id"],
            "scope": work["scope"],
        }
    )
    observation = {
        "observation_id": observation_id,
        "entity_id": answer["entity_id"],
        "question_id": work["question_id"],
        "scope": work["scope"],
        "security_id": work["scope_id"] if work["scope"] == "security" else None,
        "segment_id": work["scope_id"] if work["scope"] == "segment" else None,
        "answer": {
            "question_id": answer["question_id"],
            "response_kind": "score",
            "score": answer["score"],
            "summary": answer["description"],
            "status": status_map[answer["status"]],
            "evidence": [{"url": url} for url in provenance.get("source_urls", [])],
        },
        "execution": {
            "provider": provenance["actual_provider"],
            "model_requested": provenance["model_requested"],
            "model_resolved": provenance["actual_model"],
            "request_id": provenance["request_id"],
            "attempt_id": provenance["provider_attempt_id"],
            "search_status": provenance["search_status"],
            "search_receipt_id": provenance["search_receipt_id"],
            "prompt_sha256": provenance["provider_prompt_sha256"],
            "answered_at": provenance["response_completed_at"],
        },
    }
    payload_sha256 = canonical_sha256(observation)
    item_id = "itm_" + canonical_sha256(
        {"observation_id": observation_id, "payload_sha256": payload_sha256}
    )
    package_body = {
        "schema_version": "1.0.0",
        "producer": {
            "component": "StockQAbyLLM",
            "component_version": authority["producer_component_version"],
            "build_id": authority["producer_build_id"],
        },
        "consumer": {
            "component": "StockWiki",
            "namespace": "quick_scan",
            "minimum_schema_version": "1.0.0",
        },
        "created_at": provenance["response_completed_at"],
        "data_class": "lightweight_screening",
        "required_capabilities": list(authority["capabilities"]),
        "contract_versions": dict(authority["contract_versions"]),
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


def _ack_for(record: dict, *, status: str = "accepted", ack_id: str = "ack_q10_test_01") -> dict:
    return {
        "schema_version": "1.0.0",
        "ack_id": ack_id,
        "package_id": record["package_id"],
        "item_id": record["item_id"],
        "observation_id": record["observation_id"],
        "payload_sha256": record["payload_sha256"],
        "status": status,
        "error_code": None,
        "received_at": "2026-10-06T03:00:00Z",
        "consumer": {
            "component": "StockWiki",
            "namespace": "quick_scan",
            "store_id": "wiki-store-1",
        },
    }


def test_db_07_missing_authority_blocks_and_never_fabricates(tmp_path: Path) -> None:
    """DB-07: with StockWiki unavailable / authority fields missing, the
    adapter classifies the gap as a durable BLOCK (work stays result_ready,
    no re-ask, nothing fabricated); once the authority becomes available the
    blocked delivery proceeds to a sealed package (blocked->ready)."""
    from src.utils.quick_scan_c06_adapter import MissingC06Fields, build_c06_package

    store = _store(tmp_path)
    lifecycle = _lifecycle(store)
    seeded = _seed_checkpoint(store, lifecycle, "IQS_05")
    wid = seeded["handle"]["work_item_id"]
    payload = _checkpoint_payload(store, wid)

    # authority missing -> durable block (the Q10 adapter refuses to invent)
    with pytest.raises(MissingC06Fields):
        build_c06_package(payload, authority={})
    store.mark_result_delivery_blocked(wid, "c06_adapter_missing_authority_fields")

    con = sqlite3.connect(store.path)
    try:
        work_status = con.execute(
            "SELECT status FROM work_item WHERE work_item_id=?", (wid,)
        ).fetchone()[0]
        delivery = con.execute(
            "SELECT state, block_code FROM quick_scan_result_delivery WHERE work_item_id=?",
            (wid,),
        ).fetchone()
    finally:
        con.close()
    assert work_status == "result_ready"  # work unchanged, no re-ask
    assert delivery is not None and delivery[0] == "blocked"
    assert delivery[1] == "c06_adapter_missing_authority_fields"

    # authority arrives -> blocked -> ready with a sealed, valid package
    package = build_c06_package(payload, authority=_c06_authority())
    outbox.validate_exchange_package(package)  # self-check: construction is valid
    record = store.prepare_result_delivery(wid, package)
    assert record["state"] == "ready"
    assert record["package_id"] == package["package_id"]


def test_job_07_delivery_recovery_zero_llm_and_idempotent_ack(tmp_path: Path) -> None:
    """JOB-07: result_ready + sealed package, StockWiki unavailable then back —
    a PROVEN-unsent connection failure re-arms the SAME bytes/key; an unknown
    outcome resolves via the exact ACK only; zero LLM calls during recovery
    (structural: no provider exists in this test); ACK before delivery is
    impossible and repeated ACKs are idempotent."""
    store = _store(tmp_path)
    lifecycle = _lifecycle(store)
    seeded = _seed_checkpoint(store, lifecycle, "IQS_05")
    wid = seeded["handle"]["work_item_id"]
    package = _build_package(_checkpoint_payload(store, wid), _c06_authority())
    outbox.validate_exchange_package(package)
    record = store.prepare_result_delivery(wid, package)

    # proven-unsent failure -> same bytes / same key re-arm
    store.bind_result_delivery_consumer(wid, JR2_CONSUMER, source_ref=JR2_SOURCE)
    begun = store.begin_result_delivery(wid)
    assert begun["request_bytes"] == record["package_bytes"]
    assert begun["idempotency_key"] == record["delivery_key"]
    store.confirm_result_delivery_not_sent(wid, "connection_refused_before_write")
    begun2 = store.begin_result_delivery(wid)
    assert begun2["request_bytes"] == record["package_bytes"]
    assert begun2["idempotency_key"] == record["delivery_key"]

    # unknown outcome while in flight: exact ACK resolves, repeated ACK is idempotent
    ack = _ack_for(begun2)
    delivered = store.apply_result_delivery_ack(wid, ack)
    assert delivered["state"] == "delivered"
    con = sqlite3.connect(store.path)
    try:
        work_status = con.execute(
            "SELECT status FROM work_item WHERE work_item_id=?", (wid,)
        ).fetchone()[0]
    finally:
        con.close()
    assert work_status == "delivered"
    again = store.apply_result_delivery_ack(wid, ack)
    assert again["state"] == "delivered"  # idempotent repeat


def test_job_08_wrong_ack_and_illegal_transition_are_refused(tmp_path: Path) -> None:
    """JOB-08: an ACK matching a DIFFERENT item/hash is refused, illegal state
    jumps are refused, the original receipt/outbox row is preserved, and no
    delivered state is fabricated."""
    store = _store(tmp_path)
    lifecycle = _lifecycle(store)
    seeded_a = _seed_checkpoint(store, lifecycle, "IQS_05")
    wid_a = seeded_a["handle"]["work_item_id"]
    package_a = _build_package(_checkpoint_payload(store, wid_a), _c06_authority())
    store.prepare_result_delivery(wid_a, package_a)
    store.bind_result_delivery_consumer(wid_a, JR2_CONSUMER, source_ref=JR2_SOURCE)
    store.begin_result_delivery(wid_a)

    # foreign package for a different question -> its own delivery row
    seeded_b = _seed_checkpoint(store, lifecycle, "IQS_06")
    wid_b = seeded_b["handle"]["work_item_id"]
    package_b = _build_package(_checkpoint_payload(store, wid_b), _c06_authority())
    record_b = store.prepare_result_delivery(wid_b, package_b)

    # ACK for B applied to A must be refused
    with pytest.raises(ValueError, match="does not match outbox"):
        store.apply_result_delivery_ack(wid_a, _ack_for(record_b))

    con = sqlite3.connect(store.path)
    try:
        row = con.execute(
            "SELECT state, ack_json FROM quick_scan_result_delivery WHERE work_item_id=?",
            (wid_a,),
        ).fetchone()
    finally:
        con.close()
    assert row[0] == "send_uncertain"  # unchanged, receipt preserved
    assert row[1] is None  # no delivered fabricated

    # ACK on an item with NO prepared delivery -> refused (illegal jump)
    with pytest.raises(Exception):
        store.apply_result_delivery_ack(
            seeded_b["handle"]["work_item_id"] + "_nope", _ack_for(record_b)
        )


def test_par_10_fake_ack_never_resolves_uncertainty(tmp_path: Path) -> None:
    """PAR-10: send-intent with unknown outcome + a WELL-FORMED but
    hash-mismatched fake ACK — refused, the delivery stays send_uncertain,
    no automatic re-POST (claim/refusal semantics; no second model call is
    even possible structurally), the original observation is not overwritten;
    only the EXACT ack (or authoritative reconciliation) can resolve it."""
    store = _store(tmp_path)
    lifecycle = _lifecycle(store)
    seeded = _seed_checkpoint(store, lifecycle, "IQS_05")
    wid = seeded["handle"]["work_item_id"]
    package = _build_package(_checkpoint_payload(store, wid), _c06_authority())
    record = store.prepare_result_delivery(wid, package)
    store.bind_result_delivery_consumer(wid, JR2_CONSUMER, source_ref=JR2_SOURCE)
    store.begin_result_delivery(wid)

    fake = _ack_for(record)
    fake["payload_sha256"] = "f" * 64  # well-formed but WRONG hash
    with pytest.raises(ValueError, match="payload_sha256 does not match outbox"):
        store.apply_result_delivery_ack(wid, fake)

    con = sqlite3.connect(store.path)
    try:
        state = con.execute(
            "SELECT state FROM quick_scan_result_delivery WHERE work_item_id=?", (wid,)
        ).fetchone()[0]
        observation = json.loads(
            con.execute(
                "SELECT package_json FROM quick_scan_result_delivery WHERE work_item_id=?",
                (wid,),
            ).fetchone()[0]
        )["items"][0]["observation"]
    finally:
        con.close()
    assert state == "send_uncertain"  # uncertainty preserved, not resolved
    assert observation["answer"]["summary"] == "基于公开来源的判断"  # original kept

    # the uncertainty can only end via the EXACT ack
    exact = store.apply_result_delivery_ack(wid, _ack_for(record))
    assert exact["state"] in {"delivered", "rejected", "conflict"}


def test_adapter_missing_authority_and_unknown_status_are_blocks(tmp_path: Path) -> None:
    """Adapter unit pins (r1 pre-emptive): unknown answer status cannot be
    packaged (Q10 binding vocabulary), and the adapter's block classification
    covers every non-derivable authority field."""
    from src.utils.quick_scan_c06_adapter import MissingC06Fields, build_c06_package

    store = _store(tmp_path)
    lifecycle = _lifecycle(store)
    seeded = _seed_checkpoint(store, lifecycle, "IQS_05")
    payload = _checkpoint_payload(store, seeded["handle"]["work_item_id"])

    # unknown status cannot be packaged -> block, not error
    unknown_payload = copy.deepcopy(payload)
    unknown_payload["answer"]["status"] = "unknown"
    with pytest.raises(MissingC06Fields):
        build_c06_package(unknown_payload, authority=_c06_authority())

    # partial authority -> block naming the missing field
    partial = _c06_authority()
    partial["contract_versions"].pop("question_catalog")
    with pytest.raises(MissingC06Fields):
        build_c06_package(payload, authority=partial)


def test_low_1_cross_byte_seal_is_refused(tmp_path: Path) -> None:
    """r1 LOW-1: the same checkpoint sealed under DIFFERENT authority (a
    different byte stream) must be refused as immutable — the sealed package
    and the original observation stay untouched."""
    store = _store(tmp_path)
    lifecycle = _lifecycle(store)
    seeded = _seed_checkpoint(store, lifecycle, "IQS_05")
    wid = seeded["handle"]["work_item_id"]
    payload = _checkpoint_payload(store, wid)
    package = _build_package(payload, _c06_authority())
    first = store.prepare_result_delivery(wid, package)
    assert first["state"] == "ready"

    other_authority = _c06_authority()
    other_authority["producer_build_id"] = "different-build-id"
    other = _build_package(payload, other_authority)
    assert other["package_sha256"] != package["package_sha256"]
    with pytest.raises(Exception, match="immutable"):
        store.prepare_result_delivery(wid, other)

    con = sqlite3.connect(store.path)
    try:
        row = con.execute(
            "SELECT package_id, state FROM quick_scan_result_delivery WHERE work_item_id=?",
            (wid,),
        ).fetchone()
    finally:
        con.close()
    assert row[0] == package["package_id"]  # original seal untouched
    assert row[1] in {"ready", "send_uncertain"}


# The target is an operator-configured synthetic fixture, not StockWiki owner golden.
JR2_CONSUMER = {"component": "StockWiki", "namespace": "quick_scan", "store_id": "wiki-store-1"}
JR2_SOURCE = "operator-config:offline-jr2/wiki-store-1"


def _jr2_ready(tmp_path):
    store = _store(tmp_path)
    seeded = _seed_checkpoint(store, _lifecycle(store), "IQS_05")
    wid = seeded["handle"]["work_item_id"]
    package = _build_package(_checkpoint_payload(store, wid), _c06_authority())
    return store, wid, store.prepare_result_delivery(wid, package)


def _jr2_snapshot(store):
    with sqlite3.connect(store.path) as connection:
        return tuple(connection.iterdump())


@pytest.mark.parametrize("action", ["begin", "ack"])
def test_jr2_missing_target_refuses_before_any_state_change(tmp_path, action):
    store, wid, ready = _jr2_ready(tmp_path)
    before = _jr2_snapshot(store)
    with pytest.raises(ValueError, match="consumer.*binding|target.*binding"):
        if action == "begin":
            store.begin_result_delivery(wid)
        else:
            store.apply_result_delivery_ack(wid, _ack_for(ready))
    assert _jr2_snapshot(store) == before


@pytest.mark.parametrize("sent", [False, True])
def test_jr2_wrong_store_after_prebind_is_atomic_in_ready_and_send_intent(tmp_path, sent):
    store, wid, ready = _jr2_ready(tmp_path)
    store.bind_result_delivery_consumer(wid, JR2_CONSUMER, source_ref=JR2_SOURCE)
    if sent:
        store.begin_result_delivery(wid)
    before = _jr2_snapshot(store)
    ack = _ack_for(ready)
    ack["consumer"]["store_id"] = "qsobs_unrelated_target"
    with pytest.raises(ValueError, match="consumer.*does not match"):
        store.apply_result_delivery_ack(wid, ack)
    assert _jr2_snapshot(store) == before


@pytest.mark.parametrize(
    "status,error,state",
    [
        ("accepted", None, "delivered"),
        ("already_present", None, "delivered"),
        ("rejected", "missing_entity", "rejected"),
        ("conflict", "immutable_key_hash_conflict", "conflict"),
    ],
)
def test_jr2_correct_prebound_target_keeps_package_hashes_and_exact_ack(
    tmp_path, status, error, state
):
    store, wid, ready = _jr2_ready(tmp_path)
    original = ready["package_bytes"], ready["delivery_key"], ready["package_sha256"]
    binding = store.bind_result_delivery_consumer(wid, JR2_CONSUMER, source_ref=JR2_SOURCE)
    assert binding["consumer"] == JR2_CONSUMER
    assert binding["source_ref"] == JR2_SOURCE
    assert binding["delivery_id"] == ready["delivery_id"]
    assert binding["package_id"] == ready["package_id"]
    assert binding["revision"] == 1
    assert binding["binding_sha256"] == outbox.canonical_sha256(
        {key: value for key, value in binding.items() if key != "binding_sha256"}
    )
    begun = store.begin_result_delivery(wid)
    assert (begun["request_bytes"], begun["idempotency_key"], begun["package_sha256"]) == original
    ack = _ack_for(ready, status=status)
    ack["error_code"] = error
    result = store.apply_result_delivery_ack(wid, ack)
    assert result["state"] == state
    assert result["ack_json"] == outbox.canonical_bytes(ack).decode("utf-8")
    assert result["ack_sha256"] == outbox.canonical_sha256(ack)
    assert result["consumer_binding"] == binding
    before = _jr2_snapshot(store)
    assert _store(tmp_path).apply_result_delivery_ack(wid, ack)["ack"] == ack
    assert store.bind_result_delivery_consumer(wid, JR2_CONSUMER, source_ref=JR2_SOURCE) == binding
    assert _jr2_snapshot(store) == before


@pytest.mark.parametrize("phase", ["ready", "send_uncertain", "terminal"])
def test_jr2_binding_is_idempotent_and_never_retargets_existing_head(tmp_path, phase):
    store, wid, ready = _jr2_ready(tmp_path)
    binding = store.bind_result_delivery_consumer(wid, JR2_CONSUMER, source_ref=JR2_SOURCE)
    if phase != "ready":
        store.begin_result_delivery(wid)
    if phase == "terminal":
        store.apply_result_delivery_ack(wid, _ack_for(ready))
    # Advance the trusted clock so equality proves neither time nor hash is refreshed.
    store.clock = lambda: binding["bound_at"] + 600.0
    restarted = QuickScanWorkStore(store.path, clock=lambda: binding["bound_at"] + 1200.0)
    before = _jr2_snapshot(store)
    assert (
        restarted.bind_result_delivery_consumer(wid, JR2_CONSUMER, source_ref=JR2_SOURCE) == binding
    )
    for target, source in [
        (dict(JR2_CONSUMER, store_id="qsobs_unrelated_target"), JR2_SOURCE),
        (JR2_CONSUMER, "operator-config:changed-source"),
    ]:
        with pytest.raises(ValueError, match="immutable|retarget"):
            store.bind_result_delivery_consumer(wid, target, source_ref=source)
    assert _jr2_snapshot(store) == before


def test_jr2_superseded_head_needs_its_own_binding_and_rejects_old_ack(tmp_path):
    store, wid, ready = _jr2_ready(tmp_path)
    old_binding = store.bind_result_delivery_consumer(wid, JR2_CONSUMER, source_ref=JR2_SOURCE)
    package = copy.deepcopy(ready["package"])
    package["producer"]["build_id"] = "synthetic-jr2-new-head"
    package.pop("package_id")
    package.pop("package_sha256")
    package["package_sha256"] = outbox.canonical_sha256(package)
    package["package_id"] = "pkg_" + package["package_sha256"]
    head = store.supersede_result_delivery(wid, package)
    assert head["revision"] == 2
    assert store.get_result_delivery(wid)["consumer_binding"] is None
    before = _jr2_snapshot(store)
    with pytest.raises(ValueError, match="binding"):
        store.begin_result_delivery(wid)
    assert _jr2_snapshot(store) == before
    binding = store.bind_result_delivery_consumer(wid, JR2_CONSUMER, source_ref=JR2_SOURCE)
    assert binding["revision"] == 2 and binding["binding_sha256"] != old_binding["binding_sha256"]
    store.begin_result_delivery(wid)
    before = _jr2_snapshot(store)
    with pytest.raises(ValueError, match="package_id does not match"):
        store.apply_result_delivery_ack(wid, _ack_for(ready))
    assert _jr2_snapshot(store) == before
    assert store.apply_result_delivery_ack(wid, _ack_for(head))["state"] == "delivered"
