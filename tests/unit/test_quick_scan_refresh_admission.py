"""Owned configuration/path and exact scope tests, no synthetic auth claims."""

import copy
import json
from pathlib import Path

import pytest

from src.utils.quick_scan_refresh_admission import (
    AdmissionRejected,
    RefreshAdmissionService,
    match_question,
    safe_read,
)


def test_missing_config_or_unknown_permission_key_never_creates_ledger(tmp_path):
    path = tmp_path / "config.json"
    with pytest.raises(AdmissionRejected):
        RefreshAdmissionService(path)
    path.write_text(
        json.dumps({"protocol": "stockqa.refresh_admission_config/1.0.0", "confirmed": True}),
        encoding="utf-8",
    )
    with pytest.raises(AdmissionRejected):
        RefreshAdmissionService(path)
    assert not (tmp_path / "quick_scan_work.sqlite").exists()


@pytest.mark.parametrize("kind", ["duplicate", "large", "invalid_utf8", "array"])
def test_input_reader_is_strict_and_bounded(tmp_path, kind):
    path = tmp_path / "owned.json"
    raw = {
        "duplicate": b'{"confirmed":true,"confirmed":false}',
        "large": b" " * (8 * 1024 * 1024 + 1),
        "invalid_utf8": b"\xff",
        "array": b"[]",
    }[kind]
    path.write_bytes(raw)
    with pytest.raises(AdmissionRejected):
        safe_read(path)


def scope():
    ref = dict(
        entity_id="ENT_SYNTHETIC",
        analysis_subject_id="ASJ_11111111-1111-4111-8111-111111111111",
        analysis_subject_revision=1,
        primary_issuer_id="ENT_SYNTHETIC",
        perimeter_sha256="a" * 64,
        scope="entity",
        scope_id="ENT_SYNTHETIC",
        security_id=None,
        segment_id=None,
        listing_id=None,
    )
    item = dict(
        subject_ref=ref,
        field_id="synthetic-field",
        question_id="IQS_05",
        generation=1,
        decision="dispatch_new_work",
        reuse_observation_id=None,
        existing_work_status=None,
    )
    bundle = dict(
        identity=dict(entity_id="ENT_SYNTHETIC", identity_revision=1),
        authority=dict(
            schema_version="3.0.0",
            observation_context=dict(
                schema="stockqa.quick_scan_observation_context/2.0.0",
                questions={
                    "IQS_05": dict(
                        subject_binding=dict(subject_ref=copy.deepcopy(ref), identity_revision=1),
                        metadata=dict(field_id="synthetic-field", information_cutoff="2026-10-10"),
                    )
                },
            ),
        ),
        manifest=dict(questions=[dict(id="IQS_05", field_id="synthetic-field")]),
    )
    return item, bundle


@pytest.mark.parametrize(
    "fault",
    [
        "issuer",
        "subject",
        "revision",
        "perimeter",
        "scope",
        "security",
        "listing",
        "field",
        "question",
        "authority",
        "context",
        "cutoff",
    ],
)
def test_matching_is_full_subject_and_contract_not_entity_or_question_name_only(fault):
    item, bundle = scope()
    assert (
        match_question(item, bundle, information_cutoff="2026-10-10")["metadata"]["field_id"]
        == item["field_id"]
    )
    if fault in {"issuer", "subject", "revision", "perimeter", "scope", "security", "listing"}:
        key = {
            "issuer": "primary_issuer_id",
            "subject": "analysis_subject_id",
            "revision": "analysis_subject_revision",
            "perimeter": "perimeter_sha256",
            "scope": "scope_id",
            "security": "security_id",
            "listing": "listing_id",
        }[fault]
        item["subject_ref"][key] = 2 if key == "analysis_subject_revision" else "wrong"
    elif fault == "field":
        item["field_id"] = "wrong"
    elif fault == "question":
        item["question_id"] = "IQS_OTHER"
    elif fault == "authority":
        bundle["authority"]["schema_version"] = "2.0.0"
    elif fault == "context":
        bundle["authority"]["observation_context"][
            "schema"
        ] = "stockqa.quick_scan_observation_context/1.0.0"
    else:
        bundle["authority"]["observation_context"]["questions"]["IQS_05"]["metadata"][
            "information_cutoff"
        ] = "2025-01-01"
    with pytest.raises(AdmissionRejected):
        match_question(item, bundle, information_cutoff="2026-10-10")


@pytest.mark.parametrize("known", [True, False])
def test_paid_registered_scope_requires_actual_original_response_without_new_work(tmp_path, known):
    from tests.unit.test_quick_scan_work_store import (
        _created,
        _prepared,
        _store,
        _successful_search_attempt,
    )

    clock = [1800000000.0]
    store = _store(tmp_path, clock)
    work = _created(store)
    if known:
        _successful_search_attempt(store, work["work_item_id"])
    else:
        lease = store.claim(work["work_item_id"], lease_seconds=30)
        attempt = _prepared(store, work["work_item_id"], lease)
        store.mark_send_intent(work["work_item_id"], lease, attempt["attempt_id"])
    clock[0] += 1000
    record = {
        "receipt": {
            "items": [
                {
                    key: work[key]
                    for key in (
                        "work_item_id",
                        "entity_id",
                        "question_id",
                        "generation",
                        "scope",
                        "scope_id",
                    )
                }
            ]
        }
    }
    service = RefreshAdmissionService.__new__(RefreshAdmissionService)
    service.store = store
    before = store.path.read_bytes()
    if known:
        service._assert_received_response_scope(record)
    else:
        with pytest.raises(AdmissionRejected, match="original_response_required"):
            service._assert_received_response_scope(record)
    assert store.path.read_bytes() == before
    assert len(store.list_attempts(work["work_item_id"])) == 1
