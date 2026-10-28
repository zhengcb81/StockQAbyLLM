"""Original-context resume through actual SDK/SQLite; synthetic clocks and fake HTTP only."""

import copy
import hashlib
import json
import time
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from src.core.qa_engine import QAEngine
from src.providers.base_llm_provider import standard_answer_transport
from src.utils import quick_scan_work_transport as transport
from src.utils.quick_scan_result_outbox import canonical_sha256
from tests.unit import test_known_pack_continuation as fx


def prepared_unsent(tmp_path, monkeypatch, old_count=2):
    (tmp_path / "logs").mkdir(exist_ok=True)
    store, provider, lifecycle, questions, session, policy = fx.fixture(
        tmp_path, monkeypatch, "answer_generator"
    )
    clock = [float(int(time.time()) + 1)]
    monkeypatch.setattr(store, "clock", lambda: clock[0])
    from src.providers import llm_client

    monkeypatch.setattr(
        llm_client,
        "_utc_now",
        lambda: datetime.fromtimestamp(clock[0], timezone.utc).isoformat().replace("+00:00", "Z"),
    )
    monkeypatch.setattr(
        lifecycle._owner_refresh,
        "register_subject_dispatch",
        lambda request: fx._owner_receipt(request, clock[0]),
    )
    original = copy.deepcopy(lifecycle._observation_context)
    for row in original["questions"].values():
        row["metadata"].update(run_id="RUN_CONTINUE", scan_id="SCAN_CONTINUE")
    lifecycle._observation_context = original
    lifecycle._owner_refresh.observation_context = original
    handles = [lifecycle.before_question(q) for q in questions[:old_count]]
    assert all(h["claimed"] for h in handles)
    for h in handles:
        store.freeze_observation_context_before_send(h["work_item_id"], h["lease"], original)
    old_pack = store.prepare_question_pack(
        [{"work_item_id": h["work_item_id"], "lease": h["lease"]} for h in handles],
        route_id="pack-route",
        provider="mimo",
        model_requested="mimo-v2.6-flash",
        execution_attempt_id="22222222-2222-4222-8222-222222222222",
        prompt_sha256=hashlib.sha256(b"original synthetic pack").hexdigest(),
        provider_prompt_sha256=hashlib.sha256(b"original synthetic prompt").hexdigest(),
        request_payload_sha256=hashlib.sha256(b"original synthetic payload").hexdigest(),
        model_resolution=lifecycle._model_resolution,
    )
    assert old_pack["intent_time"] is None and session.post.call_count == 0
    clock[0] += 601
    for h in handles:
        assert store.recover_expired(h["work_item_id"]) == "pending"
        assert store.list_attempts(h["work_item_id"])[-1]["phase"] == "abandoned_unsent"
    current = copy.deepcopy(original)
    for row in current["questions"].values():
        row["metadata"].update(
            run_id="RUN_NEW_CONTEXT", scan_id="SCAN_NEW_CONTEXT", inputset_id="INPUT_NEW_CONTEXT"
        )
    owner = copy.copy(lifecycle._owner_refresh)
    owner.observation_context = current
    owner.check = Mock(wraps=owner.check)
    lifecycle._owner_refresh = owner
    lifecycle._observation_context = current
    lifecycle._run_id, lifecycle._scan_id = "RUN_NEW_CONTEXT", "SCAN_NEW_CONTEXT"
    return (
        store,
        provider,
        lifecycle,
        questions,
        session,
        policy,
        handles,
        original,
        current,
        old_pack,
    )


def execute(values, selected=None):
    store, provider, lifecycle, questions, session, policy, *_ = values
    with standard_answer_transport(True), transport.bind_quick_scan_budget(
        store,
        policy,
        cost_resolver=lambda _: {
            "actual_cost": 0.03,
            "pricing_ref": "fixture-price",
            "source_ref": "synthetic-rate",
        },
    ):
        return QAEngine(
            provider, progress_reporter=Mock(), work_item_lifecycle=lifecycle
        ).process_questions(
            questions[:2] if selected is None else selected,
            question_pack_size=2,
            thinking_enabled=True,
            max_output_tokens=32768,
        )


def test_original_unsent_context_resumes_under_new_run_and_seals_without_rewriting(
    tmp_path, monkeypatch
):
    values = prepared_unsent(tmp_path, monkeypatch)
    store, _, lifecycle, _, session, _, handles, original, current, old_pack = values
    old_attempts = [copy.deepcopy(store.list_attempts(h["work_item_id"])) for h in handles]
    observed = []
    original_factory = lifecycle.dispatch_question_pack_context

    def trace(handles):
        try:
            return original_factory(handles)
        except Exception as error:
            observed.append((type(error).__name__, str(error)))
            raise

    monkeypatch.setattr(lifecycle, "dispatch_question_pack_context", trace)
    result = execute(values)
    assert session.post.call_count == 1, observed
    assert all(r.answer.status == "scored" for r in result.results)
    for h, attempts in zip(handles, old_attempts):
        wid = h["work_item_id"]
        assert store.get_item(wid)["status"] == "result_ready"
        assert store.get_observation_context(wid)["context"] == original
        assert store.list_attempts(wid)[0] == attempts[0]
        assert store.get_answer_checkpoint(wid) is not None
        assert store.get_standard_answer(wid) is not None
    assert store.get_question_pack(old_pack["pack_id"]) == old_pack
    assert lifecycle._observation_context == current
    assert lifecycle._owner_refresh.check.call_count >= 2


def test_mixed_original_and_unfrozen_members_keep_distinct_contexts(tmp_path, monkeypatch):
    values = prepared_unsent(tmp_path, monkeypatch, old_count=1)
    store, _, _, _, session, _, handles, original, current, _ = values
    result = execute(values)
    assert session.post.call_count == 1 and all(r.answer.status == "scored" for r in result.results)
    old_id = handles[0]["work_item_id"]
    new_id = store.find_work_items(entity_id="ENT_CONTEXT_FIXTURE", question_ids=["IQS_02"])[
        "IQS_02"
    ][0]["work_item_id"]
    assert store.get_observation_context(old_id)["context"] == original
    assert store.get_observation_context(new_id)["context"] == current
    assert canonical_sha256(original) != canonical_sha256(current)


@pytest.mark.parametrize(
    "mutation", ["cutoff", "subject", "prompt", "schema", "metric", "other_question"]
)
def test_stable_context_change_is_rejected_before_http(tmp_path, monkeypatch, mutation):
    values = prepared_unsent(tmp_path, monkeypatch)
    store, _, lifecycle, questions, session, _, handles, original, current, _ = values
    q = current["questions"][questions[0].question_id]
    if mutation == "cutoff":
        q["metadata"]["information_cutoff"] = "2026-09-23"
    elif mutation == "subject":
        q["subject_binding"]["subject_ref"]["perimeter_sha256"] = "e" * 64
    elif mutation == "prompt":
        q["subject_binding"]["question_contract"]["prompt"] += " altered"
    elif mutation == "schema":
        current["answer_schema_sha256"] = "f" * 64
    elif mutation == "metric":
        current["metric_registry_sha256"] = "a" * 64
    elif mutation == "other_question":
        current["questions"][questions[-1].question_id]["metadata"][
            "information_cutoff"
        ] = "2026-09-23"
    if mutation == "subject":
        with pytest.raises(ValueError, match="question_pack_frozen_subject_mismatch"):
            execute(values)
    else:
        result = execute(values)
        assert all(r.answer.score is None and r.answer.status == "error" for r in result.results)
    assert session.post.call_count == 0
    for h in handles:
        assert store.get_observation_context(h["work_item_id"])["context"] == original
        assert len(store.list_attempts(h["work_item_id"])) == 1


def test_current_owner_rejection_still_stops_original_context_resume(tmp_path, monkeypatch):
    values = prepared_unsent(tmp_path, monkeypatch)
    lifecycle, session = values[2], values[4]
    lifecycle._owner_refresh.check.side_effect = RuntimeError("synthetic current owner refused")
    with pytest.raises(RuntimeError, match="current owner refused"):
        execute(values)
    assert session.post.call_count == 0


def loaded_authority(tmp_path, context, version="3.0.0"):
    from pathlib import Path

    from src.utils.quick_scan_c06_authority import load_c06_authority

    fixture_path = (
        Path(fx.__file__).parents[1] / "fixtures" / "quick_scan_c06_authority_v2_fixture.json"
    )
    raw = json.loads(fixture_path.read_text(encoding="utf8"))
    raw["schema_version"] = version
    raw["observation_context"] = context
    raw["observation_context_sha256"] = canonical_sha256(context)
    path = tmp_path / "synthetic-complete-authority.json"
    path.write_text(json.dumps(raw), encoding="utf8")
    manifest_bytes = fixture_path.with_name("quick_scan_c06_manifest_v2_fixture.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    manifest["manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    assert manifest["manifest_sha256"] == context["manifest_file_sha256"]
    return path, load_c06_authority(
        path, manifest=manifest, identity_snapshot_sha256=context["identity_snapshot_sha256"]
    )


def test_loaded_complete_v3_authority_really_seals_resumed_original_context(tmp_path, monkeypatch):
    values = prepared_unsent(tmp_path, monkeypatch)
    store, _, lifecycle, _, session, _, handles, original, current, old_pack = values
    path, authority = loaded_authority(tmp_path, current)
    raw_before, authority_before = path.read_bytes(), copy.deepcopy(authority)
    lifecycle._c06_authority = authority
    result = execute(values)
    assert session.post.call_count == 1 and all(r.answer.status == "scored" for r in result.results)
    from src.utils.quick_scan_delivery_seal import seal_result_delivery

    for h in handles:
        wid = h["work_item_id"]
        record = store.get_result_delivery(wid)
        assert record["state"] == "ready", record.get("block_code")
        assert record["package"] is not None
        observation = record["package"]["items"][0]["observation"]
        assert observation["run_id"] == "RUN_CONTINUE"
        assert observation["scan_id"] == "SCAN_CONTINUE"
        assert store.get_observation_context(wid)["context"] == original
        assert seal_result_delivery(store, wid, authority=authority)["action"] == "already_sealed"
    assert authority == authority_before and path.read_bytes() == raw_before
    assert store.get_question_pack(old_pack["pack_id"]) == old_pack
    assert session.post.call_count == 1


def test_loaded_complete_v2_runtime_attribution_remains_compatible(tmp_path):
    from src.utils.quick_scan_delivery_seal import seal_result_delivery
    from tests.unit import test_quick_scan_c06_complete_seal as legacy

    store, item, checkpoint, body = legacy._checkpointed(tmp_path)
    wid = item["work_item_id"]
    original = store.get_observation_context(wid)["context"]
    current = copy.deepcopy(original)
    for row in current["questions"].values():
        row["metadata"].update(
            run_id="V2_NEW_RUN", scan_id="V2_NEW_SCAN", inputset_id="V2_NEW_INPUT"
        )
    path, authority = loaded_authority(tmp_path, current, "2.0.0")
    before, raw_before = copy.deepcopy(authority), path.read_bytes()
    outcome = seal_result_delivery(store, wid, authority=authority)
    assert outcome["action"] == "sealed"
    assert store.get_result_delivery(wid)["state"] == "ready"
    assert store.get_answer_checkpoint(wid) == checkpoint
    assert store.get_observation_context(wid)["context"] == original
    assert store.get_standard_answer(wid)["answer"] == body
    assert authority == before and path.read_bytes() == raw_before


@pytest.mark.parametrize("mutation", ["cutoff", "context_hash"])
def test_complete_sealer_rejects_stable_drift_or_false_context_hash(
    tmp_path, monkeypatch, mutation
):
    from src.utils.quick_scan_delivery_seal import (
        BLOCK_CONTEXT_CONFLICT,
        seal_result_delivery,
    )

    values = prepared_unsent(tmp_path, monkeypatch)
    store, _, lifecycle, _, session, _, handles, original, current, _ = values
    _, authority = loaded_authority(tmp_path, current)
    lifecycle._c06_authority = authority
    monkeypatch.setattr(lifecycle, "_seal_delivery", lambda _wid: None)
    execute(values)  # Actual original SDK CP+standard; seal below is the real unmocked API.
    assert session.post.call_count == 1
    bad = copy.deepcopy(authority)
    if mutation == "cutoff":
        bad["observation_context"]["questions"]["IQS_01"]["metadata"][
            "information_cutoff"
        ] = "2026-09-23"
        bad["observation_context_sha256"] = canonical_sha256(bad["observation_context"])
    else:
        bad["observation_context_sha256"] = "f" * 64
    before = copy.deepcopy(bad)
    outcome = seal_result_delivery(store, handles[0]["work_item_id"], authority=bad)
    assert outcome["action"] == "blocked" and outcome["block_code"] == BLOCK_CONTEXT_CONFLICT
    assert bad == before
    assert store.get_observation_context(handles[0]["work_item_id"])["context"] == original
    assert session.post.call_count == 1


def test_runtime_compatibility_never_invents_original_run_scan_mapping(tmp_path):
    from src.utils.quick_scan_delivery_seal import BLOCK_RUN_SCAN, seal_result_delivery
    from tests.unit import test_quick_scan_c06_complete_seal as legacy

    store, item, _, body = legacy._checkpointed(tmp_path, with_context=False)
    original = copy.deepcopy(legacy.CONTEXT)
    for row in original["questions"].values():
        row["metadata"].update(run_id="UNMAPPED_OLD_RUN", scan_id="UNMAPPED_OLD_SCAN")
    store.attach_standard_inputs(
        item["work_item_id"], observation_context=original, standard_answer=body
    )
    current = copy.deepcopy(original)
    for row in current["questions"].values():
        row["metadata"].update(run_id="CURRENT_OTHER_RUN", scan_id="CURRENT_OTHER_SCAN")
    _, authority = loaded_authority(tmp_path, current, "2.0.0")
    refs = {(r["run_id"], r["scan_id"]) for r in store.list_run_refs(item["work_item_id"])}
    assert ("UNMAPPED_OLD_RUN", "UNMAPPED_OLD_SCAN") not in refs
    outcome = seal_result_delivery(store, item["work_item_id"], authority=authority)
    assert outcome["action"] == "blocked" and outcome["block_code"] == BLOCK_RUN_SCAN
    assert store.list_delivery_revisions(item["work_item_id"]) == []


def test_shared_compatibility_compares_exact_canonical_stable_values():
    from src.utils.quick_scan_observation_context import (
        validate_resumed_context_compatibility,
    )

    original = fx._v2_context()
    original["questions"]["IQS_01"]["subject_binding"]["question_contract"]["synthetic_weight"] = 1
    current = copy.deepcopy(original)
    current["questions"]["IQS_01"]["subject_binding"]["question_contract"]["synthetic_weight"] = 1.0
    with pytest.raises(ValueError, match="stable_inputs_changed"):
        validate_resumed_context_compatibility(
            original,
            current,
            original_sha256=canonical_sha256(original),
            current_sha256=canonical_sha256(current),
        )


def test_authorized_response_survives_later_owner_refusal_without_future_post(
    tmp_path, monkeypatch
):
    values = prepared_unsent(tmp_path, monkeypatch)
    store, _, lifecycle, questions, session, _, handles, original, current, _ = values
    _, authority = loaded_authority(tmp_path, current)
    lifecycle._c06_authority = authority
    original_post = session.post.side_effect
    valid_send_checks = []

    def post(*args, **kwargs):
        assert lifecycle._owner_refresh.check.side_effect is None
        valid_send_checks.append(lifecycle._owner_refresh.check.call_count)
        assert all(
            store.get_item(h["work_item_id"])["lease_expires_at"] > store.clock() for h in handles
        )
        response = original_post(*args, **kwargs)
        # Original send authorization passed; only the permission for a FUTURE
        # dispatch changes after the successful response, not this valid lease.
        lifecycle._owner_refresh.check.side_effect = RuntimeError(
            "synthetic permission revoked after response"
        )
        return response

    session.post.side_effect = post
    result = execute(values)
    assert session.post.call_count == 1 and valid_send_checks[0] > 0
    for h in handles:
        wid = h["work_item_id"]
        assert store.get_answer_checkpoint(wid) is not None
        assert store.get_standard_answer(wid) is not None
        assert store.get_result_delivery(wid)["state"] == "ready"
        assert store.get_observation_context(wid)["context"] == original
    assert all(r.answer.status == "scored" for r in result.results)
    with pytest.raises(RuntimeError, match="permission revoked after response"):
        execute(values, selected=[questions[-1]])
    assert session.post.call_count == 1
