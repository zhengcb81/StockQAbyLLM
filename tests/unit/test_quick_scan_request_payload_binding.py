"""Explicit request controls must change the durable key before any HTTP send."""

import hashlib
import json

import pytest

from src.utils.quick_scan_work_store import QuickScanWorkStore
from src.utils.quick_scan_work_transport import (
    begin_quick_scan_send,
    bind_quick_scan_route,
    bind_quick_scan_work,
)


def _claimed(tmp_path):
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    item = store.create_or_attach(
        entity_id="ENT_PAYLOAD_FIXTURE",
        question_id="IQS_01",
        generation=1,
        scope="entity",
        scope_id="ENT_PAYLOAD_FIXTURE",
        identity_revision=1,
        source_binding_version=1,
        identity_state="verified",
        source_binding_ref="BND_PAYLOAD_FIXTURE",
        source_binding_refs=["BND_PAYLOAD_FIXTURE"],
        identity_snapshot_sha256=hashlib.sha256(b"fixture identity").hexdigest(),
        question_fingerprint=hashlib.sha256(b"fixture question").hexdigest(),
        routing_fingerprint=hashlib.sha256(b"fixture route").hexdigest(),
        run_id="payload-run",
        scan_id="payload-scan",
    )
    lease = store.claim(item["work_item_id"], lease_seconds=60)
    assert lease is not None
    return store, item["work_item_id"], lease


@pytest.mark.parametrize(
    "payload_sha", [None, "0" * 64, "1" * 64], ids=["legacy", "enabled", "disabled"]
)
def test_actual_payload_digest_is_part_of_durable_request_key(tmp_path, payload_sha):
    store, work_id, lease = _claimed(tmp_path)
    prompt, system = "one frozen question", "final answer JSON only"
    kwargs = {} if payload_sha is None else {"request_payload_sha256": payload_sha}
    with bind_quick_scan_work(store, work_id, lease), bind_quick_scan_route(
        route_id="fixture-route", provider="mimo", model_requested="fixture-model"
    ):
        handle = begin_quick_scan_send(prompt, system, **kwargs)
    assert handle is not None
    identity = json.dumps(
        {
            "work_item_id": work_id,
            "route_id": "fixture-route",
            "provider": "mimo",
            "model_requested": "fixture-model",
            "prompt_sha256": hashlib.sha256((system + "\0" + prompt).encode("utf-8")).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    if payload_sha is not None:
        identity += "|payload:" + payload_sha
    attempts = store.list_attempts(work_id)
    assert len(attempts) == 1
    assert (
        attempts[0]["request_cache_key"]
        == "REQ_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    )
    assert attempts[0]["phase"] == "send_intent"


@pytest.mark.parametrize(
    "bad",
    [True, 0, "", "g" * 64, "A" * 64, "0" * 63, "0" * 65, {}, ["0" * 64]],
    ids=["bool", "int", "blank", "nonhex", "uppercase", "short", "long", "object", "array"],
)
def test_invalid_payload_digest_is_rejected_before_even_owner_guard(monkeypatch, bad):
    import src.utils.quick_scan_work_transport as transport

    owner_calls = []
    monkeypatch.setattr(transport, "_check_owner_refresh_guard", lambda: owner_calls.append(True))
    with pytest.raises(ValueError, match="request_payload_sha256"):
        begin_quick_scan_send("question", "system", request_payload_sha256=bad)
    assert owner_calls == []
