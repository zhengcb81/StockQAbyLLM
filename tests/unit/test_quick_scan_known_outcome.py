"""Synthetic settled native receipts and fixture-only owner authority; zero HTTP.

No fixture here represents a real owner approval, company answer or gold label.
The existing store/subject dispatcher/budget APIs create every normal control.
"""

import copy
import dataclasses
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.utils import quick_scan_subject_journal as subject
from src.utils.quick_scan_refresh_inspect import inspect_refresh
from src.utils.quick_scan_result_outbox import canonical_sha256
from src.utils.quick_scan_work_store import (
    Lease,
    LeaseFencedError,
    QuickScanWorkStore,
    WorkConflictError,
)
from tests.unit.test_c06_subject_send import receipt as subject_receipt
from tests.unit.test_c06_subject_send import v2
from tests.unit.test_quick_scan_question_pack_store import (
    _mark,
    _policy,
    _prepare,
    _receipt,
)

PROFILE = "iqs.refresh.native-currency/1.0.0"


def authority(*, bad=False, revoke=False):
    from src.utils.quick_scan_known_outcome_journal import (
        OwnerWorkDecisionAuthority,
        OwnerWorkDecisionGrant,
    )

    class FixtureAuthority(OwnerWorkDecisionAuthority):
        """Test double only; production must use the configured owner adapter."""

        calls = 0

        def authorize(self, request):
            self.calls += 1
            if revoke and self.calls > 1:
                raise PermissionError("synthetic revoked decision")
            return OwnerWorkDecisionGrant(
                owner_store_id="synthetic-owner",
                principal_id="synthetic-operator",
                confirmation_id="rc1_" + "a" * 64,
                action_id="synthetic-action",
                request_sha256="0" * 64 if bad else canonical_sha256(request),
                confirmed_at="2027-01-15T08:00:00Z",
            )

    return FixtureAuthority()


def fixture(
    tmp_path,
    *,
    response_status="length",
    outcome="response_available",
    cost=0.02,
    freeze=True,
    currency="CNY",
):
    clock = [1800000000.0]
    store = QuickScanWorkStore(tmp_path / "work.sqlite", clock=lambda: clock[0])
    context = v2()
    qids = list(context["questions"])[:3]
    members = []
    for qid in qids:
        row = context["questions"][qid]
        meta = row["metadata"]
        item = store.create_or_attach(
            entity_id=meta["entity_id"],
            question_id=qid,
            generation=1,
            scope=meta["scope"],
            scope_id=meta["entity_id"],
            identity_revision=1,
            source_binding_version=1,
            identity_state="verified",
            source_binding_ref="BND_FIXTURE",
            source_binding_refs=["BND_FIXTURE"],
            identity_snapshot_sha256=context["identity_snapshot_sha256"],
            question_fingerprint=row["work_prompt_sha256"],
            routing_fingerprint="e" * 64,
            run_id="RUN_SYNTHETIC",
            scan_id="SCAN_SYNTHETIC",
        )
        lease = store.claim(item["work_item_id"], lease_seconds=120)
        if freeze:
            store.freeze_observation_context_before_send(item["work_item_id"], lease, context)
        members.append(dict(work_item_id=item["work_item_id"], lease=lease))
    pack = _prepare(store, members)
    if freeze:
        with store._transaction() as con:
            for member in pack["members"]:
                item = store._item(con, member["work_item_id"])
                attempt = con.execute(
                    "SELECT * FROM attempt WHERE attempt_id=?", (member["attempt_id"],)
                ).fetchone()
                bound = subject.bound_context(con, item)
                request = subject.dispatch_request(
                    item,
                    attempt,
                    bound,
                    execution_attempt_id=pack["execution_attempt_id"],
                    provider_actual="mimo",
                    provider_prompt_sha256=pack["provider_prompt_sha256"],
                )
                original = next(m for m in members if m["work_item_id"] == member["work_item_id"])
                store.persist_subject_dispatch_receipt(
                    member["work_item_id"],
                    original["lease"],
                    member["attempt_id"],
                    subject_receipt(request),
                    expected_request=request,
                )
    policy = _policy()
    policy["budget"]["currency"] = currency
    _mark(store, pack, policy)
    now = datetime.fromtimestamp(clock[0], timezone.utc).isoformat().replace("+00:00", "Z")
    receipt = _receipt(
        response_status=response_status,
        completed_at=now,
        source_urls=["https://example.invalid/company"],
    )
    if outcome == "unknown":
        receipt.update(
            response_sha256=None,
            response_id=None,
            actual_model=None,
            http_status_code=None,
            search_status="unverified",
            response_status="unknown",
        )
    store.record_question_pack_outcome(
        pack["pack_id"],
        outcome=outcome,
        execution_receipt=receipt,
        http_status_code=receipt["http_status_code"],
        actual_cost=cost if outcome != "unknown" else None,
        cost_source_ref="synthetic-cost" if cost is not None and outcome != "unknown" else None,
    )
    return store, clock, members, pack, receipt, policy, context


def arguments(store, members, pack, receipt, *, index=0, reason="output_length", **changes):
    member = pack["members"][index]
    expected = next(m["lease"] for m in members if m["work_item_id"] == member["work_item_id"])
    value = dict(
        attempt_id=member["attempt_id"],
        expected_lease=expected,
        receipt_sha256=store.get_attempt_response(member["attempt_id"])["receipt_sha256"],
        request_payload_sha256=receipt["request_payload_sha256"],
        reason=reason,
        owner_authority=authority(),
        idempotency_key="resolve-fixture-" + str(index),
        diagnostic=None,
    )
    value.update(changes)
    return value


def resolve(store, members, pack, receipt, **changes):
    index = changes.get("index", 0)
    return store.resolve_known_response(
        pack["members"][index]["work_item_id"],
        **arguments(store, members, pack, receipt, **changes),
    )


def immutable(store):
    with closing(store._connect()) as con:
        return {
            t: [tuple(row) for row in con.execute("SELECT * FROM " + t)]
            for t in (
                "attempt",
                "quick_scan_attempt_response",
                "quick_scan_question_pack",
                "quick_scan_question_pack_member",
                "quick_scan_question_pack_outcome",
                "quick_scan_budget_attempt",
                "quick_scan_budget_terminal",
                "answer_checkpoint",
            )
        }


def test_settled_length_closes_only_one_logical_member_preserving_original_physical_cost(tmp_path):
    store, clock, members, pack, receipt, policy, _ = fixture(tmp_path)
    clock[0] += 121  # fixture clock only; production method reads real current clock.
    before = immutable(store)
    status = store.get_quick_scan_budget_status("pack-policy")
    saved = resolve(store, members, pack, receipt)
    assert saved["request"]["kind"] == "known_response_resolution"
    assert (
        saved["request"]["physical"]["budget_attempt_id"]
        == store.get_question_pack(pack["pack_id"])["budget_attempt_id"]
    )
    assert store.get_item(members[0]["work_item_id"])["status"] == "failed"
    assert store.get_item(members[1]["work_item_id"])["status"] == "leased"
    assert immutable(store) == before
    assert store.get_quick_scan_budget_status("pack-policy") == status
    assert store.get_answer_checkpoint(members[0]["work_item_id"]) is None
    again = QuickScanWorkStore(store.path, clock=lambda: clock[0])
    clock[0] += 30
    assert resolve(again, members, pack, receipt) == saved
    assert again.list_attempts(members[0]["work_item_id"])[0]["phase"] == "response_available"


def test_active_lease_cannot_be_taken_over_even_by_fixture_authority(tmp_path):
    store, clock, members, pack, receipt, _, _ = fixture(tmp_path)
    before = store.path.read_bytes()
    with pytest.raises(LeaseFencedError, match="active_lease"):
        resolve(store, members, pack, receipt)
    assert store.path.read_bytes() == before
    assert store.get_item(members[0]["work_item_id"])["lease_expires_at"] == clock[0] + 120


@pytest.mark.parametrize(
    "fault",
    [
        "lease-token",
        "epoch",
        "attempt",
        "receipt",
        "payload",
        "reason",
        "approval-dict",
        "wrong-grant",
        "revoked",
        "missing-context",
    ],
)
def test_resolution_rejects_unbound_or_unauthorized_inputs_atomically(tmp_path, fault):
    store, clock, members, pack, receipt, _, _ = fixture(
        tmp_path, freeze=fault != "missing-context"
    )
    clock[0] += 121
    kwargs = {}
    if fault == "lease-token":
        kwargs["expected_lease"] = Lease("different", members[0]["lease"].lease_epoch)
    if fault == "epoch":
        kwargs["expected_lease"] = Lease(members[0]["lease"].lease_token, 2)
    if fault == "attempt":
        kwargs["attempt_id"] = pack["members"][1]["attempt_id"]
    if fault == "receipt":
        kwargs["receipt_sha256"] = "0" * 64
    if fault == "payload":
        kwargs["request_payload_sha256"] = "0" * 64
    if fault == "reason":
        kwargs["reason"] = "provider_error"
    if fault == "approval-dict":
        kwargs["owner_authority"] = {"approved": True}
    if fault == "wrong-grant":
        kwargs["owner_authority"] = authority(bad=True)
    if fault == "revoked":
        kwargs["owner_authority"] = authority(revoke=True)
    before = store.path.read_bytes()
    with pytest.raises((ValueError, RuntimeError, PermissionError, TypeError)):
        resolve(store, members, pack, receipt, **kwargs)
    assert store.path.read_bytes() == before


def test_known_response_requires_settled_completed_physical_outcome(tmp_path):
    store, clock, members, pack, receipt, _, _ = fixture(tmp_path, cost=None)
    clock[0] += 121
    before = store.path.read_bytes()
    with pytest.raises(WorkConflictError, match="physical_not_settled"):
        resolve(store, members, pack, receipt)
    assert store.path.read_bytes() == before
    assert store.get_quick_scan_budget_status("pack-policy")["reserved_micros"] > 0


def test_unknown_send_never_becomes_known_response_or_retry(tmp_path):
    store, clock, members, pack, receipt, _, _ = fixture(tmp_path, outcome="unknown")
    clock[0] += 121
    before = store.path.read_bytes()
    # Deliberately supply fabricated hashes: the method must re-read owner data.
    with pytest.raises(WorkConflictError):
        store.resolve_known_response(
            members[0]["work_item_id"],
            attempt_id=pack["members"][0]["attempt_id"],
            expected_lease=members[0]["lease"],
            receipt_sha256="f" * 64,
            request_payload_sha256="e" * 64,
            reason="output_length",
            owner_authority=authority(),
            idempotency_key="unknown-denied",
        )
    with pytest.raises(WorkConflictError):
        store.record_owner_retry_decision(
            members[0]["work_item_id"],
            purpose="retry_unusable_answer",
            retry_after_seconds=0,
            owner_authority=authority(),
            idempotency_key="retry-denied",
        )
    assert store.path.read_bytes() == before
    assert store.get_quick_scan_budget_status("pack-policy")["reserved_micros"] == 1000000


def test_completed_invalid_answer_requires_bound_owner_validation_diagnostic(tmp_path):
    store, clock, members, pack, receipt, _, _ = fixture(tmp_path, response_status="completed")
    clock[0] += 121
    with pytest.raises(WorkConflictError):
        resolve(store, members, pack, receipt)
    with pytest.raises(WorkConflictError):
        resolve(store, members, pack, receipt, reason="answer_validation_rejected")
    response = store.get_attempt_response(pack["members"][0]["attempt_id"])
    diagnostic = dict(
        code="standard_answer_invalid",
        artifact_ref="synthetic-validation.json",
        artifact_sha256="a" * 64,
        response_sha256=response["response_sha256"],
    )
    saved = resolve(
        store, members, pack, receipt, reason="answer_validation_rejected", diagnostic=diagnostic
    )
    assert saved["request"]["diagnostic"] == diagnostic
    assert store.list_attempts(members[0]["work_item_id"])[0]["failure_category"] is None
    assert store.get_answer_checkpoint(members[0]["work_item_id"]) is None


def test_durable_retry_uses_decision_clock_without_resetting_budget_or_creating_generation(
    tmp_path,
):
    store, clock, members, pack, receipt, _, _ = fixture(tmp_path)
    clock[0] += 121
    resolved = resolve(store, members, pack, receipt)
    before = immutable(store)
    clock[0] += 10
    kwargs = dict(
        purpose="retry_unusable_answer",
        retry_after_seconds=60,
        owner_authority=authority(),
        idempotency_key="retry-control",
    )
    saved = store.record_owner_retry_decision(members[0]["work_item_id"], **kwargs)
    retry_at = datetime.fromisoformat(saved["next_retry_at"].replace("Z", "+00:00")).timestamp()
    assert retry_at == clock[0] + 60
    assert saved["request"]["source_resolution_id"] == resolved["decision_id"]
    assert saved["request"]["target_generation"] == 2
    assert immutable(store) == before
    entity = store.get_item(members[0]["work_item_id"])["entity_id"]
    qid = pack["members"][0]["question_id"]
    p = store.owner_refresh_projection(
        entity_id=entity,
        question_ids=[qid],
        scope_bindings={qid: dict(scope="entity", scope_id=entity)},
        include_owner_decisions=True,
    )
    assert p[qid]["next_retry_at"] == saved["next_retry_at"]
    assert p[qid]["retry_decision_id"] == saved["decision_id"]
    reopened = QuickScanWorkStore(store.path, clock=lambda: clock[0])
    clock[0] += 30
    assert reopened.record_owner_retry_decision(members[0]["work_item_id"], **kwargs) == saved
    assert reopened.get_owner_retry_decision(members[0]["work_item_id"]) == saved
    assert len(reopened.find_work_items(entity_id=entity, question_ids=[qid])) == 1
    with pytest.raises(WorkConflictError):
        reopened.record_owner_retry_decision(
            members[0]["work_item_id"], **{**kwargs, "retry_after_seconds": 0}
        )


@pytest.mark.parametrize("delay", [-1, True, float("nan"), float("inf"), 86401, "60"])
def test_retry_delay_cannot_be_past_or_non_numeric(tmp_path, delay):
    store, clock, members, pack, receipt, _, _ = fixture(tmp_path)
    clock[0] += 121
    resolve(store, members, pack, receipt)
    before = store.path.read_bytes()
    with pytest.raises((ValueError, TypeError)):
        store.record_owner_retry_decision(
            members[0]["work_item_id"],
            purpose="retry_unusable_answer",
            retry_after_seconds=delay,
            owner_authority=authority(),
            idempotency_key="invalid-delay",
        )
    assert store.path.read_bytes() == before


def test_independent_comparison_after_owner_resolution_does_not_relabel_invalid_as_scored(tmp_path):
    store, clock, members, pack, receipt, _, _ = fixture(tmp_path)
    clock[0] += 121
    resolve(store, members, pack, receipt)
    before = immutable(store)
    saved = store.record_owner_retry_decision(
        members[0]["work_item_id"],
        purpose="comparison",
        retry_after_seconds=0,
        owner_authority=authority(),
        idempotency_key="comparison-independent",
        comparison_controls=dict(pack_size=1, thinking_enabled=True, max_output_tokens=32768),
    )
    assert saved["request"]["source_checkpoint_sha256"] is None
    assert saved["request"]["source_resolution_id"] is not None
    assert saved["request"]["requires_prior_delivery"] is False
    assert store.get_item(members[0]["work_item_id"])["status"] == "failed"
    assert store.get_answer_checkpoint(members[0]["work_item_id"]) is None
    assert immutable(store) == before


def save_synthetic_standard(
    store, members, pack, receipt, context, *, status="insufficient_evidence"
):
    member = pack["members"][0]
    qid = member["question_id"]
    body = dict(
        question_id=qid,
        response_kind="score",
        status=status,
        score=None if status != "scored" else 8,
        summary="Synthetic test answer, no real company accuracy claim.",
        information_as_of="2026-09-01",
        period_start="2026-01-01",
        period_end="2026-06-30",
        basis="current",
        trend="stable",
        confidence="medium",
        metrics=[],
        items=[],
        evidence=[
            dict(
                id="e1",
                title="Synthetic fixture source",
                url="https://example.invalid/company",
                published_at="2026-09-01",
                claim="Synthetic fixture claim only.",
            )
        ],
        counterevidence="Synthetic missing detail.",
        watch_triggers=["Synthetic trigger"],
        missing_fields=["Synthetic missing detail"],
        coverage=dict(status="partial", reason="Synthetic fixture coverage only."),
    )
    answer = dict(
        entity_id=context["questions"][qid]["metadata"]["entity_id"],
        question_id=qid,
        status=body["status"],
        score=body["score"],
        description=body["summary"],
    )
    return store.save_answer_checkpoint(
        member["work_item_id"],
        members[0]["lease"],
        member["attempt_id"],
        answer=answer,
        execution_receipt=receipt,
        observation_context=context,
        standard_answer=body,
    )


@pytest.mark.parametrize("purpose", ["comparison", "retry_unknown_answer"])
def test_lawful_unknown_records_distinct_owner_intent_and_keeps_delivery_required(
    tmp_path, purpose
):
    store, clock, members, pack, receipt, _, context = fixture(
        tmp_path, response_status="completed"
    )
    checkpoint = save_synthetic_standard(store, members, pack, receipt, context)
    before = immutable(store)
    kwargs = dict(
        purpose=purpose,
        retry_after_seconds=30,
        owner_authority=authority(),
        idempotency_key="lawful-" + purpose,
    )
    if purpose == "comparison":
        kwargs["comparison_controls"] = dict(
            pack_size=1, thinking_enabled=True, max_output_tokens=32768
        )
    result = store.record_owner_retry_decision(members[0]["work_item_id"], **kwargs)
    assert result["request"]["source_checkpoint_sha256"] == checkpoint["payload_sha256"]
    assert result["request"]["requires_prior_delivery"] is (purpose != "comparison")
    assert result["request"]["target_generation"] == 2
    assert store.get_item(members[0]["work_item_id"])["status"] == "result_ready"
    assert (
        store.get_answer_checkpoint(members[0]["work_item_id"])["payload"]["answer"]["score"]
        is None
    )
    assert immutable(store) == before
    entity = store.get_item(members[0]["work_item_id"])["entity_id"]
    qid = pack["members"][0]["question_id"]
    projected = store.owner_refresh_projection(
        entity_id=entity,
        question_ids=[qid],
        scope_bindings={qid: dict(scope="entity", scope_id=entity)},
        include_owner_decisions=True,
    )
    assert projected[qid]["delivery_required"] is (purpose != "comparison")
    assert projected[qid]["status"] == "result_ready"
    with pytest.raises(WorkConflictError, match="existing_checkpoint"):
        resolve(store, members, pack, receipt)


@pytest.mark.parametrize(
    "fault", ["scope", "identity", "prompt", "request", "receipt", "lease_epoch"]
)
def test_current_durable_bindings_are_rechecked_not_just_caller_hashes(tmp_path, fault):
    store, clock, members, pack, receipt, _, _ = fixture(tmp_path)
    clock[0] += 121
    args = arguments(store, members, pack, receipt)
    with closing(sqlite3.connect(store.path)) as con:
        if fault == "scope":
            con.execute(
                "UPDATE work_item SET scope_id='ENT_FOREIGN' WHERE work_item_id=?",
                (members[0]["work_item_id"],),
            )
        elif fault == "identity":
            con.execute(
                "UPDATE work_item SET identity_revision=2 WHERE work_item_id=?",
                (members[0]["work_item_id"],),
            )
        else:
            field = {
                "prompt": "prompt_sha256",
                "request": "request_id",
                "receipt": "receipt_sha256",
                "lease_epoch": "lease_epoch",
            }[fault]
            value = 2 if fault == "lease_epoch" else "0" * 64
            con.execute(
                "UPDATE attempt SET " + field + "=? WHERE attempt_id=?",
                (value, pack["members"][0]["attempt_id"]),
            )
        con.commit()
    before = store.path.read_bytes()
    with pytest.raises((ValueError, RuntimeError)):
        store.resolve_known_response(members[0]["work_item_id"], **args)
    assert store.path.read_bytes() == before


def test_guard_prevents_parent_and_child_network_foreign_db_and_file_writes(tmp_path):
    import os
    import subprocess
    import sys

    code = """import os,pathlib,socket,sqlite3
checks=0
for action in (lambda:socket.create_connection(('127.0.0.1',9)),
 lambda:sqlite3.connect(str(pathlib.Path(os.environ['IQS_C06_OWN']).parent/'foreign.sqlite')),
 lambda:(pathlib.Path(os.environ['IQS_C06_OWN']).parent/'foreign.txt').write_text('blocked')):
 try:action()
 except (RuntimeError,PermissionError):checks+=1
 else:raise AssertionError('guard bypass')
assert not any(k.endswith('_API_KEY') for k in os.environ)
assert checks==3
print('3 guard refusals')
print('guard-child-pid',os.getpid())
"""
    result = subprocess.run(
        [sys.executable, "-B", "-X", "utf8", "-c", code],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "3 guard refusals" in result.stdout
    child_pid = int(result.stdout.split("guard-child-pid ")[1].split()[0])
    attestation = json.loads(
        (Path(os.environ["IQS_C06_OWN"]) / "attestations" / f"{child_pid}.json").read_bytes()
    )
    assert attestation["pid"] == child_pid
    assert attestation["guard_loaded"] is True


@pytest.mark.parametrize("currency", ["CNY", "USD"])
def test_inspection_native_quote_preserves_currency_without_writing(tmp_path, currency):
    store, clock, members, pack, receipt, policy, _ = fixture(tmp_path, currency=currency)
    clock[0] += 121
    resolve(store, members, pack, receipt)
    store.record_owner_retry_decision(
        members[0]["work_item_id"],
        purpose="retry_unusable_answer",
        retry_after_seconds=60,
        owner_authority=authority(),
        idempotency_key="retry-quote",
    )
    entity = store.get_item(members[0]["work_item_id"])["entity_id"]
    qid = pack["members"][0]["question_id"]
    req = dict(
        entity_id=entity,
        question_ids=[qid],
        scope_bindings={qid: dict(scope="entity", scope_id=entity)},
        policy_id="pack-policy",
        route_id="route-pack",
        provider_config_ref="mimo-config",
        model="mimo-v2.6-flash",
    )
    before = store.path.read_bytes()
    result = inspect_refresh(store.path, req, policy=policy, schema_version="1.1.0")
    assert (
        result["protocol"] == "stockqa.refresh_inspection/1.1.0"
        and "contract_profile" not in result
    )
    assert result["quote"]["currency"] == currency
    assert result["quote"]["max_cost_micros"] == 1980000
    assert result["quote"]["max_http_requests"] == 19
    assert "next_retry_at" not in result["work_items"][qid]
    assert store.get_owner_retry_decision(members[0]["work_item_id"])["next_retry_at"]
    assert store.path.read_bytes() == before
    if currency == "CNY":
        with pytest.raises(ValueError):
            inspect_refresh(store.path, req, policy=policy)
    else:
        original = inspect_refresh(store.path, req, policy=policy)
        explicit = inspect_refresh(store.path, req, policy=policy, schema_version="1.0.0")
        assert original == explicit and "contract_profile" not in original


def test_journal_rows_are_immutable_and_cannot_hide_source_tampering(tmp_path):
    store, clock, members, pack, receipt, _, _ = fixture(tmp_path)
    clock[0] += 121
    resolve(store, members, pack, receipt)
    with closing(sqlite3.connect(store.path)) as con:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("DELETE FROM quick_scan_owner_work_decision")
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE quick_scan_owner_work_decision SET document_sha256='x'")
    assert (
        QuickScanWorkStore(store.path, clock=lambda: clock[0]).get_item(members[0]["work_item_id"])[
            "status"
        ]
        == "failed"
    )


def test_version18_migration_preserves_original_rows_and_does_not_backfill_decisions(tmp_path):
    store, clock, members, pack, receipt, _, _ = fixture(tmp_path)
    before = immutable(store)
    from src.utils.quick_scan_known_outcome_journal import DDL_V19_ADDITIONS

    with closing(sqlite3.connect(store.path)) as con:
        for sql in reversed(DDL_V19_ADDITIONS):
            parts = sql.split()
            con.execute("DROP " + parts[1] + " " + parts[2])
        con.execute("PRAGMA user_version=18")
        con.commit()
    reopened = QuickScanWorkStore(store.path, clock=lambda: clock[0])
    assert immutable(reopened) == before
    assert reopened.get_owner_retry_decision(members[0]["work_item_id"]) is None
    with closing(sqlite3.connect(store.path)) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 19
        assert (
            con.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name IN ('quick_scan_work_event_no_delete','quick_scan_work_event_no_update')"
            ).fetchone()[0]
            == 2
        )
        assert con.execute("SELECT COUNT(*) FROM quick_scan_owner_work_decision").fetchone()[0] == 0


def test_public_read_only_quote_keeps_version18_compatible_without_hidden_migration(tmp_path):
    store, clock, members, pack, receipt, policy, _ = fixture(tmp_path, currency="USD")
    from src.utils.quick_scan_known_outcome_journal import DDL_V19_ADDITIONS

    with closing(sqlite3.connect(store.path)) as con:
        for sql in reversed(DDL_V19_ADDITIONS):
            parts = sql.split()
            con.execute("DROP " + parts[1] + " " + parts[2])
        con.execute("PRAGMA user_version=18")
        con.commit()
    entity = store.get_item(members[0]["work_item_id"])["entity_id"]
    qid = pack["members"][0]["question_id"]
    request = dict(
        entity_id=entity,
        question_ids=[qid],
        scope_bindings={qid: dict(scope="entity", scope_id=entity)},
        policy_id="pack-policy",
        route_id="route-pack",
        provider_config_ref="mimo-config",
        model="mimo-v2.6-flash",
    )
    before = store.path.read_bytes()
    result = inspect_refresh(store.path, request, policy=policy)
    assert (
        result["work_items"][qid]["status"] == "leased"
        and "next_retry_at" not in result["work_items"][qid]
    )
    assert store.path.read_bytes() == before
    with closing(sqlite3.connect(store.path)) as con:
        con.execute("PRAGMA user_version=19")
        con.commit()
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="schema"):
        inspect_refresh(store.path, request, policy=policy)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("response_status", ["completed", "length"])
def test_neutral_owner_resolution_does_not_claim_a_specific_parser_or_financial_fault(
    tmp_path, response_status
):
    store, clock, members, pack, receipt, _, _ = fixture(tmp_path, response_status=response_status)
    clock[0] += 121
    diagnostic = dict(
        code="known_response_no_accepted_answer",
        artifact_ref="synthetic-terminal.json",
        artifact_sha256="d" * 64,
        response_sha256=store.get_attempt_response(pack["members"][0]["attempt_id"])[
            "response_sha256"
        ],
    )
    before = immutable(store)
    saved = resolve(
        store,
        members,
        pack,
        receipt,
        reason="known_response_no_accepted_answer",
        diagnostic=diagnostic,
    )
    assert saved["request"]["diagnostic"]["code"] == "known_response_no_accepted_answer"
    assert immutable(store) == before
    assert store.get_answer_checkpoint(members[0]["work_item_id"]) is None
