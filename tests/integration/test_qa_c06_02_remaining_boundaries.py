"""QA-C06-02 round-2 remaining boundaries (repo mirror of the fixed cases).

SYNTHETIC ONLY — mirrors `remaining_cases.py` from the review directory so the
repo keeps this regression after the coordinator's fixed-case harness moves on:
QR1B specific scope ID bound at the loader/CLI entry, QR2B the shared run/scan
gate on every NEW complete write, QR3B finite-float strict decoding, QR4B
complete writes that require durable complete inputs. The coordinator runs the
original fixed bytes; this file keeps the same boundaries green in the normal
offline gate.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.utils.quick_scan_c06_authority import AuthorityUnavailable, load_c06_authority
from src.utils.quick_scan_delivery_seal import _send_intent_iso, seal_result_delivery
from src.utils.quick_scan_observation_context import parse_standard_answer
from src.utils.quick_scan_result_outbox import canonical_sha256
from tests.integration.test_qa_c06_02_subprocess_cli import (
    _assert_rejected_before_key_http_budget,
    _cold_argv,
    _prepare_root,
    _spawn,
)
from tests.unit.test_quick_scan_c06_complete_seal import (
    AUTHORITY_V2,
    CONTEXT,
    MANIFEST,
    V1_AUTHORITY,
    _checkpointed,
    _forged_complete_package,
    _mutate,
    _standard_body,
    _v2_authority,
    authority_adapter_input,
    bind_context_to_work_item,
    build_complete_c06_package,
)


def _signed_path(tmp_path: Path, document: dict) -> Path:
    document["observation_context_sha256"] = canonical_sha256(document["observation_context"])
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def _complete_package(store, item, checkpoint, body, context, authority) -> dict:
    bound = bind_context_to_work_item(
        context,
        work_item=store.get_item(item["work_item_id"]),
        question_id=item["question_id"],
    )
    transmission = store.get_attempt_transmission(checkpoint["attempt_id"])
    started_at = _send_intent_iso(transmission["send_intent_at"])
    assert started_at is not None
    return build_complete_c06_package(
        checkpoint["payload"],
        authority=authority_adapter_input(authority),
        context=bound,
        standard_answer=body,
        started_at=started_at,
    )


def test_normal_standard_body_remains_parseable():
    body = _standard_body()
    assert parse_standard_answer(json.dumps(body)) == body


def test_overflow_number_is_refused_at_standard_body_entry():
    raw = json.dumps(_standard_body()).replace('"metrics": []', '"metrics": [{"value": 1e400}]')
    with pytest.raises(ValueError):
        parse_standard_answer(raw)


def test_nested_overflow_number_is_refused_at_standard_body_entry():
    raw = json.dumps(_standard_body()).replace(
        '"score": 8', '"score": 8, "nested": {"deep": [1e400]}', 1
    )
    with pytest.raises(ValueError):
        parse_standard_answer(raw)


def test_finite_float_numbers_keep_their_legacy_handling():
    raw = json.dumps(_standard_body()).replace('"metrics": []', '"metrics": [{"value": 12.5}]', 1)
    parsed = parse_standard_answer(raw)
    assert parsed is not None
    assert parsed["metrics"][0]["value"] == 12.5


def test_wrong_security_id_is_refused_by_authority_loader(tmp_path):
    document = copy.deepcopy(AUTHORITY_V2)
    assert MANIFEST["profile"]["security_id"] == "SEC_CONTEXT_FIXTURE"
    document["observation_context"]["questions"]["IQS_22"]["metadata"][
        "security_id"
    ] = "SEC_FOREIGN"
    with pytest.raises(AuthorityUnavailable):
        load_c06_authority(
            _signed_path(tmp_path, document),
            manifest=MANIFEST,
            identity_snapshot_sha256=CONTEXT["identity_snapshot_sha256"],
        )


def test_wrong_security_id_cli_is_refused_before_key_http_budget(tmp_path):
    files = _prepare_root(tmp_path)
    document = json.loads(files["authority"].read_text("utf-8"))
    document["observation_context"]["questions"]["IQS_22"]["metadata"][
        "security_id"
    ] = "SEC_FOREIGN"
    document["observation_context_sha256"] = canonical_sha256(document["observation_context"])
    files["authority"].write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    result = _spawn(tmp_path, _cold_argv(files), stub=True)
    _assert_rejected_before_key_http_budget(tmp_path, files, result)


@pytest.mark.parametrize("method", ["prepare", "supersede"])
def test_new_complete_write_requires_durable_complete_inputs(tmp_path, method):
    store, item, checkpoint, body = _checkpointed(tmp_path, with_context=False)
    work_item_id = item["work_item_id"]
    assert store.get_observation_context(work_item_id) is None
    assert store.get_standard_answer(work_item_id) is None
    if method == "supersede":
        assert seal_result_delivery(store, work_item_id, authority=dict(V1_AUTHORITY))[
            "action"
        ] == ("sealed")
    before = store.get_result_delivery(work_item_id)
    revisions = store.list_delivery_revisions(work_item_id)
    package = _complete_package(store, item, checkpoint, body, CONTEXT, _v2_authority(tmp_path))
    forged = _forged_complete_package(
        package, lambda observation: _mutate(observation, "claim_and_start")
    )
    with pytest.raises(ValueError):
        if method == "prepare":
            store.prepare_result_delivery(work_item_id, forged)
        else:
            store.supersede_result_delivery(work_item_id, forged)
    after = store.get_result_delivery(work_item_id)
    assert (after["package_bytes"] if after else None) == (
        before["package_bytes"] if before else None
    )
    assert store.list_delivery_revisions(work_item_id) == revisions
    assert store.get_observation_context(work_item_id) is None
    assert store.get_standard_answer(work_item_id) is None


def test_prepare_cannot_bypass_a_run_scan_block(tmp_path):
    store, item, checkpoint, body = _checkpointed(tmp_path, with_context=False)
    work_item_id = item["work_item_id"]
    document = copy.deepcopy(AUTHORITY_V2)
    for question in document["observation_context"]["questions"].values():
        question["metadata"]["run_id"] = "FOREIGN_RUN"
        question["metadata"]["scan_id"] = "FOREIGN_SCAN"
    authority = load_c06_authority(_signed_path(tmp_path, document))
    store.attach_standard_inputs(
        work_item_id,
        observation_context=document["observation_context"],
        standard_answer=body,
    )
    result = seal_result_delivery(store, work_item_id, authority=authority)
    assert result["action"] == "blocked"
    assert result["block_code"] == "c06_run_scan_unbound"
    before = store.get_result_delivery(work_item_id)
    package = _complete_package(
        store, item, checkpoint, body, document["observation_context"], authority
    )
    with pytest.raises(ValueError):
        store.prepare_result_delivery(work_item_id, package)
    assert store.get_result_delivery(work_item_id) == before
    assert store.list_delivery_revisions(work_item_id) == []
