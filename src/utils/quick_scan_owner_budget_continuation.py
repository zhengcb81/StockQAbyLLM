"""Explicit owner acceptance of retained budget-only exposure, never settlement.

The original budget table remains the sole cost/request ledger. Hashes provide
integrity, not authority. Current fixed native owner policy is read before quote; its final original
confirmation and current checks still authorize every POST. No standalone
installation grant or second ledger is created.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DDL_V24_ADDITIONS = (
    (
        """CREATE TABLE quick_scan_owner_continuation_cell (
        policy_id TEXT NOT NULL REFERENCES quick_scan_budget_policy(policy_id),
        acceptance_ref TEXT NOT NULL, cell_key TEXT NOT NULL,
        confirmation_id TEXT NOT NULL REFERENCES quick_scan_refresh_admission(confirmation_id),
        owner_policy_sha256 TEXT NOT NULL,
        work_item_ids_json TEXT NOT NULL CHECK(json_valid(work_item_ids_json)),
        cell_json TEXT NOT NULL CHECK(json_valid(cell_json)),
        control_json TEXT NOT NULL CHECK(json_valid(control_json)),
        PRIMARY KEY(policy_id,acceptance_ref,cell_key), UNIQUE(policy_id,cell_key))""",
    )
    + tuple(
        f"CREATE TRIGGER quick_scan_owner_continuation_cell_no_{action} BEFORE {action.upper()} ON quick_scan_owner_continuation_cell "
        "BEGIN SELECT RAISE(ABORT,'owner continuation cells are immutable'); END"
        for action in ("update", "delete")
    )
)


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf8")).hexdigest()


def _sha(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError("owner_exposure_sha_invalid")


def _text(value, maximum=200):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("owner_exposure_text_invalid")


def _integer(value, *, minimum=0, maximum=1000000000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("owner_exposure_integer_invalid")


def _utc(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("owner_exposure_utc_invalid")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError("owner_exposure_utc_invalid")
    return parsed


def future_request_core(request):
    if (
        not isinstance(request, dict)
        or request.get("protocol") not in {"iqs.refresh_request/1.4.0", "iqs.refresh_request/1.5.0"}
        or "continuation_review_ref" in request
    ):
        raise ValueError("owner_exposure_request_dialect_invalid")
    result = copy.deepcopy(request)
    result.pop("continuation_decision_ref", None)
    return result


ATTEMPT_FIELDS = (
    "budget_attempt_id",
    "work_attempt_id",
    "policy_id",
    "policy_version",
    "route_id",
    "provider",
    "model_requested",
    "quota_group",
    "currency",
    "pricing_basis",
    "pricing_ref",
    "reserved_cost_micros",
    "actual_cost_micros",
    "request_counted",
    "in_flight",
    "status",
    "http_status_code",
    "cost_source_ref",
    "created_at",
    "send_intent_at",
    "completed_at",
    "reconciled_at",
)


def _fresh_exposure_snapshot(path, policy, *, owner_ack_ref):
    """Owner-explicit fresh budget: zero retained attempts, no prior cash state."""
    from .quick_scan_work_store import _budget_policy_projection

    if not isinstance(owner_ack_ref, str) or not owner_ack_ref.strip():
        raise ValueError("owner_exposure_fresh_ack_required")
    normalized = _budget_policy_projection(policy)
    path = Path(path)
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError("owner_exposure_ledger_redirected")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        stored = db.execute(
            "SELECT * FROM quick_scan_budget_policy WHERE policy_id=?", (normalized["policy_id"],)
        ).fetchone()
        if (
            stored is None
            or stored["policy_snapshot_sha256"] != normalized["policy_snapshot_sha256"]
        ):
            raise ValueError("owner_exposure_policy_changed")
        totals = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(request_counted),0), COALESCE(SUM(reserved_cost_micros),0), "
            "COALESCE(SUM(actual_cost_micros),0) FROM quick_scan_budget_attempt WHERE policy_id=?",
            (normalized["policy_id"],),
        ).fetchone()
        if totals[0] != 0 or totals[1] != 0 or totals[2] != 0 or totals[3] != 0:
            raise ValueError("owner_exposure_fresh_budget_not_empty")
        return {
            "protocol": "stockqa.owner_exposure_snapshot/1.2.0",
            "policy_id": normalized["policy_id"],
            "policy_snapshot_sha256": normalized["policy_snapshot_sha256"],
            "attempts": [],
            "requests": 0,
            "reserved_micros": 0,
            "spent_micros": 0,
            "cash_actual_complete": False,
            "fresh_budget": True,
            "owner_ack_ref": owner_ack_ref,
        }
    finally:
        db.rollback()
        db.close()


def exposure_snapshot(
    path, policy, attempt_ids=None, *, dialect="1.4.0", fresh=False, owner_ack_ref=None
):
    """Read-only before quote; no original row, schema or cost is changed."""
    if fresh:
        if dialect != "1.4.0":
            raise ValueError("owner_exposure_fresh_dialect_unsupported")
        if attempt_ids not in (None, []):
            raise ValueError("owner_exposure_fresh_refs_forbidden")
        return _fresh_exposure_snapshot(path, policy, owner_ack_ref=owner_ack_ref)
    if dialect == "1.5.0":
        from .quick_scan_owner_budget_continuation_v15 import (
            exposure_snapshot as project,
        )

        return project(path, policy, attempt_ids)
    if dialect != "1.4.0":
        raise ValueError("owner_exposure_snapshot_dialect_unsupported")
    from .quick_scan_work_store import _budget_policy_projection

    if (
        not isinstance(attempt_ids, list)
        or len(attempt_ids) != 2
        or len(set(attempt_ids)) != 2
        or any(not isinstance(v, str) or not v.startswith("DISPATCH_") for v in attempt_ids)
    ):
        raise ValueError("owner_exposure_exact_two_budget_refs_required")
    normalized = _budget_policy_projection(policy)
    path = Path(path)
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError("owner_exposure_ledger_redirected")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        stored = db.execute(
            "SELECT * FROM quick_scan_budget_policy WHERE policy_id=?", (normalized["policy_id"],)
        ).fetchone()
        if (
            stored is None
            or stored["policy_snapshot_sha256"] != normalized["policy_snapshot_sha256"]
        ):
            raise ValueError("owner_exposure_policy_changed")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        attempts = []
        for ref in sorted(attempt_ids):
            row = db.execute(
                "SELECT * FROM quick_scan_budget_attempt WHERE budget_attempt_id=? AND policy_id=?",
                (ref, normalized["policy_id"]),
            ).fetchone()
            if (
                row is None
                or row["work_attempt_id"] is not None
                or row["actual_cost_micros"] is not None
                or row["reconciled_at"] is not None
            ):
                raise ValueError("owner_exposure_source_not_budget_only_unknown_cash")
            if row["status"] not in {"outcome_uncertain", "response_unpriced"}:
                raise ValueError("owner_exposure_source_status_invalid")
            if row["status"] == "outcome_uncertain" and row["http_status_code"] is not None:
                raise ValueError("owner_exposure_unknown_http_mismatch")
            if row["status"] == "response_unpriced" and row["http_status_code"] != 200:
                raise ValueError("owner_exposure_response_http_mismatch")
            if (
                "quick_scan_question_pack_send" in tables
                and db.execute(
                    "SELECT 1 FROM quick_scan_question_pack_send WHERE budget_attempt_id=?", (ref,)
                ).fetchone()
            ):
                raise ValueError("owner_exposure_original_pack_cannot_be_bypassed")
            if (
                "quick_scan_research_intent" in tables
                and db.execute(
                    "SELECT 1 FROM quick_scan_research_intent WHERE native_attempt_id=?", (ref,)
                ).fetchone()
            ):
                raise ValueError("owner_exposure_original_intent_cannot_be_bypassed")
            attempts.append({k: row[k] for k in ATTEMPT_FIELDS})
        if {r["status"] for r in attempts} != {"outcome_uncertain", "response_unpriced"}:
            raise ValueError("owner_exposure_exact_unknown_and_http200_required")
        totals = db.execute(
            "SELECT COALESCE(SUM(request_counted),0),COALESCE(SUM(CASE WHEN status!='settled' THEN reserved_cost_micros ELSE 0 END),0),"
            "COALESCE(SUM(CASE WHEN status='settled' THEN actual_cost_micros ELSE 0 END),0) FROM quick_scan_budget_attempt WHERE policy_id=?",
            (normalized["policy_id"],),
        ).fetchone()
        return {
            "protocol": "stockqa.owner_exposure_snapshot/1.0.0",
            "policy_id": normalized["policy_id"],
            "policy_snapshot_sha256": normalized["policy_snapshot_sha256"],
            "attempts": attempts,
            "requests": totals[0],
            "reserved_micros": totals[1],
            "spent_micros": totals[2],
            "cash_actual_complete": False,
        }
    finally:
        db.rollback()
        db.close()


def cell_key(cell):
    if "execution_profile" in cell:
        from .quick_scan_owner_budget_continuation_v15 import semantic_cell

        return digest(semantic_cell(cell))
    # Task/work/generation/label cannot make the same approved comparison new.
    semantic = {
        k: v for k, v in cell.items() if k not in {"cell_id", "generation", "manifest_raw_sha256"}
    }
    semantic["question_ids"] = sorted(cell["question_ids"])
    return digest(semantic)


def validate_control(value):
    if (
        isinstance(value, dict)
        and value.get("protocol") == "stockqa.owner_budget_continuation/1.1.0"
    ):
        from .quick_scan_owner_budget_continuation_v15 import (
            validate_control as validate,
        )

        return validate(value)
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
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("owner_exposure_control_fields_invalid")
    fixed = {
        "protocol": "stockqa.owner_budget_continuation/1.0.0",
        "mode": "explicit_repeat_exposure",
        "reference_scope": "unknown_reference_scope",
        "purpose": "independent_comparison",
    }
    if any(value[k] != v for k, v in fixed.items()):
        raise ValueError("owner_exposure_control_dialect_invalid")
    for k in ("consumer_id", "cohort_id", "stop_evidence_ref"):
        _text(value[k])
    if not isinstance(value["ledger_path"], str) or not Path(value["ledger_path"]).is_absolute():
        raise ValueError("owner_exposure_ledger_invalid")
    for k in (
        "no_retry_original_intents",
        "unknown_cash_remains_unknown",
        "retained_original_requests",
        "original_process_stopped",
    ):
        if value[k] is not True:
            raise ValueError("owner_exposure_acceptance_required")
    _integer(value["max_new_http_requests"], minimum=1, maximum=350)
    _integer(value["additional_risk_hold_micros"], maximum=100000000000)
    if _utc(value["valid_from"]) >= _utc(value["expires_at"]):
        raise ValueError("owner_exposure_validity_invalid")
    core = value["future_request_core"]
    if (
        core.get("protocol") != "iqs.refresh_request/1.4.0"
        or future_request_core(core) != core
        or core.get("cohort_id") != value["cohort_id"]
        or core.get("consumer_id") != value["consumer_id"]
    ):
        raise ValueError("owner_exposure_future_action_mismatch")
    cells = value["cells"]
    if not isinstance(cells, list) or len(cells) != 1:
        raise ValueError("owner_exposure_cells_required")
    cell_fields = {
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
    }
    for cell in cells:
        if not isinstance(cell, dict) or set(cell) != cell_fields:
            raise ValueError("owner_exposure_cell_fields_invalid")
        for k in ("cell_id", "provider", "model", "route_id", "quota_group"):
            _text(cell[k])
        _integer(cell["generation"], minimum=1)
        _sha(cell["manifest_raw_sha256"])
        qids = cell["question_ids"]
        if not isinstance(qids, list) or not 1 <= len(qids) <= 30 or len(set(qids)) != len(qids):
            raise ValueError("owner_exposure_cell_questions_invalid")
        if not isinstance(cell["question_body_sha256"], dict) or set(
            cell["question_body_sha256"]
        ) != set(qids):
            raise ValueError("owner_exposure_question_bodies_required")
        for q in qids:
            _text(q)
            _sha(cell["question_body_sha256"][q])
        if not isinstance(cell["subject_ref"], dict) or not cell["subject_ref"]:
            raise ValueError("owner_exposure_subject_required")
        controls = cell["controls"]
        if not isinstance(controls, dict) or set(controls) != {
            "thinking_enabled",
            "max_output_tokens",
            "strategy_id",
            "search_policy_sha256",
            "replicate_ordinal",
        }:
            raise ValueError("owner_exposure_controls_required")
        if controls["thinking_enabled"] is not True:
            raise ValueError("owner_exposure_thinking_required")
        _integer(controls["max_output_tokens"], minimum=1, maximum=32768)
        _integer(controls["replicate_ordinal"], minimum=1, maximum=350)
        _text(controls["strategy_id"])
        _sha(controls["search_policy_sha256"])
    if len({c["cell_id"] for c in cells}) != len(cells) or len({cell_key(c) for c in cells}) != len(
        cells
    ):
        raise ValueError("owner_exposure_duplicate_semantic_cell")
    if set(core.get("question_ids", [])) != {q for c in cells for q in c["question_ids"]}:
        raise ValueError("owner_exposure_cell_question_scope_mismatch")
    snap = value["snapshot"]
    if isinstance(snap, dict) and snap.get("protocol") == "stockqa.owner_exposure_snapshot/1.2.0":
        if (
            snap.get("fresh_budget") is not True
            or not isinstance(snap.get("owner_ack_ref"), str)
            or not snap["owner_ack_ref"].strip()
            or snap.get("attempts") != []
            or snap.get("requests") != 0
            or snap.get("reserved_micros") != 0
            or snap.get("spent_micros") != 0
            or snap.get("cash_actual_complete") is not False
        ):
            raise ValueError("owner_exposure_fresh_snapshot_invalid")
    elif (
        not isinstance(snap, dict)
        or snap.get("protocol") != "stockqa.owner_exposure_snapshot/1.0.0"
        or snap.get("cash_actual_complete") is not False
    ):
        raise ValueError("owner_exposure_source_snapshot_required")
    return copy.deepcopy(value)


def accepted_control(owner_policy, request, ledger_path, *, now):
    """Pure binding checks only. Its caller must read a native current policy."""
    if owner_policy.get("protocol") not in {
        "stockwiki.refresh_owner_policy/1.4.0",
        "stockwiki.refresh_owner_policy/1.5.0",
    }:
        raise ValueError("owner_exposure_explicit_policy_required")
    accepted = owner_policy.get("budget_exposure_acceptance")
    if not isinstance(accepted, dict) or set(accepted) != {"continuation_decision_ref", "control"}:
        raise ValueError("owner_exposure_exact_acceptance_required")
    _text(accepted["continuation_decision_ref"])
    if request.get("continuation_decision_ref") != accepted["continuation_decision_ref"]:
        raise ValueError("owner_exposure_reference_not_in_native_policy")
    control = validate_control(accepted["control"])
    dialect = owner_policy["protocol"].split("/")[-1]
    if request.get("protocol") != "iqs.refresh_request/" + dialect or control[
        "protocol"
    ] != "stockqa.owner_budget_continuation/" + ("1.1.0" if dialect == "1.5.0" else "1.0.0"):
        raise ValueError("owner_exposure_policy_control_dialect_mismatch")
    if owner_policy.get("provider") != "mimo" or any(
        c["provider"] != "mimo"
        or c["controls"]["strategy_id"] not in {"free_agent", "module_packs"}
        for c in control["cells"]
    ):
        raise ValueError("owner_exposure_supported_carrier_required")
    if (
        future_request_core(request) != control["future_request_core"]
        or Path(control["ledger_path"]).resolve() != Path(ledger_path).resolve()
        or not _utc(control["valid_from"]).timestamp()
        <= now
        < _utc(control["expires_at"]).timestamp()
    ):
        raise ValueError("owner_exposure_action_or_clock_mismatch")
    if request["consumer_id"] not in owner_policy["allowed_consumer_ids"]:
        raise ValueError("owner_exposure_consumer_denied")
    if any(cell["model"] != owner_policy["model"] for cell in control["cells"]):
        raise ValueError("owner_exposure_model_scope_mismatch")
    return accepted["continuation_decision_ref"], control


def validate_retained_rows(connection, control, policy_id):
    """Never erase old cash/request/reservation facts or accept a future unknown."""
    if control.get("protocol") == "stockqa.owner_budget_continuation/1.1.0":
        from .quick_scan_owner_budget_continuation_v15 import (
            validate_retained_rows as validate,
        )

        return validate(connection, control, policy_id)
    source = control["snapshot"]
    if source["policy_id"] != policy_id:
        raise ValueError("owner_exposure_policy_mismatch")
    policy = connection.execute(
        "SELECT * FROM quick_scan_budget_policy WHERE policy_id=?", (policy_id,)
    ).fetchone()
    if policy is None:
        raise ValueError("owner_exposure_budget_policy_changed")
    if (
        not (
            source.get("protocol") == "stockqa.owner_exposure_snapshot/1.2.0"
            and source.get("fresh_budget") is True
        )
        and policy["policy_snapshot_sha256"] != source["policy_snapshot_sha256"]
    ):
        raise ValueError("owner_exposure_budget_policy_changed")
    if (
        source.get("protocol") == "stockqa.owner_exposure_snapshot/1.2.0"
        and source.get("fresh_budget") is True
    ):
        if source.get("attempts") != []:
            raise ValueError("owner_exposure_fresh_snapshot_invalid")
        totals = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(request_counted),0), "
            "COALESCE(SUM(reserved_cost_micros),0), COALESCE(SUM(actual_cost_micros),0) "
            "FROM quick_scan_budget_attempt WHERE policy_id=? AND budget_attempt_id NOT LIKE 'DISPATCH_%'",
            (policy_id,),
        ).fetchone()
        if totals[0] != 0 or totals[1] != 0 or totals[2] != 0 or totals[3] != 0:
            raise ValueError("owner_exposure_fresh_budget_not_empty")
        return []
    attempts = source.get("attempts")
    if not isinstance(attempts, list) or len(attempts) != 2:
        raise ValueError("owner_exposure_exact_sources_required")
    refs = []
    for expected in attempts:
        if not isinstance(expected, dict) or set(expected) != set(ATTEMPT_FIELDS):
            raise ValueError("owner_exposure_source_projection_invalid")
        ref = expected["budget_attempt_id"]
        if (
            not isinstance(ref, str)
            or not ref.startswith("DISPATCH_")
            or expected["work_attempt_id"] is not None
        ):
            raise ValueError("owner_exposure_budget_only_sources_required")
        actual = connection.execute(
            "SELECT * FROM quick_scan_budget_attempt WHERE budget_attempt_id=? AND policy_id=?",
            (ref, policy_id),
        ).fetchone()
        if actual is None or {k: actual[k] for k in ATTEMPT_FIELDS} != expected:
            raise ValueError("owner_exposure_source_row_changed")
        if actual["actual_cost_micros"] is not None or actual["reconciled_at"] is not None:
            raise ValueError("owner_exposure_cash_is_no_longer_unknown")
        if (
            connection.execute(
                "SELECT 1 FROM quick_scan_question_pack_send WHERE budget_attempt_id=?", (ref,)
            ).fetchone()
            or connection.execute(
                "SELECT 1 FROM quick_scan_research_intent WHERE native_attempt_id=?", (ref,)
            ).fetchone()
        ):
            raise ValueError("owner_exposure_original_owner_intent_excluded")
        refs.append(ref)
    if len(set(refs)) != 2 or {a["status"] for a in attempts} != {
        "outcome_uncertain",
        "response_unpriced",
    }:
        raise ValueError("owner_exposure_source_pair_invalid")
    for row in attempts:
        if (row["status"] == "outcome_uncertain" and row["http_status_code"] is not None) or (
            row["status"] == "response_unpriced" and row["http_status_code"] != 200
        ):
            raise ValueError("owner_exposure_source_http_mismatch")
    return sorted(refs)


def register_cells(connection, proof, items, specifications, bundles, *, ledger_path, now):
    """Internal original-admission transaction primitive, not an auth endpoint.

    The real service has already verified native confirmation and current
    policy. This primitive still cannot authorize sends without that original
    ApprovedRefreshSession and a current native permission read at each POST.
    """
    request = proof["request"]
    if request.get("protocol") not in {"iqs.refresh_request/1.4.0", "iqs.refresh_request/1.5.0"}:
        return
    ref, control = accepted_control(proof["owner_policy"], request, ledger_path, now=now)
    validate_retained_rows(connection, control, control["snapshot"]["policy_id"])
    if request["protocol"] == "iqs.refresh_request/1.5.0":
        from .quick_scan_owner_budget_continuation_v15 import assert_independent

        assert_independent(
            connection, control, confirmation_id=proof["confirmation"]["confirmation_id"]
        )
    qmap = {q["id"]: q for bundle in bundles.values() for q in bundle["manifest"]["questions"]}
    itemmap = {i["question_id"]: i for i in items}
    specmap = {i["question_id"]: i for i in specifications}
    if len(itemmap) != len(items) or len(specmap) != len(specifications):
        raise ValueError("owner_exposure_single_subject_action_required")
    if request["protocol"] == "iqs.refresh_request/1.5.0":
        from .quick_scan_owner_budget_continuation_v15 import validate_original_bindings

        validate_original_bindings(control["cells"][0], qmap, bundles)
    covered = set()
    for cell in control["cells"]:
        members = []
        for qid in cell["question_ids"]:
            item, spec = itemmap.get(qid), specmap.get(qid)
            if (
                item is None
                or spec is None
                or qid not in qmap
                or digest(qmap[qid]) != cell["question_body_sha256"][qid]
            ):
                raise ValueError("owner_exposure_original_question_mismatch")
            preview_item = next(i for i in proof["preview"]["items"] if i["question_id"] == qid)
            if (
                preview_item["subject_ref"] != cell["subject_ref"]
                or item["generation"] != cell["generation"]
                or spec["owner_refresh_binding"]["provider"] != cell["provider"]
                or spec["owner_refresh_binding"]["model"] != cell["model"]
                or spec["owner_refresh_binding"]["manifest_raw_sha256"]
                != cell["manifest_raw_sha256"]
            ):
                raise ValueError("owner_exposure_original_work_scope_mismatch")
            members.append(item["work_item_id"])
            covered.add(qid)
        fields = (
            control["snapshot"]["policy_id"],
            ref,
            cell_key(cell),
            proof["confirmation"]["confirmation_id"],
            digest(proof["owner_policy"]),
            canonical(sorted(members)),
            canonical(cell),
            canonical(control),
        )
        previous = connection.execute(
            "SELECT * FROM quick_scan_owner_continuation_cell WHERE policy_id=? AND acceptance_ref=? AND cell_key=?",
            fields[:3],
        ).fetchone()
        if previous is not None:
            if tuple(previous) != fields:
                raise ValueError("owner_exposure_cell_already_allocated")
        else:
            connection.execute(
                "INSERT INTO quick_scan_owner_continuation_cell VALUES (?,?,?,?,?,?,?,?)", fields
            )
    if covered != set(itemmap):
        raise ValueError("owner_exposure_approved_cell_scope_incomplete")


def _current_session():
    from .quick_scan_work_transport import _OWNER_REFRESH_GUARD

    guard = _OWNER_REFRESH_GUARD.get()
    session = getattr(guard, "__self__", None)
    if session is None:
        return None
    from .quick_scan_refresh_execution import ApprovedRefreshSession

    if (
        not isinstance(session, ApprovedRefreshSession)
        or getattr(guard, "__func__", None) is not ApprovedRefreshSession.check
    ):
        return None
    return session


def _physical_ids(connection, members):
    ids: set[str] = set()
    for member in members:
        ids.update(
            r[0]
            for r in connection.execute(
                "SELECT b.budget_attempt_id FROM quick_scan_budget_attempt b JOIN attempt a ON a.attempt_id=b.work_attempt_id WHERE a.work_item_id=?",
                (member,),
            )
        )
        ids.update(
            r[0]
            for r in connection.execute(
                "SELECT s.budget_attempt_id FROM quick_scan_question_pack_send s JOIN quick_scan_question_pack_member m USING(pack_id) WHERE m.work_item_id=?",
                (member,),
            )
        )
    return ids


def reserve_projection(
    connection,
    *,
    budget_attempt_id,
    work_attempt_id,
    policy_id,
    route_id,
    provider,
    model_requested,
    quota_group,
    now,
    work_item_id=None,
):
    """Native original send transaction: only its original bound owner may opt in."""
    if budget_attempt_id.startswith("DISPATCH_"):
        # External search dispatch is bounded by its own policy/exposure and the
        # shared budget row; the answer-model exposure cell does not own its route.
        # Only the journal's explicit work-item-bound search path may take this
        # exemption: a model send minted with a DISPATCH_ id must never bypass the
        # owner exposure projection.
        if work_item_id is None and _current_session() is not None:
            raise ValueError("owner_exposure_search_dispatch_work_item_required")
        return None
    session = _current_session()
    if session is None or session.proof["request"].get("protocol") not in {
        "iqs.refresh_request/1.4.0",
        "iqs.refresh_request/1.5.0",
    }:
        return None
    session.check()
    # Never accept a caller-selected namespace/policy or JSON authority.
    ref, control = accepted_control(
        session.proof["owner_policy"], session.proof["request"], session.service.store.path, now=now
    )
    actual_path = next(r[2] for r in connection.execute("PRAGMA database_list") if r[1] == "main")
    if Path(actual_path).resolve() != session.service.store.path.resolve():
        raise ValueError("owner_exposure_original_ledger_mismatch")
    refs = validate_retained_rows(connection, control, policy_id)
    if work_attempt_id is not None:
        row = connection.execute(
            "SELECT work_item_id FROM attempt WHERE attempt_id=?", (work_attempt_id,)
        ).fetchone()
        members = [] if row is None else [row[0]]
    elif budget_attempt_id.startswith("PACK_BUDGET_"):
        pack_id = "PACK_" + budget_attempt_id.removeprefix("PACK_BUDGET_")
        members = [
            r[0]
            for r in connection.execute(
                "SELECT work_item_id FROM quick_scan_question_pack_member WHERE pack_id=? ORDER BY ordinal",
                (pack_id,),
            )
        ]
    elif budget_attempt_id.startswith("DISPATCH_"):
        row = connection.execute(
            "SELECT work_item_id FROM quick_scan_external_operation WHERE budget_attempt_id=?",
            (budget_attempt_id,),
        ).fetchone()
        members = [work_item_id] if work_item_id else ([] if row is None else [row[0]])
    else:
        raise ValueError("owner_exposure_new_send_requires_original_work")
    if not members or not set(members) <= {
        e["work_item_id"] for e in session.entries.values() if e["kind"] != "reference"
    }:
        raise ValueError("owner_exposure_original_session_members_mismatch")
    rows = connection.execute(
        "SELECT * FROM quick_scan_owner_continuation_cell WHERE policy_id=? AND acceptance_ref=?",
        (policy_id, ref),
    ).fetchall()
    found, physical = [], set()
    for row in rows:
        cell, stored_control, stored_members = (
            json.loads(row[k]) for k in ("cell_json", "control_json", "work_item_ids_json")
        )
        if (
            stored_control != control
            or row["owner_policy_sha256"] != digest(session.proof["owner_policy"])
            or row["confirmation_id"] != session.proof["confirmation"]["confirmation_id"]
            or row["cell_key"] != cell_key(cell)
        ):
            raise ValueError("owner_exposure_registered_action_changed")
        physical.update(_physical_ids(connection, stored_members))
        if set(members) == set(stored_members):
            found.append(cell)
    if len(found) != 1:
        raise ValueError("owner_exposure_send_not_one_exact_approved_cell")
    cell = found[0]
    if (
        provider != session.proof["owner_policy"]["qa"]["provider_config_ref"]
        or cell["provider"] != session.proof["owner_policy"]["provider"]
        or any(
            cell[k] != v
            for k, v in {
                "route_id": route_id,
                "model": model_requested,
                "quota_group": quota_group,
            }.items()
        )
    ):
        raise ValueError("owner_exposure_original_route_mismatch")
    if len(physical) >= control["max_new_http_requests"]:
        raise ValueError("owner_exposure_new_http_limit")
    # Initial carrier: one physical single/pack for each approved cell. A new
    # turn or replay cannot masquerade as a fresh cell under another task ID.
    if _physical_ids(connection, members):
        raise ValueError("owner_exposure_cell_already_sent")
    return {
        "excluded_refs": refs,
        "additional_risk_hold_micros": control["additional_risk_hold_micros"],
    }


def assert_payload(store, work_ids, payload, *, provider, protocol):
    """The first public carrier is the already supported MiMo one-shot cell."""
    from contextlib import closing

    session = _current_session()
    if session is None or session.proof["request"].get("protocol") not in {
        "iqs.refresh_request/1.4.0",
        "iqs.refresh_request/1.5.0",
    }:
        return
    session.check()
    ref, control = accepted_control(
        session.proof["owner_policy"], session.proof["request"], store.path, now=store._now()
    )
    with closing(store._connect()) as db:
        rows = db.execute(
            "SELECT * FROM quick_scan_owner_continuation_cell WHERE acceptance_ref=? AND confirmation_id=?",
            (ref, session.proof["confirmation"]["confirmation_id"]),
        ).fetchall()
    cells = [
        json.loads(row["cell_json"])
        for row in rows
        if set(json.loads(row["work_item_ids_json"])) == set(work_ids)
    ]
    if len(cells) != 1 or cells[0] not in control["cells"]:
        raise ValueError("owner_exposure_actual_payload_cell_mismatch")
    cell, owner_policy = cells[0], session.proof["owner_policy"]
    if (
        protocol != "mimo_chat_completions"
        or provider != "mimo"
        or owner_policy["provider"] != "mimo"
        or payload.get("model") != cell["model"]
        or payload.get("thinking") != {"type": "enabled"}
        or type(payload.get("max_completion_tokens")) is not int
        or payload["max_completion_tokens"] != cell["controls"]["max_output_tokens"]
    ):
        raise ValueError("owner_exposure_actual_payload_controls_mismatch")


NO_EXTERNAL_SEARCH_POLICY = {
    "protocol": "stockqa.native_only_no_external_search/1.0.0",
    "max_external_queries": 0,
}


def assert_research_task(owner, task, *, search_service):
    if owner.proof["request"].get("protocol") not in {
        "iqs.refresh_request/1.4.0",
        "iqs.refresh_request/1.5.0",
    }:
        return
    _, control = accepted_control(
        owner.proof["owner_policy"],
        owner.proof["request"],
        owner.service.store.path,
        now=owner.service.store._now(),
    )
    if owner.proof["request"]["protocol"] == "iqs.refresh_request/1.5.0":
        from .quick_scan_owner_budget_continuation_v15 import assert_profile_task

        assert_profile_task(control["cells"][0], task)
    cells = control["cells"]
    no_search = digest(NO_EXTERNAL_SEARCH_POLICY)
    hashes = {c["controls"]["search_policy_sha256"] for c in cells}
    search_hashes = hashes - {no_search}
    profile = task.raw.get("execution_profile")
    exposure = profile.get("search_exposure") if isinstance(profile, dict) else None
    if search_hashes and no_search in hashes:
        raise ValueError("owner_exposure_registered_task_controls_mismatch")
    if search_hashes:
        if (
            len(search_hashes) != 1
            or not isinstance(exposure, dict)
            or digest(exposure) not in search_hashes
            or search_service is None
            or type(exposure.get("max_search_queries_per_session")) is not int
            or exposure["max_search_queries_per_session"] < 1
            or task.limits.get("max_search_queries_per_session")
            != exposure["max_search_queries_per_session"]
        ):
            raise ValueError("owner_exposure_registered_search_exposure_required")
    elif search_service is not None or task.limits.get("max_search_queries_per_session") != 0:
        raise ValueError("owner_exposure_registered_task_controls_mismatch")
    if (
        task.raw.get("information_cutoff")
        != owner.proof["request"]["source_query"]["payload"]["information_cutoff"]
        or {q["question_id"] for q in task.questions}
        != {q for c in cells for q in c["question_ids"]}
        or any(
            c["controls"]["strategy_id"]
            != (
                "module_packs"
                if task.raw.get("protocol") == "iqs.company-scan-task/1.0.0"
                else "free_agent"
            )
            for c in cells
        )
    ):
        raise ValueError("owner_exposure_registered_task_controls_mismatch")


def risk_hold_total(connection, policy_id, *, prospective_ref=None, prospective_control=None):
    """One conservative hold per immutable accepted exposure, never actual cash."""
    holds: dict[str, int] = {}
    for row in connection.execute(
        "SELECT acceptance_ref,control_json FROM quick_scan_owner_continuation_cell WHERE policy_id=?",
        (policy_id,),
    ):
        amount = json.loads(row["control_json"])["additional_risk_hold_micros"]
        if type(amount) is not int or amount < 0:
            raise ValueError("owner_exposure_risk_hold_invalid")
        holds[row["acceptance_ref"]] = max(holds.get(row["acceptance_ref"], 0), amount)
    if prospective_ref is not None:
        amount = prospective_control["additional_risk_hold_micros"]
        if type(amount) is not int or amount < 0:
            raise ValueError("owner_exposure_risk_hold_invalid")
        holds[prospective_ref] = max(holds.get(prospective_ref, 0), amount)
    return sum(holds.values())
