"""Synthetic real SQLite integration; never vendor/StockWiki/financial gold."""

import copy
import json
from datetime import datetime, timezone

import pytest

from src.providers.model_resolution import model_resolution_sha256
from src.utils.quick_scan_c06_adapter import build_complete_c06_package
from src.utils.quick_scan_c06_authority import (
    authority_adapter_input,
    load_c06_authority,
)
from src.utils.quick_scan_delivery_seal import (
    BLOCK_RECEIPT,
    seal_pending_deliveries,
    seal_result_delivery,
)
from src.utils.quick_scan_result_outbox import canonical_sha256
from src.utils.quick_scan_work_store import (
    QuickScanWorkStore,
    WorkConflictError,
    quick_scan_receipt_sha256,
)
from tests.unit.test_quick_scan_c06_complete_seal import _forged_complete_package
from tests.unit.test_quick_scan_vendor_receipt_seal import exported, pair


def stored(tmp_path, exported):
    raw_authority, manifest = exported
    checkpoint, bound, body, response = pair(exported)
    authority_file = tmp_path / "authority.json"
    authority_file.write_text(json.dumps(raw_authority), encoding="utf-8")
    authority = load_c06_authority(authority_file)
    meta = bound["metadata"]
    clock = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc).timestamp()
    store = QuickScanWorkStore(tmp_path / "work.sqlite", clock=lambda: clock)
    item = store.create_or_attach(
        entity_id=meta["entity_id"],
        question_id=meta["question_id"],
        generation=1,
        scope="entity",
        scope_id=meta["entity_id"],
        identity_revision=1,
        source_binding_version=1,
        identity_state="verified",
        source_binding_ref="BND_COMPLETE_SEAL",
        source_binding_refs=("BND_COMPLETE_SEAL",),
        identity_snapshot_sha256="b" * 64,
        question_fingerprint=bound["work_prompt_sha256"],
        routing_fingerprint="d" * 64,
        run_id=meta["run_id"],
        scan_id=meta["scan_id"],
    )
    work = item["work_item_id"]
    lease = store.claim(work, lease_seconds=60)
    attempt = store.prepare_attempt(
        work,
        lease,
        route_id="synthetic-vendor",
        provider="mimo",
        model_requested="mimo-v2.6-flash",
        request_cache_key="REQ_" + "f" * 64,
        prompt_sha256=bound["work_prompt_sha256"],
    )
    store.mark_send_intent(work, lease, attempt["attempt_id"])
    receipt = copy.deepcopy(response["receipt"])
    receipt["model_resolution_sha256"] = model_resolution_sha256(None)
    receipt["search_protocol"] = "mimo_chat_completions"
    store.record_attempt_outcome(
        work,
        lease,
        attempt["attempt_id"],
        outcome="response_available",
        http_status_code=200,
        receipt_sha256=quick_scan_receipt_sha256(receipt),
        request_id=None,
        execution_receipt=receipt,
    )
    answer = {
        **checkpoint["answer"],
        "entity_id": meta["entity_id"],
        "question_id": meta["question_id"],
    }
    saved = store.save_answer_checkpoint(
        work,
        lease,
        attempt["attempt_id"],
        answer=answer,
        execution_receipt=receipt,
        observation_context=authority["observation_context"],
        standard_answer=body,
    )
    return store, item, saved, authority, body, bound


def test_real_sqlite_seal_preserves_vendor_null_header_and_replays_after_restart(
    tmp_path, exported
):
    store, item, saved, authority, body, bound = stored(tmp_path, exported)
    work = item["work_item_id"]
    response = store.get_attempt_response(saved["attempt_id"])
    result = seal_result_delivery(store, work, authority=authority)
    assert result["action"] == "sealed"
    delivered = store.get_result_delivery(work)
    obs = delivered["package"]["items"][0]["observation"]
    assert obs["schema_version"] == "1.2.0" and obs["execution"]["request_id"] is None
    for key in ["response_id", "response_sha256", "receipt_sha256"]:
        assert obs["execution"][key] == response[key]
    assert obs["execution"]["attempt_id"] == response["provider_attempt_id"] != saved["attempt_id"]
    assert (
        obs["execution"]["request_payload_sha256"] == response["receipt"]["request_payload_sha256"]
    )
    reopened = QuickScanWorkStore(
        tmp_path / "work.sqlite",
        clock=lambda: datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc).timestamp(),
    )
    assert seal_result_delivery(reopened, work, authority=authority)["action"] == "already_sealed"
    assert reopened.get_result_delivery(work)["package_bytes"] == delivered["package_bytes"]
    assert len(reopened.list_delivery_revisions(work)) == 1
    assert seal_pending_deliveries(reopened, authority=authority)["model_calls"] == 0


def test_missing_adapter_durable_record_blocks_without_new_model_call_then_recovers(
    tmp_path, exported, monkeypatch
):
    store, item, saved, authority, body, bound = stored(tmp_path, exported)
    work = item["work_item_id"]
    real = store.get_attempt_response
    monkeypatch.setattr(store, "get_attempt_response", lambda attempt_id: None)
    result = seal_result_delivery(store, work, authority=authority)
    assert result["action"] == "blocked" and result["block_code"] == BLOCK_RECEIPT
    assert store.get_item(work)["status"] == "result_ready" and not store.list_delivery_revisions(
        work
    )
    monkeypatch.setattr(store, "get_attempt_response", real)
    assert seal_pending_deliveries(store, authority=authority)["model_calls"] == 0
    assert store.get_result_delivery(work)["state"] == "ready"


@pytest.mark.parametrize(
    "key",
    [
        "response_id",
        "attempt_id",
        "request_id",
        "request_payload_sha256",
        "response_sha256",
        "receipt_sha256",
    ],
)
def test_rehashed_caller_receipt_forgery_cannot_write_sqlite_head(
    tmp_path, exported, monkeypatch, key
):
    store, item, saved, authority, body, bound = stored(tmp_path, exported)
    work = item["work_item_id"]
    response = store.get_attempt_response(saved["attempt_id"])
    package = build_complete_c06_package(
        saved["payload"],
        authority=authority_adapter_input(authority),
        context=bound,
        standard_answer=body,
        started_at="2026-10-12T10:00:00Z",
        durable_response=response,
    )
    bad = _forged_complete_package(
        package,
        lambda obs: obs["execution"].__setitem__(
            key, "9" * 64 if key.endswith("sha256") else "SYNTHETIC_FORGED"
        ),
    )
    # SQL proof must re-read persisted row rather than trusting a caller getter or recomputed payload hashes.
    monkeypatch.setattr(
        store,
        "get_attempt_response",
        lambda aid: dict(response, provider_attempt_id="SYNTHETIC_FORGED"),
    )
    with pytest.raises((ValueError, WorkConflictError)):
        store.prepare_result_delivery(work, bad)
    assert not store.list_delivery_revisions(work)
    assert store.get_result_delivery(work) is None
    assert store.prepare_result_delivery(work, package)["state"] == "ready"


def test_explicit_null_header_key_cannot_be_deleted_even_in_vendor12(tmp_path, exported):
    from src.utils.quick_scan_result_outbox import validate_checkpoint_binding

    store, item, saved, authority, body, bound = stored(tmp_path, exported)
    response = store.get_attempt_response(saved["attempt_id"])
    package = build_complete_c06_package(
        saved["payload"],
        authority=authority_adapter_input(authority),
        context=bound,
        standard_answer=body,
        started_at="2026-10-12T10:00:00Z",
        durable_response=response,
    )
    bad = _forged_complete_package(package, lambda obs: obs["execution"].pop("request_id"))
    with pytest.raises(ValueError):
        validate_checkpoint_binding(bad, saved)


def test_legacy11_does_not_gain_nullable_header_by_new_profile_support(tmp_path):
    from src.utils.quick_scan_result_outbox import validate_checkpoint_binding
    from tests.unit.test_quick_scan_c06_complete_seal import (
        _checkpointed,
        _v2_authority,
    )

    store, item, saved, body = _checkpointed(tmp_path)
    assert (
        seal_result_delivery(store, item["work_item_id"], authority=_v2_authority(tmp_path))[
            "action"
        ]
        == "sealed"
    )
    package = store.get_result_delivery(item["work_item_id"])["package"]
    assert package["items"][0]["observation"]["schema_version"] == "1.1.0"
    bad = _forged_complete_package(
        package, lambda obs: obs["execution"].__setitem__("request_id", None)
    )
    changed = copy.deepcopy(saved)
    changed["payload"]["provenance"]["request_id"] = None
    with pytest.raises(ValueError):
        validate_checkpoint_binding(bad, changed)
