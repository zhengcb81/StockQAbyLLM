"""Reviewer-owned regression: durable ready work is not an ACKed fresh observation.

Synthetic Q13 CP/outbox fixture, original SQLite/lifecycle/freshness methods.
No native-consent or investment-accuracy claim is made by this component test.
"""

import copy

import pytest

freshness = pytest.importorskip(
    "stockwiki.quick_scan_freshness", reason="requires StockWiki integration source"
)

from src.utils.quick_scan_work_store import QuickScanWorkStore

build_gap_plan = freshness.build_gap_plan
from tests.unit.test_q10_delivery import (
    JR2_CONSUMER,
    JR2_SOURCE,
    _jr2_ready,
    _lifecycle,
    _q,
)


def _fields(item, generation=None):
    return {
        "synthetic.field": {
            "observation": {
                "observation_id": "synthetic-expired-observation",
                "information_as_of": "2026-01-01",
                "valid_until": "2026-01-02T00:00:00Z",
                "event_invalidated_at": None,
                "response_status": "scored",
                "question_fingerprint": item["question_fingerprint"],
                "routing_fingerprint": item["routing_fingerprint"],
                "scope": item["scope"],
                "scope_id": item["scope_id"],
                "meaning_version": "1.0.0",
                "information_cutoff": "2026-01-01",
                "check_level": "unverified_model_output",
            },
            "expected": {
                "entity_id": item["entity_id"],
                "question_id": item["question_id"],
                "scope": item["scope"],
                "scope_id": item["scope_id"],
                "model": "fixture-model",
                "question_fingerprint": item["question_fingerprint"],
                "routing_fingerprint": item["routing_fingerprint"],
                "information_cutoff": "2026-01-01",
                "meaning_version": "1.0.0",
                "minimum_check_level": "unverified_model_output",
                "search_enabled": True,
                "generation": item["generation"] if generation is None else generation,
            },
            "work_item": {"status": item["status"], "generation": item["generation"]},
        }
    }


def test_ready_checkpoint_reopened_after_observation_ttl_keeps_exact_work_and_outbox(tmp_path):
    store, wid, ready = _jr2_ready(tmp_path)
    before_checkpoint = copy.deepcopy(store.get_answer_checkpoint(wid))
    before_attempts = copy.deepcopy(store.list_attempts(wid))
    store.bind_result_delivery_consumer(wid, JR2_CONSUMER, source_ref=JR2_SOURCE)
    first = store.begin_result_delivery(wid)
    exact = first["request_bytes"], first["idempotency_key"], first["package_sha256"]
    store.confirm_result_delivery_not_sent(wid, "connection_refused_before_write")
    # A new store/lifecycle models process restart. Only the synthetic work clock
    # moves; no owner permission or frozen observation dates are rewritten.
    reopened = QuickScanWorkStore(store.path, clock=lambda: 1900000000.0)
    item = reopened.get_item(wid)
    assert item["status"] == "result_ready"
    fields = _fields(item)
    before_fields = copy.deepcopy(fields)
    plan = build_gap_plan(now="2026-10-30T00:00:00Z", fields=fields)
    assert plan["fields"][0]["decision"] == "resume_existing_work"
    assert plan["model_calls_planned"] == plan["api_requests_planned"] == 0
    assert fields == before_fields
    handle = _lifecycle(reopened, run_id="REVIEWER_RESTART").before_question(
        _q("IQS_05", "问题 IQS_05")
    )
    assert handle["claimed"] is False and handle["work_item_id"] == wid
    assert reopened.list_attempts(wid) == before_attempts
    assert reopened.get_answer_checkpoint(wid) == before_checkpoint
    resumed = reopened.begin_result_delivery(wid)
    assert (
        resumed["request_bytes"],
        resumed["idempotency_key"],
        resumed["package_sha256"],
    ) == exact
    assert reopened.get_result_delivery(wid)["package_bytes"] == ready["package_bytes"]


def test_ready_checkpoint_does_not_mask_generation_change_after_restart(tmp_path):
    store, wid, _ = _jr2_ready(tmp_path)
    reopened = QuickScanWorkStore(store.path)
    item = reopened.get_item(wid)
    before = copy.deepcopy(reopened.get_answer_checkpoint(wid))
    plan = build_gap_plan(now="2026-10-30T00:00:00Z", fields=_fields(item, item["generation"] + 1))
    assert plan["fields"][0]["decision"] != "resume_existing_work"
    assert plan["totals"]["planned_dispatches"] == 1
    # Planning a new-generation scope never destroys the old checkpoint.
    assert reopened.get_answer_checkpoint(wid) == before
    assert reopened.get_item(wid)["generation"] == item["generation"]
