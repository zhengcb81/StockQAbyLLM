"""Actual Q13 temporary SQLite, synthetic receipts/typed owner-authority seam.

No fixture is real owner permission. These tests never open a production DB or
dispatch a model; actual owner confirmation is still required in production.
"""

import copy

import pytest

from src.utils.quick_scan_work_decision_inspect import derive_work_decision
from src.utils.quick_scan_work_store import Lease
from tests.unit.test_quick_scan_known_outcome import (
    arguments,
    authority,
    fixture,
    immutable,
    save_synthetic_standard,
)


def terminal(tmp_path):
    values = fixture(tmp_path, response_status="completed", currency="USD")
    store, _, members, pack, _, _, _ = values
    member = pack["members"][0]
    assert (
        store.fail_received_without_answer(
            member["work_item_id"],
            members[0]["lease"],
            member["attempt_id"],
            reason_code="invalid_question",
        )
        == "failed"
    )
    return values


def diagnostic(store, pack):
    return {
        "code": "standard_answer_invalid",
        "artifact_ref": "synthetic-schema-rejection.json",
        "artifact_sha256": "a" * 64,
        "response_sha256": store.get_attempt_response(pack["members"][0]["attempt_id"])[
            "response_sha256"
        ],
    }


def kwargs(values, **overrides):
    store, _, members, pack, receipt, _, _ = values
    return arguments(
        store,
        members,
        pack,
        receipt,
        reason="answer_validation_rejected",
        diagnostic=diagnostic(store, pack),
        **overrides,
    )


def test_terminal_received_failure_can_be_readonly_preflighted(tmp_path):
    values = terminal(tmp_path)
    store, clock, _, pack, _, policy, _ = values
    args = kwargs(values)
    intent = {k: args[k] for k in ("attempt_id", "idempotency_key", "reason", "diagnostic")}
    intent.update(kind="known_response_resolution", work_item_id=pack["members"][0]["work_item_id"])
    before = store.path.read_bytes()
    result = derive_work_decision(store.path, intent, policy=policy, clock=lambda: clock[0])
    assert result["request"]["work_item_id"] == intent["work_item_id"]
    assert result["API_requests"] == result["database_writes"] == 0
    assert store.path.read_bytes() == before


def test_owner_resolution_then_retry_targets_next_generation_without_rewriting_old_work(tmp_path):
    values = terminal(tmp_path)
    store, clock, _, pack, _, _, _ = values
    wid = pack["members"][0]["work_item_id"]
    original_work = copy.deepcopy(store.get_item(wid))
    before = immutable(store)
    budget = store.get_quick_scan_budget_status("pack-policy")
    resolution = store.resolve_known_response(wid, **kwargs(values))
    assert resolution["request"]["reason"] == "answer_validation_rejected"
    assert store.get_item(wid) == original_work  # Already failed: no timestamp/state rewrite.
    assert immutable(store) == before
    retry = store.record_owner_retry_decision(
        wid,
        purpose="retry_unusable_answer",
        retry_after_seconds=0,
        owner_authority=authority(),
        idempotency_key="retry-after-native-failure",
    )
    assert retry["request"]["source_resolution_id"] == resolution["decision_id"]
    assert retry["request"]["target_generation"] == original_work["generation"] + 1
    assert retry["request"]["requires_prior_delivery"] is False
    assert store.get_item(wid) == original_work
    assert immutable(store) == before
    assert store.get_quick_scan_budget_status("pack-policy") == budget
    assert store.claim(wid, lease_seconds=120) is None
    # This is a retry decision, not a new execution/admission or permission to POST.
    assert (
        len(
            store.find_work_items(
                entity_id=original_work["entity_id"], question_ids=[original_work["question_id"]]
            )[original_work["question_id"]]
        )
        == 1
    )


@pytest.mark.parametrize("fault", ["receipt", "lease", "unconfirmed_authority"])
def test_terminal_failure_still_refuses_wrong_original_proof_or_unconfirmed_caller(tmp_path, fault):
    values = terminal(tmp_path)
    store, _, members, pack, _, _, _ = values
    args = kwargs(values)
    if fault == "receipt":
        args["receipt_sha256"] = "f" * 64
    if fault == "lease":
        args["expected_lease"] = Lease("wrong-original-lease", members[0]["lease"].lease_epoch)
    if fault == "unconfirmed_authority":
        args["owner_authority"] = {"approved": True}
    before = store.path.read_bytes()
    with pytest.raises((ValueError, TypeError, RuntimeError, PermissionError)):
        store.resolve_known_response(pack["members"][0]["work_item_id"], **args)
    assert store.path.read_bytes() == before


def test_other_failed_event_is_not_deterministic_received_member_failure(tmp_path):
    values = terminal(tmp_path)
    store, clock, members, pack, _, _, _ = values
    wid = pack["members"][0]["work_item_id"]
    # Explicit synthetic bad-history fixture: later failed event is unrelated.
    # No production event/row is touched; original events are not overwritten.
    with store._transaction() as con:
        store._event(
            con,
            wid,
            "unrelated_failure",
            members[0]["lease"].lease_epoch,
            clock[0],
            attempt_id=pack["members"][0]["attempt_id"],
            old="leased",
            new="failed",
        )
    before = store.path.read_bytes()
    with pytest.raises((ValueError, RuntimeError)):
        store.resolve_known_response(wid, **kwargs(values))
    assert store.path.read_bytes() == before


def test_existing_checkpoint_stays_protected(tmp_path):
    values = fixture(tmp_path, response_status="completed", currency="USD")
    store, _, members, pack, receipt, _, context = values
    cp = save_synthetic_standard(store, members, pack, receipt, context)
    before = store.path.read_bytes()
    with pytest.raises((ValueError, RuntimeError)):
        store.resolve_known_response(pack["members"][0]["work_item_id"], **kwargs(values))
    assert store.path.read_bytes() == before
    assert (
        store.get_answer_checkpoint(pack["members"][0]["work_item_id"])["payload_sha256"]
        == cp["payload_sha256"]
    )


def test_without_owner_resolution_failed_member_cannot_retry_or_reclaim(tmp_path):
    values = terminal(tmp_path)
    store, _, _, pack, _, _, _ = values
    wid = pack["members"][0]["work_item_id"]
    before = store.path.read_bytes()
    assert store.claim(wid, lease_seconds=120) is None
    with pytest.raises((ValueError, RuntimeError)):
        store.record_owner_retry_decision(
            wid,
            purpose="retry_unusable_answer",
            retry_after_seconds=0,
            owner_authority=authority(),
            idempotency_key="no-resolution-no-retry",
        )
    assert store.path.read_bytes() == before


def test_validation_failure_cannot_be_relabeled_missing_question(tmp_path):
    values = terminal(tmp_path)
    store, _, _, pack, _, _, _ = values
    args = kwargs(values)
    args["reason"] = "missing_question_answer"
    args["diagnostic"]["code"] = "missing_question_answer"
    before = store.path.read_bytes()
    with pytest.raises((ValueError, RuntimeError)):
        store.resolve_known_response(pack["members"][0]["work_item_id"], **args)
    assert store.path.read_bytes() == before


def test_expired_original_paid_lease_saves_without_new_claim_or_budget(tmp_path, monkeypatch):
    from src.utils.quick_scan_refresh_execution import ApprovedRefreshSession

    values = fixture(tmp_path, response_status="completed", currency="USD")
    store, clock, members, pack, receipt, _, context = values
    member = pack["members"][0]
    wid = member["work_item_id"]
    original = store.get_attempt_response(member["attempt_id"])
    budget_before = store.get_quick_scan_budget_status("pack-policy")
    clock[0] += 1000
    owner = ApprovedRefreshSession.__new__(ApprovedRefreshSession)
    owner.recovery_only = True
    seen = []
    # Explicit component permission seam. Actual actor/grant checks are exercised
    # separately; the real SQLite/physical receipt/CAS path is never replaced.
    owner.assert_received_checkpoint_recovery = lambda work, attempt, **kw: seen.append(
        (work, attempt)
    )
    real_save = store.save_answer_checkpoint
    monkeypatch.setattr(
        store,
        "save_answer_checkpoint",
        lambda *a, **kw: real_save(*a, **kw, _recovery_session=owner),
    )
    cp = save_synthetic_standard(store, members, pack, receipt, context)
    assert cp["attempt_id"] == member["attempt_id"] and seen
    assert store.get_item(wid)["status"] == "result_ready"
    assert store.get_item(wid)["lease_epoch"] == members[0]["lease"].lease_epoch
    assert store.get_attempt_response(member["attempt_id"]) == original
    assert store.get_quick_scan_budget_status("pack-policy") == budget_before
    assert len(store.list_attempts(wid)) == 1
    events = store.list_events(wid)
    saved_event = next(e for e in reversed(events) if e["to_status"] == "result_ready")
    assert saved_event["from_status"] == "leased"
    assert not any(e["to_status"] == "failed" for e in events)


@pytest.mark.parametrize("journal", [False, True])
@pytest.mark.parametrize("member_fault", [None, "missing", "invalid", "semantic_invalid"])
@pytest.mark.parametrize("description_object", [False, True])
def test_captured_leased_paid_pack_recovers_original_final_json_after_expiry(
    tmp_path,
    monkeypatch,
    description_object,
    member_fault,
    journal,
    mixed_state=None,
    delivery_fault=None,
    preexisting_rejected=False,
    capture_path_fault=False,
    historical_target=False,
):
    """Real parser/capture/SQLite/physical-save chain; current actor is a seam."""
    import hashlib
    import json
    from types import SimpleNamespace as NS

    from src.utils.quick_scan_refresh_execution import ApprovedRefreshSession

    values = fixture(tmp_path, response_status="completed", currency="USD")
    store, clock, members, pack, receipt, _, context = values
    pack = store.get_question_pack(pack["pack_id"])
    real_save = store.save_answer_checkpoint
    supplied = {}

    def get_body(*args, **kwargs):
        supplied.update(kwargs)
        return {}

    monkeypatch.setattr(store, "save_answer_checkpoint", get_body)
    save_synthetic_standard(store, members, pack, receipt, context, status="scored")
    monkeypatch.setattr(store, "save_answer_checkpoint", real_save)
    template = supplied["standard_answer"]
    company_name = "Synthetic captured issuer"
    payloads = []
    entries = {}
    for member in pack["members"]:
        qid = member["question_id"]
        body = copy.deepcopy(template)
        body["question_id"] = qid
        entity = context["questions"][qid]["metadata"]["entity_id"]
        payloads.append(
            dict(
                entity_id=entity,
                company_name=company_name,
                question_id=qid,
                score=body["score"],
                description=body if description_object else json.dumps(body),
            )
        )
        entries[qid] = dict(
            kind="work", work_item_id=member["work_item_id"], item={"question_id": qid}
        )
    if member_fault == "missing":
        payloads.pop()
    elif member_fault == "invalid":
        payloads[-1]["score"] = 0
    if member_fault == "semantic_invalid":
        bad_body = payloads[-1]["description"]
        bad_body = bad_body if isinstance(bad_body, dict) else json.loads(bad_body)
        bad_body["items"] = [{"name": "invalid score-fact mixture"}]
        payloads[-1]["description"] = bad_body if description_object else json.dumps(bad_body)
    text = json.dumps({"answers": payloads})
    if journal:
        from src.utils.quick_scan_research_output import persist, visible_output_sha256
        from tests.unit import test_quick_scan_known_outcome as fixtures

        visible = dict(content=text, tool_calls=[], finish_reason="stop")
        original_receipt = fixtures._receipt

        def anchored_receipt(**kwargs):
            return {
                **original_receipt(**kwargs),
                "research_visible_output_sha256": visible_output_sha256(visible),
            }

        monkeypatch.setattr(fixtures, "_receipt", anchored_receipt)
        journal_root = tmp_path / "journal"
        journal_root.mkdir()
        store, clock, members, pack, receipt, _, context = fixture(
            journal_root, response_status="completed", currency="USD"
        )
        pack = store.get_question_pack(pack["pack_id"])
        entries = {
            m["question_id"]: dict(
                kind="work", work_item_id=m["work_item_id"], item={"question_id": m["question_id"]}
            )
            for m in pack["members"]
        }
        with store._transaction() as connection:
            persist(store, connection, pack["budget_attempt_id"], visible)
        from src.utils.quick_scan_work_store import QuickScanWorkStore

        store = QuickScanWorkStore(store.path, clock=lambda: clock[0])
    original = store.get_attempt_response(pack["members"][0]["attempt_id"])
    capture = dict(
        record_kind="final_answer_diagnostic",
        final_answer_retained=True,
        final_answer=text,
        final_answer_sha256=hashlib.sha256(text.encode()).hexdigest(),
        final_answer_bytes=len(text.encode()),
        response_sha256=original["response_sha256"],
        original_transport=dict(
            question_pack_id=pack["pack_id"],
            execution_attempt_id=pack["execution_attempt_id"],
            physical_budget_attempt_id=pack["budget_attempt_id"],
            members=[
                dict(
                    question_id=m["question_id"],
                    work_item_id=m["work_item_id"],
                    work_attempt_id=m["attempt_id"],
                )
                for m in pack["members"]
            ],
        ),
    )
    capture.update(
        {
            k: receipt.get(v)
            for k, v in (
                ("provider", "provider"),
                ("actual_model", "actual_model"),
                ("response_id", "response_id"),
                ("response_status", "response_status"),
            )
        }
    )
    directory = tmp_path / "captured"
    directory.mkdir()
    (directory / ("a" * 64 + ".final.json")).write_text(json.dumps(capture), encoding="utf8")
    owner = ApprovedRefreshSession.__new__(ApprovedRefreshSession)
    owner.recovery_only = True
    extra_ids = []
    extra_original = []
    extra_statuses = {}
    if mixed_state:
        candidates = [q for q in context["questions"] if q not in entries][:2]
        extra_members = []
        for extra_qid in candidates:
            row = context["questions"][extra_qid]
            meta = row["metadata"]
            extra = store.create_or_attach(
                entity_id=meta["entity_id"],
                question_id=extra_qid,
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
            wid = extra["work_item_id"]
            extra_ids.append(wid)
            if mixed_state != "pending":
                lease = store.claim(wid, lease_seconds=120)
                store.freeze_observation_context_before_send(wid, lease, context)
                extra_members.append(dict(work_item_id=wid, lease=lease))
            entries[extra_qid] = dict(
                kind="work", work_item_id=wid, item={"question_id": extra_qid}
            )
        if mixed_state in {
            "unknown",
            "in_flight",
            "missing_body",
            "corrupt_body",
            "binding_conflict",
            "store_conflict",
            "lease_conflict",
        }:
            from tests.unit.test_quick_scan_question_pack_store import (
                _mark,
                _prepare,
                _receipt,
            )

            extra_pack = _prepare(store, extra_members, execution_attempt_id="HTTP_PACK_EXTRA")
            from src.utils import quick_scan_subject_journal as subject
            from tests.unit.test_c06_subject_send import receipt as subject_receipt

            with store._transaction() as con:
                for extra_member in extra_pack["members"]:
                    item = store._item(con, extra_member["work_item_id"])
                    attempt = con.execute(
                        "SELECT * FROM attempt WHERE attempt_id=?", (extra_member["attempt_id"],)
                    ).fetchone()
                    request = subject.dispatch_request(
                        item,
                        attempt,
                        subject.bound_context(con, item),
                        execution_attempt_id=extra_pack["execution_attempt_id"],
                        provider_actual="mimo",
                        provider_prompt_sha256=extra_pack["provider_prompt_sha256"],
                    )
                    lease = next(
                        m["lease"]
                        for m in extra_members
                        if m["work_item_id"] == extra_member["work_item_id"]
                    )
                    store.persist_subject_dispatch_receipt(
                        extra_member["work_item_id"],
                        lease,
                        extra_member["attempt_id"],
                        subject_receipt(request),
                        expected_request=request,
                    )
            _mark(store, extra_pack)
            if mixed_state != "in_flight":
                unknown = mixed_state == "unknown"
                extra_receipt = _receipt(
                    attempt_id="HTTP_PACK_EXTRA",
                    response_id="resp-extra",
                    response_sha256="b" * 64,
                    source_urls=receipt["source_urls"],
                    completed_at=receipt["completed_at"],
                )
                if unknown:
                    extra_receipt.update(
                        response_sha256=None,
                        response_id=None,
                        actual_model=None,
                        http_status_code=None,
                        search_status="unverified",
                        response_status="unknown",
                    )
                store.record_question_pack_outcome(
                    extra_pack["pack_id"],
                    outcome="unknown" if unknown else "response_available",
                    execution_receipt=extra_receipt,
                    http_status_code=extra_receipt["http_status_code"],
                    actual_cost=None if unknown else 0.02,
                    cost_source_ref=None if unknown else "synthetic-cost",
                )
                if mixed_state == "corrupt_body":
                    bad_capture = copy.deepcopy(capture)
                    bad_capture.update(response_sha256="b" * 64)
                    bad_capture["original_transport"].update(
                        question_pack_id=extra_pack["pack_id"],
                        execution_attempt_id="HTTP_PACK_EXTRA",
                        physical_budget_attempt_id=extra_pack["budget_attempt_id"],
                    )
                    bad_capture["final_answer_sha256"] = "0" * 64
                    (directory / ("b" * 64 + ".final.json")).write_text(
                        json.dumps(bad_capture), encoding="utf8"
                    )
        extra_original = [store.list_attempts(wid) for wid in extra_ids]
        extra_statuses = {wid: store.get_item(wid) for wid in extra_ids}
    if mixed_state == "only_unsent":
        entries = {
            qid: entry for qid, entry in entries.items() if entry["work_item_id"] in extra_ids
        }
    owner.entries = entries
    owner.service = NS(store=store)
    from tests.unit.test_quick_scan_observation_context import MANIFEST

    owner.bundle = {
        "authority": {},
        "identity": {"entity_id": entity},
        "manifest": copy.deepcopy(MANIFEST),
    }
    owner.input_raw = {
        "identity.json": json.dumps({"payload": {"canonical_name": company_name}}).encode()
    }
    owner.check = lambda: None

    def assert_original(work_id, *args, **kwargs):
        if mixed_state == "member_conflict" and work_id == pack["members"][1]["work_item_id"]:
            from src.utils.quick_scan_work_store import LeaseFencedError

            raise LeaseFencedError("synthetic middle member superseded")
        if work_id in extra_ids:
            if mixed_state == "binding_conflict":
                from src.utils.quick_scan_refresh_admission import AdmissionRejected

                raise AdmissionRejected("received_checkpoint_original_binding_required")
            if mixed_state == "store_conflict":
                from src.utils.quick_scan_work_store import WorkConflictError

                raise WorkConflictError("original synthetic item binding differs")
            if mixed_state == "lease_conflict":
                from src.utils.quick_scan_work_store import LeaseFencedError

                raise LeaseFencedError("original synthetic lease was superseded")

    owner.assert_received_checkpoint_recovery = assert_original
    if delivery_fault or preexisting_rejected or historical_target:
        from tests.unit.test_q10_delivery import _c06_authority

        owner.bundle["authority"] = _c06_authority()
    if preexisting_rejected:
        from src.utils.quick_scan_work_store import WorkConflictError

        original_save = store.save_answer_checkpoint
        middle = pack["members"][1]["work_item_id"]

        def save_middle_only(wid, *args, **kwargs):
            if wid != middle:
                raise WorkConflictError("synthetic first-pass member not yet saved")
            return original_save(wid, *args, **kwargs)

        monkeypatch.setattr(store, "save_answer_checkpoint", save_middle_only)
        owner.recover_received_checkpoints(
            capture_policy=None if journal else {"directory": directory}
        )
        monkeypatch.setattr(store, "save_answer_checkpoint", original_save)
        existing_middle = store.get_answer_checkpoint(middle)
        assert existing_middle is not None

        def reject_existing_middle(wid, *args, **kwargs):
            if wid == middle:
                from src.utils.quick_scan_refresh_admission import AdmissionRejected

                raise AdmissionRejected("received_checkpoint_registered_work_superseded")
            return assert_original(wid, *args, **kwargs)

        owner.assert_received_checkpoint_recovery = reject_existing_middle
    seal_calls = []
    if delivery_fault:
        import sqlite3

        from src.utils import quick_scan_delivery_seal as sealing
        from src.utils.quick_scan_work_store import LeaseFencedError, WorkConflictError

        original_seal = sealing.seal_result_delivery

        def fail_middle_seal(store_, wid, **kwargs):
            seal_calls.append(wid)
            if wid == pack["members"][1]["work_item_id"]:
                if delivery_fault == "permission":
                    raise PermissionError("synthetic global actor revoked")
                if delivery_fault == "database":
                    raise sqlite3.DatabaseError("synthetic database corruption")
                if delivery_fault == "after_success_conflict":
                    original_seal(store_, wid, **kwargs)
                    raise WorkConflictError("synthetic diagnostic after original seal committed")
                if delivery_fault == "work_conflict":
                    raise WorkConflictError("synthetic local delivery race")
                if delivery_fault == "lease_fenced":
                    raise LeaseFencedError("synthetic local delivery fence")
                if delivery_fault == "blocked":
                    return {"work_item_id": wid, "action": "blocked", "state": "blocked"}
                if delivery_fault == "in_flight":
                    return {
                        "work_item_id": wid,
                        "action": "left_in_flight",
                        "state": "send_uncertain",
                    }
            return original_seal(store_, wid, **kwargs)

        monkeypatch.setattr(sealing, "seal_result_delivery", fail_middle_seal)
    clock[0] += 1000
    original_responses = [store.get_attempt_response(m["attempt_id"]) for m in pack["members"]]
    budget = store.get_quick_scan_budget_status("pack-policy")
    policy = None if journal else {"directory": directory}
    if capture_path_fault:
        bad_capture = tmp_path / "obsolete-capture-file"
        bad_capture.write_text("synthetic obsolete optional diagnostic", encoding="utf8")
        policy = {"directory": bad_capture}
    if historical_target:
        first = owner.recover_received_checkpoints(capture_policy=policy)
        assert first["status"] == "completed"
        before_cp = [store.get_answer_checkpoint(m["work_item_id"]) for m in pack["members"]]
        assert all(
            store.get_result_delivery(m["work_item_id"])["state"] == "ready"
            for m in pack["members"]
        )
        # A complete target cannot be assembled from this intentionally absent
        # current context. Native sealing preserves the old compact head.
        owner.bundle["authority"] = {"schema_version": "2.0.0"}
        second = owner.recover_received_checkpoints(capture_policy=policy)
        assert second["status"] == "partial"
        assert second["remaining_work_item_ids"] == []
        assert set(second["delivery_pending_work_item_ids"]) == {
            m["work_item_id"] for m in pack["members"]
        }
        assert all(row["action"] == "left_in_flight" for row in second["deliveries"])
        assert [
            store.get_answer_checkpoint(m["work_item_id"]) for m in pack["members"]
        ] == before_cp
        assert store.get_quick_scan_budget_status("pack-policy") == budget
        return
    if capture_path_fault and not journal:
        rejected = owner.recover_received_checkpoints(capture_policy=policy)
        assert rejected["recovered_work_item_ids"] == []
        assert rejected["status"] == "partial"
        assert all(store.get_answer_checkpoint(m["work_item_id"]) is None for m in pack["members"])
        assert (
            rejected["recovery_errors"][0]["reason"] == "received_checkpoint_capture_path_invalid"
        )
        assert store.get_quick_scan_budget_status("pack-policy") == budget
        policy = {"directory": directory}
    if delivery_fault in {"permission", "database"}:
        error = PermissionError if delivery_fault == "permission" else sqlite3.DatabaseError
        with pytest.raises(error):
            owner.recover_received_checkpoints(capture_policy=policy)
        assert store.get_quick_scan_budget_status("pack-policy") == budget
        return
    report = owner.recover_received_checkpoints(capture_policy=policy)
    valid_members = (
        []
        if mixed_state == "only_unsent"
        else (pack["members"][:-1] if member_fault else pack["members"])
    )
    if mixed_state == "member_conflict":
        valid_members = [pack["members"][0], pack["members"][2]]
        assert store.get_answer_checkpoint(pack["members"][1]["work_item_id"]) is None
        assert report["recovery_errors"]
    if preexisting_rejected:
        valid_members = [pack["members"][0], pack["members"][2]]
        assert store.get_answer_checkpoint(middle) == existing_middle
        assert middle not in report["attached_work_item_ids"]
        assert middle in report["remaining_work_item_ids"]
    if delivery_fault:
        assert seal_calls == [m["work_item_id"] for m in pack["members"]]
        assert report["delivery_pending_work_item_ids"] == (
            []
            if delivery_fault == "after_success_conflict"
            else [pack["members"][1]["work_item_id"]]
        )
        assert report["remaining_work_item_ids"] == []
        for member in (pack["members"][0], pack["members"][2]):
            assert store.get_result_delivery(member["work_item_id"])["state"] == "ready"
    assert set(report["recovered_work_item_ids"]) == {m["work_item_id"] for m in valid_members}
    assert report["status"] == (
        "partial"
        if member_fault
        or mixed_state
        or preexisting_rejected
        or report["delivery_pending_work_item_ids"]
        else "completed"
    )
    assert set(extra_ids) <= set(report["remaining_work_item_ids"])
    if mixed_state in {
        "missing_body",
        "corrupt_body",
        "binding_conflict",
        "store_conflict",
        "lease_conflict",
    }:
        assert report["recovery_errors"]
    if mixed_state:
        assert [store.list_attempts(wid) for wid in extra_ids] == extra_original
        assert {wid: store.get_item(wid) for wid in extra_ids} == extra_statuses
        assert all(store.get_answer_checkpoint(wid) is None for wid in extra_ids)
    again = owner.recover_received_checkpoints(capture_policy=policy)
    assert again["recovered_work_item_ids"] == []
    assert set(again["attached_work_item_ids"]) == {m["work_item_id"] for m in valid_members}
    assert report["API_requests"] == report["new_attempts"] == report["model_calls"] == 0
    assert report["dispatch_started"] is False
    assert [
        store.get_attempt_response(m["attempt_id"]) for m in pack["members"]
    ] == original_responses
    assert store.get_quick_scan_budget_status("pack-policy") == budget
    for member in valid_members:
        assert (
            store.get_answer_checkpoint(member["work_item_id"])["attempt_id"]
            == member["attempt_id"]
        )
        assert len(store.list_attempts(member["work_item_id"])) == 1


@pytest.mark.parametrize(
    "mixed_state",
    [
        "pending",
        "claimed_unsent",
        "unknown",
        "in_flight",
        "missing_body",
        "corrupt_body",
        "binding_conflict",
        "only_unsent",
        "store_conflict",
        "lease_conflict",
        "member_conflict",
    ],
)
@pytest.mark.parametrize("journal", [False, True])
def test_mixed_scope_recovers_paid_members_without_touching_unavailable_work(
    tmp_path, monkeypatch, mixed_state, journal
):
    test_captured_leased_paid_pack_recovers_original_final_json_after_expiry(
        tmp_path,
        monkeypatch,
        False,
        None,
        journal,
        mixed_state=mixed_state,
    )


@pytest.mark.parametrize("journal", [False, True])
@pytest.mark.parametrize(
    "delivery_fault",
    [
        "work_conflict",
        "lease_fenced",
        "blocked",
        "in_flight",
        "permission",
        "database",
        "after_success_conflict",
    ],
)
def test_paid_recovery_seals_independent_members_and_reports_saved_pending_separately(
    tmp_path, monkeypatch, journal, delivery_fault
):
    test_captured_leased_paid_pack_recovers_original_final_json_after_expiry(
        tmp_path, monkeypatch, False, None, journal, delivery_fault=delivery_fault
    )


@pytest.mark.parametrize("journal", [False, True])
def test_existing_checkpoint_rejected_during_collection_is_not_reattached_by_its_pack(
    tmp_path, monkeypatch, journal
):
    test_captured_leased_paid_pack_recovers_original_final_json_after_expiry(
        tmp_path, monkeypatch, False, None, journal, preexisting_rejected=True
    )


@pytest.mark.parametrize("journal", [False, True])
def test_valid_paid_journal_does_not_depend_on_unusable_old_capture_path(
    tmp_path, monkeypatch, journal
):
    test_captured_leased_paid_pack_recovers_original_final_json_after_expiry(
        tmp_path, monkeypatch, False, None, journal, capture_path_fault=True
    )


@pytest.mark.parametrize("journal", [False, True])
def test_old_ready_package_is_not_current_target_completion(tmp_path, monkeypatch, journal):
    test_captured_leased_paid_pack_recovers_original_final_json_after_expiry(
        tmp_path, monkeypatch, False, None, journal, historical_target=True
    )
