"""Synthetic owner/SQLite boundaries; all owner CLI and HTTP calls are replaced."""

import copy
import sqlite3
from collections import Counter
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.core.models import Question
from src.core.qa_engine import QAEngine
from src.runners.llm_runner import QuickScanWorkLifecycle
from src.utils import quick_scan_subject_owner
from src.utils import quick_scan_work_transport as transport
from src.utils.quick_scan_owner_refresh import OwnerRefreshRejected, OwnerRefreshSession
from src.utils.quick_scan_refresh_consent import RefreshConsentClient
from src.utils.quick_scan_refresh_execution import ApprovedRefreshSession
from src.utils.quick_scan_result_outbox import canonical_sha256
from src.utils.quick_scan_work_store import (
    Lease,
    LeaseFencedError,
    QuickScanWorkStore,
    quick_scan_receipt_sha256,
)
from tests.unit.test_quick_scan_question_pack_transport import (
    MANIFEST,
    _begin,
    _owner_receipt,
    _policy,
    _response,
    _v2_context,
)


@pytest.fixture
def session(tmp_path, monkeypatch):
    clock = [1800000000.0]
    store = QuickScanWorkStore(tmp_path / "isolated.sqlite", clock=lambda: clock[0])
    context = _v2_context()
    qids = ("IQS_01", "IQS_02", "IQS_04")
    questions = {
        qid: Question(
            text=context["questions"][qid]["subject_binding"]["question_contract"]["prompt"],
            question_id=qid,
        )
        for qid in qids
    }
    identity = dict(
        entity_id="ENT_CONTEXT_FIXTURE",
        identity_revision=1,
        source_binding_version=1,
        identity_state="verified",
        source_binding_ref="BND_FIXTURE",
        source_binding_refs=["BND_FIXTURE"],
        identity_snapshot_sha256=context["identity_snapshot_sha256"],
    )
    scopes = {qid: dict(scope="entity", scope_id=identity["entity_id"]) for qid in qids}
    counts = Counter()
    state = {"revoked": False}
    current = {
        "anchor_version": 1,
        "expected_decision_id": "route_" + "a" * 64,
        "snapshot": {
            "subject_binding": {
                "subject": context["questions"][qids[0]]["subject_binding"]["subject_ref"],
            }
        },
    }

    def anchor(*_args):
        counts["anchor"] += 1
        if state["revoked"]:
            raise OwnerRefreshRejected("fixture_revoked")

    native = OwnerRefreshSession(
        SimpleNamespace(config={}, _environment={}, assert_current=anchor),
        current,
        dict(
            fields=[
                dict(
                    field_id=qid,
                    decision="dispatch_new_work",
                    generation=1,
                    request_identity_key="fixture-" + qid,
                )
                for qid in qids
            ],
            subject_key="fixture-subject",
            perimeter_sha256="d" * 64,
            decision_id=current["expected_decision_id"],
            anchor_version=1,
            manifest_raw_sha256=context["manifest_file_sha256"],
            provider="mimo",
            model="mimo-v2.6-flash",
            information_cutoff="2026-09-22",
        ),
        scopes,
        {},
        identity,
        store,
        observation_context=context,
    )
    specs, items, entries = [], [], {}
    for qid in qids:
        binding = native.binding(qid)
        spec = dict(
            **identity,
            question_id=qid,
            generation=1,
            **scopes[qid],
            question_fingerprint=context["questions"][qid]["work_prompt_sha256"],
            routing_fingerprint=canonical_sha256(
                dict(
                    subject_key=binding["subject_key"],
                    request_identity_key=binding["request_identity_key"],
                )
            ),
            owner_refresh_binding=binding,
        )
        item = store.create_or_attach(**spec, run_id="ORIGINAL", scan_id="ORIGINAL")
        specs.append(spec)
        items.append(item)
        entries[qid] = dict(
            kind="work",
            work_item_id=item["work_item_id"],
            specification=spec,
            item=dict(
                question_id=qid,
                subject_ref=context["questions"][qid]["subject_binding"]["subject_ref"],
            ),
        )
        native.fields[qid]["decision"] = "resume_existing_work"
        native.projection[qid] = dict(
            work_item_id=item["work_item_id"], generation=1, status="pending"
        )
    record = dict(
        inputs=dict(action=dict(confirmation_id="rc2_" + "a" * 64), specifications=specs),
        receipt=dict(items=items),
    )
    proof = dict(
        request=dict(protocol="iqs.refresh_request/1.0.0"),
        confirmation=dict(confirmation_id="rc2_" + "a" * 64),
        preview={},
        owner_policy={},
    )

    def inspect(_confirmation, *, mode):
        counts[mode] += 1
        if state["revoked"] and mode == "current_permissions":
            raise OwnerRefreshRejected("fixture_revoked")
        return copy.deepcopy(proof)

    service = SimpleNamespace(
        store=store,
        budget=_policy(),
        bridge=SimpleNamespace(inspect=inspect),
        _record=lambda _confirmation: copy.deepcopy(record),
        _inputs_unchanged=Mock(),
    )
    bundle = dict(manifest=MANIFEST, identity=identity, questions=questions, authority={})
    approved = ApprovedRefreshSession(
        service,
        proof,
        {},
        bundle,
        record,
        entries,
        native,
        {},
        {},
    )

    def register(_config, _current, request, **_kwargs):
        counts["register"] += 1
        return _owner_receipt(request, clock[0])

    monkeypatch.setattr(quick_scan_subject_owner, "register", register)
    lifecycle = QuickScanWorkLifecycle(
        store,
        entity_id=identity["entity_id"],
        run_id="RUN_FIXTURE",
        scan_id="SCAN_FIXTURE",
        identity=identity,
        transport_managed=True,
        owner_refresh_session=approved,
        observation_context=context,
        scope_by_question=scopes,
        route_id="pack-route",
        provider_name="mimo-fixture",
        model_requested="mimo-v2.6-flash",
        budget_policy=_policy(),
        budget_route=_policy()["routes"][0],
    )
    return SimpleNamespace(
        store=store,
        owner=approved,
        lifecycle=lifecycle,
        questions=questions,
        counts=counts,
        state=state,
        clock=clock,
    )


@contextmanager
def _start(session, qids, **changes):
    questions = [session.questions[qid] for qid in qids]
    lifecycle = session.lifecycle
    lifecycle.assert_question_pack_ready(questions)
    handles = []
    for question in questions:
        assert lifecycle.hydrate_question(question) is None
        handle = lifecycle.before_question(question)
        assert handle["claimed"] is True, handle
        handles.append(handle)
    with lifecycle.dispatch_question_pack_context(handles), transport.bind_quick_scan_budget(
        session.store,
        _policy(),
        cost_resolver=lambda _: {
            "actual_cost": 0.03,
            "pricing_ref": "fixture-price",
            "source_ref": "fixture-rate",
        },
    ):
        yield _begin(**changes)


@contextmanager
def _single(session, *, handle=None, **changes):
    handle = handle or session.lifecycle.before_question(session.questions["IQS_01"])
    assert handle["claimed"] is True, handle
    with session.lifecycle.dispatch_context(handle), transport.bind_quick_scan_route(
        route_id="pack-route",
        provider="mimo-fixture",
        model_requested="mimo-v2.6-flash",
        quota_group="pack-quota",
    ), transport.bind_quick_scan_budget(
        session.store,
        _policy(),
        cost_resolver=lambda _: {
            "actual_cost": 0.03,
            "pricing_ref": "fixture-price",
            "source_ref": "fixture-rate",
        },
    ):
        yield _begin(**changes)


def test_two_question_pack_uses_two_registrations_and_one_live_check(session):
    with _start(session, ("IQS_01", "IQS_02")) as handle:
        before_consume = session.counts.copy()
        handle.consume_for_post()
    assert session.counts == Counter(current_permissions=1, register=2)
    assert session.counts == before_consume


def test_revocation_before_intent_releases_unsent_without_budget_or_unknown(session, monkeypatch):
    original = quick_scan_subject_owner.register

    def revoke(*args, **kwargs):
        value = original(*args, **kwargs)
        if session.counts["register"] == 2:
            session.state["revoked"] = True
        return value

    monkeypatch.setattr(quick_scan_subject_owner, "register", revoke)
    with pytest.raises(transport.QuickScanWorkPersistenceError) as caught:
        with _start(session, ("IQS_01", "IQS_02")):
            pytest.fail("revoked owner cannot obtain a send permit")
    assert caught.value.diagnostic["released_unsent"] is True
    for qid in ("IQS_01", "IQS_02"):
        wid = session.owner.entries[qid]["work_item_id"]
        assert session.store.get_item(wid)["status"] == "pending"
        assert session.store.list_attempts(wid)[-1]["phase"] == "abandoned_unsent"
    assert session.store.get_quick_scan_budget_status("pack-transport")["requests"] == 0


def test_same_native_permit_survives_later_revocation_but_next_send_rechecks(session):
    with _start(session, ("IQS_01", "IQS_02")) as handle:
        session.state["revoked"] = True
        handle.consume_for_post()
        # A known response is persisted locally even though future sends are revoked.
        handle.record_response(http_status_code=200, request_id="req-fixture", receipt=_response())
    before = session.store.get_attempt_response(handle.attempt_id)
    with pytest.raises(transport.QuickScanWorkPersistenceError) as caught:
        with _start(
            session, ("IQS_04",), execution_attempt_id="22222222-2222-4222-8222-222222222222"
        ):
            pytest.fail("the next send must recheck revocation")
    assert caught.value.diagnostic["released_unsent"] is True
    assert session.counts == Counter(current_permissions=2, register=3)
    assert session.store.get_attempt_response(handle.attempt_id) == before
    assert session.store.get_quick_scan_budget_status("pack-transport")["requests"] == 1


def test_snapshot_and_paid_binding_reads_never_recheck_live_permission(session, monkeypatch):
    with _start(session, ("IQS_01", "IQS_02")) as handle:
        handle.consume_for_post()
        handle.record_response(http_status_code=200, request_id="req-fixture", receipt=_response())
    session.state["revoked"] = True
    before = session.counts.copy()
    owner_port = session.lifecycle._owner_refresh
    for _ in range(3):
        owner_port.snapshot()
        owner_port.existing_work("IQS_01")
    # The separate pure binding proof is still required, not a new-send check.
    proof = Mock()
    monkeypatch.setattr(session.owner, "_assert_original_registered_binding", proof)
    member = session.store.get_question_pack(handle.question_pack_id)["members"][0]
    session.owner.assert_received_checkpoint_recovery(
        member["work_item_id"],
        member["attempt_id"],
        store=session.store,
    )
    assert proof.called and session.counts == before


def test_paid_recovery_does_not_require_future_send_permission(session):
    with _start(session, ("IQS_01", "IQS_02")) as handle:
        handle.consume_for_post()
        handle.record_response(http_status_code=200, request_id="req-fixture", receipt=_response())
    session.state["revoked"] = True
    before = session.counts.copy()
    report = session.owner.recover_received_checkpoints()
    assert report["API_requests"] == 0 and report["dispatch_started"] is False
    assert session.counts == before


def test_identical_frozen_contract_is_validated_once_but_changed_bytes_are_not_reused(tmp_path):
    client = object.__new__(RefreshConsentClient)
    client.owner = SimpleNamespace(config={"iqs_code_root": str(tmp_path)})
    calls = []

    def run(*_args, input_bytes, **_kwargs):
        import hashlib

        calls.append(input_bytes)
        return dict(
            protocol="iqs.refresh_validation/1.0.0",
            status="validated",
            operation="frozen-record",
            input_sha256=hashlib.sha256(input_bytes).hexdigest(),
            API_requests=0,
            database_writes=0,
            permission_authenticated=False,
        )

    client._run_read = run
    frozen = dict(request={"original": 1}, preview={}, owner_state={})
    client._validate_frozen_record(frozen)
    client._validate_frozen_record(copy.deepcopy(frozen))
    assert len(calls) == 1
    frozen["request"]["original"] = 2
    client._validate_frozen_record(frozen)
    assert len(calls) == 2


@pytest.mark.parametrize("failure", ["permission", "ordinary", "busy"])
def test_single_preintent_failure_returns_exact_original_work_without_fee(
    session, monkeypatch, failure
):
    store = session.store
    wid = session.owner.entries["IQS_01"]["work_item_id"]
    original = store.mark_send_intent
    if failure == "permission":
        session.state["revoked"] = True
    else:
        error = RuntimeError("synthetic pre-intent failure")
        if failure == "busy":
            error = sqlite3.OperationalError("synthetic busy")
            error.sqlite_errorcode, error.sqlite_errorname = (
                getattr(sqlite3, "SQLITE_BUSY", 5),
                "SQLITE_BUSY",
            )

        def fail(*_args, **_kwargs):
            raise error

        monkeypatch.setattr(store, "mark_send_intent", fail)
    with pytest.raises(transport.QuickScanWorkPersistenceError) as caught:
        with _single(session):
            pytest.fail("failed admission must not return a permit")
    attempt = store.list_attempts(wid)[-1]
    assert store.get_item(wid)["status"] == "pending"
    assert attempt["phase"] == "abandoned_unsent" and attempt["send_intent_at"] is None
    assert store.get_subject_dispatch_receipt(attempt["attempt_id"]) is not None
    assert caught.value.diagnostic["released_unsent"] is True
    assert store.get_quick_scan_budget_status("pack-transport")["requests"] == 0
    old_lease = Lease(attempt["lease_token"], attempt["lease_epoch"])
    with pytest.raises(LeaseFencedError):
        store.release_unsent(wid, old_lease)
    fresh = store.claim(wid, lease_seconds=60)
    assert fresh.lease_epoch == old_lease.lease_epoch + 1
    session.state["revoked"] = False
    monkeypatch.setattr(store, "mark_send_intent", original)
    with _single(
        session,
        handle=dict(claimed=True, work_item_id=wid, lease=fresh),
        execution_attempt_id="22222222-2222-4222-8222-222222222222",
    ) as permit:
        assert permit.attempt_id != attempt["attempt_id"]
    assert store.list_attempts(wid)[-1]["phase"] == "send_intent"
    assert store.get_quick_scan_budget_status("pack-transport")["requests"] == 1


@pytest.mark.parametrize("outcome", ["send_intent", "response_available", "unknown"])
def test_single_committed_native_evidence_is_never_released(session, monkeypatch, outcome):
    store = session.store
    wid = session.owner.entries["IQS_01"]["work_item_id"]
    original = store.mark_send_intent

    def fail(work_id, lease, attempt_id, **kwargs):
        original(work_id, lease, attempt_id, **kwargs)
        if outcome == "response_available":
            receipt = _response()
            store.record_attempt_outcome(
                work_id,
                lease,
                attempt_id,
                outcome=outcome,
                http_status_code=200,
                receipt_sha256=quick_scan_receipt_sha256(receipt),
                execution_receipt=receipt,
                actual_cost=0.03,
                cost_source_ref="fixture-rate",
            )
        elif outcome == "unknown":
            store.record_attempt_outcome(work_id, lease, attempt_id, outcome="unknown")
        raise RuntimeError("synthetic post-intent failure")

    monkeypatch.setattr(store, "mark_send_intent", fail)
    with pytest.raises(transport.QuickScanWorkPersistenceError) as caught:
        with _single(session):
            pytest.fail("failed admission must not return another permit")
    assert store.list_attempts(wid)[-1]["phase"] == (
        "uncertain" if outcome == "unknown" else outcome
    )
    assert caught.value.diagnostic["released_unsent"] is False
    assert store.claim(wid, lease_seconds=60) is None
    assert store.get_quick_scan_budget_status("pack-transport")["requests"] == 1


@pytest.mark.parametrize(
    "failure",
    [
        "ordinary",
        "uncertain",
        "implicit_context",
        "from_none",
        "both_links",
        "unreadable_diagnostic",
    ],
)
def test_provider_failure_before_begin_releases_only_known_unsent_work(session, failure):
    cause = transport.QuickScanWorkUncertainError(
        "original-physical",
        {"request_id": "original-request", "search_status": "unverified"},
    )
    error = RuntimeError("synthetic render failure")
    if failure == "uncertain":
        error.__cause__ = cause
    elif failure == "unreadable_diagnostic":

        class UnreadableDiagnostic(RuntimeError):
            @property
            def diagnostic(self):
                raise RuntimeError("synthetic optional diagnostic property failed")

        error = UnreadableDiagnostic("synthetic render failure")

    def search(*_args, **_kwargs):
        if failure in {"implicit_context", "from_none", "both_links"}:
            try:
                raise cause
            except transport.QuickScanWorkUncertainError:
                if failure == "from_none":
                    raise error from None
                if failure == "both_links":
                    raise error from ValueError("synthetic explicit cause")
                raise error
        raise error

    provider = SimpleNamespace(
        require_search=True,
        search_question_pack=search,
        get_provider_name=lambda: "synthetic",
    )
    engine = QAEngine(provider, progress_reporter=Mock(), work_item_lifecycle=session.lifecycle)
    batch = engine.process_questions(
        [session.questions["IQS_01"], session.questions["IQS_02"]],
        question_pack_size=2,
    )
    for result in batch.results:
        wid = session.owner.entries[result.question.question_id]["work_item_id"]
        known_uncertain = failure in {"uncertain", "implicit_context", "from_none", "both_links"}
        assert session.store.get_item(wid)["status"] == ("leased" if known_uncertain else "pending")
        assert session.store.list_attempts(wid) == []
        if known_uncertain:
            assert result.answer.metadata["execution"]["attempt_id"] == "original-physical"
    assert (
        session.counts == Counter()
    )  # Rendering failed before subject registration or fee checks.


def test_legacy_lifecycle_still_records_unknown_for_typed_transport_failure(tmp_path):
    from tests.unit.test_quick_scan_work_store import _prepared
    from tests.unit.test_quick_scan_work_transport import _create_claimed_item

    store, wid, lease = _create_claimed_item(tmp_path)
    attempt = _prepared(store, wid, lease)
    store.mark_send_intent(wid, lease, attempt["attempt_id"])
    legacy = QuickScanWorkLifecycle(
        store,
        entity_id="ENT_FIXTURE_CO",
        run_id="LEGACY",
        scan_id="LEGACY",
        identity={},
    )
    legacy.after_question_failed(
        dict(work_item_id=wid, lease=lease, attempt_id=attempt["attempt_id"]),
        transport.QuickScanWorkUncertainError("legacy-physical"),
    )
    assert store.get_item(wid)["status"] == "uncertain"


def test_public_exception_chain_is_bounded_and_deduplicates_both_links():
    from src.interfaces.transport_outcome import iter_exception_chain

    errors = [RuntimeError("synthetic") for _ in range(30)]
    for left, right in zip(errors, errors[1:]):
        left.__cause__ = right
    errors[-1].__context__ = errors[0]
    errors[0].__context__ = errors[1]
    found = list(iter_exception_chain(errors[0]))
    assert len(found) == 16 and len({id(error) for error in found}) == 16
