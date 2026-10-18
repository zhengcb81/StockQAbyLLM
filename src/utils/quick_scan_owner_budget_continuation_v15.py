"""1.5 retained-exposure projections on the original ledger, no dispatch authority.

This is a private dialect implementation of the original continuation module.
It creates no tables and never mutates, prices or retries an exposed request.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

from .quick_scan_owner_budget_continuation import (
    ATTEMPT_FIELDS,
    _integer,
    _sha,
    _text,
    _utc,
    canonical,
    digest,
)

PROTOCOL = "stockqa.owner_budget_continuation/1.1.0"
SNAPSHOT = "stockqa.owner_exposure_snapshot/1.1.0"
BLOCKING = ("response_unpriced", "failure_unpriced", "outcome_uncertain")
MAX_EXPOSURES = 350


def _read_view(connection, path):
    from .quick_scan_refresh_inspect import ReadOnlyWorkView

    # The original native/inspection transaction already owns this connection.
    # Borrow its read methods without creating a second connection or writer.
    class BoundReadView(ReadOnlyWorkView):
        def __init__(self, path, connection):
            self.path = Path(path)
            self._inspection_connection = connection

        def get_question_pack(self, pack_id):
            from .quick_scan_question_pack_journal import _read

            return _read(self._inspection_connection, pack_id)

    return BoundReadView(path, connection)


def native_projection(connection, path, row):
    from .quick_scan_owner_refresh_journal import read as read_binding
    from .quick_scan_question_pack_journal import _read as read_pack
    from .quick_scan_research_intent import get_intent_response

    view = _read_view(connection, path)
    ref = row["budget_attempt_id"]
    packsend = connection.execute(
        "SELECT pack_id FROM quick_scan_question_pack_send WHERE budget_attempt_id=?", (ref,)
    ).fetchone()
    single = (
        None
        if row["work_attempt_id"] is None
        else connection.execute(
            "SELECT * FROM attempt WHERE attempt_id=?", (row["work_attempt_id"],)
        ).fetchone()
    )
    if row["work_attempt_id"] is not None and single is None:
        raise ValueError("owner_exposure_native_single_missing")
    if packsend is not None and single is not None:
        raise ValueError("owner_exposure_ambiguous_native_origin")
    receipt = view.get_attempt_response(ref)
    pack = None if packsend is None else read_pack(connection, packsend[0])
    if pack is not None and pack["budget_attempt_id"] != ref:
        raise ValueError("owner_exposure_pack_send_mismatch")
    ids = (
        [single["work_item_id"]]
        if single is not None
        else [] if pack is None else [m["work_item_id"] for m in pack["members"]]
    )
    members = []
    for workid in sorted(ids):
        work = connection.execute(
            "SELECT * FROM work_item WHERE work_item_id=?", (workid,)
        ).fetchone()
        if work is None:
            raise ValueError("owner_exposure_native_work_missing")
        binding = read_binding(connection, workid)
        context = view.get_observation_context(workid)
        subject = None
        if context is not None:
            if digest(context["context"]) != context["context_sha256"]:
                raise ValueError("owner_exposure_context_hash_changed")
            subject = copy.deepcopy(
                context["context"]["questions"][work["question_id"]]["subject_binding"][
                    "subject_ref"
                ]
            )
        members.append(
            dict(
                work_item_id=workid,
                entity_id=work["entity_id"],
                question_id=work["question_id"],
                scope=work["scope"],
                scope_id=work["scope_id"],
                generation=work["generation"],
                question_fingerprint=work["question_fingerprint"],
                routing_fingerprint=work["routing_fingerprint"],
                subject_ref=subject,
                owner_binding_sha256=None if binding is None else digest(binding["binding"]),
                information_cutoff=(
                    None if binding is None else binding["binding"]["information_cutoff"]
                ),
            )
        )
    intent_rows = connection.execute(
        "SELECT * FROM quick_scan_research_intent WHERE native_attempt_id=?", (ref,)
    ).fetchall()
    if len(intent_rows) > 1:
        raise ValueError("owner_exposure_ambiguous_native_intent")
    intent = None
    if intent_rows:
        source = intent_rows[0]
        verified = get_intent_response(view, source["task_sha256"], source["intent_sha256"])
        if not members or verified["work_item_ids"] != sorted(ids):
            raise ValueError("owner_exposure_native_intent_members_changed")
        intent = {
            k: source[k]
            for k in ("task_sha256", "intent_sha256", "request_payload_sha256", "binding_sha256")
        }
    safe = view.get_research_visible_output(ref)
    physical = (
        (None if single is None else {"uncertain": "unknown"}.get(single["phase"], single["phase"]))
        if pack is None
        else pack["physical_outcome"]
    )
    if physical not in {None, "response_available", "unknown", "confirmed_failure"}:
        physical = None
    if (
        row["http_status_code"] is not None
        or physical == "response_available"
        or receipt is not None
        and receipt["receipt"].get("http_status_code") is not None
    ):
        transport = "known_received"
    elif physical == "confirmed_failure":
        transport = "confirmed_failure"
    else:
        transport = "unknown"
    return dict(
        kind=(
            "native_pack"
            if pack is not None
            else "native_single" if single is not None else "budget_only"
        ),
        members=members,
        pack_id=None if pack is None else pack["pack_id"],
        pack_manifest_sha256=None if pack is None else pack["manifest_sha256"],
        request_payload_sha256=(
            (
                (None if intent is None else intent["request_payload_sha256"])
                if receipt is None
                else receipt["receipt"].get("request_payload_sha256")
            )
            if pack is None
            else pack["request_payload_sha256"]
        ),
        native_receipt_sha256=None if receipt is None else receipt["receipt_sha256"],
        pack_binding_sha256=None if pack is None else digest(pack),
        single_attempt_sha256=None if single is None else digest(dict(single)),
        intent=intent,
        physical_outcome=physical,
        members_uncertain=None if pack is None else pack["members_uncertain"],
        transport_state=transport,
        usable_visible_output=safe is not None,
        visible_output_sha256=None if safe is None else digest(safe),
    )


def snapshot_tx(connection, path, policy_id, attempt_ids=None):
    stored = connection.execute(
        "SELECT * FROM quick_scan_budget_policy WHERE policy_id=?", (policy_id,)
    ).fetchone()
    if stored is None:
        raise ValueError("owner_exposure_policy_missing")
    rows = connection.execute(
        "SELECT * FROM quick_scan_budget_attempt WHERE policy_id=? AND status IN ('response_unpriced','failure_unpriced','outcome_uncertain') ORDER BY budget_attempt_id",
        (policy_id,),
    ).fetchall()
    refs = [row["budget_attempt_id"] for row in rows]
    if not 1 <= len(rows) <= MAX_EXPOSURES:
        raise ValueError("owner_exposure_bounded_complete_sources_required")
    if attempt_ids is not None and (
        not isinstance(attempt_ids, list)
        or any(not isinstance(v, str) for v in attempt_ids)
        or len(set(attempt_ids)) != len(attempt_ids)
        or set(attempt_ids) != set(refs)
    ):
        raise ValueError("owner_exposure_complete_current_sources_required")
    attempts = []
    for row in rows:
        if row["actual_cost_micros"] is not None or row["reconciled_at"] is not None:
            raise ValueError("owner_exposure_source_cash_inconsistent")
        attempts.append(
            dict(
                {k: row[k] for k in ATTEMPT_FIELDS}, native=native_projection(connection, path, row)
            )
        )
    totals = connection.execute(
        "SELECT COALESCE(SUM(request_counted),0),COALESCE(SUM(CASE WHEN status!='settled' THEN reserved_cost_micros ELSE 0 END),0),COALESCE(SUM(CASE WHEN status='settled' THEN actual_cost_micros ELSE 0 END),0) FROM quick_scan_budget_attempt WHERE policy_id=?",
        (policy_id,),
    ).fetchone()
    return dict(
        protocol=SNAPSHOT,
        policy_id=policy_id,
        policy_snapshot_sha256=stored["policy_snapshot_sha256"],
        attempts=attempts,
        requests=totals[0],
        reserved_micros=totals[1],
        spent_micros=totals[2],
        cash_actual_complete=False,
    )


def exposure_snapshot(path, policy, attempt_ids=None):
    from contextlib import closing

    from .quick_scan_refresh_inspect import ReadOnlyWorkView
    from .quick_scan_work_store import _budget_policy_projection

    normalized = _budget_policy_projection(policy)
    view = ReadOnlyWorkView(path)
    with closing(view._connect()) as connection:
        connection.execute("BEGIN")
        result = snapshot_tx(connection, path, normalized["policy_id"], attempt_ids)
        if result["policy_snapshot_sha256"] != normalized["policy_snapshot_sha256"]:
            raise ValueError("owner_exposure_policy_changed")
        return result


def semantic_cell(cell):
    profile = cell["execution_profile"]
    subject = cell["subject_ref"]
    stable = {k: subject[k] for k in ("entity_id", "scope", "scope_id", "perimeter_sha256")}
    return dict(
        protocol="stockqa.owner_comparison_semantics/1.0.0",
        subject=stable,
        question_bindings=sorted(profile["question_bindings"], key=lambda r: r["question_id"]),
        information_cutoff=profile["information_cutoff"],
        packs=sorted([sorted(p) for p in profile["packs"]]),
        strategy_id=cell["controls"]["strategy_id"],
        pack_by=profile["pack_by"],
        route_id=cell["route_id"],
        provider=cell["provider"],
        model=cell["model"],
        controls={
            k: cell["controls"][k]
            for k in (
                "thinking_enabled",
                "max_output_tokens",
                "search_policy_sha256",
                "replicate_ordinal",
            )
        },
    )


def validate_profile(cell):
    profile = cell["execution_profile"]
    _sha(cell["execution_profile_sha256"])
    keys = {
        "protocol",
        "mode",
        "question_ids",
        "packs",
        "pack_by",
        "max_questions_per_pack",
        "route_id",
        "information_cutoff",
        "resource_limits",
        "question_bindings",
        "native_pack_group_sha256",
    }
    if (
        not isinstance(profile, dict)
        or set(profile) != keys
        or digest(profile) != cell["execution_profile_sha256"]
        or profile["protocol"] != "iqs.company-scan-profile/1.0.0"
        or profile["mode"] != "one_shot_packs"
        or profile["pack_by"] != "module"
    ):
        raise ValueError("owner_exposure_exact_native_profile_required")
    qids = cell["question_ids"]
    if (
        not isinstance(qids, list)
        or not 1 <= len(qids) <= 12
        or len(set(qids)) != len(qids)
        or profile["question_ids"] != qids
        or profile["packs"] != [qids]
        or profile["route_id"] != cell["route_id"]
    ):
        raise ValueError("owner_exposure_single_native_pack_required")
    _integer(profile["max_questions_per_pack"], minimum=len(qids), maximum=12)
    from datetime import date

    if (
        date.fromisoformat(profile["information_cutoff"]).isoformat()
        != profile["information_cutoff"]
    ):
        raise ValueError("owner_exposure_real_cutoff_required")
    bindings = profile["question_bindings"]
    if (
        not isinstance(bindings, list)
        or len(bindings) != len(qids)
        or [r.get("question_id") for r in bindings] != qids
    ):
        raise ValueError("owner_exposure_profile_question_bindings_required")
    for row in bindings:
        if set(row) != {
            "question_id",
            "question_object_sha256",
            "module_id",
            "module_version",
        } or row["question_object_sha256"] != cell["question_body_sha256"].get(row["question_id"]):
            raise ValueError("owner_exposure_profile_body_mismatch")
        _sha(row["question_object_sha256"])
        _text(row["module_id"])
        _text(row["module_version"])
    if len({(row["module_id"], row["module_version"]) for row in bindings}) != 1:
        raise ValueError("owner_exposure_one_original_module_pack_required")
    subject = cell["subject_ref"]
    group = digest(
        [
            subject["entity_id"],
            None,
            subject["scope"],
            subject["scope_id"],
            subject["perimeter_sha256"],
        ]
    )
    if profile["native_pack_group_sha256"] != {qid: group for qid in qids}:
        raise ValueError("owner_exposure_native_group_mismatch")
    limits = profile["resource_limits"]
    from .quick_scan_owner_budget_continuation import NO_EXTERNAL_SEARCH_POLICY

    if (
        not isinstance(limits, dict)
        or limits.get("max_search_queries_per_session") != 0
        or cell["controls"]["search_policy_sha256"] != digest(NO_EXTERNAL_SEARCH_POLICY)
        or cell["controls"]["strategy_id"] != "module_packs"
    ):
        raise ValueError("owner_exposure_zero_search_module_strategy_required")
    # Full resource ceilings and deterministic packing are revalidated by the
    # original Company build/validate and postadmission factory before POST.
    return profile


def validate_control(value):
    fields = {
        "protocol",
        "mode",
        "reference_scope",
        "purpose",
        "consumer_id",
        "cohort_id",
        "ledger_path",
        "snapshot",
        "future_request_core",
        "cells",
        "max_new_http_requests",
        "valid_from",
        "expires_at",
        "no_retry_original_intents",
        "unknown_cash_remains_unknown",
        "retained_original_requests",
        "original_process_stopped",
        "stop_evidence_ref",
        "additional_risk_hold_micros",
    }
    if not isinstance(value, dict) or set(value) != fields or value["protocol"] != PROTOCOL:
        raise ValueError("owner_exposure_v15_control_dialect_invalid")
    if any(
        value[k] != v
        for k, v in {
            "mode": "explicit_repeat_exposure",
            "reference_scope": "unknown_reference_scope",
            "purpose": "independent_comparison",
        }.items()
    ):
        raise ValueError("owner_exposure_v15_acceptance_required")
    for key in ("consumer_id", "cohort_id", "stop_evidence_ref"):
        _text(value[key])
    if not isinstance(value["ledger_path"], str) or not Path(value["ledger_path"]).is_absolute():
        raise ValueError("owner_exposure_ledger_invalid")
    for key in (
        "no_retry_original_intents",
        "unknown_cash_remains_unknown",
        "retained_original_requests",
        "original_process_stopped",
    ):
        if value[key] is not True:
            raise ValueError("owner_exposure_acceptance_required")
    if value["max_new_http_requests"] != 1 or type(value["max_new_http_requests"]) is not int:
        raise ValueError("owner_exposure_one_future_native_post_required")
    _integer(value["additional_risk_hold_micros"], maximum=100000000000)
    if _utc(value["valid_from"]) >= _utc(value["expires_at"]):
        raise ValueError("owner_exposure_validity_invalid")
    core = value["future_request_core"]
    if (
        core.get("protocol") != "iqs.refresh_request/1.5.0"
        or "continuation_decision_ref" in core
        or "continuation_review_ref" in core
        or core.get("consumer_id") != value["consumer_id"]
        or core.get("cohort_id") != value["cohort_id"]
    ):
        raise ValueError("owner_exposure_future_action_mismatch")
    cells = value["cells"]
    base = {
        "cell_id",
        "question_ids",
        "question_body_sha256",
        "subject_ref",
        "provider",
        "model",
        "route_id",
        "quota_group",
        "generation",
        "controls",
        "manifest_raw_sha256",
        "execution_profile",
        "execution_profile_sha256",
    }
    if (
        not isinstance(cells, list)
        or len(cells) != 1
        or not isinstance(cells[0], dict)
        or set(cells[0]) != base
    ):
        raise ValueError("owner_exposure_single_v15_cell_required")
    cell = cells[0]
    for key in ("cell_id", "provider", "model", "route_id", "quota_group"):
        _text(cell[key])
    _integer(cell["generation"], minimum=1)
    _sha(cell["manifest_raw_sha256"])
    if set(cell["question_body_sha256"]) != set(cell["question_ids"]):
        raise ValueError("owner_exposure_body_scope_required")
    controls = cell["controls"]
    if (
        not isinstance(controls, dict)
        or set(controls)
        != {
            "thinking_enabled",
            "max_output_tokens",
            "strategy_id",
            "search_policy_sha256",
            "replicate_ordinal",
        }
        or controls["thinking_enabled"] is not True
    ):
        raise ValueError("owner_exposure_controls_required")
    _integer(controls["max_output_tokens"], minimum=1, maximum=32768)
    _integer(controls["replicate_ordinal"], minimum=1, maximum=350)
    profile = validate_profile(cell)
    if (
        core.get("execution_profile_sha256") != cell["execution_profile_sha256"]
        or core.get("question_ids") != cell["question_ids"]
        or core.get("subject_refs") != [cell["subject_ref"]]
        or core.get("source_query", {}).get("payload", {}).get("information_cutoff")
        != profile["information_cutoff"]
    ):
        raise ValueError("owner_exposure_profile_future_scope_mismatch")
    snap = value["snapshot"]
    if (
        not isinstance(snap, dict)
        or snap.get("protocol") != SNAPSHOT
        or snap.get("cash_actual_complete") is not False
        or not isinstance(snap.get("attempts"), list)
        or not 1 <= len(snap["attempts"]) <= MAX_EXPOSURES
    ):
        raise ValueError("owner_exposure_source_snapshot_required")
    refs = []
    for row in snap["attempts"]:
        if (
            set(row) != set(ATTEMPT_FIELDS) | {"native"}
            or row["status"] not in BLOCKING
            or row["actual_cost_micros"] is not None
            or row["reconciled_at"] is not None
        ):
            raise ValueError("owner_exposure_source_projection_invalid")
        _text(row["budget_attempt_id"])
        refs.append(row["budget_attempt_id"])
    if len(set(refs)) != len(refs):
        raise ValueError("owner_exposure_duplicate_source")
    return copy.deepcopy(value)


def validate_retained_rows(connection, control, policy_id):
    actual = snapshot_tx(connection, control["ledger_path"], policy_id)
    if actual != control["snapshot"]:
        raise ValueError("owner_exposure_complete_snapshot_changed")
    return [row["budget_attempt_id"] for row in actual["attempts"]]


def _proven_new_comparison(connection, control, work):
    """Distinguish original semantic scope; never grants a work generation.

    Native refresh planning still requires a genuinely eligible original new
    scope/generation. Missing provenance cannot establish independence. Merely
    changing a carrier, task, profile digest, strategy or generation is useless.
    """
    from .quick_scan_owner_refresh_journal import read as read_binding

    cell = control["cells"][0]
    qid = work["question_id"]
    binding = read_binding(connection, work["work_item_id"])
    context = _read_view(connection, control["ledger_path"]).get_observation_context(
        work["work_item_id"]
    )
    if (
        binding is None
        or context is None
        or digest(context["context"]) != context["context_sha256"]
    ):
        return False
    source = context["context"].get("questions", {}).get(qid, {}).get("subject_binding", {})
    original_subject = source.get("subject_ref")
    body = source.get("question_contract")
    if not isinstance(original_subject, dict) or not isinstance(body, dict):
        return False
    old_cutoff = binding["binding"]["information_cutoff"]
    metadata = context["context"]["questions"][qid].get("metadata", {})
    if metadata.get("information_cutoff") != old_cutoff:
        return False
    subject_keys = ("entity_id", "scope", "scope_id", "perimeter_sha256")
    if any(original_subject.get(key) is None for key in subject_keys):
        return False
    if {key: original_subject[key] for key in subject_keys} != {
        key: cell["subject_ref"][key] for key in subject_keys
    }:
        return True
    if cell["execution_profile"]["information_cutoff"] != old_cutoff:
        return True
    new_binding = next(
        row for row in cell["execution_profile"]["question_bindings"] if row["question_id"] == qid
    )
    if body.get("id") != qid or not isinstance(body.get("module_id"), str):
        return False
    if (
        digest(body) != new_binding["question_object_sha256"]
        or body["module_id"] != new_binding["module_id"]
    ):
        return True
    prior = []
    for row in connection.execute(
        "SELECT cell_json,work_item_ids_json FROM quick_scan_owner_continuation_cell WHERE policy_id=?",
        (control["snapshot"]["policy_id"],),
    ):
        if work["work_item_id"] in json.loads(row["work_item_ids_json"]):
            prior.append(json.loads(row["cell_json"]))
    if len(prior) == 1:
        old_ordinal = prior[0].get("controls", {}).get("replicate_ordinal")
        new_ordinal = cell["controls"]["replicate_ordinal"]
        if type(old_ordinal) is int and old_ordinal > 0 and new_ordinal != old_ordinal:
            return True
    return False


def assert_independent(connection, control, confirmation_id=None):
    """No renamed carrier may take over an old native semantic comparison."""
    cell = control["cells"][0]
    subject = cell["subject_ref"]
    for qid in cell["question_ids"]:
        work = connection.execute(
            "SELECT w.* FROM work_item w WHERE w.entity_id=? AND w.question_id=? AND w.scope=? AND w.scope_id=? AND w.status NOT IN ('failed','result_ready','delivered') AND (EXISTS(SELECT 1 FROM attempt a WHERE a.work_item_id=w.work_item_id) OR EXISTS(SELECT 1 FROM quick_scan_question_pack_member m WHERE m.work_item_id=w.work_item_id))",
            (subject["entity_id"], qid, subject["scope"], subject["scope_id"]),
        ).fetchall()
        if any(not _proven_new_comparison(connection, control, row) for row in work):
            raise ValueError("owner_exposure_existing_native_members_require_original_resolution")
    key = digest(semantic_cell(cell))
    old = connection.execute(
        "SELECT confirmation_id FROM quick_scan_owner_continuation_cell WHERE policy_id=? AND cell_key=?",
        (control["snapshot"]["policy_id"], key),
    ).fetchone()
    if old is not None and old[0] != confirmation_id:
        raise ValueError("owner_exposure_duplicate_semantic_comparison")


def validate_original_bindings(cell, qmap, bundles):
    versions = {
        row["module_id"]: row["version"]
        for bundle in bundles.values()
        for row in bundle["manifest"]["module_locks"]
    }
    expected = []
    for qid in cell["question_ids"]:
        row = qmap[qid]
        expected.append(
            dict(
                question_id=qid,
                question_object_sha256=digest(row),
                module_id=row["module_id"],
                module_version=versions[row["module_id"]],
            )
        )
    if cell["execution_profile"]["question_bindings"] != expected:
        raise ValueError("owner_exposure_original_profile_modules_changed")


def assert_profile_task(cell, task):
    if (
        task.raw.get("protocol") != "iqs.company-scan-task/1.0.0"
        or task.raw.get("execution_profile_sha256") != cell["execution_profile_sha256"]
        or task.raw.get("execution_profile") != cell["execution_profile"]
    ):
        raise ValueError("owner_exposure_approved_profile_task_mismatch")
