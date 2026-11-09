"""Ordered shared dispatch coordination; synthetic ports, no financial claims."""

from types import SimpleNamespace

import pytest

from src.utils.quick_scan_shared_routes import next_route_questions, routed_lifecycle


def targets():
    return [
        dict(
            provider="mimo",
            model="model-a",
            route_id="a",
            provider_config_ref="account-a",
            quota_group="q",
        ),
        dict(
            provider="mimo",
            model="model-b",
            route_id="b",
            provider_config_ref="account-b",
            quota_group="q",
        ),
    ]


def row(qid, phase=None, route="a", category="quota_exhausted", completed=False):
    return dict(
        question_id=qid,
        checkpoint_id="CP" if completed else None,
        latest_attempt=(
            None
            if phase is None
            else dict(
                phase=phase,
                route_id=route,
                provider="account-" + route,
                model_requested="model-" + route,
                failure_category=category,
                settled=True,
            )
        ),
    )


def test_only_explicit_known_refusal_advances_and_completed_slots_are_excluded():
    rows = [row("Q1", completed=True), row("Q2", "confirmed_failure"), row("Q3")]
    assert next_route_questions(rows, targets(), 0) == ["Q3"]
    assert next_route_questions(rows, targets(), 1) == ["Q2", "Q3"]


@pytest.mark.parametrize(
    "phase,category,settled",
    [
        ("uncertain", "quota_exhausted", True),
        ("send_intent", "quota_exhausted", True),
        ("response_available", None, True),
        ("confirmed_failure", "quota_exhausted", False),
        ("confirmed_failure", None, True),
        ("prepared", None, True),
    ],
)
def test_received_unknown_or_unsettled_work_never_falls_through(phase, category, settled):
    r = row("Q1", phase, category=category)
    r["latest_attempt"]["settled"] = settled
    with pytest.raises(ValueError, match="unresolved"):
        next_route_questions([r], targets(), 1)


def test_failed_backup_is_not_retried_on_resume_and_unlisted_route_is_not_guessed():
    assert next_route_questions([row("Q1", "confirmed_failure", route="b")], targets(), 1) == []
    r = row("Q1", "confirmed_failure")
    r["latest_attempt"]["provider"] = "other-account"
    with pytest.raises(ValueError, match="route"):
        next_route_questions([r], targets(), 1)


def test_no_route_clone_from_an_unapproved_owner():
    with pytest.raises(ValueError, match="original"):
        routed_lifecycle(SimpleNamespace(), {}, SimpleNamespace())


def test_native_progress_is_read_only_and_empty_before_any_send(tmp_path):
    from tests.unit.test_owner_batch_claim import prepare

    store, clock, confirmation, digest, items = prepare(tmp_path, 2)
    before = store.path.read_bytes()
    progress = store.company_scan_progress([r["work_item_id"] for r in items])
    assert progress["model_requests"] == 0
    assert len(progress["rows"]) == 2
    assert all(r["latest_attempt"] is None and r["checkpoint_id"] is None for r in progress["rows"])
    assert store.path.read_bytes() == before


def ordered_prepared(tmp_path, count=1):
    from tests.unit.test_owner_batch_claim import prepare

    def binding(value):
        return dict(
            value,
            protocol="stockqa.owner_refresh_binding/5.0.0",
            model="model-a",
            subject_context_schema="stockqa.quick_scan_observation_context/2.0.0",
            ordered_route_targets=targets(),
        )

    return prepare(tmp_path, count, binding_transform=binding)


def route_policy():
    from tests.unit.test_quick_scan_budget import _policy

    value = _policy(max_cost_per_attempt=1)
    value["routes"] = [
        dict(
            id=t["route_id"],
            provider_config_ref=t["provider_config_ref"],
            model=t["model"],
            quota_group="A",
            max_in_flight=2,
            eligible=True,
            unavailable_reason=None,
        )
        for t in targets()
    ]
    return value


def send_failure(store, handle, index=0, *, settled=True):
    from tests.unit.test_owner_batch_claim import sha

    target = targets()[index]
    wid = handle["work_item_id"]
    lease = handle["lease"]
    a = store.prepare_attempt(
        wid,
        lease,
        route_id=target["route_id"],
        provider=target["provider_config_ref"],
        model_requested=target["model"],
        request_cache_key="REQ_" + sha(wid + target["route_id"]),
        prompt_sha256=sha(target["route_id"]),
    )
    store.mark_send_intent(
        wid,
        lease,
        a["attempt_id"],
        budget_policy=route_policy(),
        budget_route=dict(
            route_id=target["route_id"],
            provider=target["provider_config_ref"],
            model_requested=target["model"],
            quota_group="A",
        ),
    )
    store.record_attempt_outcome(
        wid,
        lease,
        a["attempt_id"],
        outcome="confirmed_failure",
        http_status_code=401,
        receipt_sha256=sha("explicit401"),
        failure_category="authentication_rejected",
        actual_cost=0 if settled else None,
        cost_source_ref="scripted-refusal" if settled else None,
    )
    return a


def test_ordered_known_failure_reuses_original_live_lease_and_rejects_same_rank(tmp_path):
    from src.utils.quick_scan_work_store import WorkConflictError
    from tests.unit.test_owner_batch_claim import invoke, sha

    prepared = ordered_prepared(tmp_path)
    store = prepared[0]
    store.configure_quick_scan_budget(route_policy())
    first = invoke(prepared)[0]
    a = send_failure(store, first)
    resumed = invoke(prepared)[0]
    assert resumed["lease"] == first["lease"]
    with pytest.raises(WorkConflictError, match="ordered"):
        store.prepare_attempt(
            first["work_item_id"],
            resumed["lease"],
            route_id="a",
            provider="account-a",
            model_requested="model-a",
            request_cache_key="REQ_" + sha("repeat"),
            prompt_sha256=sha("a"),
        )
    b = send_failure(store, resumed, index=1)
    assert b["ordinal"] == 2
    progress = store.company_scan_progress([first["work_item_id"]])
    assert progress["model_requests"] == 2
    assert next_route_questions(progress["rows"], targets(), 1) == []


@pytest.mark.parametrize("settled", [False, True])
def test_old_approval_never_gains_live_lease_reclaim(tmp_path, settled):
    from src.utils.quick_scan_work_store import WorkConflictError
    from tests.unit.test_owner_batch_claim import invoke, prepare

    prepared = prepare(tmp_path, 1)
    store = prepared[0]
    store.configure_quick_scan_budget(route_policy())
    handle = invoke(prepared)[0]
    send_failure(store, handle, settled=settled)
    with pytest.raises(WorkConflictError, match="pending"):
        invoke(prepared)


def test_company_cap_is_atomic_across_models_and_store_restart(tmp_path):
    from src.utils.quick_scan_work_store import BudgetAdmissionError, QuickScanWorkStore
    from tests.unit.test_owner_batch_claim import invoke, sha

    prepared = ordered_prepared(tmp_path)
    store = prepared[0]
    store.configure_quick_scan_budget(route_policy())
    first = invoke(prepared)[0]
    send_failure(store, first)
    wid = first["work_item_id"]
    store = QuickScanWorkStore(store.path, clock=lambda: prepared[1][0])
    # Reuse exactly the native confirmed-failure lease; primary refusal already
    # consumed the original company cap even after a fresh store instance.
    a = store.prepare_attempt(
        wid,
        first["lease"],
        route_id="b",
        provider="account-b",
        model_requested="model-b",
        request_cache_key="REQ_" + sha("fallback"),
        prompt_sha256=sha("b"),
    )
    with pytest.raises(BudgetAdmissionError, match="company_model_request_cap"):
        store.mark_send_intent(
            wid,
            first["lease"],
            a["attempt_id"],
            budget_policy=route_policy(),
            budget_route=dict(
                route_id="b", provider="account-b", model_requested="model-b", quota_group="A"
            ),
            company_request_scope=([wid], 1),
        )
    assert store.company_scan_progress([wid])["model_requests"] == 1
    assert store.list_attempts(wid)[-1]["phase"] == "prepared"


def test_abandoned_backup_never_returns_to_rejected_primary():
    r = row("Q1", "abandoned_unsent", route="b")
    assert next_route_questions([r], targets(), 0) == []
    assert next_route_questions([r], targets(), 1) == ["Q1"]


def test_expired_known_failure_uses_original_fenced_epoch(tmp_path):
    from tests.unit.test_owner_batch_claim import invoke

    prepared = ordered_prepared(tmp_path)
    store = prepared[0]
    store.configure_quick_scan_budget(route_policy())
    old = invoke(prepared)[0]
    send_failure(store, old)
    prepared[1][0] += 601
    new = invoke(prepared)[0]
    assert new["lease"].lease_epoch == old["lease"].lease_epoch + 1
    assert new["lease"].lease_token != old["lease"].lease_token
    send_failure(store, new, index=1)


def test_unsettled_known_refusal_cannot_reclaim_ordered_work(tmp_path):
    from src.utils.quick_scan_work_store import WorkConflictError
    from tests.unit.test_owner_batch_claim import invoke

    prepared = ordered_prepared(tmp_path)
    store = prepared[0]
    store.configure_quick_scan_budget(route_policy())
    first = invoke(prepared)[0]
    send_failure(store, first, settled=False)
    with pytest.raises(WorkConflictError, match="pending"):
        invoke(prepared)
    with pytest.raises(ValueError, match="unresolved"):
        next_route_questions(
            store.company_scan_progress([first["work_item_id"]])["rows"], targets(), 1
        )


def test_company_request_cap_serializes_disjoint_work_in_original_budget(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from src.utils.quick_scan_work_store import BudgetAdmissionError, QuickScanWorkStore
    from tests.unit.test_owner_batch_claim import invoke, sha

    prepared = ordered_prepared(tmp_path, 2)
    store = prepared[0]
    policy = route_policy()
    store.configure_quick_scan_budget(policy)
    handles = invoke(prepared)
    ids = [h["work_item_id"] for h in handles]
    attempts = []
    for h in handles:
        attempts.append(
            store.prepare_attempt(
                h["work_item_id"],
                h["lease"],
                route_id="a",
                provider="account-a",
                model_requested="model-a",
                request_cache_key="REQ_" + sha(h["work_item_id"]),
                prompt_sha256=sha("p"),
            )
        )
    barrier = threading.Barrier(2)

    def send(i):
        native = QuickScanWorkStore(store.path, clock=lambda: prepared[1][0])
        barrier.wait()
        try:
            native.mark_send_intent(
                ids[i],
                handles[i]["lease"],
                attempts[i]["attempt_id"],
                budget_policy=policy,
                budget_route=dict(
                    route_id="a", provider="account-a", model_requested="model-a", quota_group="A"
                ),
                company_request_scope=(ids, 1),
            )
            return "reserved"
        except BudgetAdmissionError as e:
            assert e.reason == "shared_company_model_request_cap_reached"
            return "capped"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(send, [0, 1])) == ["capped", "reserved"]
    assert store.company_scan_progress(ids)["model_requests"] == 1


def test_physical_pack_counts_once_against_original_company_cap(tmp_path):
    from src.utils.quick_scan_work_store import BudgetAdmissionError
    from tests.unit.test_owner_batch_claim import invoke, sha

    prepared = ordered_prepared(tmp_path, 3)
    store = prepared[0]
    policy = route_policy()
    store.configure_quick_scan_budget(policy)
    handles = invoke(prepared)
    ids = [h["work_item_id"] for h in handles]
    pack = store.prepare_question_pack(
        handles[:2],
        route_id="a",
        provider="account-a",
        model_requested="model-a",
        execution_attempt_id="EXEC_fixture",
        prompt_sha256=sha("p"),
        provider_prompt_sha256=sha("p"),
        request_payload_sha256=sha("payload"),
    )
    route = dict(route_id="a", provider="account-a", model_requested="model-a", quota_group="A")
    store.mark_question_pack_send_intent(
        pack["pack_id"], budget_policy=policy, budget_route=route, company_request_scope=(ids, 1)
    )
    progress = store.company_scan_progress(ids)
    assert progress["model_requests"] == 1
    h = handles[-1]
    a = store.prepare_attempt(
        ids[-1],
        h["lease"],
        route_id="a",
        provider="account-a",
        model_requested="model-a",
        request_cache_key="REQ_" + sha("extra"),
        prompt_sha256=sha("p"),
    )
    with pytest.raises(BudgetAdmissionError, match="company_model_request_cap"):
        store.mark_send_intent(
            ids[-1],
            h["lease"],
            a["attempt_id"],
            budget_policy=policy,
            budget_route=route,
            company_request_scope=(ids, 1),
        )
    assert store.company_scan_progress(ids)["model_requests"] == 1


def test_registered_paid_recovery_keeps_each_original_model_in_approved_order():
    from src.utils.quick_scan_shared_company import registered_recovery_routes

    rows = {
        "Q1": [
            dict(
                phase="response_available",
                attempt_id="A",
                route_id="a",
                provider="account-a",
                model_requested="model-a",
            )
        ],
        "Q2": [
            dict(
                phase="confirmed_failure",
                attempt_id="B0",
                route_id="a",
                provider="account-a",
                model_requested="model-a",
            ),
            dict(
                phase="response_available",
                attempt_id="B",
                route_id="b",
                provider="account-b",
                model_requested="model-b",
            ),
        ],
    }
    routes = route_policy()["routes"]
    store = SimpleNamespace(
        list_attempts=lambda wid: rows[wid],
        get_attempt_response=lambda aid: {"receipt": "original"},
    )
    session = SimpleNamespace(
        requested_question_ids=["Q1", "Q2"],
        entries={q: {"kind": "work"} for q in rows},
        existing_work=lambda q: {"work_item_id": q},
        service=SimpleNamespace(store=store),
        ordered_route_targets=targets(),
        execution_routes=lambda policy: list(reversed(routes)),
    )
    result = registered_recovery_routes(session, {})
    assert [r["id"] for r in result] == ["a", "b"]
    # An old single-model action must never inherit that permission.
    session.ordered_route_targets = None
    with pytest.raises(ValueError, match="original_route_required"):
        registered_recovery_routes(session, {})


@pytest.fixture
def coordinator_ports(tmp_path, monkeypatch):
    # Explicit control-flow ports. This is not an owner authentication positive;
    # original native authority/SDK is separately exercised by the isolated E2E.
    import copy

    from src.core.models import Question
    from src.providers import llm_research_client as sdk
    from src.utils import quick_scan_shared_company as entry
    from src.utils import quick_scan_shared_routes as routes

    state = {"rows": [row("Q1"), row("Q2")], "model_requests": 0}
    owner = SimpleNamespace(
        ordered_route_targets=targets(),
        requested_question_ids=["Q1", "Q2"],
        entries={q: {"kind": "work", "work_item_id": "W" + q} for q in ("Q1", "Q2")},
        proof={"confirmation": {"confirmation_id": "original-owner-scope"}},
        check=lambda: None,
    )
    lifecycle = SimpleNamespace(
        registered_research_work=lambda: SimpleNamespace(owner=owner),
        company_scan_progress=lambda question_ids: copy.deepcopy(state),
        company_scan_uses_native_search=lambda: True,
    )
    clients = [
        (r, SimpleNamespace(model=r["model"], provider_name="mimo"))
        for r in route_policy()["routes"]
    ]
    questions = [Question(q, question_id=q) for q in ("Q1", "Q2")]
    config = dict(state_root=str(tmp_path / "state"), limits={"max_model_requests_per_session": 10})
    monkeypatch.setattr(
        routes, "routed_lifecycle", lambda life, route, client: SimpleNamespace(route=route["id"])
    )
    monkeypatch.setattr(entry, "original_task_checkpoint", lambda root: None)
    monkeypatch.setattr(
        sdk,
        "research_channel_capabilities",
        lambda client: dict(
            native_final_json=True, external_final_json=True, final_json_max_output_tokens=32768
        ),
    )
    return SimpleNamespace(
        entry=entry,
        routes=routes,
        sdk=sdk,
        state=state,
        owner=owner,
        life=lifecycle,
        clients=clients,
        questions=questions,
        config=config,
    )


def test_partial_completed_pack_is_excluded_from_later_model(coordinator_ports, monkeypatch):
    p = coordinator_ports
    calls = []

    def scan(life, client, *, questions, **kw):
        calls.append((life.route, [q.question_id for q in questions]))
        if life.route == "a":
            p.state["rows"] = [row("Q1", completed=True), row("Q2", "confirmed_failure")]
            p.state["model_requests"] = 2
            raise p.entry.SharedCompanyIncomplete()
        p.state["rows"] = [row("Q1", completed=True), row("Q2", completed=True)]
        p.state["model_requests"] = 3

    monkeypatch.setattr(p.entry, "run_shared_company_scan", scan)
    p.routes.run_ordered_company_scan(p.life, p.clients, config=p.config, questions=p.questions)
    assert calls == [("a", ["Q1", "Q2"]), ("b", ["Q2"])]


@pytest.mark.parametrize("external", [False, True])
def test_unsupported_final_json_route_is_skipped_even_for_singleton(
    coordinator_ports, monkeypatch, external
):
    p = coordinator_ports
    calls = []
    if external:
        p.life._external_retrieval = {"original": "bound"}
    monkeypatch.setattr(
        p.sdk,
        "research_channel_capabilities",
        lambda client: dict(
            native_final_json=client.model == "model-b",
            external_final_json=client.model == "model-b",
            final_json_max_output_tokens=32768,
        ),
    )

    def scan(life, client, **kw):
        calls.append(life.route)
        p.state["rows"] = [row("Q1", completed=True), row("Q2", completed=True)]

    monkeypatch.setattr(p.entry, "run_shared_company_scan", scan)
    p.routes.run_ordered_company_scan(
        p.life, p.clients, config=p.config, questions=p.questions, question_pack_size=1
    )
    assert calls == ["b"]


def test_resume_new_backup_inherits_first_frozen_company_cap(coordinator_ports, monkeypatch):
    p = coordinator_ports
    calls = []
    p.state["rows"] = [row("Q1", completed=True), row("Q2", "confirmed_failure")]
    p.state["model_requests"] = 2
    primary = p.entry.ordered_task_root(p.config, p.owner, ["Q1", "Q2"], "a")
    monkeypatch.setattr(
        p.entry,
        "original_task_checkpoint",
        lambda root: root / "checkpoint.json" if root == primary else None,
    )
    monkeypatch.setattr(
        p.entry,
        "load_original_company_task",
        lambda checkpoint: SimpleNamespace(limits={"max_model_requests_per_session": 5}),
    )

    def scan(life, client, **kw):
        calls.append(
            (
                life.route,
                kw["config"]["limits"]["max_model_requests_per_session"],
                kw["request_scope"],
            )
        )
        p.state["rows"] = [row("Q1", completed=True), row("Q2", completed=True)]

    monkeypatch.setattr(p.entry, "run_shared_company_scan", scan)
    p.routes.run_ordered_company_scan(
        p.life, p.clients, config=p.config, questions=list(reversed(p.questions))
    )
    assert calls == [("b", 5, (["WQ1", "WQ2"], 5))]
    assert p.config["limits"]["max_model_requests_per_session"] == 10


def test_mixed_paid_recovery_uses_route_task_roots_and_never_live_dispatch(
    coordinator_ports, monkeypatch
):
    p = coordinator_ports
    calls = []
    p.state["rows"] = [row("Q1", "response_available"), row("Q2", "response_available", route="b")]
    roots = {
        p.entry.ordered_task_root(p.config, p.owner, ["Q1", "Q2"], route): qid
        for route, qid in (("a", "Q1"), ("b", "Q2"))
    }
    monkeypatch.setattr(p.entry, "original_task_checkpoint", lambda root: root / "checkpoint.json")

    def task(checkpoint):
        q = roots[checkpoint.parent]
        return SimpleNamespace(
            limits={"max_model_requests_per_session": 5},
            questions=[dict(question_id=q, question={"prompt": q})],
        )

    monkeypatch.setattr(p.entry, "load_original_company_task", task)

    def scan(life, client, **kw):
        calls.append(
            (
                life.route,
                kw["recovery_only"],
                kw["ordered_scope"],
                [q.question_id for q in kw["questions"]],
            )
        )
        for r in p.state["rows"]:
            if r["question_id"] in [q.question_id for q in kw["questions"]]:
                r["checkpoint_id"] = "CP-original"

    monkeypatch.setattr(p.entry, "run_shared_company_scan", scan)
    p.routes.run_ordered_company_scan(
        p.life,
        list(reversed(p.clients)),
        config=p.config,
        questions=list(reversed(p.questions)),
        recovery_only=True,
    )
    assert calls == [("a", True, ["Q1", "Q2"], ["Q1"]), ("b", True, ["Q1", "Q2"], ["Q2"])]


def test_old_confirmation_does_not_automatically_try_backup(coordinator_ports, monkeypatch):
    p = coordinator_ports
    p.owner.ordered_route_targets = None
    p.life.registered_research_work = lambda: SimpleNamespace(
        owner=p.owner, route_id="a", model_requested="model-a"
    )
    calls = []

    def scan(life, client, **kw):
        calls.append(client.model)
        raise p.entry.SharedCompanyIncomplete()

    monkeypatch.setattr(p.entry, "run_shared_company_scan", scan)
    with pytest.raises(p.entry.SharedCompanyIncomplete):
        p.routes.run_ordered_company_scan(p.life, p.clients, config=p.config, questions=p.questions)
    assert calls == ["model-a"]


def test_paid_recovery_skips_partial_primary_whose_success_already_has_cp(
    coordinator_ports, monkeypatch
):
    p = coordinator_ports
    calls = []
    p.state["rows"] = [
        row("Q1", "response_available", completed=True),
        row("Q2", "response_available", route="b"),
    ]
    monkeypatch.setattr(p.entry, "original_task_checkpoint", lambda root: root / "checkpoint.json")
    monkeypatch.setattr(
        p.entry,
        "load_original_company_task",
        lambda path: SimpleNamespace(
            limits={"max_model_requests_per_session": 5},
            questions=[dict(question_id="Q2", question={"prompt": "Q2"})],
        ),
    )

    def scan(life, client, **kw):
        assert kw["recovery_only"] is True
        calls.append(life.route)
        p.state["rows"][1]["checkpoint_id"] = "CP-original"

    monkeypatch.setattr(p.entry, "run_shared_company_scan", scan)
    p.routes.run_ordered_company_scan(
        p.life, p.clients, config=p.config, questions=p.questions, recovery_only=True
    )
    assert calls == ["b"]


def test_paid_only_without_original_safe_never_reports_success(coordinator_ports, monkeypatch):
    p = coordinator_ports
    monkeypatch.setattr(
        p.entry, "run_shared_company_scan", lambda *a, **kw: pytest.fail("no original response")
    )
    with pytest.raises(ValueError, match="original_saved_response_required"):
        p.routes.run_ordered_company_scan(
            p.life, p.clients, config=p.config, questions=p.questions, recovery_only=True
        )
