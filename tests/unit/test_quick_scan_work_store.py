"""Q06 stage-one durable work semantics; every database lives under tmp_path."""

import hashlib
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

from src.utils.quick_scan_result_outbox import canonical_bytes, canonical_sha256
from src.utils.quick_scan_work_store import (
    LeaseFencedError,
    QuickScanWorkStore,
    WorkConflictError,
    quick_scan_receipt_sha256,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _store(tmp_path, clock):
    return QuickScanWorkStore(tmp_path / "quick_scan_work.sqlite", clock=lambda: clock[0])


def _install_v1_fixture(tmp_path):
    path = tmp_path / "quick_scan_work_v1.sqlite"
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "quick_scan_work_store_v1.sql"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(fixture.read_text(encoding="utf-8"))
    return path


def _spec(**overrides):
    values = {
        "entity_id": "ENT_BYD",
        "question_id": "IQS_05",
        "generation": 1,
        "scope": "entity",
        "scope_id": "ENT_BYD",
        "identity_revision": 2,
        "source_binding_version": 3,
        "identity_state": "verified",
        "source_binding_ref": "BND_CN_002594",
        "source_binding_refs": ("BND_CN_002594",),
        "identity_snapshot_sha256": "a" * 64,
        "question_fingerprint": "b" * 64,
        "routing_fingerprint": "c" * 64,
    }
    values.update(overrides)
    return values


def _created(store, *, run_id="RUN_1", scan_id="SCAN_1", **changes):
    return store.create_or_attach(**_spec(**changes), run_id=run_id, scan_id=scan_id)


def _prepared(
    store,
    item_id,
    lease,
    *,
    provider="mimo",
    model="mimo-v2.6-flash",
    route_id="route-1",
    request_cache_key=None,
    prompt_sha256="e" * 64,
    allow_format_repair=False,
):
    if request_cache_key is None:
        request_cache_key = (
            "REQ_" + hashlib.sha256(f"{item_id}:{model}".encode("utf-8")).hexdigest()
        )
    return store.prepare_attempt(
        item_id,
        lease,
        route_id=route_id,
        provider=provider,
        model_requested=model,
        request_cache_key=request_cache_key,
        prompt_sha256=prompt_sha256,
        allow_format_repair=allow_format_repair,
    )


def test_same_logical_item_attaches_two_runs_without_model_in_primary_key(tmp_path):
    clock = [1_800_000_000.0]
    store = _store(tmp_path, clock)
    first = _created(store)
    second = _created(store, run_id="RUN_2", scan_id="SCAN_2")
    duplicate = _created(store, run_id="RUN_2", scan_id="SCAN_2")
    assert first["work_item_id"] == second["work_item_id"] == duplicate["work_item_id"]
    assert len(store.list_run_refs(first["work_item_id"])) == 2
    assert len(store.list_events(first["work_item_id"])) == 3  # create + two distinct refs
    assert _created(store, generation=2)["work_item_id"] != first["work_item_id"]
    security = _created(store, scope="security", scope_id="SEC_BYD_A")
    assert security["work_item_id"] != first["work_item_id"]


def test_v2_identity_change_creates_new_work_without_rebinding_old_attempt(tmp_path):
    clock = [1_800_000_000.0]
    store = _store(tmp_path, clock)
    old = _created(store)
    lease = store.claim(old["work_item_id"], lease_seconds=60)
    attempt = _prepared(store, old["work_item_id"], lease)
    store.mark_send_intent(old["work_item_id"], lease, attempt["attempt_id"])
    revised = _created(store, run_id="RUN_2", identity_revision=3)
    rebound = _created(store, run_id="RUN_3", source_binding_version=4)
    assert len({old["work_item_id"], revised["work_item_id"], rebound["work_item_id"]}) == 3
    assert store.list_attempts(old["work_item_id"])[0]["phase"] == "send_intent"
    assert store.get_item(old["work_item_id"])["identity_revision"] == 2
    assert store.get_item(revised["work_item_id"])["identity_revision"] == 3
    assert store.get_item(rebound["work_item_id"])["source_binding_version"] == 4
    clock[0] += 60
    assert store.recover_expired(old["work_item_id"]) == "uncertain"
    assert store.get_item(revised["work_item_id"])["status"] == "pending"


def test_frozen_identity_and_question_metadata_cannot_be_silently_rebound(tmp_path):
    store = _store(tmp_path, [1_800_000_000.0])
    first = _created(store)
    for change in (
        {"identity_snapshot_sha256": "0" * 64},
        {"question_fingerprint": "0" * 64},
        {"routing_fingerprint": "0" * 64},
    ):
        with pytest.raises(WorkConflictError):
            _created(store, run_id="RUN_2", scan_id="SCAN_2", **change)
    assert len(store.list_run_refs(first["work_item_id"])) == 1
    assert store.get_item(first["work_item_id"])["identity_revision"] == 2


def test_verified_dual_listing_freezes_complete_binding_set_not_just_prompt_anchor(tmp_path):
    store = _store(tmp_path, [1_800_000_000.0])
    refs = ("BND_CN_002594", "BND_HK_01211")
    first = _created(store, source_binding_refs=refs)
    reversed_refs = _created(
        store, run_id="RUN_2", scan_id="SCAN_2", source_binding_refs=tuple(reversed(refs))
    )
    assert reversed_refs["work_item_id"] == first["work_item_id"]
    assert first["source_binding_refs_json"] == '["BND_CN_002594","BND_HK_01211"]'
    with pytest.raises(WorkConflictError):
        _created(
            store,
            run_id="RUN_3",
            scan_id="SCAN_3",
            source_binding_refs=refs,
            identity_snapshot_sha256="0" * 64,
        )
    another_anchor = _created(
        store,
        run_id="RUN_4",
        scan_id="SCAN_4",
        source_binding_ref="BND_HK_01211",
        source_binding_refs=refs,
    )
    assert another_anchor["work_item_id"] == first["work_item_id"]
    assert another_anchor["source_binding_ref"] == "BND_CN_002594"
    changed_set = _created(
        store,
        run_id="RUN_5",
        scan_id="SCAN_5",
        source_binding_refs=("BND_CN_002594",),
    )
    assert changed_set["work_item_id"] != first["work_item_id"]
    with pytest.raises(ValueError, match="unique"):
        _created(store, source_binding_refs=("BND_CN_002594", "BND_CN_002594"))
    with pytest.raises(ValueError, match="anchor"):
        _created(store, source_binding_refs=("BND_HK_01211",))
    assert len(store.list_run_refs(first["work_item_id"])) == 3


def test_identity_state_and_binding_version_are_required_and_frozen(tmp_path):
    store = _store(tmp_path, [1_800_000_000.0])
    first = _created(store)
    for change in (
        {"source_binding_version": 0},
        {"source_binding_version": None},
        {"identity_state": "legacy_unreviewed"},
        {"identity_state": None},
        {"identity_state": []},
        {
            "identity_state": "provisional",
            "source_binding_refs": ("BND_CN_002594", "BND_HK_01211"),
        },
    ):
        with pytest.raises(ValueError):
            _created(store, **change)
    promoted = _created(store, run_id="RUN_2", identity_state="provisional")
    assert promoted["work_item_id"] != first["work_item_id"]
    assert promoted["identity_state"] == "provisional"


def test_schema_is_versioned_full_and_rejects_fake_or_extra_tables(tmp_path):
    fake = tmp_path / "fake.sqlite"
    with closing(sqlite3.connect(fake)) as connection, connection:
        connection.execute("CREATE TABLE work_item(work_item_id TEXT PRIMARY KEY)")
        connection.execute("PRAGMA user_version=1")
    before = fake.read_bytes()
    with pytest.raises(ValueError, match="schema"):
        QuickScanWorkStore(fake)
    assert fake.read_bytes() == before

    clock = [1_800_000_000.0]
    store = _store(tmp_path, clock)
    with closing(store._connect()) as connection, connection:
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("CREATE TABLE unrecognized(data TEXT)")
    with pytest.raises(ValueError, match="schema"):
        _store(tmp_path, clock)


def _claim_process(db_path, work_id, ready_path, start_path, result_path):
    program = (
        "import pathlib,time,sys; "
        "from src.utils.quick_scan_work_store import QuickScanWorkStore; "
        "p=pathlib.Path; db,work,ready,start,out=map(p,sys.argv[1:]); "
        "s=QuickScanWorkStore(db,clock=lambda:1800000000.0); ready.write_text('ready'); "
        "deadline=time.time()+15; "
        "exec('while not start.exists() and time.time()<deadline: time.sleep(0.01)\\n'); "
        "lease=s.claim(str(work),lease_seconds=60); "
        "out.write_text('won' if lease else 'lost')"
    )
    return subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-c",
            program,
            db_path,
            work_id,
            ready_path,
            start_path,
            result_path,
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_two_processes_race_on_one_lease_and_stale_epoch_is_fenced(tmp_path):
    clock = [1_800_000_000.0]
    store = _store(tmp_path, clock)
    item = _created(store)
    work_id = item["work_item_id"]
    start = tmp_path / "start"
    processes = [
        _claim_process(
            str(store.path),
            work_id,
            str(tmp_path / f"ready-{i}"),
            str(start),
            str(tmp_path / f"result-{i}"),
        )
        for i in range(2)
    ]
    try:
        deadline = time.monotonic() + 15
        while not all((tmp_path / f"ready-{i}").exists() for i in range(2)):
            assert time.monotonic() < deadline, "child workers did not reach the barrier"
            time.sleep(0.01)
        start.write_text("go", encoding="ascii")
        for process in processes:
            _, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, stderr
        assert sorted((tmp_path / f"result-{i}").read_text() for i in range(2)) == [
            "lost",
            "won",
        ]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
    first = store.get_item(work_id)
    assert first["status"] == "leased" and first["lease_epoch"] == 1
    clock[0] += 60
    assert store.recover_expired(work_id) == "pending"
    second = store.claim(work_id, lease_seconds=60)
    assert second is not None and second.lease_epoch == 2
    with pytest.raises(LeaseFencedError):
        _prepared(store, work_id, type(second)(first["lease_token"], 1))
    assert any(e["event_type"] == "lease_expired_unsent" for e in store.list_events(work_id))


def test_prepared_crash_recovers_as_unsent_but_send_intent_crash_is_uncertain(tmp_path):
    clock = [1_800_000_000.0]
    store = _store(tmp_path, clock)
    item = _created(store)
    work_id = item["work_item_id"]
    lease = store.claim(work_id, lease_seconds=10)
    attempt = _prepared(store, work_id, lease)
    assert attempt["phase"] == "prepared"
    clock[0] += 10
    assert _store(tmp_path, clock).recover_expired(work_id) == "pending"
    next_lease = _store(tmp_path, clock).claim(work_id, lease_seconds=10)
    assert next_lease.lease_epoch == 2
    assert store.list_attempts(work_id)[0]["phase"] == "abandoned_unsent"
    next_attempt = _prepared(store, work_id, next_lease)
    assert next_attempt["attempt_id"] != attempt["attempt_id"]
    assert (
        store.mark_send_intent(work_id, next_lease, next_attempt["attempt_id"]).attempt_id
        == next_attempt["attempt_id"]
    )

    other = _created(store, question_id="IQS_06")
    other_id = other["work_item_id"]
    second_lease = store.claim(other_id, lease_seconds=10)
    second_attempt = _prepared(store, other_id, second_lease)
    permit = store.mark_send_intent(other_id, second_lease, second_attempt["attempt_id"])
    assert permit.attempt_id == second_attempt["attempt_id"]
    assert permit.committed_at == clock[0]
    clock[0] += 10
    restarted = _store(tmp_path, clock)
    assert restarted.recover_expired(other_id) == "uncertain"
    assert restarted.get_item(other_id)["uncertain_attempt_id"] == permit.attempt_id
    assert restarted.claim(other_id, lease_seconds=10) is None


def test_send_intent_survives_immediate_process_exit_before_any_http(tmp_path):
    clock = [1_800_000_000.0]
    store = _store(tmp_path, clock)
    work_id = _created(store)["work_item_id"]
    lease = store.claim(work_id, lease_seconds=10)
    attempt = _prepared(store, work_id, lease)
    program = (
        "import os,sys; from pathlib import Path; "
        "from src.utils.quick_scan_work_store import QuickScanWorkStore,Lease; "
        "s=QuickScanWorkStore(Path(sys.argv[1]),clock=lambda:1800000000.0); "
        "s.mark_send_intent(sys.argv[2],Lease(sys.argv[3],int(sys.argv[4])),sys.argv[5]); "
        "os._exit(0)"
    )
    child = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            program,
            str(store.path),
            work_id,
            lease.lease_token,
            str(lease.lease_epoch),
            attempt["attempt_id"],
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    assert _store(tmp_path, clock).list_attempts(work_id)[0]["phase"] == "send_intent"
    clock[0] += 10
    assert _store(tmp_path, clock).recover_expired(work_id) == "uncertain"


def test_send_intent_commit_failure_never_returns_post_permit(tmp_path):
    store = _store(tmp_path, [1_800_000_000.0])
    item = _created(store)
    lease = store.claim(item["work_item_id"], lease_seconds=60)
    attempt = _prepared(store, item["work_item_id"], lease)
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER reject_send BEFORE UPDATE OF phase ON attempt "
            "WHEN NEW.phase='send_intent' BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(sqlite3.DatabaseError, match="injected"):
        store.mark_send_intent(item["work_item_id"], lease, attempt["attempt_id"])
    assert store.list_attempts(item["work_item_id"])[0]["phase"] == "prepared"
    assert not any(
        e["event_type"] == "send_intent" for e in store.list_events(item["work_item_id"])
    )


def test_fallback_attempts_share_work_and_response_needs_future_checkpoint(tmp_path):
    store = _store(tmp_path, [1_800_000_000.0])
    work_id = _created(store)["work_item_id"]
    lease = store.claim(work_id, lease_seconds=60)
    first = _prepared(store, work_id, lease)
    store.mark_send_intent(work_id, lease, first["attempt_id"])
    store.record_attempt_outcome(
        work_id,
        lease,
        first["attempt_id"],
        outcome="confirmed_failure",
        http_status_code=429,
        receipt_sha256="f" * 64,
        failure_category="rate_limited",
        provider_error_code="rate_limit_exceeded",
    )
    second = _prepared(store, work_id, lease, provider="minimax", model="MiniMax-M3")
    assert second["attempt_id"] != first["attempt_id"]
    store.mark_send_intent(work_id, lease, second["attempt_id"])
    store.record_attempt_outcome(
        work_id,
        lease,
        second["attempt_id"],
        outcome="response_available",
        http_status_code=200,
        receipt_sha256="1" * 64,
    )
    assert (
        store.get_item(work_id)["status"] == "leased"
    )  # HTTP success alone is not an answer checkpoint.
    assert store.get_answer_checkpoint(work_id) is None
    assert store.claim(work_id, lease_seconds=60) is None
    assert len(store.list_attempts(work_id)) == 2
    with pytest.raises(WorkConflictError):
        _prepared(store, work_id, lease, provider="openai", model="gpt-4.1-mini")
    assert len(store.list_run_refs(work_id)) == 1


def _successful_search_attempt(
    store,
    work_id,
    *,
    provider="mimo",
    model="mimo-v2.6-flash",
    source_urls=None,
):
    lease = store.claim(work_id, lease_seconds=60)
    attempt = _prepared(store, work_id, lease, provider=provider, model=model)
    store.mark_send_intent(work_id, lease, attempt["attempt_id"])
    receipt = {
        "provider": provider,
        "request_id": "req_fixture_01",
        "response_id": "resp_fixture_01",
        "actual_model": model,
        "search_status": "executed",
        "response_status": "completed",
        "http_status_code": 200,
        "attempt_id": "provider_transport_attempt_01",
        "prompt_sha256": hashlib.sha256(b"provider prompt only").hexdigest(),
        "search_receipt_id": "search_call_01",
        "completed_at": "2026-09-27T12:00:00Z",
        "source_urls": (
            source_urls if source_urls is not None else ["https://example.com/company-source"]
        ),
    }
    store.record_attempt_outcome(
        work_id,
        lease,
        attempt["attempt_id"],
        outcome="response_available",
        http_status_code=200,
        receipt_sha256=quick_scan_receipt_sha256(receipt),
        request_id=receipt["request_id"],
    )
    return lease, attempt, receipt


def _answer_for(item, *, score=8, status="scored", description="具备稳定竞争优势。"):
    return {
        "entity_id": item["entity_id"],
        "question_id": item["question_id"],
        "status": status,
        "score": score,
        "description": description,
    }


def test_checkpoint_is_atomic_immutable_and_idempotent_after_restart(tmp_path):
    clock = [1_800_000_000.0]
    store = _store(tmp_path, clock)
    item = _created(store)
    work_id = item["work_item_id"]
    lease, attempt, receipt = _successful_search_attempt(store, work_id)

    checkpoint = store.save_answer_checkpoint(
        work_id,
        lease,
        attempt["attempt_id"],
        answer=_answer_for(item),
        execution_receipt=receipt,
    )
    assert checkpoint["payload"]["answer"]["score"] == 8
    assert checkpoint["payload"]["work"]["question_fingerprint"] == item["question_fingerprint"]
    assert checkpoint["payload"]["provenance"]["actual_provider"] == "mimo"
    assert checkpoint["payload"]["provenance"]["actual_model"] == "mimo-v2.6-flash"
    assert (
        checkpoint["payload"]["provenance"]["provider_attempt_id"]
        == "provider_transport_attempt_01"
    )
    assert checkpoint["attempt_id"] == attempt["attempt_id"]
    assert checkpoint["payload"]["provenance"]["work_prompt_sha256"] == attempt["prompt_sha256"]
    assert checkpoint["payload"]["provenance"]["provider_prompt_sha256"] != attempt["prompt_sha256"]
    assert checkpoint["payload"]["provenance"]["source_urls"] == [
        "https://example.com/company-source"
    ]
    assert store.get_item(work_id)["status"] == "result_ready"

    restarted = _store(tmp_path, clock)
    repeated = restarted.save_answer_checkpoint(
        work_id,
        lease,
        attempt["attempt_id"],
        answer=_answer_for(item),
        execution_receipt=receipt,
    )
    assert repeated["checkpoint_id"] == checkpoint["checkpoint_id"]
    with pytest.raises(WorkConflictError, match="immutable"):
        restarted.save_answer_checkpoint(
            work_id,
            lease,
            attempt["attempt_id"],
            answer=_answer_for(item, score=9),
            execution_receipt=receipt,
        )
    assert [event["event_type"] for event in restarted.list_events(work_id)].count(
        "answer_checkpointed"
    ) == 1

    with closing(sqlite3.connect(restarted.path)) as connection, connection:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute(
                "UPDATE answer_checkpoint SET payload_json='{}' WHERE work_item_id=?",
                (work_id,),
            )
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute("DELETE FROM answer_checkpoint WHERE work_item_id=?", (work_id,))
    assert (
        restarted.get_answer_checkpoint(work_id)["payload_sha256"] == checkpoint["payload_sha256"]
    )


@pytest.mark.parametrize(
    "answer_changes,receipt_changes,error_type",
    [
        ({"entity_id": "ENT_OTHER"}, {}, WorkConflictError),
        ({"question_id": "IQS_99"}, {}, WorkConflictError),
        ({"score": True}, {}, ValueError),
        ({"status": "insufficient_evidence", "score": 4}, {}, ValueError),
        ({"status": "error"}, {}, ValueError),
        ({}, {"search_status": "unverified"}, ValueError),
        ({}, {"source_urls": ["https://user:secret@example.com/private"]}, ValueError),
        ({}, {"completed_at": "2026-09-27T12:00:00"}, ValueError),
    ],
)
def test_checkpoint_rejects_unbound_or_unverified_answers(
    tmp_path, answer_changes, receipt_changes, error_type
):
    store = _store(tmp_path, [1_800_000_000.0])
    item = _created(store)
    work_id = item["work_item_id"]
    lease, attempt, receipt = _successful_search_attempt(store, work_id)
    answer = _answer_for(item)
    answer.update(answer_changes)
    receipt.update(receipt_changes)
    with pytest.raises(error_type):
        store.save_answer_checkpoint(
            work_id,
            lease,
            attempt["attempt_id"],
            answer=answer,
            execution_receipt=receipt,
        )
    assert store.get_item(work_id)["status"] == "leased"
    assert store.get_answer_checkpoint(work_id) is None


def test_checkpoint_receipt_hash_binds_source_and_completion_metadata_but_not_raw_body():
    base = {
        "provider": "mimo",
        "response_id": "resp_fixture_01",
        "actual_model": "mimo-v2.6-flash",
        "search_status": "executed",
        "response_status": "completed",
        "http_status_code": 200,
        "attempt_id": "ATTEMPT_fixture",
        "prompt_sha256": "e" * 64,
        "search_receipt_id": "search_call_01",
        "completed_at": "2026-09-27T12:00:00Z",
        "source_urls": ["https://example.com/a"],
        "usage": {
            "schema": "quick-scan-usage-v1",
            "input_tokens": 100,
            "cached_input_tokens": 10,
            "cache_creation_input_tokens": 0,
            "output_tokens": 25,
            "reasoning_output_tokens": 5,
            "search_tool_calls": 1,
        },
        "raw_response": "must never be persisted",
    }
    assert quick_scan_receipt_sha256(base) == quick_scan_receipt_sha256(
        {**base, "raw_response": "different private body"}
    )
    assert quick_scan_receipt_sha256(base) != quick_scan_receipt_sha256(
        {**base, "source_urls": ["https://example.com/b"]}
    )
    assert quick_scan_receipt_sha256(base) != quick_scan_receipt_sha256(
        {**base, "completed_at": "2026-09-27T12:01:00Z"}
    )
    assert quick_scan_receipt_sha256(base) != quick_scan_receipt_sha256(
        {**base, "usage": {**base["usage"], "output_tokens": 26}}
    )
    without_usage = {key: value for key, value in base.items() if key != "usage"}
    assert quick_scan_receipt_sha256(
        {**base, "usage": {**base["usage"], "provider_raw_payload": "must be dropped"}}
    ) == quick_scan_receipt_sha256(without_usage)
    assert quick_scan_receipt_sha256(
        {**base, "usage": {**base["usage"], "search_tool_calls": True}}
    ) == quick_scan_receipt_sha256(without_usage)


def test_checkpoint_payload_hash_corruption_fails_closed_on_reopen(tmp_path):
    store = _store(tmp_path, [1_800_000_000.0])
    item = _created(store)
    work_id = item["work_item_id"]
    lease, attempt, receipt = _successful_search_attempt(store, work_id)
    store.save_answer_checkpoint(
        work_id,
        lease,
        attempt["attempt_id"],
        answer=_answer_for(item),
        execution_receipt=receipt,
    )
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("DROP TRIGGER answer_checkpoint_no_update")
        connection.execute(
            "UPDATE answer_checkpoint SET payload_json='{}' WHERE work_item_id=?",
            (work_id,),
        )
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER answer_checkpoint_no_update BEFORE UPDATE ON answer_checkpoint "
            "BEGIN SELECT RAISE(ABORT,'answer checkpoints are immutable'); END"
        )
    with pytest.raises(ValueError, match="hash mismatch"):
        _store(tmp_path, [1_800_000_000.0])


def test_checkpoint_requires_verified_identity_and_current_lease(tmp_path):
    clock = [1_800_000_000.0]
    store = _store(tmp_path, clock)
    provisional = _created(
        store, identity_state="provisional", source_binding_refs=("BND_CN_002594",)
    )
    lease, attempt, receipt = _successful_search_attempt(store, provisional["work_item_id"])
    with pytest.raises(WorkConflictError, match="verified identity"):
        store.save_answer_checkpoint(
            provisional["work_item_id"],
            lease,
            attempt["attempt_id"],
            answer=_answer_for(provisional),
            execution_receipt=receipt,
        )

    verified = _created(store, question_id="IQS_06")
    expired_lease, expired_attempt, expired_receipt = _successful_search_attempt(
        store, verified["work_item_id"]
    )
    clock[0] += 61
    with pytest.raises(LeaseFencedError):
        store.save_answer_checkpoint(
            verified["work_item_id"],
            expired_lease,
            expired_attempt["attempt_id"],
            answer=_answer_for(verified),
            execution_receipt=expired_receipt,
        )


def test_checkpoint_database_failure_rolls_back_answer_status_and_event(tmp_path):
    store = _store(tmp_path, [1_800_000_000.0])
    item = _created(store)
    work_id = item["work_item_id"]
    lease, attempt, receipt = _successful_search_attempt(store, work_id)
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER reject_result_ready BEFORE UPDATE OF status ON work_item "
            "WHEN NEW.status='result_ready' BEGIN SELECT RAISE(ABORT,'injected checkpoint failure'); END"
        )
    with pytest.raises(sqlite3.DatabaseError, match="injected checkpoint failure"):
        store.save_answer_checkpoint(
            work_id,
            lease,
            attempt["attempt_id"],
            answer=_answer_for(item),
            execution_receipt=receipt,
        )
    assert store.get_item(work_id)["status"] == "leased"
    assert store.get_answer_checkpoint(work_id) is None
    assert not any(
        event["event_type"] == "answer_checkpointed" for event in store.list_events(work_id)
    )


def test_restart_resumes_only_two_missing_questions_after_four_checkpoints(tmp_path):
    clock = [1_800_000_000.0]
    store = _store(tmp_path, clock)
    items = [
        _created(
            store,
            question_id=f"IQS_{index:02d}",
            run_id="RUN_PARTIAL",
            scan_id="SCAN_PARTIAL",
        )
        for index in range(1, 7)
    ]
    for item in items[:4]:
        work_id = item["work_item_id"]
        lease, attempt, receipt = _successful_search_attempt(store, work_id)
        store.save_answer_checkpoint(
            work_id,
            lease,
            attempt["attempt_id"],
            answer=_answer_for(item),
            execution_receipt=receipt,
        )

    restarted = _store(tmp_path, clock)
    resumed = restarted.list_run_items("RUN_PARTIAL", "SCAN_PARTIAL")
    assert len(resumed) == 6
    assert sum(row["checkpoint"] is not None for row in resumed) == 4
    assert [row["status"] for row in resumed] == [
        "result_ready",
        "result_ready",
        "result_ready",
        "result_ready",
        "pending",
        "pending",
    ]
    for row in resumed[:4]:
        assert restarted.claim(row["work_item_id"], lease_seconds=60) is None
    claimed = [restarted.claim(row["work_item_id"], lease_seconds=60) for row in resumed[4:]]
    assert all(claimed)


def test_v1_migration_preserves_work_rows_and_adds_empty_checkpoint_table(tmp_path):
    clock = [1_800_000_000.0]
    path = _install_v1_fixture(tmp_path)
    table_names = ("work_item", "work_run_ref", "attempt", "work_event")
    with closing(sqlite3.connect(path)) as connection, connection:
        before = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in table_names
        }

    migrated = QuickScanWorkStore(path, clock=lambda: clock[0])
    with closing(migrated._connect()) as connection, connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert connection.execute("SELECT COUNT(*) FROM answer_checkpoint").fetchone()[0] == 0
        after = {
            table: [
                tuple(row)
                for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            ]
            for table in table_names
        }
    assert after == before
    assert migrated.get_item("WK_V1_FIXTURE")["status"] == "pending"
    assert migrated.get_answer_checkpoint("WK_V1_FIXTURE") is None


def test_v1_migration_rejects_completed_work_without_checkpoint(tmp_path):
    path = _install_v1_fixture(tmp_path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE work_item SET status='result_ready' WHERE work_item_id='WK_V1_FIXTURE'"
        )
    with pytest.raises(ValueError, match="without answer checkpoints"):
        QuickScanWorkStore(path, clock=lambda: 1_800_000_000.0)
    with closing(sqlite3.connect(path)) as connection, connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='answer_checkpoint'"
            ).fetchone()[0]
            == 0
        )


def test_fixed_v2_migration_preserves_existing_rows_and_adds_budget_ledger(tmp_path):
    path = tmp_path / "quick-scan-v2.sqlite"
    fixture = Path(__file__).parents[1] / "fixtures" / "quick_scan_work_store_v2.sql"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(fixture.read_text(encoding="utf-8"))
    before = {}
    with closing(sqlite3.connect(path)) as connection:
        for table in ("work_item", "work_run_ref", "attempt", "work_event", "answer_checkpoint"):
            before[table] = connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()

    migrated = QuickScanWorkStore(path, clock=lambda: 1_800_000_000.0)

    with closing(migrated._connect()) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert (
            connection.execute("SELECT COUNT(*) FROM quick_scan_budget_policy").fetchone()[0] == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM quick_scan_budget_attempt").fetchone()[0] == 0
        )
    with closing(sqlite3.connect(path)) as connection:
        after = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in before
        }
    assert after == before
    assert migrated.get_item("WK_V1_FIXTURE")["status"] == "pending"


def test_checkpoint_redacts_secret_query_parameters_from_source_urls(tmp_path):
    store = _store(tmp_path, [1_800_000_000.0])
    item = _created(store)
    raw_url = (
        "https://example.com/company?region=us&access_token=private-token&"
        "key=private-key&X-Amz-Signature=private-signature&page=2#summary"
    )
    lease, attempt, receipt = _successful_search_attempt(
        store, item["work_item_id"], source_urls=[raw_url]
    )

    checkpoint = store.save_answer_checkpoint(
        item["work_item_id"],
        lease,
        attempt["attempt_id"],
        answer=_answer_for(item),
        execution_receipt=receipt,
    )

    sanitized_url = checkpoint["payload"]["provenance"]["source_urls"][0]
    assert sanitized_url == "https://example.com/company?region=us&page=2"
    assert all(
        secret not in sanitized_url
        for secret in ("private-token", "private-key", "private-signature")
    )
    saved_attempt = store.list_attempts(item["work_item_id"])[0]
    assert saved_attempt["receipt_sha256"] == quick_scan_receipt_sha256(receipt)


def test_response_available_without_checkpoint_is_uncertain_after_crash(tmp_path):
    clock = [1_800_000_000.0]
    store = _store(tmp_path, clock)
    work_id = _created(store)["work_item_id"]
    lease, attempt, _receipt = _successful_search_attempt(store, work_id)
    assert store.get_answer_checkpoint(work_id) is None

    restarted = _store(tmp_path, clock)
    assert restarted.get_item(work_id)["status"] == "leased"
    clock[0] += 61
    assert restarted.recover_expired(work_id) == "uncertain"
    assert restarted.claim(work_id, lease_seconds=60) is None
    assert restarted.list_attempts(work_id)[0]["attempt_id"] == attempt["attempt_id"]
    assert restarted.get_answer_checkpoint(work_id) is None


def test_pending_cancel_is_idempotent_and_never_cancels_inflight_work(tmp_path):
    store = _store(tmp_path, [1_800_000_000.0])
    pending = _created(store)
    assert store.cancel_pending(pending["work_item_id"]) == "cancelled"
    assert store.cancel_pending(pending["work_item_id"]) == "cancelled"
    assert store.get_item(pending["work_item_id"])["status"] == "cancelled"
    assert [event["event_type"] for event in store.list_events(pending["work_item_id"])].count(
        "cancelled"
    ) == 1

    active = _created(store, question_id="IQS_06")
    lease = store.claim(active["work_item_id"], lease_seconds=60)
    with pytest.raises(WorkConflictError, match="only pending"):
        store.cancel_pending(active["work_item_id"])
    assert store.get_item(active["work_item_id"])["status"] == "leased"
    assert lease is not None


def test_same_request_key_cannot_change_model_or_prompt(tmp_path):
    store = _store(tmp_path, [1_800_000_000.0])
    work_id = _created(store)["work_item_id"]
    lease = store.claim(work_id, lease_seconds=60)
    first = _prepared(store, work_id, lease)
    store.mark_send_intent(work_id, lease, first["attempt_id"])
    store.record_attempt_outcome(
        work_id,
        lease,
        first["attempt_id"],
        outcome="confirmed_failure",
        http_status_code=429,
        receipt_sha256="f" * 64,
        failure_category="quota_exhausted",
        provider_error_code="insufficient_quota",
    )
    with pytest.raises(WorkConflictError, match="request key"):
        store.prepare_attempt(
            work_id,
            lease,
            route_id="route-1",
            provider="minimax",
            model_requested="MiniMax-M3",
            request_cache_key=first["request_cache_key"],
            prompt_sha256="e" * 64,
        )
    with pytest.raises(WorkConflictError, match="request key"):
        store.prepare_attempt(
            work_id,
            lease,
            route_id="route-1",
            provider="mimo",
            model_requested="mimo-v2.6-flash",
            request_cache_key=first["request_cache_key"],
            prompt_sha256="0" * 64,
        )
    assert len(store.list_attempts(work_id)) == 1


def test_budgeted_format_repair_allows_only_same_route_with_a_changed_prompt(tmp_path):
    store = _store(tmp_path, [1_800_000_000.0])
    work_id = _created(store)["work_item_id"]
    lease = store.claim(work_id, lease_seconds=60)
    initial = _prepared(store, work_id, lease)
    store.mark_send_intent(work_id, lease, initial["attempt_id"])
    store.record_attempt_outcome(
        work_id,
        lease,
        initial["attempt_id"],
        outcome="response_available",
        http_status_code=200,
        receipt_sha256="a" * 64,
    )

    repair = _prepared(
        store,
        work_id,
        lease,
        request_cache_key="REQ_" + hashlib.sha256(b"format-repair").hexdigest(),
        prompt_sha256="f" * 64,
        allow_format_repair=True,
    )
    assert repair["ordinal"] == 2
    assert repair["route_id"] == initial["route_id"]
    assert repair["provider"] == initial["provider"]
    assert repair["model_requested"] == initial["model_requested"]
    store.mark_send_intent(work_id, lease, repair["attempt_id"])
    store.record_attempt_outcome(
        work_id,
        lease,
        repair["attempt_id"],
        outcome="response_available",
        http_status_code=200,
        receipt_sha256="b" * 64,
    )
    with pytest.raises(WorkConflictError, match="previous attempt is unresolved"):
        _prepared(
            store,
            work_id,
            lease,
            request_cache_key="REQ_" + hashlib.sha256(b"second-format-repair").hexdigest(),
            prompt_sha256="d" * 64,
            allow_format_repair=True,
        )


@pytest.mark.parametrize(
    "changes,allow_format_repair",
    [
        ({}, False),
        ({"prompt_sha256": "e" * 64}, True),
        ({"route_id": "route-2"}, True),
        ({"provider": "backup"}, True),
        ({"model": "mimo-v2.6"}, True),
    ],
)
def test_format_repair_cannot_repeat_prompt_or_change_route(tmp_path, changes, allow_format_repair):
    store = _store(tmp_path, [1_800_000_000.0])
    work_id = _created(store)["work_item_id"]
    lease = store.claim(work_id, lease_seconds=60)
    initial = _prepared(store, work_id, lease)
    store.mark_send_intent(work_id, lease, initial["attempt_id"])
    store.record_attempt_outcome(
        work_id,
        lease,
        initial["attempt_id"],
        outcome="response_available",
        http_status_code=200,
        receipt_sha256="a" * 64,
    )
    proposed = {
        "request_cache_key": "REQ_" + hashlib.sha256(b"invalid-repair").hexdigest(),
        "prompt_sha256": "f" * 64,
        "allow_format_repair": allow_format_repair,
    }
    proposed.update(changes)
    with pytest.raises(WorkConflictError, match="previous attempt is unresolved"):
        _prepared(store, work_id, lease, **proposed)


def test_request_key_cannot_cross_identity_revision_even_same_generation(tmp_path):
    store = _store(tmp_path, [1_800_000_000.0])
    old = _created(store)
    new = _created(store, run_id="RUN_2", identity_revision=3)
    old_lease = store.claim(old["work_item_id"], lease_seconds=60)
    old_attempt = _prepared(store, old["work_item_id"], old_lease)
    new_lease = store.claim(new["work_item_id"], lease_seconds=60)
    with pytest.raises(WorkConflictError, match="request key"):
        store.prepare_attempt(
            new["work_item_id"],
            new_lease,
            route_id="route-1",
            provider="mimo",
            model_requested="mimo-v2.6-flash",
            request_cache_key=old_attempt["request_cache_key"],
            prompt_sha256="e" * 64,
        )
    assert store.list_attempts(new["work_item_id"]) == []


@pytest.mark.parametrize("status_code", [408, 500, 503])
def test_http_error_may_have_executed_and_keeps_status_for_reconciliation(tmp_path, status_code):
    store = _store(tmp_path, [1_800_000_000.0])
    work_id = _created(store)["work_item_id"]
    lease = store.claim(work_id, lease_seconds=60)
    attempt = _prepared(store, work_id, lease)
    store.mark_send_intent(work_id, lease, attempt["attempt_id"])
    with pytest.raises(ValueError, match="confirmed refusal"):
        store.record_attempt_outcome(
            work_id,
            lease,
            attempt["attempt_id"],
            outcome="confirmed_failure",
            http_status_code=status_code,
            receipt_sha256="f" * 64,
            failure_category="provider_server_error",
        )
    assert store.list_attempts(work_id)[0]["phase"] == "send_intent"
    store.record_attempt_outcome(
        work_id,
        lease,
        attempt["attempt_id"],
        outcome="unknown",
        http_status_code=status_code,
        receipt_sha256="f" * 64,
        failure_category="provider_server_error",
        provider_error_code="internal_error",
        request_id="provider_request_123",
    )
    persisted = store.list_attempts(work_id)[0]
    assert persisted["phase"] == "uncertain"
    assert persisted["http_status_code"] == status_code
    assert persisted["receipt_sha256"] == "f" * 64
    assert persisted["provider_error_code"] == "internal_error"
    assert persisted["request_id"] == "provider_request_123"
    assert store.get_item(work_id)["status"] == "uncertain"
    assert store.claim(work_id, lease_seconds=60) is None


def test_confirmed_429_requires_specific_refusal_evidence(tmp_path):
    store = _store(tmp_path, [1_800_000_000.0])
    work_id = _created(store)["work_item_id"]
    lease = store.claim(work_id, lease_seconds=60)
    attempt = _prepared(store, work_id, lease)
    store.mark_send_intent(work_id, lease, attempt["attempt_id"])
    with pytest.raises(ValueError, match="confirmed refusal"):
        store.record_attempt_outcome(
            work_id,
            lease,
            attempt["attempt_id"],
            outcome="confirmed_failure",
            http_status_code=429,
            receipt_sha256="f" * 64,
            failure_category="quota_exhausted",
            provider_error_code="unrelated_error",
        )
    store.record_attempt_outcome(
        work_id,
        lease,
        attempt["attempt_id"],
        outcome="confirmed_failure",
        http_status_code=429,
        receipt_sha256="f" * 64,
        failure_category="quota_exhausted",
        provider_error_code="insufficient_quota",
    )
    assert store.list_attempts(work_id)[0]["phase"] == "confirmed_failure"
    assert store.get_item(work_id)["status"] == "leased"


def test_confirmed_auth_and_model_refusals_allow_ordered_fallback(tmp_path):
    for status_code, category in (
        (401, "authentication_rejected"),
        (403, "authentication_rejected"),
        (404, "model_or_endpoint_unavailable"),
    ):
        store = _store(tmp_path / str(status_code), [1_800_000_000.0])
        work_id = _created(store)["work_item_id"]
        lease = store.claim(work_id, lease_seconds=60)
        attempt = _prepared(store, work_id, lease)
        store.mark_send_intent(work_id, lease, attempt["attempt_id"])
        store.record_attempt_outcome(
            work_id,
            lease,
            attempt["attempt_id"],
            outcome="confirmed_failure",
            http_status_code=status_code,
            receipt_sha256="a" * 64,
            failure_category=category,
        )
        assert store.list_attempts(work_id)[0]["phase"] == "confirmed_failure"
        next_attempt = store.prepare_attempt(
            work_id,
            lease,
            route_id="route_fallback",
            provider="fallback",
            model_requested="fallback-model",
            request_cache_key="REQ_" + str(status_code).zfill(64),
            prompt_sha256="b" * 64,
        )
        assert next_attempt["ordinal"] == 2


def test_unknown_attempt_never_releases_for_fallback_or_new_claim(tmp_path):
    store = _store(tmp_path, [1_800_000_000.0])
    work_id = _created(store)["work_item_id"]
    lease = store.claim(work_id, lease_seconds=60)
    attempt = _prepared(store, work_id, lease)
    store.mark_send_intent(work_id, lease, attempt["attempt_id"])
    store.record_attempt_outcome(work_id, lease, attempt["attempt_id"], outcome="unknown")
    assert store.get_item(work_id)["status"] == "uncertain"
    assert store.claim(work_id, lease_seconds=60) is None
    with pytest.raises(LeaseFencedError):
        _prepared(store, work_id, lease, provider="minimax", model="MiniMax-M3")


def test_late_old_lease_receipt_is_retained_without_state_overwrite(tmp_path):
    clock = [1_800_000_000.0]
    store = _store(tmp_path, clock)
    work_id = _created(store)["work_item_id"]
    lease = store.claim(work_id, lease_seconds=10)
    attempt = _prepared(store, work_id, lease)
    store.mark_send_intent(work_id, lease, attempt["attempt_id"])
    clock[0] += 10
    assert store.recover_expired(work_id) == "uncertain"
    with pytest.raises(LeaseFencedError):
        store.record_attempt_outcome(
            work_id,
            lease,
            attempt["attempt_id"],
            outcome="response_available",
            http_status_code=200,
            receipt_sha256="1" * 64,
        )
    store.note_late_receipt(work_id, lease, attempt["attempt_id"], receipt_sha256="1" * 64)
    store.note_late_receipt(work_id, lease, attempt["attempt_id"], receipt_sha256="1" * 64)
    with pytest.raises(WorkConflictError, match="late receipt hash"):
        store.note_late_receipt(work_id, lease, attempt["attempt_id"], receipt_sha256="2" * 64)
    assert store.get_item(work_id)["status"] == "uncertain"
    assert store.list_attempts(work_id)[0]["late_receipt_sha256"] == "1" * 64
    assert any(e["event_type"] == "late_receipt" for e in store.list_events(work_id))


def test_no_prompt_credentials_urls_or_company_name_are_stored(tmp_path):
    store = _store(tmp_path, [1_800_000_000.0])
    item = _created(store)
    lease = store.claim(item["work_item_id"], lease_seconds=60)
    attempt = _prepared(store, item["work_item_id"], lease)
    store.mark_send_intent(item["work_item_id"], lease, attempt["attempt_id"])
    raw = store.path.read_bytes()
    for forbidden in (b"Bearer", b"api_key", b"https://", b"BYD Company", b"question body"):
        assert forbidden not in raw
    with pytest.raises(ValueError):
        store.prepare_attempt(
            item["work_item_id"],
            lease,
            route_id="https://provider/api_key=secret",
            provider="mimo",
            model_requested="mimo-v2.6-flash",
            request_cache_key="REQ_" + "d" * 64,
            prompt_sha256="e" * 64,
        )


def _c06_package_for_checkpoint(checkpoint, *, observation_changes=None):
    payload = checkpoint["payload"]
    work = payload["work"]
    answer = payload["answer"]
    provenance = payload["provenance"]
    question_id = work["question_id"]
    scope = work["scope"]
    observation = {
        "schema_version": "1.0.0",
        "entity_id": answer["entity_id"],
        "security_id": work["scope_id"] if scope == "security" else None,
        "segment_id": work["scope_id"] if scope == "segment" else None,
        "field_id": "score." + question_id.lower(),
        "construct_id": question_id,
        "question_id": question_id,
        "question_version": "1.0.0",
        "template_version": "3.0.0",
        "method_id": "core-constructs-v1/context-1/fixture",
        "scope": scope,
        "cohort": {
            "company_type": "operating",
            "industries": ["industrial"],
            "stage": "mature",
            "subtype": None,
        },
        "information_cutoff": "2026-09-27",
        "run_id": "RUN_C06_FIXTURE",
        "scan_id": "SCAN_C06_FIXTURE",
        "inputset_id": "INPUT_C06_FIXTURE_V1",
        "task_mode": "comparison",
        "comparison_group_id": "GROUP_C06_FIXTURE",
        "observed_at": provenance["response_completed_at"],
        "execution": {
            "provider": provenance["actual_provider"],
            "model_requested": provenance["model_requested"],
            "model_resolved": provenance["actual_model"],
            "model_revision": "fixture-model-revision",
            "request_id": provenance["request_id"],
            "attempt_id": provenance["provider_attempt_id"],
            "started_at": "2026-09-27T11:59:00Z",
            "answered_at": provenance["response_completed_at"],
            "search_status": provenance["search_status"],
            "search_receipt_id": provenance["search_receipt_id"],
            "prompt_sha256": provenance["provider_prompt_sha256"],
        },
        "answer": {
            "question_id": question_id,
            "response_kind": "score",
            "status": {
                "scored": "scored",
                "insufficient_evidence": "insufficient_evidence",
                "not_applicable": "not_applicable",
            }.get(answer["status"], "unknown"),
            "score": answer["score"],
            "summary": answer["description"],
            "information_as_of": "2026-09-27",
            "period_start": None,
            "period_end": None,
            "basis": "current",
            "trend": "uncertain",
            "confidence": "medium",
            "metrics": [],
            "items": [],
            "evidence": [
                {
                    "id": "e1",
                    "title": "Offline test fixture",
                    "url": provenance["source_urls"][0],
                    "published_at": "2026-09-27",
                    "claim": "Synthetic evidence used only by an offline test.",
                }
            ],
            "counterevidence": "No independent counterevidence in this synthetic fixture.",
            "watch_triggers": [],
            "missing_fields": [],
            "coverage": {"status": "partial", "reason": "Synthetic offline fixture."},
        },
        "evidence_review_status": "unreviewed",
    }
    if observation_changes:
        observation.update(observation_changes)
    observation_id = (
        "obs_"
        + hashlib.sha256(
            canonical_bytes(
                {
                    "entity_id": observation["entity_id"],
                    "question_id": observation["question_id"],
                    "fixture": True,
                }
            )
        ).hexdigest()
    )
    observation["observation_id"] = observation_id
    payload_sha256 = canonical_sha256(observation)
    item_id = "itm_" + canonical_sha256(
        {"observation_id": observation_id, "payload_sha256": payload_sha256}
    )
    package = {
        "schema_version": "1.0.0",
        "producer": {
            "component": "StockQAbyLLM",
            "component_version": "fixture-1.0",
            "build_id": "OFFLINE_FIXTURE_1",
        },
        "consumer": {
            "component": "StockWiki",
            "namespace": "quick_scan",
            "minimum_schema_version": "1.0.0",
        },
        "created_at": "2026-09-27T12:01:00Z",
        "data_class": "lightweight_screening",
        "required_capabilities": [
            "entity_security_identity_v1",
            "standard_observation_v1",
            "score_v1",
        ],
        "contract_versions": {
            "identity_schema": "1.0.0",
            "answer_schema": "standard-1",
            "observation_schema": "1.0.0",
            "question_catalog": "3.0.0",
            "model_policy_schema": "2.0.0",
        },
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
    package["package_sha256"] = canonical_sha256(package)
    package["package_id"] = "pkg_" + package["package_sha256"]
    return package


def _readdress_c06_package(package):
    item = package["items"][0]
    observation = item["observation"]
    payload_sha256 = canonical_sha256(observation)
    item["payload_sha256"] = payload_sha256
    item["item_id"] = "itm_" + canonical_sha256(
        {"observation_id": item["observation_id"], "payload_sha256": payload_sha256}
    )
    package_body = {
        key: value for key, value in package.items() if key not in {"package_id", "package_sha256"}
    }
    package["package_sha256"] = canonical_sha256(package_body)
    package["package_id"] = "pkg_" + package["package_sha256"]


def _ack_for_package(package, *, status="accepted", error_code=None, **changes):
    item = package["items"][0]
    ack = {
        "schema_version": "1.0.0",
        "ack_id": "ack_12345678",
        "package_id": package["package_id"],
        "item_id": item["item_id"],
        "observation_id": item["observation_id"],
        "payload_sha256": item["payload_sha256"],
        "status": status,
        "error_code": error_code,
        "received_at": "2026-09-27T12:02:00Z",
        "consumer": {
            "component": "StockWiki",
            "namespace": "quick_scan",
            "store_id": "stockwiki-test-store",
        },
    }
    ack.update(changes)
    return ack


def _checkpointed_work_item(tmp_path):
    clock = [1_800_000_000.0]
    store = _store(tmp_path, clock)
    item = _created(store)
    lease, attempt, receipt = _successful_search_attempt(store, item["work_item_id"])
    checkpoint = store.save_answer_checkpoint(
        item["work_item_id"],
        lease,
        attempt["attempt_id"],
        answer=_answer_for(item),
        execution_receipt=receipt,
    )
    return store, item, checkpoint, clock


def test_result_delivery_outbox_restarts_replays_exact_ack_and_never_reasks(tmp_path):
    store, item, checkpoint, clock = _checkpointed_work_item(tmp_path)
    work_id = item["work_item_id"]
    assert len(store.list_attempts(work_id)) == 1
    assert store.list_result_ready_without_delivery() == [work_id]

    blocked = store.mark_result_delivery_blocked(work_id, "exchange_package_incomplete")
    assert blocked["state"] == "blocked"
    assert blocked["block_code"] == "exchange_package_incomplete"
    assert store.list_result_ready_without_delivery() == []

    package = _c06_package_for_checkpoint(checkpoint)
    ready = store.prepare_result_delivery(work_id, package)
    restarted = _store(tmp_path, clock)
    repeated = restarted.prepare_result_delivery(work_id, package)
    assert repeated["delivery_id"] == ready["delivery_id"]
    assert repeated["state"] == "ready"
    assert repeated["package_bytes"] == canonical_bytes(package)

    dispatch = restarted.begin_result_delivery(work_id)
    assert dispatch["state"] == "send_uncertain"
    assert dispatch["request_bytes"] == canonical_bytes(package)
    assert dispatch["idempotency_key"] == ready["delivery_key"]
    with pytest.raises(WorkConflictError, match="dispatchable"):
        _store(tmp_path, clock).begin_result_delivery(work_id)
    assert _store(tmp_path, clock).get_result_delivery(work_id)["state"] == "send_uncertain"

    ack = _ack_for_package(package)
    delivered = _store(tmp_path, clock).apply_result_delivery_ack(work_id, ack)
    assert delivered["state"] == "delivered"
    assert delivered["ack"] == ack
    assert _store(tmp_path, clock).get_item(work_id)["status"] == "delivered"
    assert _store(tmp_path, clock).apply_result_delivery_ack(work_id, ack)["state"] == "delivered"
    assert len(_store(tmp_path, clock).list_attempts(work_id)) == 1
    assert [
        event["event_type"]
        for event in _store(tmp_path, clock).list_result_delivery_events(work_id)
    ] == [
        "blocked",
        "package_prepared",
        "send_intent",
        "accepted",
    ]


def test_result_delivery_confirmed_not_sent_retries_identical_bytes_only(tmp_path):
    store, item, checkpoint, _ = _checkpointed_work_item(tmp_path)
    work_id = item["work_item_id"]
    package = _c06_package_for_checkpoint(checkpoint)
    prepared = store.prepare_result_delivery(work_id, package)
    first = store.begin_result_delivery(work_id)
    rearmed = store.confirm_result_delivery_not_sent(work_id, "connection_refused_before_write")
    assert rearmed["state"] == "ready"
    second = store.begin_result_delivery(work_id)
    assert second["request_bytes"] == first["request_bytes"]
    assert second["idempotency_key"] == first["idempotency_key"] == prepared["delivery_key"]
    assert (
        store.apply_result_delivery_ack(
            work_id, _ack_for_package(package, status="already_present")
        )["state"]
        == "delivered"
    )
    assert len(store.list_attempts(work_id)) == 1


def test_result_delivery_rejects_wrong_ack_without_mutating_uncertain_delivery(tmp_path):
    store, item, checkpoint, _ = _checkpointed_work_item(tmp_path)
    work_id = item["work_item_id"]
    package = _c06_package_for_checkpoint(checkpoint)
    store.prepare_result_delivery(work_id, package)
    store.begin_result_delivery(work_id)
    with pytest.raises(ValueError, match="does not match"):
        store.apply_result_delivery_ack(work_id, _ack_for_package(package, payload_sha256="0" * 64))
    assert store.get_result_delivery(work_id)["state"] == "send_uncertain"
    assert store.get_item(work_id)["status"] == "result_ready"
    assert store.get_result_delivery(work_id)["ack"] is None


@pytest.mark.parametrize(
    "status,error_code,expected_state",
    [
        ("rejected", "invalid_payload", "rejected"),
        ("conflict", "immutable_key_hash_conflict", "conflict"),
    ],
)
def test_result_delivery_rejection_is_terminal_and_does_not_mark_work_delivered(
    tmp_path, status, error_code, expected_state
):
    store, item, checkpoint, _ = _checkpointed_work_item(tmp_path)
    work_id = item["work_item_id"]
    package = _c06_package_for_checkpoint(checkpoint)
    store.prepare_result_delivery(work_id, package)
    store.begin_result_delivery(work_id)
    result = store.apply_result_delivery_ack(
        work_id, _ack_for_package(package, status=status, error_code=error_code)
    )
    assert result["state"] == expected_state
    assert store.get_item(work_id)["status"] == "result_ready"
    with pytest.raises(WorkConflictError):
        store.begin_result_delivery(work_id)
    with pytest.raises(WorkConflictError, match="terminal"):
        store.apply_result_delivery_ack(work_id, _ack_for_package(package))


def test_result_delivery_rejects_checkpoint_mismatch_and_keeps_block_reason(tmp_path):
    store, item, checkpoint, _ = _checkpointed_work_item(tmp_path)
    work_id = item["work_item_id"]
    store.mark_result_delivery_blocked(work_id, "adapter_unavailable")
    package = _c06_package_for_checkpoint(
        checkpoint, observation_changes={"entity_id": "ENT_OTHER"}
    )
    with pytest.raises(ValueError, match="does not match checkpoint"):
        store.prepare_result_delivery(work_id, package)
    persisted = store.get_result_delivery(work_id)
    assert persisted["state"] == "blocked"
    assert persisted["block_code"] == "adapter_unavailable"
    assert store.get_item(work_id)["status"] == "result_ready"


def test_result_delivery_rejects_evidence_url_not_in_checkpoint_sources(tmp_path):
    store, item, checkpoint, _ = _checkpointed_work_item(tmp_path)
    work_id = item["work_item_id"]
    store.mark_result_delivery_blocked(work_id, "adapter_unavailable")
    package = _c06_package_for_checkpoint(checkpoint)
    package["items"][0]["observation"]["answer"]["evidence"][0][
        "url"
    ] = "https://example.invalid/unrelated-source"
    _readdress_c06_package(package)

    with pytest.raises(ValueError, match="evidence URL is not bound to checkpoint"):
        store.prepare_result_delivery(work_id, package)
    persisted = store.get_result_delivery(work_id)
    assert persisted["state"] == "blocked"
    assert persisted["block_code"] == "adapter_unavailable"
    assert store.get_item(work_id)["status"] == "result_ready"


def test_result_delivery_rejects_scored_answer_without_evidence(tmp_path):
    store, item, checkpoint, _ = _checkpointed_work_item(tmp_path)
    work_id = item["work_item_id"]
    store.mark_result_delivery_blocked(work_id, "adapter_unavailable")
    package = _c06_package_for_checkpoint(checkpoint)
    package["items"][0]["observation"]["answer"]["evidence"] = []
    _readdress_c06_package(package)

    with pytest.raises(ValueError, match="scored exchange answer requires evidence"):
        store.prepare_result_delivery(work_id, package)
    persisted = store.get_result_delivery(work_id)
    assert persisted["state"] == "blocked"
    assert persisted["block_code"] == "adapter_unavailable"
    assert store.get_item(work_id)["status"] == "result_ready"


def test_result_delivery_package_and_event_ledger_are_database_immutable(tmp_path):
    store, item, checkpoint, _ = _checkpointed_work_item(tmp_path)
    work_id = item["work_item_id"]
    package = _c06_package_for_checkpoint(checkpoint)
    delivery = store.prepare_result_delivery(work_id, package)
    with closing(store._connect()) as connection, connection:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute(
                "UPDATE quick_scan_result_delivery SET package_json='{}' WHERE delivery_id=?",
                (delivery["delivery_id"],),
            )
        with pytest.raises(sqlite3.DatabaseError, match="invalid quick-scan"):
            connection.execute(
                "UPDATE quick_scan_result_delivery SET state='delivered',ack_json='{}',"
                "ack_sha256=?,consumer_store_id='test-store' WHERE delivery_id=?",
                ("a" * 64, delivery["delivery_id"]),
            )
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute(
                "DELETE FROM quick_scan_result_delivery WHERE delivery_id=?",
                (delivery["delivery_id"],),
            )
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute(
                "UPDATE quick_scan_result_delivery_event SET event_type='blocked' "
                "WHERE delivery_id=?",
                (delivery["delivery_id"],),
            )
    store.apply_result_delivery_ack(work_id, _ack_for_package(package))
    with closing(store._connect()) as connection, connection:
        with pytest.raises(sqlite3.DatabaseError, match="ACK is immutable"):
            connection.execute(
                "UPDATE quick_scan_result_delivery SET ack_json='{}' WHERE delivery_id=?",
                (delivery["delivery_id"],),
            )


def test_schema_v4_upgrade_preserves_checkpoint_and_creates_delivery_ledger(tmp_path):
    store, item, checkpoint, clock = _checkpointed_work_item(tmp_path)
    with closing(store._connect()) as connection, connection:
        connection.execute("DROP TABLE quick_scan_result_delivery_event")
        connection.execute("DROP TABLE quick_scan_result_delivery")
        connection.execute("PRAGMA user_version=4")
    migrated = _store(tmp_path, clock)
    assert migrated.get_item(item["work_item_id"])["status"] == "result_ready"
    assert (
        migrated.get_answer_checkpoint(item["work_item_id"])["payload_sha256"]
        == checkpoint["payload_sha256"]
    )
    with closing(migrated._connect()) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert (
            connection.execute("SELECT COUNT(*) FROM quick_scan_result_delivery").fetchone()[0] == 0
        )
