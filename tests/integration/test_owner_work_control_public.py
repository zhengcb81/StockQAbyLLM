"""Public owner control over real temporary native stores, synthetic data only.

No vendor POST or company facts. Consent is explicitly fixture-only; actual
native CLI expiry, permission, journal and replay behavior are under test.
"""

import copy
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from contextlib import closing

import pytest
from stockwiki.quick_scan_work_decision_owner import (
    WorkDecisionConfirmationStore,
    WorkDecisionPreviewStore,
)

from src.utils import quick_scan_subject_journal as subject
from src.utils.quick_scan_subject_owner import register
from src.utils.quick_scan_work_decision_inspect import derive_work_decision
from src.utils.quick_scan_work_store import QuickScanWorkStore
from tests.integration.test_c06_refresh_admission import setup_admission
from tests.integration.test_c06_subject_public_e2e import OWN, actual, invoke, utc
from tests.unit.test_quick_scan_budget import _policy
from tests.unit.test_quick_scan_question_pack_store import _prepare, _receipt


def resolved_public_fixture(actual, tmp_path, *, policy=None, ledger_path=None):
    if policy is None:
        policy = _policy(model="fixture-model")
        policy["budget"]["currency"] = "CNY"
    prepared = setup_admission(
        actual, tmp_path, policy=policy, ledger_path=ledger_path, refresh_version="1.1.0"
    )
    routes, current, config, authority, manifest, identity_file = actual
    store = prepared["ledger"]
    context = authority["observation_context"]
    q = next(x for x in manifest["questions"] if x["scope"] == "entity")
    row = context["questions"][q["id"]]
    ref = row["subject_binding"]["subject_ref"]
    item = store.create_or_attach(
        entity_id=ref["entity_id"],
        question_id=q["id"],
        generation=1,
        scope="entity",
        scope_id=ref["scope_id"],
        identity_revision=1,
        identity_state="verified",
        source_binding_version=1,
        source_binding_ref="BND_fixture_1",
        source_binding_refs=["BND_fixture_1"],
        identity_snapshot_sha256=hashlib.sha256(identity_file.read_bytes()).hexdigest(),
        question_fingerprint=row["work_prompt_sha256"],
        routing_fingerprint="e" * 64,
        run_id="RUN_CONTROL_SYNTHETIC",
        scan_id="SCAN_CONTROL_SYNTHETIC",
    )
    lease = store.claim(item["work_item_id"], lease_seconds=300)
    store.freeze_observation_context_before_send(item["work_item_id"], lease, context)
    route = policy["routes"][0]
    pack = _prepare(
        store,
        [dict(work_item_id=item["work_item_id"], lease=lease)],
        route_id=route["id"],
        provider=route["provider_config_ref"],
        model_requested=route["model"],
        execution_attempt_id=str(uuid.uuid4()),
    )
    member = pack["members"][0]
    with store._transaction() as con:
        original = store._item(con, item["work_item_id"])
        attempt = con.execute(
            "SELECT * FROM attempt WHERE attempt_id=?", (member["attempt_id"],)
        ).fetchone()
        request = subject.dispatch_request(
            original,
            attempt,
            subject.bound_context(con, original),
            execution_attempt_id=pack["execution_attempt_id"],
            provider_actual="mimo",
            provider_prompt_sha256=pack["provider_prompt_sha256"],
        )
    pre = register(config, current, request, process_env=dict(os.environ))
    store.persist_subject_dispatch_receipt(
        item["work_item_id"], lease, member["attempt_id"], pre, expected_request=request
    )
    store.mark_question_pack_send_intent(
        pack["pack_id"],
        budget_policy=policy,
        budget_route=dict(
            route_id=route["id"],
            provider=route["provider_config_ref"],
            model_requested=route["model"],
            quota_group=route["quota_group"],
        ),
    )
    receipt = _receipt(
        request_id=None,
        actual_model=route["model"],
        requested_model=route["model"],
        attempt_id=pack["execution_attempt_id"],
        response_status="length",
        completed_at=utc(),
    )
    store.record_question_pack_outcome(
        pack["pack_id"],
        outcome="response_available",
        execution_receipt=receipt,
        http_status_code=200,
        actual_cost=0.02,
        cost_source_ref="synthetic-bookkeeping-only",
    )
    # Controlled fixture expiry, not a production recovery SQL operation.
    with closing(sqlite3.connect(store.path)) as con:
        con.execute(
            "UPDATE work_item SET lease_expires_at=0 WHERE work_item_id=?", (item["work_item_id"],)
        )
        con.commit()
    intent = dict(
        kind="known_response_resolution",
        work_item_id=item["work_item_id"],
        attempt_id=member["attempt_id"],
        idempotency_key="synthetic-native-resolution",
        reason="output_length",
        diagnostic=None,
    )
    derived = derive_work_decision(store.path, intent, policy=policy)
    owner_policy_file = prepared["paths"].config_dir / "quick_scan_refresh.json"
    owner_policy = json.loads(owner_policy_file.read_bytes())
    owner_policy["provider"] = "mimo"
    owner_policy_file.write_text(json.dumps(owner_policy), encoding="utf-8")
    from stockwiki.quick_scan_refresh_records import RefreshRecordStore

    original_refresh = RefreshRecordStore(prepared["paths"]).load(
        prepared["consent"]["preview_id"]
    )["request"]
    outer = dict(
        protocol="stockwiki.work_decision_request/1.0.0",
        consumer_id="invest-quick-scan",
        decision_request=derived["request"],
        permission_request=original_refresh,
    )
    file = tmp_path / "work-control.json"
    file.write_text(json.dumps(outer), encoding="utf-8")
    base = [
        sys.executable,
        "-B",
        "-X",
        "utf8",
        "-m",
        "stockwiki.quick_scan_refresh_owner_cli",
        "--root",
        str(prepared["paths"].root),
        "--iqs-code-root",
        str(OWN / "iqs"),
        "--iqs-release-root",
        config["iqs_release_root"],
    ]
    preview = invoke(base + ["--work-decision-request", str(file)], tmp_path, "control-preview")
    record = WorkDecisionPreviewStore(prepared["paths"]).load(preview["preview_id"])
    confirmation = WorkDecisionConfirmationStore(prepared["paths"]).save_control(
        record, idempotency_key=intent["idempotency_key"], confirmed_at=utc()
    )
    command = [
        sys.executable,
        "-B",
        "-X",
        "utf8",
        "-m",
        "src.utils.quick_scan_work_decision_apply_cli",
        "--ledger",
        str(store.path),
        "--policy",
        str(tmp_path / "budget.json"),
        "--owner-code-root",
        str(OWN / "sw"),
        "--owner-workspace",
        str(prepared["paths"].root),
        "--iqs-code-root",
        str(OWN / "iqs"),
        "--iqs-release-root",
        config["iqs_release_root"],
        "--consumer-id",
        "invest-quick-scan",
        "--confirmation-id",
        confirmation["confirmation_id"],
        "--refresh-schema-version",
        "1.1.0",
    ]

    def physical():
        with closing(sqlite3.connect(store.path)) as con:
            return {
                t: list(con.execute("SELECT * FROM " + t))
                for t in (
                    "attempt",
                    "quick_scan_attempt_response",
                    "quick_scan_question_pack",
                    "quick_scan_question_pack_member",
                    "quick_scan_question_pack_outcome",
                    "quick_scan_budget_attempt",
                    "quick_scan_budget_terminal",
                    "quick_scan_subject_send_receipt",
                )
            }

    before = physical()
    first = invoke(command, tmp_path, "control-apply-first", timeout=180)
    assert (
        first["status"] == "recorded"
        and first["API_requests"] == 0
        and first["dispatch_started"] is False
    )
    assert store.get_item(item["work_item_id"])["status"] == "failed"
    assert physical() == before
    replay = invoke(command, tmp_path, "control-apply-replay", timeout=180)
    assert replay["decision"] == first["decision"] and physical() == before
    assert store.get_answer_checkpoint(item["work_item_id"]) is None
    with closing(sqlite3.connect(store.path)) as con:
        assert con.execute("SELECT COUNT(*) FROM quick_scan_owner_work_decision").fetchone()[0] == 1
    return dict(
        prepared=prepared,
        store=store,
        item=item,
        policy=policy,
        outer=outer,
        base=base,
        command=command,
        owner_policy=owner_policy,
        owner_policy_file=owner_policy_file,
        physical=physical,
        before=before,
    )


@pytest.mark.parametrize("actual", ["1.2.0"], indirect=True)
def test_public_native_resolution_apply_and_exact_replay_preserve_physical_ledger(actual, tmp_path):
    resolved_public_fixture(actual, tmp_path)
