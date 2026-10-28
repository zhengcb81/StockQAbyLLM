"""Original settled SQLite fixtures, synthetic authority only; no real API/owner."""

import copy
from contextlib import closing

import pytest

from src.utils.quick_scan_result_outbox import canonical_sha256
from src.utils.quick_scan_work_decision_inspect import (
    derive_work_decision,
    inspect_work_decision,
)
from src.utils.quick_scan_work_store import WorkConflictError
from tests.unit.test_quick_scan_known_outcome import (
    authority,
    fixture,
    immutable,
    save_synthetic_standard,
)

PURPOSE = "retry_unusable_checkpoint"


def source(tmp_path, *, standard=False):
    store, clock, members, pack, receipt, policy, context = fixture(
        tmp_path, response_status="completed"
    )
    member = pack["members"][0]
    wid = member["work_item_id"]
    qid = member["question_id"]
    if standard:
        save_synthetic_standard(store, members, pack, receipt, context)
    else:
        store.save_answer_checkpoint(
            wid,
            members[0]["lease"],
            member["attempt_id"],
            answer={
                "entity_id": store.get_item(wid)["entity_id"],
                "question_id": qid,
                "status": "scored",
                "score": 6,
                "description": "Legacy compact fixture; no full standard body.",
            },
            execution_receipt=receipt,
            observation_context=context,
        )
    store.mark_result_delivery_blocked(wid, "c06_standard_answer_unavailable")
    return store, clock, wid, member, policy


def intent(wid, purpose=PURPOSE):
    return dict(
        kind="retry_decision",
        work_item_id=wid,
        idempotency_key="fixture-unusable-checkpoint",
        purpose=purpose,
        retry_after_seconds=0,
        comparison_controls=None,
    )


def test_readonly_new_dialect_binds_original_checkpoint_without_mutation(tmp_path):
    store, clock, wid, member, policy = source(tmp_path)
    before = store.path.read_bytes()
    out = derive_work_decision(store.path, intent(wid), policy=policy, clock=lambda: clock[0])
    assert out["protocol"] == "stockqa.work_decision_inspection/1.2.0"
    assert (
        out["request"]["target_generation"] == 2
        and out["request"]["requires_prior_delivery"] is False
    )
    assert (
        out["request"]["unusable_checkpoint"]["checkpoint_sha256"]
        == store.get_answer_checkpoint(wid)["payload_sha256"]
    )
    assert (
        inspect_work_decision(store.path, out["request"], policy=policy, clock=lambda: clock[0])[
            "request"
        ]
        == out["request"]
    )
    assert store.path.read_bytes() == before


def test_native_journal_idempotent_no_old_work_or_material_or_physical_change(tmp_path):
    store, clock, wid, member, policy = source(tmp_path)
    before = immutable(store)
    old = store.get_item(wid)
    cp = store.get_answer_checkpoint(wid)
    outbox = store.get_result_delivery(wid)
    kwargs = dict(
        purpose=PURPOSE,
        retry_after_seconds=0,
        owner_authority=authority(),
        idempotency_key="fixture-unusable-checkpoint",
    )
    result = store.record_owner_retry_decision(wid, **kwargs)
    assert result["protocol"] == "stockqa.owner_work_decision/1.2.0"
    assert store.record_owner_retry_decision(wid, **kwargs) == result
    assert store.get_item(wid) == old and old["status"] == "result_ready"
    assert store.get_answer_checkpoint(wid) == cp and store.get_result_delivery(wid) == outbox
    assert immutable(store) == before
    from src.utils.quick_scan_work_control import project_owner_control

    p = project_owner_control(
        result,
        source_work=old,
        subject_ref=result["request"]["physical"]["subject_binding"]["subject_ref"],
        provider=result["request"]["physical"]["provider"],
        model=result["request"]["physical"]["model_requested"],
        now=clock[0],
        cohort_id="fixture",
    )
    assert p["decision"] == "dispatch_new_generation" and p["generation"] == 2


@pytest.mark.parametrize(
    "fault",
    [
        "old_purpose",
        "full_standard",
        "wrong_block",
        "foreign_proof",
        "extra_proof",
        "model",
        "date",
    ],
)
def test_only_exact_unusable_checkpoint_and_original_request_are_eligible(tmp_path, fault):
    store, clock, wid, member, policy = source(tmp_path, standard=fault == "full_standard")
    if fault == "wrong_block":
        store.mark_result_delivery_blocked(wid, "other_block")
    before = store.path.read_bytes()
    with pytest.raises((ValueError, RuntimeError)):
        if fault in {"old_purpose", "full_standard", "wrong_block"}:
            derive_work_decision(
                store.path,
                intent(wid, "retry_unusable_answer" if fault == "old_purpose" else PURPOSE),
                policy=policy,
                clock=lambda: clock[0],
            )
        else:
            request = derive_work_decision(
                store.path, intent(wid), policy=policy, clock=lambda: clock[0]
            )["request"]
            if fault == "foreign_proof":
                request["unusable_checkpoint"]["checkpoint_sha256"] = "a" * 64
            elif fault == "extra_proof":
                request["unusable_checkpoint"]["body"] = "not permitted"
            elif fault == "model":
                request["physical"]["model_requested"] = "other"
            else:
                request["physical"]["subject_binding"]["question_contract"][
                    "information_cutoff"
                ] = "2099-01-01"
            inspect_work_decision(store.path, request, policy=policy, clock=lambda: clock[0])
    assert store.path.read_bytes() == before


def test_revocation_cannot_append_decision_or_change_original_material(tmp_path):
    store, clock, wid, member, policy = source(tmp_path)
    before = store.path.read_bytes()
    with pytest.raises(PermissionError):
        store.record_owner_retry_decision(
            wid,
            purpose=PURPOSE,
            retry_after_seconds=0,
            owner_authority=authority(revoke=True),
            idempotency_key="revoke",
        )
    assert store.path.read_bytes() == before


def test_new_decision_reopens_through_original_store_validator(tmp_path):
    from src.utils.quick_scan_work_store import QuickScanWorkStore

    store, clock, wid, member, policy = source(tmp_path)
    result = store.record_owner_retry_decision(
        wid,
        purpose=PURPOSE,
        retry_after_seconds=0,
        owner_authority=authority(),
        idempotency_key="reopen",
    )
    reopened = QuickScanWorkStore(store.path, clock=lambda: clock[0])
    assert reopened.get_owner_retry_decision(wid) == result
    assert reopened.get_item(wid)["status"] == "result_ready"


def test_original_public_read_cli_derives_exact_new_proof_without_writing(tmp_path):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    store, clock, wid, member, policy = source(tmp_path)
    # Fixture historical date affects only expiry-free pure reads, not grant authentication.
    pp = tmp_path / "policy.json"
    pp.write_text(json.dumps(policy), encoding="utf-8")
    ip = tmp_path / "intent.json"
    ip.write_text(json.dumps(intent(wid)), encoding="utf-8")
    before = store.path.read_bytes()
    env = {
        k: v
        for k, v in os.environ.items()
        if not any(t in k.upper() for t in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
    }
    env["PYTHONPATH"] = (
        str(Path(__file__).resolve().parents[2])
        + os.pathsep
        + str(Path(__file__).resolve().parents[3] / "invest-quick-scan" / "scripts")
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            "-m",
            "src.utils.quick_scan_work_decision_inspect_cli",
            "--ledger",
            str(store.path),
            "--policy",
            str(pp),
            "--intent",
            str(ip),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = json.loads(proc.stdout)
    assert (
        out["request"]["purpose"] == PURPOSE and out["API_requests"] == out["database_writes"] == 0
    )
    assert store.path.read_bytes() == before


def test_a_newer_original_generation_blocks_another_retry_key(tmp_path):
    store, clock, wid, member, policy = source(tmp_path)
    old = store.get_item(wid)
    kwargs = {
        k: old[k]
        for k in (
            "entity_id",
            "question_id",
            "scope",
            "scope_id",
            "identity_revision",
            "source_binding_version",
            "identity_state",
            "source_binding_ref",
            "identity_snapshot_sha256",
            "question_fingerprint",
            "routing_fingerprint",
        )
    }
    import json

    kwargs.update(
        generation=2,
        source_binding_refs=json.loads(old["source_binding_refs_json"]),
        run_id="NEW",
        scan_id="NEW",
    )
    store.create_or_attach(**kwargs)
    before = store.path.read_bytes()
    with pytest.raises(WorkConflictError, match="latest_generation"):
        derive_work_decision(store.path, intent(wid), policy=policy, clock=lambda: clock[0])
    assert store.path.read_bytes() == before


@pytest.mark.parametrize(
    "fault", ["delivery_state", "package", "ack", "wrong_work_status", "wrong_checkpoint_hash"]
)
def test_same_native_transaction_refuses_unusable_proof_corruption_or_ineligible_state(
    tmp_path, fault
):
    from src.utils.quick_scan_known_outcome_journal import unusable_checkpoint_proof

    store, clock, wid, member, policy = source(tmp_path)
    with closing(store._connect()) as con:
        con.execute("BEGIN")
        item = dict(con.execute("SELECT * FROM work_item WHERE work_item_id=?", (wid,)).fetchone())
        attempt = con.execute(
            "SELECT * FROM attempt WHERE attempt_id=?", (member["attempt_id"],)
        ).fetchone()
        if fault == "wrong_work_status":
            item["status"] = "delivered"
        else:
            # These impossible combinations are refused by original SQLite CHECKs/triggers.
            import sqlite3

            statements = {
                "delivery_state": (
                    "UPDATE quick_scan_result_delivery SET state='send_uncertain' WHERE work_item_id=?",
                    (wid,),
                ),
                "package": (
                    "UPDATE quick_scan_result_delivery SET package_id='pkg_not_authorized' WHERE work_item_id=?",
                    (wid,),
                ),
                "ack": (
                    "UPDATE quick_scan_result_delivery SET ack_json='{}' WHERE work_item_id=?",
                    (wid,),
                ),
                "wrong_checkpoint_hash": (
                    "UPDATE answer_checkpoint SET payload_sha256=? WHERE work_item_id=?",
                    ("a" * 64, wid),
                ),
            }
            sql, args = statements[fault]
            with pytest.raises(sqlite3.IntegrityError):
                con.execute(sql, args)
            con.rollback()
            return
        with pytest.raises((ValueError, RuntimeError, WorkConflictError)):
            unusable_checkpoint_proof(store, con, item, attempt)
        con.rollback()


@pytest.mark.parametrize("downgrade", [False, True])
def test_original_sdk_control_authority_new_grant_dialect_is_exact(tmp_path, downgrade):
    from tests.unit.test_quick_scan_work_delegation_authority import fixture as wire
    from tests.unit.test_quick_scan_work_delegation_authority import seal

    client, value = wire(tmp_path)
    request = value["request"]["decision_request"]
    request.update(kind="retry_decision", purpose=PURPOSE)
    grant = value["confirmation"]["delegation"]
    grant["protocol"] = (
        "stockwiki.work_delegation/1.0.0" if downgrade else "stockwiki.work_delegation/1.1.0"
    )
    grant["binding"].update(kind="retry_decision", purpose=PURPOSE)
    seal(grant)
    if downgrade:
        with pytest.raises(ValueError):
            client._validate_control_delegation(value)
    else:
        client._validate_control_delegation(value)


def controlled_successor_fixture(tmp_path):
    from src.utils.quick_scan_work_control import project_owner_control
    from tests.unit.test_w15_owner_refresh import _binding

    store, clock, wid, member, policy = source(tmp_path)
    old = store.get_item(wid)
    document = store.record_owner_retry_decision(
        wid,
        purpose=PURPOSE,
        retry_after_seconds=0,
        owner_authority=authority(),
        idempotency_key="controlled-successor",
    )
    physical = document["request"]["physical"]
    subject = physical["subject_binding"]["subject_ref"]
    control = project_owner_control(
        document,
        source_work=old,
        subject_ref=subject,
        provider=physical["provider"],
        model=physical["model_requested"],
        now=clock[0],
        cohort_id="checkpoint-recovery",
    )["owner_control"]
    binding = _binding(
        protocol="stockqa.owner_refresh_binding/3.0.0",
        subject_context_schema="stockqa.quick_scan_observation_context/2.0.0",
        owner_control=control,
        subject_key=subject["analysis_subject_id"]
        + "@"
        + str(subject["analysis_subject_revision"]),
        perimeter_sha256=subject["perimeter_sha256"],
        entity_id=old["entity_id"],
        question_id=old["question_id"],
        scope=old["scope"],
        scope_id=old["scope_id"],
        identity_revision=old["identity_revision"],
        provider=physical["provider"],
        model=physical["model_requested"],
    )
    keys = (
        "entity_id",
        "question_id",
        "scope",
        "scope_id",
        "identity_revision",
        "source_binding_version",
        "identity_state",
        "source_binding_ref",
        "identity_snapshot_sha256",
        "question_fingerprint",
    )
    spec = {key: old[key] for key in keys}
    spec.update(
        source_binding_refs=[old["source_binding_ref"]], generation=2, routing_fingerprint="a" * 64
    )
    return store, clock, old, document, binding, spec, policy


@pytest.mark.parametrize("state", ["pending", "failed", "result_ready", "delivered"])
def test_exact_successor_selected_without_mutating_compact_source(tmp_path, state):
    store, clock, old, document, binding, spec, policy = controlled_successor_fixture(tmp_path)
    checkpoint = store.get_answer_checkpoint(old["work_item_id"])
    outbox = store.get_result_delivery(old["work_item_id"])
    target = store.create_or_attach(
        **spec, owner_refresh_binding=binding, run_id="RECOVERY", scan_id="RECOVERY"
    )
    # Only this target lifecycle status is fixture-controlled to exercise all read branches.
    with store._transaction() as con:
        con.execute(
            "UPDATE work_item SET status=? WHERE work_item_id=?", (state, target["work_item_id"])
        )
    before = store.path.read_bytes()
    projection = store.owner_refresh_projection(
        entity_id=old["entity_id"],
        question_ids=[old["question_id"]],
        scope_bindings={old["question_id"]: {"scope": old["scope"], "scope_id": old["scope_id"]}},
        include_owner_decisions=True,
    )
    assert projection[old["question_id"]] == dict(
        work_item_id=target["work_item_id"], generation=2, status=state
    )
    assert store.get_item(old["work_item_id"]) == old
    assert (
        store.get_answer_checkpoint(old["work_item_id"]) == checkpoint
        and store.get_result_delivery(old["work_item_id"]) == outbox
    )
    assert store.path.read_bytes() == before


def test_unbound_successor_does_not_hide_original_unusable_checkpoint(tmp_path):
    store, clock, old, document, binding, spec, policy = controlled_successor_fixture(tmp_path)
    store.create_or_attach(**spec, run_id="LEGACY", scan_id="LEGACY")
    with store._transaction() as con:
        con.execute("UPDATE work_item SET status='delivered' WHERE generation=2")
    projection = store.owner_refresh_projection(
        entity_id=old["entity_id"],
        question_ids=[old["question_id"]],
        scope_bindings={old["question_id"]: {"scope": old["scope"], "scope_id": old["scope_id"]}},
    )
    assert projection[old["question_id"]]["work_item_id"] == old["work_item_id"]


def test_controlled_successor_does_not_bypass_another_uncertain_work(tmp_path):
    store, clock, old, document, binding, spec, policy = controlled_successor_fixture(tmp_path)
    extra = store.create_or_attach(**dict(spec, generation=3), run_id="OTHER", scan_id="OTHER")
    with store._transaction() as con:
        con.execute(
            "UPDATE work_item SET status='uncertain' WHERE work_item_id=?", (extra["work_item_id"],)
        )
    before = store.path.read_bytes()
    with pytest.raises(WorkConflictError, match="prior_work_unresolved"):
        store.create_or_attach(
            **spec, owner_refresh_binding=binding, run_id="RECOVERY", scan_id="RECOVERY"
        )
    assert store.path.read_bytes() == before


def test_no_target_still_projects_original_unusable_source(tmp_path):
    store, clock, old, document, binding, spec, policy = controlled_successor_fixture(tmp_path)
    before = store.path.read_bytes()
    projection = store.owner_refresh_projection(
        entity_id=old["entity_id"],
        question_ids=[old["question_id"]],
        scope_bindings={old["question_id"]: {"scope": old["scope"], "scope_id": old["scope_id"]}},
        include_owner_decisions=True,
    )
    assert projection[old["question_id"]]["work_item_id"] == old["work_item_id"]
    assert projection[old["question_id"]]["retry_purpose"] == PURPOSE
    assert store.path.read_bytes() == before


@pytest.mark.parametrize(
    "field",
    [
        "decision_id",
        "document_sha256",
        "source_work_item_id",
        "source_generation",
        "target_generation",
        "confirmation_id",
        "purpose",
    ],
)
def test_projection_mismatched_exact_target_binding_never_hides_old_source(
    tmp_path, monkeypatch, field
):
    import src.utils.quick_scan_owner_refresh_journal as journal

    store, clock, old, document, binding, spec, policy = controlled_successor_fixture(tmp_path)
    target = store.create_or_attach(
        **spec, owner_refresh_binding=binding, run_id="RECOVERY", scan_id="RECOVERY"
    )
    with store._transaction() as con:
        con.execute(
            "UPDATE work_item SET status='delivered' WHERE work_item_id=?",
            (target["work_item_id"],),
        )
    native = journal.read

    def different(con, wid):
        result = native(con, wid)
        if result and wid == target["work_item_id"]:
            result = copy.deepcopy(result)
            result["binding"]["owner_control"][field] = "different"
        return result

    monkeypatch.setattr(journal, "read", different)
    before = store.path.read_bytes()
    projection = store.owner_refresh_projection(
        entity_id=old["entity_id"],
        question_ids=[old["question_id"]],
        scope_bindings={old["question_id"]: {"scope": old["scope"], "scope_id": old["scope_id"]}},
    )
    assert projection[old["question_id"]]["work_item_id"] == old["work_item_id"]
    assert store.path.read_bytes() == before


def test_other_purpose_ready_keeps_original_projection_semantics(tmp_path):
    from tests.unit.test_controlled_refresh_binding import create, prepared

    store, clock, old, doc, binding, spec = prepared(tmp_path, ready=True, purpose="comparison")
    target = create(store, binding, spec)
    with store._transaction() as con:
        con.execute(
            "UPDATE work_item SET status='delivered' WHERE work_item_id=?",
            (target["work_item_id"],),
        )
    result = store.owner_refresh_projection(
        entity_id=old["entity_id"],
        question_ids=[old["question_id"]],
        scope_bindings={old["question_id"]: {"scope": old["scope"], "scope_id": old["scope_id"]}},
    )
    assert result[old["question_id"]]["work_item_id"] == old["work_item_id"]


def test_original_control_projection_cli_has_zero_write_and_no_answer_body(tmp_path):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    store, clock, old, doc, binding, spec, policy = controlled_successor_fixture(tmp_path)
    qid = old["question_id"]
    query = {
        "entity_id": old["entity_id"],
        "question_ids": [qid],
        "scope_bindings": {qid: {"scope": old["scope"], "scope_id": old["scope_id"]}},
    }
    (tmp_path / "query.json").write_text(json.dumps(query), encoding="utf-8")
    (tmp_path / "policy.json").write_text(json.dumps(policy), encoding="utf-8")
    env = {
        k: v
        for k, v in os.environ.items()
        if not any(t in k.upper() for t in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
    }
    env["PYTHONPATH"] = (
        str(Path(__file__).resolve().parents[2])
        + os.pathsep
        + str(Path(__file__).resolve().parents[3] / "invest-quick-scan" / "scripts")
    )
    before = store.path.read_bytes()
    done = subprocess.run(
        [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            "-m",
            "src.utils.quick_scan_work_decision_inspect_cli",
            "--ledger",
            str(store.path),
            "--policy",
            str(tmp_path / "policy.json"),
            "--owner-controls",
            str(tmp_path / "query.json"),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    result = json.loads(done.stdout)
    assert result["API_requests"] == result["database_writes"] == 0
    assert result["rows"][qid]["retry_decision_id"] == doc["decision_id"]
    assert "physical" not in done.stdout.decode(
        "utf-8"
    ) and "description" not in done.stdout.decode("utf-8")
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("mismatch", [False, True])
def test_registered_successor_inspection_uses_exact_native_control_or_refuses(
    tmp_path, monkeypatch, mismatch
):
    import types

    import src.utils.quick_scan_refresh_inspect as inspector

    store, clock, old, doc, binding, spec, policy = controlled_successor_fixture(tmp_path)
    target = store.create_or_attach(
        **spec, owner_refresh_binding=binding, run_id="RECOVERY", scan_id="RECOVERY"
    )
    monkeypatch.setattr(inspector, "time", types.SimpleNamespace(time=lambda: clock[0]))
    qid = old["question_id"]
    physical = doc["request"]["physical"]
    request = {
        "entity_id": old["entity_id"],
        "question_ids": [qid],
        "scope_bindings": {qid: {"scope": old["scope"], "scope_id": old["scope_id"]}},
        "policy_id": physical["budget_policy_id"],
        "route_id": physical["route_id"],
        "provider_config_ref": physical["route_provider"],
        "model": physical["model_requested"],
        "cohort_id": "foreign-cohort" if mismatch else "checkpoint-recovery",
        "work_decision_refs": [
            {"work_item_id": old["work_item_id"], "decision_id": doc["decision_id"]}
        ],
    }
    before = store.path.read_bytes()
    if mismatch:
        with pytest.raises(ValueError, match="source_work_changed"):
            inspector.inspect_refresh(store.path, request, policy=policy, schema_version="1.2.0")
    else:
        result = inspector.inspect_refresh(
            store.path, request, policy=policy, schema_version="1.2.0"
        )
        assert result["work_items"][qid]["work_item_id"] == target["work_item_id"]
        assert result["work_items"][qid]["status"] == "pending"
    assert store.path.read_bytes() == before


def _ordinary_successor_case(tmp_path, monkeypatch, *, fault=None):
    """Real ledger/control; validated delivery READER is a unit boundary stub.

    The native three-owner integration separately requires real seal/import/ACK.
    No label-only delivered fixture is claimed to prove consumer acceptance.
    """
    from src.utils.quick_scan_work_store import QuickScanWorkStore

    store, clock, old, doc, control_binding, spec, _ = controlled_successor_fixture(tmp_path)
    target = store.create_or_attach(
        **spec, owner_refresh_binding=control_binding, run_id="RECOVERY", scan_id="RECOVERY"
    )
    with store._transaction() as con:
        con.execute(
            "UPDATE work_item SET status=? WHERE work_item_id=?",
            ("pending" if fault == "not_delivered" else "delivered", target["work_item_id"]),
        )
    validated = {
        "state": "delivered",
        "ack": {"status": "accepted"},
        "consumer_binding": {"consumer": "synthetic-reader-boundary"},
        "package": {"synthetic": True},
    }
    if fault == "no_ack":
        validated["ack"] = None
    if fault == "unbound_consumer":
        validated["consumer_binding"] = None
    if fault == "unsealed":
        validated["package"] = None
    if fault != "missing_outbox":
        original_row = QuickScanWorkStore._delivery_row
        original_reader = QuickScanWorkStore._result_delivery_record.__func__
        monkeypatch.setattr(
            QuickScanWorkStore,
            "_delivery_row",
            staticmethod(
                lambda con, wid: (
                    {"unit_boundary": wid}
                    if wid == target["work_item_id"]
                    else original_row(con, wid)
                )
            ),
        )
        monkeypatch.setattr(
            QuickScanWorkStore,
            "_result_delivery_record",
            classmethod(
                lambda cls, row, **kw: (
                    copy.deepcopy(validated)
                    if "unit_boundary" in row.keys()
                    else original_reader(cls, row, **kw)
                )
            ),
        )
    ordinary = copy.deepcopy(control_binding)
    ordinary.update(protocol="stockqa.owner_refresh_binding/2.0.0", target_generation=3)
    ordinary.pop("owner_control")
    return store, clock, old, target, ordinary, dict(spec, generation=3)


def test_acknowledged_exact_successor_allows_ordinary_next_generation_and_send_guard(
    tmp_path, monkeypatch
):
    from src.utils.quick_scan_owner_refresh_journal import guard_send

    store, clock, old, target, binding, spec = _ordinary_successor_case(tmp_path, monkeypatch)
    before_old = store.get_item(old["work_item_id"])
    checkpoint = store.get_answer_checkpoint(old["work_item_id"])
    physical = immutable(store)
    new = store.create_or_attach(
        **spec, owner_refresh_binding=binding, run_id="ORDINARY", scan_id="ORDINARY"
    )
    assert new["generation"] == 3 and new["status"] == "pending"
    with closing(store._connect()) as con:
        guard_send(con, store.get_item(new["work_item_id"]), now=clock[0])
    assert store.get_item(old["work_item_id"]) == before_old
    assert store.get_answer_checkpoint(old["work_item_id"]) == checkpoint
    assert immutable(store) == physical


@pytest.mark.parametrize(
    "fault", ["no_ack", "unbound_consumer", "unsealed", "missing_outbox", "not_delivered"]
)
def test_ordinary_generation_cannot_retire_source_on_delivery_label_alone(
    tmp_path, monkeypatch, fault
):
    store, clock, old, target, binding, spec = _ordinary_successor_case(
        tmp_path, monkeypatch, fault=fault
    )
    before = store.path.read_bytes()
    with pytest.raises(WorkConflictError, match="owner_refresh_prior_work_unresolved"):
        store.create_or_attach(
            **spec, owner_refresh_binding=binding, run_id="ORDINARY", scan_id="ORDINARY"
        )
    assert store.path.read_bytes() == before


def test_acknowledged_successor_never_hides_another_uncertain_work(tmp_path, monkeypatch):
    store, clock, old, target, binding, spec = _ordinary_successor_case(tmp_path, monkeypatch)
    other = store.create_or_attach(**dict(spec, generation=4), run_id="OTHER", scan_id="OTHER")
    with store._transaction() as con:
        con.execute(
            "UPDATE work_item SET status='uncertain' WHERE work_item_id=?", (other["work_item_id"],)
        )
    before = store.path.read_bytes()
    with pytest.raises(WorkConflictError, match="owner_refresh_prior_work_unresolved"):
        store.create_or_attach(
            **spec, owner_refresh_binding=binding, run_id="ORDINARY", scan_id="ORDINARY"
        )
    assert store.path.read_bytes() == before


def test_source_skip_requires_strictly_later_ordinary_generation(tmp_path, monkeypatch):
    from src.utils.quick_scan_owner_refresh_journal import guard_unresolved

    store, clock, old, target, binding, spec = _ordinary_successor_case(tmp_path, monkeypatch)
    before = store.path.read_bytes()
    with closing(store._connect()) as con, pytest.raises(
        WorkConflictError, match="owner_refresh_prior_work_unresolved"
    ):
        guard_unresolved(con, dict(old, generation=2), binding=binding, now=clock[0])
    assert store.path.read_bytes() == before


def chained_failed_successor_fixture(tmp_path):
    """Original native controls/receipts/settlement; no fake production recovery."""
    from datetime import datetime, timezone

    from src.utils import quick_scan_subject_journal as subject
    from src.utils.quick_scan_work_control import project_owner_control
    from tests.unit.test_c06_subject_send import receipt as subject_receipt
    from tests.unit.test_quick_scan_known_outcome import arguments
    from tests.unit.test_quick_scan_question_pack_store import _mark, _prepare, _receipt

    store, clock, old, document, binding, spec, policy = controlled_successor_fixture(tmp_path)
    target = store.create_or_attach(
        **spec, owner_refresh_binding=binding, run_id="FIRST_RETRY", scan_id="FIRST_RETRY"
    )
    lease = store.claim(target["work_item_id"], lease_seconds=120)
    context = store.get_observation_context(old["work_item_id"])["context"]
    store.freeze_observation_context_before_send(target["work_item_id"], lease, context)
    members = [{"work_item_id": target["work_item_id"], "lease": lease}]
    pack = _prepare(store, members, execution_attempt_id="HTTP_PACK_RETRY_02")
    with store._transaction() as con:
        member = pack["members"][0]
        item = store._item(con, target["work_item_id"])
        attempt = con.execute(
            "SELECT * FROM attempt WHERE attempt_id=?", (member["attempt_id"],)
        ).fetchone()
        request = subject.dispatch_request(
            item,
            attempt,
            subject.bound_context(con, item),
            execution_attempt_id=pack["execution_attempt_id"],
            provider_actual="mimo",
            provider_prompt_sha256=pack["provider_prompt_sha256"],
        )
        store.persist_subject_dispatch_receipt(
            target["work_item_id"],
            lease,
            member["attempt_id"],
            subject_receipt(request),
            expected_request=request,
        )
    _mark(store, pack, policy)
    receipt = _receipt(
        attempt_id=pack["execution_attempt_id"],
        response_status="completed",
        completed_at=datetime.fromtimestamp(clock[0], timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        source_urls=["https://example.invalid/company"],
    )
    store.record_question_pack_outcome(
        pack["pack_id"],
        outcome="response_available",
        execution_receipt=receipt,
        http_status_code=200,
        actual_cost=0.02,
        cost_source_ref="synthetic-cost",
    )
    clock[0] += 121
    args = arguments(
        store,
        members,
        pack,
        receipt,
        reason="known_response_no_accepted_answer",
        diagnostic={
            "code": "known_response_no_accepted_answer",
            "artifact_ref": "synthetic-local-projection-failure",
            "artifact_sha256": "9" * 64,
            "response_sha256": receipt["response_sha256"],
        },
    )
    store.resolve_known_response(target["work_item_id"], **args)
    retry = store.record_owner_retry_decision(
        target["work_item_id"],
        purpose="retry_unusable_answer",
        retry_after_seconds=0,
        owner_authority=authority(),
        idempotency_key="retry-after-local-projection-failure",
    )
    failed = store.get_item(target["work_item_id"])
    physical = retry["request"]["physical"]
    control = project_owner_control(
        retry,
        source_work=failed,
        subject_ref=physical["subject_binding"]["subject_ref"],
        provider=physical["provider"],
        model=physical["model_requested"],
        now=clock[0],
        cohort_id="second-retry",
    )["owner_control"]
    next_binding = dict(binding, target_generation=3, owner_control=control)
    return store, clock, old, failed, next_binding, dict(spec, generation=3)


def test_exact_received_failure_retry_chain_can_admit_and_send_without_rewriting_ancestors(
    tmp_path,
):
    from src.utils.quick_scan_owner_refresh_journal import guard_send

    store, clock, old, failed, binding, spec = chained_failed_successor_fixture(tmp_path)
    materials = immutable(store)
    checkpoint = store.get_answer_checkpoint(old["work_item_id"])
    target = store.create_or_attach(
        **spec, owner_refresh_binding=binding, run_id="SECOND_RETRY", scan_id="SECOND_RETRY"
    )
    with closing(store._connect()) as con:
        guard_send(con, store.get_item(target["work_item_id"]), now=clock[0])
    assert target["generation"] == 3 and target["status"] == "pending"
    assert store.get_item(old["work_item_id"]) == old
    assert store.get_item(failed["work_item_id"]) == failed
    assert store.get_answer_checkpoint(old["work_item_id"]) == checkpoint
    assert immutable(store) == materials
    projected = store.owner_refresh_projection(
        entity_id=old["entity_id"],
        question_ids=[old["question_id"]],
        scope_bindings={old["question_id"]: {"scope": old["scope"], "scope_id": old["scope_id"]}},
    )
    assert projected[old["question_id"]]["work_item_id"] == target["work_item_id"]


@pytest.mark.parametrize("fault", ["no_ack", "unbound_consumer", "unsealed", None])
def test_exact_received_failure_retry_chain_ordinary_requires_descendant_exact_delivery(
    tmp_path, monkeypatch, fault
):
    """Only the validated delivery reader is a unit boundary stub, not real ACK evidence."""
    from src.utils.quick_scan_owner_refresh_journal import guard_send
    from src.utils.quick_scan_work_store import QuickScanWorkStore

    store, clock, old, failed, binding, spec = chained_failed_successor_fixture(tmp_path)
    target = store.create_or_attach(
        **spec, owner_refresh_binding=binding, run_id="SECOND_RETRY", scan_id="SECOND_RETRY"
    )
    with store._transaction() as con:
        con.execute(
            "UPDATE work_item SET status='delivered' WHERE work_item_id=?",
            (target["work_item_id"],),
        )
    validated = {
        "state": "delivered",
        "ack": {"status": "accepted"},
        "consumer_binding": {"consumer": "synthetic-reader-boundary"},
        "package": {"synthetic": True},
    }
    if fault:
        validated[
            {"no_ack": "ack", "unbound_consumer": "consumer_binding", "unsealed": "package"}[fault]
        ] = None
    original_row = QuickScanWorkStore._delivery_row
    original_reader = QuickScanWorkStore._result_delivery_record.__func__
    monkeypatch.setattr(
        QuickScanWorkStore,
        "_delivery_row",
        staticmethod(
            lambda con, wid: (
                {"unit_boundary": wid} if wid == target["work_item_id"] else original_row(con, wid)
            )
        ),
    )
    monkeypatch.setattr(
        QuickScanWorkStore,
        "_result_delivery_record",
        classmethod(
            lambda cls, row, **kw: (
                copy.deepcopy(validated)
                if "unit_boundary" in row.keys()
                else original_reader(cls, row, **kw)
            )
        ),
    )
    ordinary = dict(binding, protocol="stockqa.owner_refresh_binding/2.0.0", target_generation=4)
    ordinary.pop("owner_control")
    physical = immutable(store)
    if fault:
        before = store.path.read_bytes()
        with pytest.raises(WorkConflictError, match="prior_work_unresolved"):
            store.create_or_attach(
                **dict(spec, generation=4),
                owner_refresh_binding=ordinary,
                run_id="ORDINARY",
                scan_id="ORDINARY",
            )
        assert store.path.read_bytes() == before
    else:
        new = store.create_or_attach(
            **dict(spec, generation=4),
            owner_refresh_binding=ordinary,
            run_id="ORDINARY",
            scan_id="ORDINARY",
        )
        with closing(store._connect()) as con:
            guard_send(con, store.get_item(new["work_item_id"]), now=clock[0])
        assert new["generation"] == 4 and new["status"] == "pending"
    assert store.get_item(old["work_item_id"]) == old
    assert store.get_item(failed["work_item_id"]) == failed
    assert immutable(store) == physical


@pytest.mark.parametrize("fault", ["foreign_uncertain", "broken_document", "broken_purpose"])
def test_exact_received_failure_retry_chain_never_skips_foreign_or_broken_work(
    tmp_path, monkeypatch, fault
):
    import src.utils.quick_scan_owner_refresh_journal as journal

    store, clock, old, failed, binding, spec = chained_failed_successor_fixture(tmp_path)
    if fault == "foreign_uncertain":
        extra = store.create_or_attach(**dict(spec, generation=4), run_id="OTHER", scan_id="OTHER")
        with store._transaction() as con:
            con.execute(
                "UPDATE work_item SET status='uncertain' WHERE work_item_id=?",
                (extra["work_item_id"],),
            )
    else:
        original_read = journal.read

        def corrupt(con, wid):
            value = original_read(con, wid)
            if value and wid == failed["work_item_id"]:
                value = copy.deepcopy(value)
                field = "document_sha256" if fault == "broken_document" else "purpose"
                value["binding"]["owner_control"][field] = "not-the-original-control"
            return value

        monkeypatch.setattr(journal, "read", corrupt)
    before = store.path.read_bytes()
    with pytest.raises((WorkConflictError, ValueError)):
        store.create_or_attach(
            **spec, owner_refresh_binding=binding, run_id="SECOND_RETRY", scan_id="SECOND_RETRY"
        )
    assert store.path.read_bytes() == before


def test_exact_received_failure_retry_chain_cannot_dispatch_ordinary_before_ack(tmp_path):
    store, clock, old, failed, binding, spec = chained_failed_successor_fixture(tmp_path)
    target = store.create_or_attach(
        **spec, owner_refresh_binding=binding, run_id="SECOND_RETRY", scan_id="SECOND_RETRY"
    )
    ordinary = dict(binding, protocol="stockqa.owner_refresh_binding/2.0.0", target_generation=4)
    ordinary.pop("owner_control")
    before = store.path.read_bytes()
    with pytest.raises(WorkConflictError, match="prior_work_unresolved"):
        store.create_or_attach(
            **dict(spec, generation=4),
            owner_refresh_binding=ordinary,
            run_id="ORDINARY",
            scan_id="ORDINARY",
        )
    assert store.path.read_bytes() == before
    assert store.get_item(target["work_item_id"])["status"] == "pending"


# Permanent adversarial regressions promoted from the independent native review.
def test_incoming_foreign_binding_cannot_erase_checkpoint_ancestor(tmp_path):
    store, clock, old, failed, binding, spec = chained_failed_successor_fixture(tmp_path)
    before = store.path.read_bytes()
    mutations = [
        ("provider", "other-provider"),
        ("model", "other-model"),
        ("subject_key", "other@1"),
        ("perimeter_sha256", "f" * 64),
        ("information_cutoff", "invalid-date"),
        ("scope_id", "other-scope"),
        ("entity_id", "other-entity"),
        ("question_id", "OTHER"),
        ("target_generation", 4),
        ("identity_revision", 99),
    ]
    for field, value in mutations:
        foreign = copy.deepcopy(binding)
        foreign[field] = value
        with pytest.raises((WorkConflictError, ValueError)):
            store.create_or_attach(
                **spec, owner_refresh_binding=foreign, run_id="FOREIGN", scan_id="FOREIGN"
            )
        assert store.path.read_bytes() == before, field
    for field in [
        "decision_id",
        "document_sha256",
        "confirmation_id",
        "source_work_item_id",
        "source_generation",
        "purpose",
    ]:
        foreign = copy.deepcopy(binding)
        foreign["owner_control"][field] = "foreign"
        with pytest.raises((WorkConflictError, ValueError)):
            store.create_or_attach(
                **spec, owner_refresh_binding=foreign, run_id="FOREIGN", scan_id="FOREIGN"
            )
        assert store.path.read_bytes() == before, field


def test_chain_before_generation_excludes_incoming_and_readonly(tmp_path):
    import src.utils.quick_scan_owner_refresh_journal as journal

    store, clock, old, failed, binding, spec = chained_failed_successor_fixture(tmp_path)
    target = store.create_or_attach(
        **spec, owner_refresh_binding=binding, run_id="THIRD", scan_id="THIRD"
    )
    before = store.path.read_bytes()
    with closing(store._connect()) as con:
        assert journal.checkpoint_successor(con, old, before_generation=2) is None
        assert (
            journal.checkpoint_successor(con, old, before_generation=3)["work_item_id"]
            == failed["work_item_id"]
        )
        assert (
            journal.checkpoint_successor(con, old, before_generation=4)["work_item_id"]
            == target["work_item_id"]
        )
        assert journal.checkpoint_successor(con, old)["work_item_id"] == target["work_item_id"]
        assert journal.checkpoint_successor(con, failed) is None
    assert store.path.read_bytes() == before


def _fail_and_retry(store, clock, old, target, binding):
    from datetime import datetime, timezone

    from src.utils import quick_scan_subject_journal as subject
    from src.utils.quick_scan_work_control import project_owner_control
    from tests.unit.test_c06_subject_send import receipt as subject_receipt
    from tests.unit.test_quick_scan_known_outcome import arguments

    # Original policy is retained in the store's physical pack binding; load fixture helper's value
    from tests.unit.test_quick_scan_question_pack_store import (
        _mark,
        _policy,
        _prepare,
        _receipt,
    )

    POLICY = _policy()
    POLICY["budget"]["currency"] = "CNY"
    lease = store.claim(target["work_item_id"], lease_seconds=120)
    context = store.get_observation_context(old["work_item_id"])["context"]
    store.freeze_observation_context_before_send(target["work_item_id"], lease, context)
    members = [{"work_item_id": target["work_item_id"], "lease": lease}]
    pack = _prepare(store, members, execution_attempt_id="HTTP_PACK_RETRY_03")
    with store._transaction() as con:
        member = pack["members"][0]
        item = store._item(con, target["work_item_id"])
        attempt = con.execute(
            "SELECT * FROM attempt WHERE attempt_id=?", (member["attempt_id"],)
        ).fetchone()
        request = subject.dispatch_request(
            item,
            attempt,
            subject.bound_context(con, item),
            execution_attempt_id=pack["execution_attempt_id"],
            provider_actual="mimo",
            provider_prompt_sha256=pack["provider_prompt_sha256"],
        )
        store.persist_subject_dispatch_receipt(
            target["work_item_id"],
            lease,
            member["attempt_id"],
            subject_receipt(request),
            expected_request=request,
        )
    _mark(store, pack, POLICY)
    receipt = _receipt(
        attempt_id=pack["execution_attempt_id"],
        response_status="completed",
        completed_at=datetime.fromtimestamp(clock[0], timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        source_urls=["https://example.invalid/company"],
    )
    store.record_question_pack_outcome(
        pack["pack_id"],
        outcome="response_available",
        execution_receipt=receipt,
        http_status_code=200,
        actual_cost=0.02,
        cost_source_ref="synthetic-cost",
    )
    clock[0] += 121
    args = arguments(
        store,
        members,
        pack,
        receipt,
        reason="known_response_no_accepted_answer",
        diagnostic={
            "code": "known_response_no_accepted_answer",
            "artifact_ref": "synthetic-local-projection-failure",
            "artifact_sha256": "9" * 64,
            "response_sha256": receipt["response_sha256"],
        },
    )
    store.resolve_known_response(target["work_item_id"], **args)
    retry = store.record_owner_retry_decision(
        target["work_item_id"],
        purpose="retry_unusable_answer",
        retry_after_seconds=0,
        owner_authority=authority(),
        idempotency_key="retry-after-second-local-projection-failure",
    )
    failed = store.get_item(target["work_item_id"])
    physical = retry["request"]["physical"]
    control = project_owner_control(
        retry,
        source_work=failed,
        subject_ref=physical["subject_binding"]["subject_ref"],
        provider=physical["provider"],
        model=physical["model_requested"],
        now=clock[0],
        cohort_id="third-retry",
    )["owner_control"]
    return failed, dict(binding, target_generation=4, owner_control=control)


def test_two_failed_native_descendants_allow_only_exact_fourth_generation(tmp_path):
    import src.utils.quick_scan_owner_refresh_journal as journal

    store, clock, old, failed2, binding, spec = chained_failed_successor_fixture(tmp_path)
    third = store.create_or_attach(
        **spec, owner_refresh_binding=binding, run_id="THIRD", scan_id="THIRD"
    )
    failed3, binding4 = _fail_and_retry(store, clock, old, third, binding)
    materials = immutable(store)
    checkpoint = store.get_answer_checkpoint(old["work_item_id"])
    fourth = store.create_or_attach(
        **dict(spec, generation=4),
        owner_refresh_binding=binding4,
        run_id="FOURTH",
        scan_id="FOURTH",
    )
    with closing(store._connect()) as con:
        journal.guard_send(con, store.get_item(fourth["work_item_id"]), now=clock[0])
        assert (
            journal.checkpoint_successor(con, old, before_generation=4)["work_item_id"]
            == failed3["work_item_id"]
        )
    assert fourth["generation"] == 4 and fourth["status"] == "pending"
    assert store.get_item(old["work_item_id"]) == old
    assert store.get_item(failed2["work_item_id"]) == failed2
    assert store.get_item(failed3["work_item_id"]) == failed3
    assert store.get_answer_checkpoint(old["work_item_id"]) == checkpoint
    assert immutable(store) == materials
