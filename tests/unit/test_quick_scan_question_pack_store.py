"""Native question-pack journal: one physical HTTP, independent logical answers.

All identities, payloads, HTTP receipts and databases below are synthetic and
isolated in pytest's own tmp_path. No provider or owner request is dispatched.
"""

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing

import pytest

from src.providers.model_resolution import model_resolution_sha256
from src.utils.quick_scan_work_store import (
    BudgetAdmissionError,
    LeaseFencedError,
    QuickScanWorkStore,
    WorkConflictError,
    _sanitized_receipt,
)


@pytest.fixture
def pack_store(tmp_path):
    clock = [1_800_000_000.0]
    store = QuickScanWorkStore(tmp_path / "work.sqlite", clock=lambda: clock[0])
    return store, clock


def _members(store, changes=None, count=3):
    members = []
    for i in range(count):
        spec = dict(
            entity_id="ENT_PACK_FIXTURE",
            question_id=f"IQS_{i + 1:02d}",
            generation=1,
            scope="entity",
            scope_id="ENT_PACK_FIXTURE",
            identity_revision=1,
            source_binding_version=1,
            identity_state="verified",
            source_binding_ref="BND_PACK",
            source_binding_refs=["BND_PACK"],
            identity_snapshot_sha256="a" * 64,
            question_fingerprint=hashlib.sha256(f"question-{i}".encode()).hexdigest(),
            routing_fingerprint="b" * 64,
            run_id="RUN_PACK",
            scan_id="SCAN_PACK",
        )
        if changes and i == 1:
            spec.update(changes)
        item = store.create_or_attach(**spec)
        lease = store.claim(item["work_item_id"], lease_seconds=60 if i == 1 else 120)
        members.append({"work_item_id": item["work_item_id"], "lease": lease})
    return members


def _prepare(store, members, **changes):
    fields = dict(
        route_id="route-pack",
        provider="mimo-config",
        model_requested="mimo-v2.6-flash",
        execution_attempt_id="HTTP_PACK_01",
        prompt_sha256="c" * 64,
        provider_prompt_sha256="d" * 64,
        request_payload_sha256="e" * 64,
        search_mode="native_search",
    )
    fields.update(changes)
    return store.prepare_question_pack(members, **fields)


def _policy(max_requests=20, max_cost=2):
    return {
        "configured": True,
        "policy_id": "pack-policy",
        "policy_version": "pack-policy@1",
        "budget": {
            "currency": "USD",
            "max_cost": max_cost,
            "max_requests": max_requests,
            "max_cost_per_attempt": 1,
        },
        "cost_policy": {
            "pricing_basis": "user_cap",
            "pricing_ref": None,
            "reserve_before_dispatch": True,
            "unknown_actual_cost_action": "retain_reservation_and_pause",
        },
        "dispatch": {"max_in_flight_total": 1},
        "quota_groups": [{"id": "pack-quota", "max_in_flight": 1}],
        "routes": [
            {
                "id": "route-pack",
                "provider_config_ref": "mimo-config",
                "model": "mimo-v2.6-flash",
                "quota_group": "pack-quota",
                "max_in_flight": 1,
                "eligible": True,
                "unavailable_reason": None,
            }
        ],
    }


def _mark(store, pack, policy=None):
    return store.mark_question_pack_send_intent(
        pack["pack_id"],
        budget_policy=policy or _policy(),
        budget_route={
            "route_id": "route-pack",
            "provider": "mimo-config",
            "model_requested": "mimo-v2.6-flash",
            "quota_group": "pack-quota",
        },
    )


def _receipt(**changes):
    value = {
        "provider": "mimo",
        "request_id": "req-pack",
        "response_id": "resp-pack",
        "actual_model": "mimo-v2.6-flash",
        "requested_model": "mimo-v2.6-flash",
        "search_protocol": "mimo_chat_completions",
        "response_sha256": "f" * 64,
        "response_json_basis": "strict_http_json",
        "model_resolution_sha256": model_resolution_sha256(None),
        "search_status": "executed",
        "response_status": "completed",
        "http_status_code": 200,
        "attempt_id": "HTTP_PACK_01",
        "prompt_sha256": "d" * 64,
        "request_payload_sha256": "e" * 64,
        "search_receipt_id": "SEARCH_PACK_01",
        "completed_at": "2026-09-27T12:00:00Z",
        "source_urls": ["https://example.com/synthetic"],
        "usage": {
            "schema": "quick-scan-usage-v1",
            "input_tokens": 300,
            "cached_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 600,
            "reasoning_output_tokens": 0,
            "search_tool_calls": 1,
        },
    }
    value.update(changes)
    return value


def _count(store, table, condition="1=1"):
    with closing(store._connect()) as con:
        return con.execute(f"SELECT COUNT(*) FROM {table} WHERE {condition}").fetchone()[0]


def test_question_pack_three_members_have_one_physical_budget_and_independent_attempts(
    pack_store,
):
    store, _ = pack_store
    pack = _prepare(store, _members(store))
    assert len(pack["members"]) == 3
    assert len({row["attempt_id"] for row in pack["members"]}) == 3
    assert _count(store, "attempt") == 3
    assert _count(store, "quick_scan_budget_attempt") == 0
    intent = _mark(store, pack)
    assert intent["budget_attempt_id"] != "HTTP_PACK_01"
    assert _count(store, "quick_scan_budget_attempt") == 1
    assert _count(store, "attempt", "phase='send_intent'") == 3
    status = store.get_quick_scan_budget_status("pack-policy")
    assert status["requests"] == 1
    assert status["in_flight"] == 1
    store.record_question_pack_outcome(
        pack["pack_id"],
        outcome="response_available",
        execution_receipt=_receipt(),
        http_status_code=200,
        actual_cost=0.02,
        cost_source_ref="fixture-only",
    )
    assert _count(store, "attempt", "phase='response_available'") == 3
    status = store.get_quick_scan_budget_status("pack-policy")
    assert status["requests"] == 1
    assert status["spent_micros"] == 20000
    assert status["in_flight"] == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"entity_id": "ENT_OTHER", "scope_id": "ENT_OTHER"},
        {"scope": "security", "scope_id": "SEC_OTHER"},
        {"identity_revision": 2},
        {"identity_snapshot_sha256": "0" * 64},
        {"identity_state": "provisional"},
    ],
)
def test_question_pack_rejects_mixed_identity_or_scope_atomically(pack_store, changes):
    store, _ = pack_store
    members = _members(store, changes)
    with pytest.raises((ValueError, WorkConflictError)):
        _prepare(store, members)
    assert _count(store, "attempt") == 0
    assert _count(store, "quick_scan_question_pack") == 0


def test_question_pack_rejects_duplicate_or_empty_members_before_any_attempt(
    pack_store,
):
    store, _ = pack_store
    members = _members(store)
    for candidate in [[], [members[0], members[0]], members * 11]:
        with pytest.raises(ValueError):
            _prepare(store, candidate)
    assert _count(store, "attempt") == 0


def test_question_pack_expired_middle_member_rolls_back_prepare_and_send_intents(
    pack_store,
):
    store, clock = pack_store
    members = _members(store)
    clock[0] += 61
    with pytest.raises(LeaseFencedError):
        _prepare(store, members)
    assert _count(store, "attempt") == 0
    clock[0] -= 61
    pack = _prepare(store, members)
    clock[0] += 61
    with pytest.raises(LeaseFencedError):
        _mark(store, pack)
    assert _count(store, "attempt", "phase='prepared'") == 3
    assert _count(store, "quick_scan_budget_attempt") == 0
    assert store.get_question_pack(pack["pack_id"])["phase"] == "prepared"


def test_question_pack_send_budget_failure_has_no_partial_intent(pack_store):
    store, _ = pack_store
    pack = _prepare(store, _members(store))
    with pytest.raises(BudgetAdmissionError, match="budget_cost_limit"):
        _mark(store, pack, _policy(max_cost=0.5))
    assert _count(store, "attempt", "phase='send_intent'") == 0
    assert _count(store, "quick_scan_budget_attempt") == 0


def test_question_pack_nested_existing_method_failure_rolls_back_whole_mark(
    pack_store, monkeypatch
):
    store, _ = pack_store
    pack = _prepare(store, _members(store))
    original = store.mark_send_intent
    calls = []

    def interrupted(*args, **kwargs):
        calls.append(args[0])
        if len(calls) == 2:
            raise RuntimeError("synthetic second member failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "mark_send_intent", interrupted)
    with pytest.raises(RuntimeError, match="second member"):
        _mark(store, pack)
    assert _count(store, "attempt", "phase='send_intent'") == 0
    assert _count(store, "quick_scan_budget_attempt") == 0
    assert store.get_question_pack(pack["pack_id"])["phase"] == "prepared"


def test_question_pack_unknown_keeps_one_reserved_cost_and_all_members_unclaimable(
    pack_store,
):
    store, _ = pack_store
    members = _members(store)
    pack = _prepare(store, members)
    _mark(store, pack)
    receipt = _receipt(
        response_sha256=None,
        response_id=None,
        actual_model=None,
        http_status_code=None,
        search_status="unverified",
        response_status="unknown",
    )
    store.record_question_pack_outcome(
        pack["pack_id"], outcome="unknown", execution_receipt=receipt
    )
    for member in members:
        assert store.get_item(member["work_item_id"])["status"] == "uncertain"
        assert store.claim(member["work_item_id"], lease_seconds=60) is None
    assert _count(store, "attempt", "phase='uncertain'") == 3
    assert store.get_question_pack(pack["pack_id"])["members_uncertain"] is True
    status = store.get_quick_scan_budget_status("pack-policy")
    assert status["requests"] == 1
    assert status["reserved_micros"] == 1000000
    with pytest.raises((WorkConflictError, LeaseFencedError)):
        _prepare(store, members, execution_attempt_id="HTTP_PACK_02")
    assert _count(store, "quick_scan_budget_attempt") == 1


def test_question_pack_partial_answer_saves_only_valid_member_and_cannot_reask_others(
    pack_store,
):
    store, clock = pack_store
    members = _members(store)
    pack = _prepare(store, members)
    _mark(store, pack)
    receipt = _receipt()
    store.record_question_pack_outcome(
        pack["pack_id"],
        outcome="response_available",
        execution_receipt=receipt,
        http_status_code=200,
        actual_cost=0.02,
        cost_source_ref="fixture-only",
    )
    first = pack["members"][0]
    store.save_answer_checkpoint(
        first["work_item_id"],
        members[0]["lease"],
        first["attempt_id"],
        answer={
            "entity_id": "ENT_PACK_FIXTURE",
            "question_id": "IQS_01",
            "status": "scored",
            "score": 8,
            "description": "synthetic valid first member",
        },
        execution_receipt=receipt,
    )
    assert store.get_answer_checkpoint(members[0]["work_item_id"]) is not None
    for member in members[1:]:
        assert store.get_answer_checkpoint(member["work_item_id"]) is None
    clock[0] += 121
    for member in members[1:]:
        assert store.recover_expired(member["work_item_id"]) == "uncertain"
        assert store.claim(member["work_item_id"], lease_seconds=60) is None
    assert store.get_quick_scan_budget_status("pack-policy")["requests"] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"request_payload_sha256": "0" * 64},
        {"attempt_id": "HTTP_OTHER"},
        {"prompt_sha256": "0" * 64},
        {"requested_model": "other-model"},
        {"actual_model": "other-model"},
    ],
)
def test_question_pack_rejects_wrong_physical_receipt_without_partial_state(pack_store, changes):
    store, _ = pack_store
    pack = _prepare(store, _members(store))
    _mark(store, pack)
    with pytest.raises((ValueError, WorkConflictError)):
        store.record_question_pack_outcome(
            pack["pack_id"],
            outcome="response_available",
            execution_receipt=_receipt(**changes),
            http_status_code=200,
        )
    assert _count(store, "attempt", "phase='send_intent'") == 3
    assert _count(store, "quick_scan_attempt_response") == 0


def test_question_pack_late_middle_lease_retains_real_receipt_once_and_all_members_uncertain(
    pack_store,
):
    store, clock = pack_store
    members = _members(store)
    pack = _prepare(store, members)
    _mark(store, pack)
    clock[0] += 61
    result = store.record_question_pack_outcome(
        pack["pack_id"],
        outcome="response_available",
        execution_receipt=_receipt(),
        http_status_code=200,
        actual_cost=0.02,
        cost_source_ref="fixture-only",
    )
    assert result["members_uncertain"] is True
    assert result["physical_outcome"] == "response_available"
    assert all(
        store.get_item(member["work_item_id"])["status"] == "uncertain" for member in members
    )
    assert (
        store.get_attempt_response(result["budget_attempt_id"])["receipt"]["usage"]["input_tokens"]
        == 300
    )
    assert store.get_quick_scan_budget_status("pack-policy")["requests"] == 1
    assert store.get_quick_scan_budget_status("pack-policy")["spent_micros"] == 20000


def test_question_pack_individual_member_cannot_bypass_group_send(pack_store):
    store, _ = pack_store
    members = _members(store)
    pack = _prepare(store, members)
    first = pack["members"][0]
    with pytest.raises(WorkConflictError, match="question.pack"):
        store.mark_send_intent(first["work_item_id"], members[0]["lease"], first["attempt_id"])
    assert _count(store, "attempt", "phase='send_intent'") == 0


def test_question_pack_membership_is_immutable_and_reopens_in_exact_order(pack_store):
    store, clock = pack_store
    pack = _prepare(store, list(reversed(_members(store))))
    expected_ids = [member["question_id"] for member in pack["members"]]
    assert expected_ids == ["IQS_03", "IQS_02", "IQS_01"]
    with closing(store._connect()) as con:
        for sql in [
            "DELETE FROM quick_scan_question_pack_member",
            "UPDATE quick_scan_question_pack_member SET ordinal=0",
            "UPDATE quick_scan_question_pack SET request_payload_sha256='" + "0" * 64 + "'",
        ]:
            with pytest.raises(sqlite3.IntegrityError):
                con.execute(sql)
    reopened = QuickScanWorkStore(store.path, clock=lambda: clock[0])
    assert reopened.get_question_pack(pack["pack_id"]) == pack


@pytest.mark.parametrize("search_mode", ["external_context_only", "external_plus_native"])
def test_question_pack_rejects_external_context_v1_without_downgrading(pack_store, search_mode):
    store, _ = pack_store
    with pytest.raises(ValueError, match="native"):
        _prepare(store, _members(store), search_mode=search_mode)
    assert _count(store, "attempt") == 0


def test_question_pack_schema16_fixture_migration_preserves_original_and_old_rows(
    pack_store, tmp_path
):
    from src.utils import quick_scan_work_store as work_module

    store, clock = pack_store
    fixture_members = _members(store)
    with closing(store._connect()) as con:
        later = (
            work_module._DDL_V17_ADDITIONS
            + work_module._DDL_V18_ADDITIONS
            + work_module._DDL_V19_ADDITIONS
            + work_module._DDL_V20_ADDITIONS
            + work_module._DDL_V21_ADDITIONS
            + work_module._DDL_V22_ADDITIONS
            + work_module._DDL_V23_ADDITIONS
        )
        for statement in reversed(later):
            words = statement.split()
            con.execute(f"DROP {words[1]} {words[2]}")
        # v21 replaced this pre-existing v13 guard. Exact v16 needs its
        # original SQL too, not merely fewer table names and a lowered pragma.
        con.execute(work_module._MCP_GUARD_OLD)
        con.execute("PRAGMA user_version=16")
        old_rows = {
            row[0]: con.execute(f'SELECT * FROM "{row[0]}"').fetchall()
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    original_bytes = store.path.read_bytes()
    target = tmp_path / "migrated.sqlite"
    shutil.copyfile(store.path, target)
    migrated = QuickScanWorkStore(target, clock=lambda: clock[0])
    assert store.path.read_bytes() == original_bytes
    with closing(migrated._connect()) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == work_module.SCHEMA_VERSION
        assert {
            name: con.execute(f'SELECT * FROM "{name}"').fetchall() for name in old_rows
        } == old_rows
    assert migrated.get_item(fixture_members[0]["work_item_id"])


@pytest.mark.parametrize("value", [None, 5, True, "E" * 64, "e" * 63, "e" * 65])
def test_question_pack_receipt_payload_hash_only_retains_exact_lowercase_sha256(value):
    assert "request_payload_sha256" not in _sanitized_receipt({"request_payload_sha256": value})


def test_question_pack_receipt_payload_hash_survives_sanitization():
    assert _sanitized_receipt({"request_payload_sha256": "e" * 64}) == {
        "request_payload_sha256": "e" * 64
    }


def test_question_pack_post_rechecks_middle_lease_and_restart_never_marks_twice(
    pack_store,
):
    store, clock = pack_store
    pack = _prepare(store, _members(store))
    _mark(store, pack)
    assert store.validate_question_pack_for_post(pack["pack_id"])["phase"] == "send_intent"
    reopened = QuickScanWorkStore(store.path, clock=lambda: clock[0])
    with pytest.raises(WorkConflictError):
        _mark(reopened, pack)
    clock[0] += 61
    with pytest.raises(LeaseFencedError):
        reopened.validate_question_pack_for_post(pack["pack_id"])
    assert store.get_quick_scan_budget_status("pack-policy")["requests"] == 1


def test_question_pack_confirmed_auth_refusal_updates_three_attempts_and_one_zero_cost(
    pack_store,
):
    store, _ = pack_store
    pack = _prepare(store, _members(store))
    _mark(store, pack)
    receipt = _receipt(
        http_status_code=401,
        response_id=None,
        actual_model=None,
        search_status="unverified",
        response_status="error",
        failure_category="authentication_rejected",
    )
    result = store.record_question_pack_outcome(
        pack["pack_id"],
        outcome="confirmed_failure",
        execution_receipt=receipt,
        http_status_code=401,
        failure_category="authentication_rejected",
        actual_cost=0,
        cost_source_ref="synthetic-refusal",
    )
    assert result["phase"] == "confirmed_failure"
    assert _count(store, "attempt", "phase='confirmed_failure'") == 3
    assert store.get_quick_scan_budget_status("pack-policy")["requests"] == 1
    assert store.get_quick_scan_budget_status("pack-policy")["reserved_micros"] == 0


def test_question_pack_physical_settlement_failure_rolls_back_all_logical_outcomes(
    pack_store, monkeypatch
):
    store, _ = pack_store
    pack = _prepare(store, _members(store))
    _mark(store, pack)

    def interrupted(*args, **kwargs):
        raise RuntimeError("synthetic physical settlement failure")

    monkeypatch.setattr(store, "record_budget_outcome", interrupted)
    with pytest.raises(RuntimeError, match="settlement"):
        store.record_question_pack_outcome(
            pack["pack_id"],
            outcome="response_available",
            execution_receipt=_receipt(),
            http_status_code=200,
        )
    assert _count(store, "attempt", "phase='send_intent'") == 3
    assert _count(store, "quick_scan_attempt_response") == 0
    assert store.get_question_pack(pack["pack_id"])["phase"] == "send_intent"
    assert store.get_quick_scan_budget_status("pack-policy")["reserved_micros"] == 1000000


def test_question_pack_existing_checkpoint_or_sent_attempt_cannot_join_new_pack(
    pack_store,
):
    store, _ = pack_store
    members = _members(store)
    pack = _prepare(store, members)
    _mark(store, pack)
    with pytest.raises(WorkConflictError, match="unresolved"):
        _prepare(store, members, execution_attempt_id="HTTP_PACK_02")
    assert _count(store, "quick_scan_question_pack") == 1


def test_question_pack_original_bounded_native_events_preserved_for_each_answer_and_physical_once(
    pack_store,
):
    store, _ = pack_store
    pack = _prepare(store, _members(store))
    _mark(store, pack)
    events = [
        {
            "id": "synthetic-native-event-1",
            "status": "completed",
            "action_type": "search",
            "source_urls": ["https://example.com/synthetic"],
        }
    ]
    receipt = _receipt(web_search_calls=events)
    result = store.record_question_pack_outcome(
        pack["pack_id"],
        outcome="response_available",
        execution_receipt=receipt,
        http_status_code=200,
        actual_cost=0.02,
        cost_source_ref="fixture-only",
    )
    assert result["execution_receipt"]["web_search_calls"] == events
    assert result["native_search_events"] == events
    for member in result["members"]:
        response = store.get_attempt_response(member["attempt_id"])
        assert response["native_search_events"] == events
        assert response["receipt"]["attempt_id"] == "HTTP_PACK_01"
    assert store.get_attempt_response(result["budget_attempt_id"])["native_search_events"] == events
    assert store.get_quick_scan_budget_status("pack-policy")["requests"] == 1
    reopened = QuickScanWorkStore(store.path)
    assert (
        reopened.get_question_pack(pack["pack_id"])["execution_receipt"]["web_search_calls"]
        == events
    )


def test_question_pack_store_can_be_imported_first_in_a_fresh_offline_process():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            "-c",
            "from src.utils.quick_scan_work_store import QuickScanWorkStore; print(QuickScanWorkStore.__name__)",
        ],
        capture_output=True,
        text=True,
        env=__import__("os").environ.copy(),
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "QuickScanWorkStore"


def test_question_pack_missing_member_cannot_bypass_no_resend_using_single_format_repair(
    pack_store,
):
    store, _ = pack_store
    members = _members(store)
    pack = _prepare(store, members)
    _mark(store, pack)
    store.record_question_pack_outcome(
        pack["pack_id"],
        outcome="response_available",
        execution_receipt=_receipt(),
        http_status_code=200,
        actual_cost=0.02,
        cost_source_ref="fixture-only",
    )
    second = pack["members"][1]
    with pytest.raises(WorkConflictError, match="question pack"):
        store.prepare_attempt(
            second["work_item_id"],
            members[1]["lease"],
            route_id="route-pack",
            provider="mimo-config",
            model_requested="mimo-v2.6-flash",
            request_cache_key="REQ_" + "1" * 64,
            prompt_sha256="2" * 64,
            allow_format_repair=True,
        )
    assert _count(store, "attempt") == 3


def test_partial_pack_known_failures_finish_individually_without_repricing_or_reask(
    pack_store,
):
    store, clock = pack_store
    members = _members(store)
    pack = _prepare(store, members)
    _mark(store, pack)
    receipt = _receipt()
    store.record_question_pack_outcome(
        pack["pack_id"],
        outcome="response_available",
        execution_receipt=receipt,
        http_status_code=200,
        actual_cost=0.02,
        cost_source_ref="fixture-only",
    )
    first = pack["members"][0]
    store.save_answer_checkpoint(
        first["work_item_id"],
        members[0]["lease"],
        first["attempt_id"],
        answer=dict(
            entity_id="ENT_PACK_FIXTURE",
            question_id="IQS_01",
            status="scored",
            score=8,
            description="Synthetic valid member",
        ),
        execution_receipt=receipt,
    )
    budget = store.get_quick_scan_budget_status("pack-policy")
    original_pack = store.get_question_pack(pack["pack_id"])
    attempts = {m["work_item_id"]: store.list_attempts(m["work_item_id"]) for m in members}
    for index, reason in ((1, "missing_question"), (2, "invalid_question")):
        row = pack["members"][index]
        assert (
            store.fail_received_without_answer(
                row["work_item_id"],
                members[index]["lease"],
                row["attempt_id"],
                reason_code=reason,
            )
            == "failed"
        )
        assert store.claim(row["work_item_id"], lease_seconds=30) is None
    assert store.get_item(first["work_item_id"])["status"] == "result_ready"
    assert store.get_quick_scan_budget_status("pack-policy") == budget and budget["requests"] == 1
    assert store.get_question_pack(pack["pack_id"]) == original_pack
    assert attempts == {m["work_item_id"]: store.list_attempts(m["work_item_id"]) for m in members}
    before = store.path.read_bytes()
    with pytest.raises((WorkConflictError, LeaseFencedError)):
        store.fail_received_without_answer(
            first["work_item_id"],
            members[0]["lease"],
            first["attempt_id"],
            reason_code="invalid_question",
        )
    assert store.path.read_bytes() == before


def test_received_pack_without_cost_settlement_cannot_finish_logical_failure(
    pack_store,
):
    store, clock = pack_store
    members = _members(store)
    pack = _prepare(store, members)
    _mark(store, pack)
    store.record_question_pack_outcome(
        pack["pack_id"],
        outcome="response_available",
        execution_receipt=_receipt(),
        http_status_code=200,
    )
    before = store.path.read_bytes()
    row = pack["members"][0]
    with pytest.raises(WorkConflictError, match="budget_unresolved"):
        store.fail_received_without_answer(
            row["work_item_id"],
            members[0]["lease"],
            row["attempt_id"],
            reason_code="missing_question",
        )
    assert store.path.read_bytes() == before
