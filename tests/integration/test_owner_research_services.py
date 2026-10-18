"""Original native owner records + SQLite + scripted SDK wire, never financial gold."""

import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.models import Question
from src.providers.llm_client import LLMClient
from src.providers.llm_research_client import ResearchChatSession
from src.runners.llm_runner import QuickScanWorkLifecycle
from src.utils.quick_scan_owner_research import (
    OwnerResearchRejected,
    OwnerResearchServices,
)
from src.utils.quick_scan_refresh_admission import RefreshAdmissionService
from src.utils.quick_scan_refresh_execution import prepare_execution
from src.utils.quick_scan_result_outbox import canonical_sha256
from tests.integration.test_c06_refresh_admission import setup_admission
from tests.integration.test_c06_subject_public_e2e import actual
from tests.unit.test_quick_scan_budget import _policy


def test_arbitrary_owner_double_refused_before_any_sdk():
    with pytest.raises(OwnerResearchRejected, match="original_lifecycle"):
        OwnerResearchServices(SimpleNamespace(), SimpleNamespace(), task=SimpleNamespace())


def _setup(actual, tmp_path):
    policy = _policy(model="deepseek-flash", max_cost_per_attempt=1)
    prepared = setup_admission(
        actual, tmp_path, policy=policy, provider="deepseek", selected_question_count=2
    )
    accepted = RefreshAdmissionService(
        prepared["deployment_file"], process_env=dict(os.environ)
    ).admit(prepared["consent"]["confirmation_id"])
    assert accepted["status"] == "accepted"
    session = prepare_execution(
        prepared["deployment_file"],
        prepared["consent"]["confirmation_id"],
        input_paths={
            n: prepared["bundle"] / n
            for n in (
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
    qid = prepared["q"]["id"]
    context = session.native.observation_context
    contract = context["questions"][qid]["subject_binding"]["question_contract"]
    ref = context["questions"][qid]["subject_binding"]["subject_ref"]
    task = SimpleNamespace(
        raw={
            "run_id": "NATIVE_OWNER_SCRIPTED",
            "information_cutoff": session.plan["information_cutoff"],
        },
        questions=[
            dict(
                question_id=row["id"],
                module_id=row["module_id"],
                module_version=next(
                    lock["version"]
                    for lock in session.bundle["manifest"]["module_locks"]
                    if lock["module_id"] == row["module_id"]
                ),
                question=copy.deepcopy(
                    context["questions"][row["id"]]["subject_binding"]["question_contract"]
                ),
                question_object_sha256=canonical_sha256(
                    context["questions"][row["id"]]["subject_binding"]["question_contract"]
                ),
            )
            for row in prepared["selected_questions"]
        ],
        company_doc={"origin": "owner_public", "subject": {"subject_ref": copy.deepcopy(ref)}},
        company_ref={"path": "original-synthetic-identity.json", "sha256": "a" * 64, "bytes": 1},
        limits={"max_model_requests_per_session": 8},
    )
    route = policy["routes"][0]
    lifecycle = QuickScanWorkLifecycle(
        store,
        entity_id=ref["entity_id"],
        run_id="NATIVE_OWNER_SCRIPTED",
        scan_id="NATIVE_OWNER_SCAN",
        identity=session.bundle["identity"],
        transport_managed=True,
        route_id=route["id"],
        provider_name=route["provider_config_ref"],
        model_requested="deepseek-flash",
        budget_policy=policy,
        budget_route=route,
        owner_refresh_session=session,
        observation_context=context,
        scope_by_question=session.scope_bindings,
        generation_by_question={
            row["id"]: session.existing_work(row["id"])["generation"]
            for row in prepared["selected_questions"]
        },
    )
    client = LLMClient(
        "SCRIPTED-NOT-A-SECRET",
        "deepseek-flash",
        "https://api.deepseek.com/chat/completions",
        provider_name="deepseek",
        timeout=10,
    )
    services = OwnerResearchServices(
        lifecycle,
        client,
        task=task,
        cost_resolver=lambda _: dict(
            actual_cost=0.001, pricing_ref="fixture-price-v1", source_ref="scripted-rate"
        ),
        native_session=True,
        native_max_turns=2,
    )
    services.bind_task_owner_binding(
        dict(synthetic=False, subject_ref=ref, authority_ref={"id": "original-test-owner"})
    )
    dynamic = dict(
        company_ref=task.company_ref,
        company=task.company_doc,
        information_cutoff=task.raw["information_cutoff"],
        requested_question_ids=[qid],
    )
    stable = {"questions": task.questions}
    envelope = dict(
        stable_prefix=stable,
        dynamic_context=dynamic,
        stable_prefix_sha256=canonical_sha256(stable),
        dynamic_context_sha256=canonical_sha256(dynamic),
        intent=dict(action_id="owner-native-turn-1", question_ids=[qid]),
    )
    return services, envelope, prepared


@pytest.mark.parametrize("actual", ["1.2.0"], indirect=True)
def test_original_registered_owner_two_sdk_turns_and_scope_recovery(actual, tmp_path, monkeypatch):
    services, envelope, prepared = _setup(actual, tmp_path)
    from src.providers import llm_research_client as sdk_module

    posts = []

    class Response:
        status_code = 200
        headers = {"x-request-id": "scripted-original-wire"}

        def __init__(self, n):
            self.content = json.dumps(
                dict(
                    id="scripted-response-" + str(n),
                    model="deepseek-flash",
                    choices=[
                        dict(
                            message=dict(
                                role="assistant",
                                content="bounded final " + str(n),
                                reasoning_content="PRIVATE-REASONING-NEVER-PERSIST",
                            ),
                            finish_reason="stop",
                        )
                    ],
                    usage=dict(
                        prompt_tokens=60,
                        prompt_cache_hit_tokens=20,
                        prompt_cache_miss_tokens=40,
                        completion_tokens=12,
                        completion_tokens_details=dict(reasoning_tokens=4),
                    ),
                )
            ).encode()

        def raise_for_status(self):
            pass

    class Wire:
        def post(self, *args, **kwargs):
            posts.append(kwargs["json"])
            assert kwargs["json"]["thinking"] == {"type": "enabled"}
            assert (
                json.loads(kwargs["json"]["messages"][0]["content"])["questions"]
                == services.task.questions
            )
            return Response(len(posts))

    monkeypatch.setattr(sdk_module.http_client_manager, "get_metered_sync_session", lambda: Wire())
    sdk = ResearchChatSession(
        services.client,
        system_prefix=json.dumps(envelope["stable_prefix"], ensure_ascii=False, sort_keys=True),
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "finish",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
            }
        ],
        max_output_tokens=1000,
        session_input_tokens=240000,
        session_output_tokens=5000,
    )
    receipts = []
    for n in (1, 2):
        envelope["intent"]["action_id"] = "owner-native-turn-" + str(n)
        with services.generation_scope(envelope):
            step = sdk.step(user_context="original question and native subject " + str(n))
            ref = services.observe_native_step(step)
            receipts.append((ref, step["receipt"]))
    assert len(posts) == 2
    other_qid = prepared["selected_questions"][1]["id"]
    other_work = services.owner.existing_work(other_qid)
    assert other_work["status"] == "pending"
    assert services.store.list_attempts(other_work["work_item_id"]) == []
    assert set(services._handles) == {prepared["q"]["id"]}
    assert (
        len(
            services.store.list_attempts(
                prepared["ledger"].get_item(
                    services.owner.existing_work(prepared["q"]["id"])["work_item_id"]
                )["work_item_id"]
            )
        )
        == 2
    )
    for ref, native in receipts:
        inspection = services.inspect_attempt(ref)
        assert inspection["known"] and inspection["response_saved"]["final_content"].startswith(
            "bounded final"
        )
        assert inspection["response_saved"]["session_handle"] is None
        assert inspection["native_receipt"]["actual_model"] == "deepseek-flash"
        assert services.request_resume(ref)["decision"] == "recover_saved_receipt"
    observed = services.attempt_observations(transport_origin="scripted_wire")
    assert len(observed) == 2 and all(not r["replayed"] for r in observed)
    assert "PRIVATE-REASONING" not in json.dumps(observed)
    assert services.inspect_attempt("stockqa:foreign-sdk-id")["known"] is False
    assert services.request_resume("stockqa:foreign-sdk-id")["decision"] == "pause_for_inspection"
    # Reconstruct through native task/work/intent joins with no in-memory
    # handles, no hidden conversation and no third physical request.
    restarted = OwnerResearchServices(
        services.lifecycle,
        services.client,
        task=services.task,
        native_session=True,
        native_max_turns=32,
    )
    assert restarted.native_max_turns == 8  # Task cap cannot be enlarged by adapter controls.
    recovered = restarted.recover_intent(copy.deepcopy(envelope["intent"]))
    assert recovered["decision"] == "recover_saved_receipt"
    assert recovered["saved_receipt"]["runtime_attempt_ref"] == receipts[-1][0]
    assert recovered["saved_receipt"]["session_handle"] is None
    assert restarted.attempt_observations(transport_origin="scripted_wire")[0]["replayed"] is True
    assert len(posts) == 2
    # Every question and every subject field remains bound even with rehashed envelopes.
    changed = copy.deepcopy(envelope)
    changed["stable_prefix"]["questions"][0]["question"]["prompt"] += " other"
    changed["stable_prefix_sha256"] = canonical_sha256(changed["stable_prefix"])
    with pytest.raises(OwnerResearchRejected, match="question_contract_changed"):
        with services.generation_scope(changed):
            pass
    changed = copy.deepcopy(envelope)
    changed["dynamic_context"]["company"]["subject"]["subject_ref"][
        "analysis_subject_revision"
    ] += 1
    changed["dynamic_context_sha256"] = canonical_sha256(changed["dynamic_context"])
    with pytest.raises(OwnerResearchRejected, match="subject_or_cutoff"):
        with services.generation_scope(changed):
            pass
    # Original native turn cap fences an attempted third send before HTTP.
    from src.utils.quick_scan_work_transport import QuickScanWorkPersistenceError

    with pytest.raises(QuickScanWorkPersistenceError) as blocked:
        envelope["intent"]["action_id"] = "owner-native-turn-3"
        with services.generation_scope(envelope):
            sdk.step(user_context="third send must not happen")
    assert "turn_limit" in str(blocked.value.__cause__)
    assert len(posts) == 2
    # Research without a native search/context proof must NOT become a standard checkpoint.
    qid = prepared["q"]["id"]
    with pytest.raises(Exception):
        services.commit_standard_answers(
            [dict(question_id=qid, status="unknown", score=None, summary="unsupported")],
            native_reference=receipts[-1][0],
        )
    assert (
        services.store.get_answer_checkpoint(services.owner.existing_work(qid)["work_item_id"])
        is None
    )
    assert len(posts) == 2
