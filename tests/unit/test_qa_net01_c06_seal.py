"""QA-NET-01 batch A: checkpoint -> C06 seal/block glue (Q10 residual).

Binds Q10 JOB-07/JOB-08/DB-07/PAR-10 glue gaps that the store/adapter
primitives already cover on their own: the RUNNER now settles a persisted
checkpoint into a sealed package (or a durable block) with no re-ask, and the
public ``--seal-deliveries`` entry re-runs that settlement with zero model
calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.models import Answer, Question
from src.utils import quick_scan_result_outbox as outbox
from src.utils.quick_scan_c06_adapter import MissingC06Fields, build_c06_package
from src.utils.quick_scan_c06_authority import AuthorityUnavailable, load_c06_authority
from src.utils.quick_scan_delivery_seal import seal_pending_deliveries
from src.utils.quick_scan_work_store import QuickScanWorkStore

UUID_ENTITY = "ENT_1b2a4d3e-0000-4a1b-8c2d-000000000001"
MODEL = "mimo-v2.6-flash"
IDENTITY = {
    "identity_revision": 1,
    "source_binding_version": 1,
    "identity_state": "verified",
    "source_binding_ref": "BND_TEST_1",
    "source_binding_refs": ["BND_TEST_1"],
    "identity_snapshot_sha256": "a" * 64,
}
SOURCE_URL = "https://example.com/issuer"


def _authority_document() -> dict:
    return {
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
        "producer": {"component_version": "1.0.0", "build_id": "stockqa-offline-test"},
    }


def _write_authority(tmp_path: Path, document: dict | None = None) -> Path:
    path = tmp_path / "quick_scan_c06_authority.json"
    payload = document if document is not None else _authority_document()
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _lifecycle(store: QuickScanWorkStore, *, authority=None, run_id: str = "RUN_1"):
    from src.runners.llm_runner import QuickScanWorkLifecycle

    return QuickScanWorkLifecycle(
        store,
        entity_id=UUID_ENTITY,
        run_id=run_id,
        scan_id="SCAN_1",
        identity=dict(IDENTITY),
        model_requested=MODEL,
        c06_authority=authority,
    )


def _q(qid: str) -> Question:
    return Question(text=f"问题 {qid}", question_id=qid)


def _metadata() -> dict:
    return {
        "search_status": "executed",
        "actual_model": MODEL,
        "response_id": "resp_net01_01",
        "request_id": "req_net01_01",
        "source_urls": [SOURCE_URL],
        "execution": {
            "provider": "mimo",
            "response_id": "resp_net01_01",
            "attempt_id": "provider_attempt_net01_01",
            "search_receipt_id": "ws_net01_01",
            "response_status": "completed",
            "http_status_code": 200,
            "completed_at": "2026-10-07T00:00:00Z",
            "prompt_sha256": "e" * 64,
        },
    }


def _result(qid: str, *, score: int = 8) -> SimpleNamespace:
    return SimpleNamespace(
        question=_q(qid),
        answer=Answer(
            text="基于公开来源的判断",
            score=score,
            status="scored",
            source="mimo",
            metadata=_metadata(),
        ),
    )


def _settle(store: QuickScanWorkStore, lifecycle, qid: str) -> str:
    handle = lifecycle.before_question(_q(qid))
    assert handle["claimed"] is True, handle
    lifecycle.after_question(handle, _result(qid))
    return str(handle["work_item_id"])


def _adapter_input(authority: dict) -> dict:
    return {
        "contract_versions": dict(authority["contract_versions"]),
        "capabilities": list(authority["capabilities"]),
        "producer_component_version": authority["producer_component_version"],
        "producer_build_id": authority["producer_build_id"],
    }


def test_runner_seals_checkpoint_into_valid_c06_package(tmp_path: Path) -> None:
    authority = load_c06_authority(_write_authority(tmp_path))
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    lifecycle = _lifecycle(store, authority=authority)

    work_item_id = _settle(store, lifecycle, "IQS_05")

    delivery = store.get_result_delivery(work_item_id)
    assert delivery is not None
    assert delivery["state"] == "ready"
    assert delivery["package"] is not None
    outbox.validate_exchange_package(delivery["package"])
    observation = delivery["package"]["items"][0]["observation"]
    assert observation["question_id"] == "IQS_05"
    assert observation["execution"]["model_resolved"] == MODEL
    assert store.get_answer_checkpoint(work_item_id) is not None


def test_missing_authority_records_durable_block_then_seals_on_restart(
    tmp_path: Path,
) -> None:
    """DB-07: no authority -> durable block, work stays result_ready, nothing
    fabricated; the restart entry seals the SAME checkpoint with model_calls=0."""
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    lifecycle = _lifecycle(store, authority=None)
    work_item_id = _settle(store, lifecycle, "IQS_05")

    delivery = store.get_result_delivery(work_item_id)
    assert delivery is not None and delivery["state"] == "blocked"
    assert delivery["block_code"] == "c06_authority_unavailable"
    assert delivery["package"] is None

    authority = load_c06_authority(_write_authority(tmp_path))
    report = seal_pending_deliveries(store, authority=authority)
    assert report["model_calls"] == 0
    assert [item["work_item_id"] for item in report["sealed"]] == [work_item_id]

    delivery = store.get_result_delivery(work_item_id)
    assert delivery is not None and delivery["state"] == "ready"
    assert delivery["package"] is not None
    outbox.validate_exchange_package(delivery["package"])

    # a second restart is idempotent: nothing is rebuilt, nothing re-asked
    again = seal_pending_deliveries(store, authority=authority)
    assert again["sealed"] == []
    assert [item["work_item_id"] for item in again["already_sealed"]] == [work_item_id]
    assert again["model_calls"] == 0


def test_invalid_authority_documents_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(AuthorityUnavailable):
        load_c06_authority(tmp_path / "absent.json")

    truncated = tmp_path / "truncated.json"
    truncated.write_text('{"schema_version": "1.0.0"', encoding="utf-8")
    with pytest.raises(AuthorityUnavailable):
        load_c06_authority(truncated)

    partial = _authority_document()
    partial["contract_versions"].pop("question_catalog")
    with pytest.raises(AuthorityUnavailable):
        load_c06_authority(_write_authority(tmp_path, partial))

    unknown_capability = _authority_document()
    unknown_capability["capabilities"].append("invented_capability_v9")
    with pytest.raises(AuthorityUnavailable):
        load_c06_authority(_write_authority(tmp_path, unknown_capability))

    wrong_version = _authority_document()
    wrong_version["schema_version"] = "9.9.9"
    with pytest.raises(AuthorityUnavailable):
        load_c06_authority(_write_authority(tmp_path, wrong_version))


def test_unknown_answer_status_cannot_be_packaged(tmp_path: Path) -> None:
    """An answer the C06 adapter refuses to package must raise — the store
    then keeps the durable block instead of an invented envelope."""
    authority = load_c06_authority(_write_authority(tmp_path))
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    lifecycle = _lifecycle(store, authority=None)
    work_item_id = _settle(store, lifecycle, "IQS_05")

    checkpoint = store.get_answer_checkpoint(work_item_id)
    assert checkpoint is not None
    payload = json.loads(json.dumps(checkpoint["payload"]))
    payload["answer"]["status"] = "unknown"
    with pytest.raises(MissingC06Fields):
        build_c06_package(payload, authority=_adapter_input(authority))

    delivery = store.get_result_delivery(work_item_id)
    assert delivery is not None and delivery["state"] == "blocked"
    assert store.get_answer_checkpoint(work_item_id) is not None
