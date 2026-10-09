"""QA-NET-01 batch B: frozen manifest consumption and incremental dispatch.

Binds Q13 MOD-06/MOD-09/JOB-03: the published questionnaire manifest is the
only dispatch authority (tampering, duplicate IDs, truncation and cross-module
replacement conflicts are refused before any HTTP), and only genuinely new or
expired questions may consume a model call.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.core.models import Answer, Question
from src.utils.quick_scan_question_manifest import (
    QuestionManifestRejected,
    bind_manifest_to_questions,
    load_question_manifest,
    manifest_scope_bindings,
    plan_manifest_dispatch,
)
from src.utils.quick_scan_work_store import QuickScanWorkStore

UUID_ENTITY = "ENT_1b2a4d3e-0000-4a1b-8c2d-000000000001"
MODEL = "mimo-v2.6-flash"
IDENTITY = {
    "identity_revision": 1,
    "source_binding_version": 1,
    "identity_state": "verified",
    "source_binding_ref": "BND_TEST_1",
    "source_binding_refs": ["BND_TEST_1"],
    "identity_snapshot_sha256": "a" * 64,
}


def _question_record(question_id: str, *, prompt: str, scope: str = "entity") -> dict:
    return {
        "id": question_id,
        "module_id": "common",
        "scope": scope,
        "prompt": prompt,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "semantic_sha256": hashlib.sha256(("sem:" + question_id).encode()).hexdigest(),
        "definition_sha256": hashlib.sha256(("def:" + question_id).encode()).hexdigest(),
        "metric_contract": {"question_id": question_id, "replacement_for": None},
    }


def _manifest(question_ids: list[str], *, prompts: dict[str, str] | None = None) -> dict:
    prompts = prompts or {}
    questions = [
        _question_record(question_id, prompt=prompts.get(question_id, f"问题 {question_id} 的题面"))
        for question_id in question_ids
    ]
    return {
        "schema_version": "3.1.0",
        "template_version": "3.2.0",
        "question_count": len(questions),
        "questions": questions,
        "modules": ["common"],
        "module_locks": [{"module_id": "common", "version": "3.0.0"}],
        "replacements": {},
        "module_package_id": "pkg_" + "b" * 64,
    }


def _write_manifest(tmp_path: Path, manifest: dict, *, name: str = "manifest.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return path


def _authority() -> dict:
    return {
        "contract_versions": {
            "identity_schema": "2.1.0",
            "answer_schema": "quick-scan-answer-v1",
            "observation_schema": "1.0.0",
            "question_catalog": "iqs-2026-10",
            "model_policy_schema": "2.0.0",
        },
        "capabilities": [
            "entity_security_identity_v1",
            "standard_observation_v1",
            "score_v1",
        ],
        "producer_component_version": "1.0.0",
        "producer_build_id": "stockqa-offline-tests",
        "authority_sha256": "c" * 64,
    }


def _lifecycle(store: QuickScanWorkStore, *, authority: dict | None = None):
    from src.runners.llm_runner import QuickScanWorkLifecycle

    return QuickScanWorkLifecycle(
        store,
        entity_id=UUID_ENTITY,
        run_id="RUN_1",
        scan_id="SCAN_1",
        identity=dict(IDENTITY),
        model_requested=MODEL,
        provider_name="mimo",
        c06_authority=_authority() if authority is None else authority,
    )


def _settle(store, lifecycle, question_id: str, prompt: str) -> str:
    from types import SimpleNamespace

    question = Question(text=prompt, question_id=question_id)
    handle = lifecycle.before_question(question)
    assert handle["claimed"] is True, handle
    from tests.unit.test_quick_scan_work_store import _synthetic_search_receipt

    fixture_receipt = _synthetic_search_receipt(
        model=MODEL,
        response_id="resp_" + question_id,
        request_id="req_" + question_id,
        provider_attempt_id="pa_" + question_id,
    )
    metadata = {
        "search_status": "executed",
        "actual_model": MODEL,
        "response_id": "resp_" + question_id,
        "request_id": "req_" + question_id,
        "source_urls": ["https://example.com/issuer"],
        "execution": fixture_receipt,
    }
    result = SimpleNamespace(
        question=question,
        answer=Answer(
            text="基于公开来源的判断",
            score=8,
            status="scored",
            source="mimo",
            metadata=metadata,
        ),
    )
    lifecycle.after_question(handle, result)
    return str(handle["work_item_id"])


def _bindings(manifest: dict, **over) -> dict:
    return manifest_scope_bindings(manifest, entity_id=UUID_ENTITY, **over)


def test_mod_09_tampered_prompt_and_duplicate_ids_are_refused(tmp_path: Path) -> None:
    manifest = _manifest(["IQS_01", "IQS_02"])
    loaded = load_question_manifest(_write_manifest(tmp_path, manifest))
    assert loaded["manifest_sha256"]

    # prompt edited but the frozen hash kept -> refused, no HTTP possible
    tampered = json.loads(json.dumps(manifest))
    tampered["questions"][0]["prompt"] = "被篡改的题面"
    with pytest.raises(QuestionManifestRejected) as duplicate:
        load_question_manifest(_write_manifest(tmp_path, tampered, name="tampered.json"))
    assert duplicate.value.reason == "manifest_prompt_hash_mismatch"

    duplicated = json.loads(json.dumps(manifest))
    duplicated["questions"][1]["id"] = "IQS_01"
    with pytest.raises(QuestionManifestRejected) as duplicate_id:
        load_question_manifest(_write_manifest(tmp_path, duplicated, name="dup.json"))
    assert duplicate_id.value.reason == "manifest_duplicate_question_id"

    truncated = json.loads(json.dumps(manifest))
    truncated["question_count"] = 99
    with pytest.raises(QuestionManifestRejected) as truncation:
        load_question_manifest(_write_manifest(tmp_path, truncated, name="trunc.json"))
    assert truncation.value.reason == "manifest_truncated"

    broken = tmp_path / "broken.json"
    broken.write_text('{"questions": [', encoding="utf-8")
    with pytest.raises(QuestionManifestRejected) as invalid:
        load_question_manifest(broken)
    assert invalid.value.reason == "manifest_invalid_json"


def test_mod_09_cross_module_replacement_conflict_is_refused(tmp_path: Path) -> None:
    manifest = _manifest(["IQS_01", "OPERATING_01", "CYCLICAL_01"])
    manifest["questions"][1]["metric_contract"]["replacement_for"] = "IQS_11"
    manifest["questions"][2]["metric_contract"]["replacement_for"] = "IQS_11"
    with pytest.raises(QuestionManifestRejected) as conflict:
        load_question_manifest(_write_manifest(tmp_path, manifest))
    assert conflict.value.reason == "manifest_conflicting_replacement"

    consistent = _manifest(["IQS_01", "OPERATING_01"])
    consistent["questions"][1]["metric_contract"]["replacement_for"] = "IQS_11"
    consistent["replacements"] = {"IQS_11": "OPERATING_01"}
    load_question_manifest(_write_manifest(tmp_path, consistent, name="ok.json"))

    selected_replaced = _manifest(["IQS_01", "OPERATING_01"])
    selected_replaced["replacements"] = {"IQS_01": "OPERATING_01"}
    with pytest.raises(QuestionManifestRejected) as overlap:
        load_question_manifest(_write_manifest(tmp_path, selected_replaced, name="ov.json"))
    assert overlap.value.reason == "manifest_conflicting_replacement"


def test_question_file_binding_rejects_reordered_or_reworded_prompts(
    tmp_path: Path,
) -> None:
    manifest = _manifest(["IQS_01", "IQS_02"])
    loaded = load_question_manifest(_write_manifest(tmp_path, manifest))
    questions = [
        Question(text=item["prompt"], question_id=item["id"]) for item in manifest["questions"]
    ]
    bind_manifest_to_questions(loaded, questions)

    reordered = [questions[1], questions[0]]
    with pytest.raises(QuestionManifestRejected) as mismatch:
        bind_manifest_to_questions(loaded, reordered)
    assert mismatch.value.reason == "manifest_question_set_mismatch"

    reworded = [
        Question(text=questions[0].text + " 追加", question_id="IQS_01"),
        questions[1],
    ]
    with pytest.raises(QuestionManifestRejected) as prompt_mismatch:
        bind_manifest_to_questions(loaded, reworded)
    assert prompt_mismatch.value.reason == "manifest_prompt_hash_mismatch"


def test_mod_06_only_new_and_expired_questions_are_planned_for_dispatch(
    tmp_path: Path,
) -> None:
    """MOD-06: four settled old questions stay reused (0 new model calls);
    a newly added module question and an expired (re-worded) question are the
    only ones planned for dispatch."""
    old_ids = ["IQS_01", "IQS_02", "IQS_03", "IQS_04"]
    new_manifest = _manifest(
        ["IQS_01", "IQS_02", "IQS_03", "IQS_04", "NEW_01", "IQS_05"],
        prompts={"IQS_05": "改写后的题面版本二"},
    )
    loaded = load_question_manifest(_write_manifest(tmp_path, new_manifest))

    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    lifecycle = _lifecycle(store)
    # the four old questions were answered against the ORIGINAL prompt text
    for question_id in old_ids:
        _settle(store, lifecycle, question_id, f"问题 {question_id} 的题面")
    # IQS_05 was previously answered with an older prompt (expired now)
    _settle(store, lifecycle, "IQS_05", "旧版题面")

    plan = plan_manifest_dispatch(
        loaded,
        store,
        entity_id=UUID_ENTITY,
        identity_snapshot_sha256=IDENTITY["identity_snapshot_sha256"],
        bindings=_bindings(loaded),
    )
    actions = {item["question_id"]: item["action"] for item in plan["questions"]}
    assert actions["IQS_01"] == "reuse"
    assert actions["IQS_04"] == "reuse"
    assert actions["NEW_01"] == "dispatch"
    assert actions["IQS_05"] == "expired_dispatch"
    assert plan["model_calls_planned"] == 2
    assert plan["generation_by_question"] == {"IQS_05": 2}

    # JOB-03: the pack survives partial success — 4 persisted, 2 to refill
    assert plan["counts"]["reuse"] == 4
    assert plan["counts"]["dispatch"] == 1
    assert plan["counts"]["expired_dispatch"] == 1


def test_mod_06_uncertain_and_not_applicable_are_never_redispatched(
    tmp_path: Path,
) -> None:
    manifest = _manifest(["IQS_01", "IQS_02", "IQS_03"])
    loaded = load_question_manifest(_write_manifest(tmp_path, manifest))
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    lifecycle = _lifecycle(store)

    # IQS_01: sent, outcome unknown -> reconciliation, never a fresh dispatch
    uncertain_question = Question(text="问题 IQS_01 的题面", question_id="IQS_01")
    handle = lifecycle.before_question(uncertain_question)
    assert handle["claimed"] is True
    store.record_attempt_outcome(
        handle["work_item_id"], handle["lease"], handle["attempt_id"], outcome="unknown"
    )

    # IQS_02: answered but marked not applicable -> no dispatch
    _settle(store, lifecycle, "IQS_02", "问题 IQS_02 的题面")
    checkpoint_row = store.find_work_items(entity_id=UUID_ENTITY, question_ids=["IQS_02"])[
        "IQS_02"
    ][0]
    store_path = store.path
    assert store_path.exists()

    plan = plan_manifest_dispatch(
        loaded,
        store,
        entity_id=UUID_ENTITY,
        identity_snapshot_sha256=IDENTITY["identity_snapshot_sha256"],
        bindings=_bindings(loaded),
    )
    actions = {item["question_id"]: item["action"] for item in plan["questions"]}
    assert actions["IQS_01"] == "reconcile"
    assert actions["IQS_03"] == "dispatch"
    assert plan["model_calls_planned"] == 1
    assert checkpoint_row["status"] == "result_ready"


def test_unbound_security_scope_questions_are_deferred(tmp_path: Path) -> None:
    manifest = _manifest(["IQS_01", "IQS_22"], prompts={"IQS_22": "security 题面"})
    manifest["questions"][1]["scope"] = "security"
    manifest["questions"][1]["prompt_sha256"] = hashlib.sha256(
        "security 题面".encode("utf-8")
    ).hexdigest()
    loaded = load_question_manifest(_write_manifest(tmp_path, manifest))
    store = QuickScanWorkStore(tmp_path / "work.sqlite")

    deferred = plan_manifest_dispatch(
        loaded,
        store,
        entity_id=UUID_ENTITY,
        identity_snapshot_sha256=IDENTITY["identity_snapshot_sha256"],
        bindings=_bindings(loaded),
    )
    actions = {item["question_id"]: item["action"] for item in deferred["questions"]}
    assert actions["IQS_22"] == "deferred_scope_unbound"
    assert actions["IQS_01"] == "dispatch"

    bound = plan_manifest_dispatch(
        loaded,
        store,
        entity_id=UUID_ENTITY,
        identity_snapshot_sha256=IDENTITY["identity_snapshot_sha256"],
        bindings=_bindings(loaded, security_scope_id="SEC_listing_1"),
    )
    bound_actions = {item["question_id"]: item["action"] for item in bound["questions"]}
    assert bound_actions["IQS_22"] == "dispatch"
    assert bound["questions"][1]["scope"] == "security"
    assert bound["questions"][1]["scope_id"] == "SEC_listing_1"


def test_manifest_directory_input_and_seal_only_action(tmp_path: Path) -> None:
    manifest = _manifest(["IQS_01"])
    directory = tmp_path / "compose"
    directory.mkdir()
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    loaded = load_question_manifest(directory)
    assert loaded["question_count"] == 1

    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    lifecycle = _lifecycle(store, authority={})  # no usable authority -> block
    work_item_id = _settle(store, lifecycle, "IQS_01", "问题 IQS_01 的题面")
    # a settled checkpoint with no sealed package is seal-only (0 model calls)
    plan = plan_manifest_dispatch(
        loaded,
        store,
        entity_id=UUID_ENTITY,
        identity_snapshot_sha256=IDENTITY["identity_snapshot_sha256"],
        bindings=_bindings(loaded),
    )
    assert plan["questions"][0]["action"] == "seal_only"
    assert plan["model_calls_planned"] == 0
    assert plan["questions"][0]["work_item_id"] == work_item_id


def test_changed_prompt_waits_for_uncertain_prior_attempt(tmp_path):
    from src.core.models import Question
    from src.utils.quick_scan_question_manifest import (
        load_question_manifest,
        plan_manifest_dispatch,
    )
    from src.utils.quick_scan_work_store import QuickScanWorkStore

    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    lifecycle = _lifecycle(store)
    question = Question(text="旧版题面", question_id="IQS_01")
    handle = lifecycle.before_question(question)
    assert handle["claimed"] is True
    store.record_attempt_outcome(
        handle["work_item_id"], handle["lease"], handle["attempt_id"], outcome="unknown"
    )
    doc = _manifest(["IQS_01"], prompts={"IQS_01": "新版题面"})
    loaded = load_question_manifest(_write_manifest(tmp_path, doc))
    plan = plan_manifest_dispatch(
        loaded,
        store,
        entity_id=UUID_ENTITY,
        identity_snapshot_sha256=IDENTITY["identity_snapshot_sha256"],
        bindings=_bindings(loaded),
    )
    assert (
        plan["model_calls_planned"] == 0
    ), "Uncertain old attempt must reconcile before new generation"
    assert plan["questions"][0]["action"] == "reconcile"
