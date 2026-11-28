"""1.5 concentrated native-budget projections; no authority granted by these tests."""

import copy
import json
import os
from contextlib import closing
from pathlib import Path

import pytest

from src.utils import quick_scan_owner_budget_continuation as api

ROOT = Path(os.environ.get("IQS_V15_EVIDENCE_ROOT", str(Path(__file__).resolve().parents[3])))
pytestmark = pytest.mark.skipif(
    os.environ.get("IQS_V15_PRIVATE_ORIGINAL") != "1",
    reason="explicit isolated original101 copy verification only",
)


def original101():
    return ROOT / "evidence/original101-backup.sqlite", json.loads(
        (ROOT / "evidence/original-budget-policy.json").read_text(encoding="utf8")
    )


def test_v15_projects_all_three_native_exposures_without_changing_original_copy():
    path, policy = original101()
    before = path.read_bytes()
    snapshot = api.exposure_snapshot(path, policy, dialect="1.5.0")
    assert path.read_bytes() == before
    assert snapshot["protocol"] == "stockqa.owner_exposure_snapshot/1.1.0"
    assert snapshot["requests"] == 101 and len(snapshot["attempts"]) == 3
    assert sorted(row["native"]["kind"] for row in snapshot["attempts"]) == [
        "budget_only",
        "budget_only",
        "native_pack",
    ]
    pack = next(row for row in snapshot["attempts"] if row["native"]["kind"] == "native_pack")
    assert len(pack["native"]["members"]) == 12
    assert pack["native"]["transport_state"] == "known_received"
    assert pack["native"]["usable_visible_output"] is False
    assert pack["actual_cost_micros"] is None and pack["in_flight"] == 0
    unknown = next(row for row in snapshot["attempts"] if row["status"] == "outcome_uncertain")
    assert unknown["native"]["transport_state"] == "unknown" and unknown["in_flight"] == 1


@pytest.mark.parametrize("bad", ["missing", "duplicate", "foreign"])
def test_v15_supplied_references_must_equal_all_current_blockers(bad):
    path, policy = original101()
    snapshot = api.exposure_snapshot(path, policy, dialect="1.5.0")
    refs = [row["budget_attempt_id"] for row in snapshot["attempts"]]
    refs = (
        refs[:-1]
        if bad == "missing"
        else refs + [refs[0]] if bad == "duplicate" else refs[:-1] + ["DISPATCH_foreign"]
    )
    with pytest.raises(ValueError):
        api.exposure_snapshot(path, policy, refs, dialect="1.5.0")


def test_old_v14_cannot_be_silently_upgraded_to_three_or_native():
    path, policy = original101()
    with pytest.raises(ValueError, match="exact_two"):
        api.exposure_snapshot(path, policy, [])


def pure_control(*, same_subject=False):
    path, policy = original101()
    snap = api.exposure_snapshot(path, policy, dialect="1.5.0")
    native = next(
        row["native"] for row in snap["attempts"] if row["native"]["kind"] == "native_pack"
    )
    subject = copy.deepcopy(native["members"][0]["subject_ref"])
    if not same_subject:
        subject.update(entity_id="ENT_DIFFERENT", scope_id="ENT_DIFFERENT")
    from src.utils.quick_scan_refresh_inspect import ReadOnlyWorkView

    member = native["members"][0]
    source = ReadOnlyWorkView(path).get_observation_context(member["work_item_id"])["context"][
        "questions"
    ][member["question_id"]]["subject_binding"]["question_contract"]
    ids = [member["question_id"]]
    bodies = {ids[0]: api.digest(source)}
    cutoff = member["information_cutoff"] if same_subject else "2026-10-18"
    profile = dict(
        protocol="iqs.company-scan-profile/1.0.0",
        mode="one_shot_packs",
        question_ids=ids,
        packs=[ids],
        pack_by="module",
        max_questions_per_pack=12,
        route_id="mimo-route",
        information_cutoff=cutoff,
        resource_limits={"max_search_queries_per_session": 0},
        question_bindings=[
            dict(
                question_id=q,
                question_object_sha256=bodies[q],
                module_id=source["module_id"],
                module_version="1.0.0",
            )
            for q in ids
        ],
        native_pack_group_sha256={
            q: api.digest(
                [
                    subject["entity_id"],
                    None,
                    subject["scope"],
                    subject["scope_id"],
                    subject["perimeter_sha256"],
                ]
            )
            for q in ids
        },
    )
    cell = dict(
        cell_id="human-label",
        question_ids=ids,
        question_body_sha256=bodies,
        subject_ref=subject,
        provider="mimo",
        model="mimo-v2.6-pro",
        route_id="mimo-route",
        quota_group="mimo",
        generation=1,
        manifest_raw_sha256="b" * 64,
        controls=dict(
            thinking_enabled=True,
            max_output_tokens=16384,
            strategy_id="module_packs",
            search_policy_sha256=api.digest(api.NO_EXTERNAL_SEARCH_POLICY),
            replicate_ordinal=1,
        ),
        execution_profile=profile,
        execution_profile_sha256=api.digest(profile),
    )
    core = dict(
        protocol="iqs.refresh_request/1.5.0",
        consumer_id="iqs",
        cohort_id="one-original-action",
        question_ids=ids,
        subject_refs=[subject],
        execution_profile_sha256=cell["execution_profile_sha256"],
        source_query={"payload": {"information_cutoff": cutoff}},
    )
    return dict(
        protocol="stockqa.owner_budget_continuation/1.1.0",
        mode="explicit_repeat_exposure",
        reference_scope="unknown_reference_scope",
        purpose="independent_comparison",
        consumer_id="iqs",
        cohort_id=core["cohort_id"],
        ledger_path=str(path),
        snapshot=snap,
        future_request_core=core,
        cells=[cell],
        max_new_http_requests=1,
        valid_from="2026-10-18T00:00:00Z",
        expires_at="2026-10-19T00:00:00Z",
        no_retry_original_intents=True,
        unknown_cash_remains_unknown=True,
        retained_original_requests=True,
        original_process_stopped=True,
        stop_evidence_ref="synthetic-unit-risk-ack",
        additional_risk_hold_micros=0,
    )


def test_pure_v15_profile_control_integrity_and_semantic_identity():
    control = pure_control()
    assert api.validate_control(control) == control
    key = api.cell_key(control["cells"][0])
    renamed = copy.deepcopy(control["cells"][0])
    renamed.update(cell_id="another-name", generation=400, manifest_raw_sha256="c" * 64)
    assert api.cell_key(renamed) == key
    renamed["execution_profile"]["resource_limits"]["max_model_requests_per_session"] = 4
    renamed["execution_profile_sha256"] = api.digest(renamed["execution_profile"])
    assert api.cell_key(renamed) == key
    renamed["controls"]["replicate_ordinal"] = 2
    assert api.cell_key(renamed) != key
    dated = copy.deepcopy(control["cells"][0])
    dated["execution_profile"]["information_cutoff"] = "2026-10-19"
    assert api.cell_key(dated) != key


@pytest.mark.parametrize(
    "mutation",
    [
        "omit_source",
        "tamper_origin",
        "tamper_member",
        "tamper_intent",
        "tamper_profile",
        "renamed_request_profile",
        "wrong_strategy",
        "search",
        "two_packs",
    ],
)
def test_malformed_v15_risk_or_profile_cannot_be_used(mutation):
    import sqlite3

    from src.utils import quick_scan_owner_budget_continuation_v15 as v15

    control = pure_control()
    if mutation == "omit_source":
        control["snapshot"]["attempts"].pop()
    elif mutation in {"tamper_origin", "tamper_member", "tamper_intent"}:
        pack = next(
            row for row in control["snapshot"]["attempts"] if row["native"]["kind"] == "native_pack"
        )
        if mutation == "tamper_origin":
            pack["native"]["kind"] = "budget_only"
        elif mutation == "tamper_member":
            pack["native"]["members"][0]["work_item_id"] = "WORK_DIFFERENT"
        else:
            pack["native"]["intent"]["request_payload_sha256"] = "f" * 64
    else:
        cell = control["cells"][0]
        if mutation == "tamper_profile":
            cell["execution_profile"]["route_id"] = "different"
        elif mutation == "renamed_request_profile":
            control["future_request_core"]["execution_profile_sha256"] = "d" * 64
        elif mutation == "wrong_strategy":
            cell["controls"]["strategy_id"] = "free_agent"
        elif mutation == "search":
            cell["execution_profile"]["resource_limits"]["max_search_queries_per_session"] = 1
        else:
            cell["execution_profile"]["packs"] = [cell["question_ids"], cell["question_ids"]]
    path, _ = original101()
    with pytest.raises((ValueError, KeyError)):
        api.validate_control(control)
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            v15.validate_retained_rows(db, control, control["snapshot"]["policy_id"])


def test_original_native_members_cannot_be_rewrapped_as_a_new_profile():
    import sqlite3

    from src.utils import quick_scan_owner_budget_continuation_v15 as v15

    path, _ = original101()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        v15.assert_independent(db, pure_control())
        with pytest.raises(ValueError, match="original_resolution"):
            v15.assert_independent(db, pure_control(same_subject=True))


def test_v14_policy_cannot_accept_v15_control_and_profile():
    control = pure_control()
    request = dict(control["future_request_core"], continuation_decision_ref="owner-ref")
    policy = dict(
        protocol="stockwiki.refresh_owner_policy/1.4.0",
        budget_exposure_acceptance={"continuation_decision_ref": "owner-ref", "control": control},
        provider="mimo",
        model="mimo-v2.6-pro",
        allowed_consumer_ids=["iqs"],
    )
    with pytest.raises(ValueError, match="dialect"):
        api.accepted_control(policy, request, control["ledger_path"], now=1792324800)


def test_true_new_cutoff_is_distinct_but_does_not_grant_original_generation():
    import sqlite3

    from src.utils import quick_scan_owner_budget_continuation_v15 as v15
    from src.utils.quick_scan_refresh_inspect import ReadOnlyWorkView

    c = pure_control(same_subject=True)
    path, _ = original101()
    c["cells"][0]["execution_profile"]["information_cutoff"] = "2026-10-18"
    c["cells"][0]["execution_profile_sha256"] = api.digest(c["cells"][0]["execution_profile"])
    c["future_request_core"]["execution_profile_sha256"] = c["cells"][0]["execution_profile_sha256"]
    c["future_request_core"]["source_query"]["payload"]["information_cutoff"] = "2026-10-18"
    api.validate_control(c)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        v15.assert_independent(db, c)
    subject = c["cells"][0]["subject_ref"]
    qid = c["cells"][0]["question_ids"][0]
    projection = ReadOnlyWorkView(path).owner_refresh_projection(
        entity_id=subject["entity_id"],
        question_ids=[qid],
        scope_bindings={qid: {"scope": subject["scope"], "scope_id": subject["scope_id"]}},
    )
    assert projection[qid]["status"] == "leased"
    # Existing work remains unresolved; distinction is not a retry/newgen grant.
    assert projection[qid]["generation"] == 1


def test_explicit_replicate_is_distinct_only_from_native_original_cell():
    import sqlite3

    from src.utils import quick_scan_owner_budget_continuation_v15 as v15

    c = pure_control(same_subject=True)
    path, _ = original101()
    c["cells"][0]["controls"]["replicate_ordinal"] = 2
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        v15.assert_independent(db, c)
