"""Actual public delegated software chain over synthetic company, scripted wire.
No fake TTY, injected confirmer, real network, production data or actual grant.
"""

import os
from contextlib import closing, contextmanager

import httpx
import pytest

if os.environ.get("IQS_C06_RUN_INTEGRATION") != "1":
    pytest.skip(
        "requires explicitly isolated three-component delegated runtime", allow_module_level=True
    )

from src.core.models import Question
from src.providers.llm_client import LLMClient
from src.providers.llm_research_client import ResearchChatSession
from src.runners.llm_runner import QuickScanWorkLifecycle
from src.utils.quick_scan_refresh_admission import (
    AdmissionRejected,
    RefreshAdmissionService,
)
from src.utils.quick_scan_refresh_execution import prepare_execution
from src.utils.quick_scan_work_transport import (
    bind_quick_scan_budget,
    bind_quick_scan_route,
)
from tests.delegated_fixture import setup_delegated
from tests.integration.test_c06_subject_public_e2e import actual as actual
from tests.integration.test_c06_subject_public_e2e import invoke
from tests.unit.test_quick_scan_budget import _policy


@pytest.mark.parametrize("actual", ["1.2.0"], indirect=True)
def test_actual_public_delegation_native_admission_revocation_and_unknown_cost(
    actual, tmp_path, monkeypatch
):
    policy = _policy(model="deepseek-flash", max_cost_per_attempt=1)
    prepared = setup_delegated(
        actual, tmp_path, policy=policy, provider="deepseek", selected_question_count=2
    )
    service = RefreshAdmissionService(prepared["deployment_file"], process_env=dict(os.environ))
    first = service.admit(prepared["consent"]["confirmation_id"])
    assert first["status"] == "accepted", first
    assert first["permission_authenticated"] is True
    assert first["registration"]["permission_authenticated"] is False
    assert first["API_requests"] == 0
    replay = service.admit(prepared["consent"]["confirmation_id"])
    assert (
        replay["status"] == "already_registered" and replay["registration"] == first["registration"]
    )
    session = prepare_execution(
        prepared["deployment_file"],
        prepared["consent"]["confirmation_id"],
        input_paths={
            name: prepared["bundle"] / name
            for name in (
                "manifest.json",
                "identity.json",
                "authority.json",
                "owner-config.json",
                "questions.json",
            )
        },
        process_env=dict(os.environ),
    )
    store = session.service.store
    q = prepared["q"]
    ref = prepared["ref"]
    route = policy["routes"][0]
    lifecycle = QuickScanWorkLifecycle(
        store,
        entity_id=ref["entity_id"],
        run_id="DELEGATED_SCRIPTED",
        scan_id="DELEGATED_SCRIPTED",
        identity=session.bundle["identity"],
        transport_managed=True,
        route_id=route["id"],
        provider_name=route["provider_config_ref"],
        model_requested="deepseek-flash",
        budget_policy=policy,
        budget_route=route,
        owner_refresh_session=session,
        observation_context=session.native.observation_context,
        scope_by_question=session.scope_bindings,
        generation_by_question={
            row["id"]: session.existing_work(row["id"])["generation"]
            for row in prepared["selected_questions"]
        },
    )
    handle = lifecycle.before_question(Question(q["prompt"], question_id=q["id"]))
    assert handle["claimed"] is True, handle
    other = prepared["selected_questions"][1]
    other_handle = lifecycle.before_question(Question(other["prompt"], question_id=other["id"]))
    assert other_handle["claimed"] is True, other_handle
    assert store.list_attempts(other_handle["work_item_id"]) == []
    client = LLMClient(
        "SCRIPTED-NOT-A-SECRET",
        "deepseek-flash",
        "https://api.deepseek.com/chat/completions",
        provider_name="deepseek",
        timeout=10,
    )
    from src.providers import llm_research_client as sdk_module

    posts = []

    class Wire:
        def post(self, *args, **kwargs):
            posts.append(kwargs["json"])
            raise httpx.ReadTimeout("scripted received outcome unknown")

    monkeypatch.setattr(sdk_module.http_client_manager, "get_metered_sync_session", lambda: Wire())

    def fresh_sdk():
        return ResearchChatSession(
            client,
            system_prefix="synthetic delegated question",
            tools=[
                dict(
                    type="function",
                    function=dict(
                        name="finish",
                        parameters=dict(type="object", properties={}, additionalProperties=False),
                    ),
                )
            ],
            max_output_tokens=1000,
            session_input_tokens=240000,
            session_output_tokens=5000,
        )

    sdk = fresh_sdk()

    @contextmanager
    def original_scope(work_handle):
        with lifecycle.dispatch_context(work_handle), bind_quick_scan_route(
            route_id=route["id"],
            provider=route["provider_config_ref"],
            model_requested="deepseek-flash",
            quota_group=route["quota_group"],
        ), bind_quick_scan_budget(store, policy):
            yield

    # This is a different approved, original work with no native send. Prove
    # its owner pre-POST guard is valid before any timeout or revocation.
    from src.utils.quick_scan_work_transport import _check_owner_refresh_guard

    with original_scope(other_handle):
        _check_owner_refresh_guard()
        with original_scope(handle):
            with pytest.raises(Exception):
                sdk.step(user_context="scripted timeout, do not retry")
            assert len(posts) == 1
            with pytest.raises(Exception, match="requires_inspection_or_new_admission"):
                sdk.step(user_context="same SDK session remains fenced")
        with closing(store._connect()) as connection:
            prior_budget = [
                dict(row) for row in connection.execute("SELECT * FROM quick_scan_budget_attempt")
            ]
            prior_work = [dict(row) for row in connection.execute("SELECT * FROM attempt")]
        assert len(prior_budget) == 1 and prior_budget[0]["status"] == "outcome_uncertain"
        assert prior_budget[0]["actual_cost_micros"] is None
        # Revoke with the independent unsent work's scope already bound. Its
        # initial SDK object has no timeout barrier to mask the native owner
        # before-POST check; the original global budget remains unchanged.
        invoke(
            prepared["native_owner_command"] + ["--revoke-delegation", prepared["grant_id"]],
            tmp_path,
            "delegated-revoke",
        )
        with pytest.raises(Exception) as refused:
            fresh_sdk().step(user_context="independent approved unsent work must be revoked")
        chain = []
        error = refused.value
        while error is not None:
            chain.append(str(error))
            error = error.__cause__
        assert any(
            "owner_refresh_guard" in text or "consent_native_rejected" in text for text in chain
        ), chain
        assert len(posts) == 1
        assert store.list_attempts(other_handle["work_item_id"]) == []
    with closing(store._connect()) as connection:
        assert prior_budget == [
            dict(row) for row in connection.execute("SELECT * FROM quick_scan_budget_attempt")
        ]
        assert prior_work == [dict(row) for row in connection.execute("SELECT * FROM attempt")]
        assert (
            connection.execute("SELECT COUNT(*) FROM quick_scan_refresh_admission").fetchone()[0]
            == 1
        )
    with pytest.raises(AdmissionRejected, match="native_or_producer_rejected"):
        service.admit(prepared["consent"]["confirmation_id"])
    assert len(posts) == 1
