"""Publish an existing native safe research response; never send or settle HTTP.

The fixed owner work-decision authority grants this distinct purpose. No caller
answer body, generation lease renewal, search proof or fee correction is taken.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from typing import NoReturn

from .quick_scan_known_outcome_journal import _authorize, _physical
from .quick_scan_owner_budget_continuation_v15 import _read_view
from .quick_scan_refresh_inspect import ReadOnlyWorkView
from .quick_scan_research_intent import get_intent_response
from .quick_scan_result_outbox import (
    canonical_bytes,
    canonical_sha256,
    strict_json_loads,
)

KIND = "publish_saved_research"
PURPOSE = "research_no_verified_search"
WORK_KEYS = (
    "entity_id",
    "question_id",
    "generation",
    "scope",
    "scope_id",
    "identity_revision",
    "source_binding_version",
    "identity_state",
    "source_binding_ref",
    "source_binding_refs_json",
    "identity_snapshot_sha256",
    "question_fingerprint",
    "routing_fingerprint",
)


def _reject(reason: str) -> NoReturn:
    raise ValueError("saved_research_" + reason)


def _task(bundle):
    from importlib import import_module

    task_module = import_module("iqs_company_agent_lab.task")
    scan_profile = import_module("iqs_company_agent_lab.scan_profile")
    if not isinstance(bundle, bytes):
        _reject("original_bundle_bytes_required")
    raw = strict_json_loads(bundle.decode("utf8"))
    task = task_module.task_from_bundle(raw)
    scan_profile.validate_scan_task(task)
    return task


def _derive(store, connection, bundle, sdk_id, key):
    from importlib import import_module

    receiptmap = import_module("iqs_agent_runtime_lab.receiptmap")
    answers = import_module("iqs_company_agent_lab.answers")
    runner_module = import_module("iqs_company_agent_lab.runner")
    project_batch_response = receiptmap.project_batch_response
    batch_response_scope = receiptmap.batch_response_scope
    parse_answers_payload = answers.parse_answers_payload
    validate_single_answer = answers.validate_single_answer
    Runner = runner_module.Runner
    from .quick_scan_observation_context import validate_standard_answer
    from .quick_scan_subject_journal import receipt_record

    task = _task(bundle)
    if not isinstance(key, str) or not key or len(key) > 160:
        _reject("action_key_invalid")
    view = _read_view(connection, store.path)
    rows = connection.execute(
        "SELECT i.intent_sha256 FROM quick_scan_research_intent i JOIN quick_scan_attempt_response r ON r.attempt_id=i.native_attempt_id WHERE i.task_sha256=? AND json_extract(r.receipt_json,'$.attempt_id')=?",
        (canonical_sha256(task.raw), sdk_id),
    ).fetchall()
    if len(rows) != 1:
        _reject("original_task_native_intent_required")
    original = get_intent_response(view, canonical_sha256(task.raw), rows[0][0])
    if original is None or not original.get("native_receipt") or not original.get("visible_output"):
        _reject("safe_native_response_required")
    receipt = original["native_receipt"]
    visible = original["visible_output"]
    workids = original["work_item_ids"]
    items = {w: dict(view.get_item(w)) for w in workids}
    selected = [q for q in task.question_ids if q in {i["question_id"] for i in items.values()}]
    if len(selected) != len(items):
        _reject("original_pack_members_required")
    batch_response_scope(task, selected)
    output = visible["visible_output"]
    projected = project_batch_response(
        task,
        selected,
        content=output["content"],
        tool_calls=output["tool_calls"],
        finish_reason=output["finish_reason"],
    )
    payload = parse_answers_payload(projected["content"])
    # The original Runner's pure evidence intake/answer validators, no run loop.
    checker = Runner(task, object(), execution_revision=2)
    checker._start()
    bodies = {a["question_id"]: a for a in payload["answers"]}
    members = []
    for qid in selected:
        body = bodies[qid]
        if (
            body.get("status") not in {"unknown", "insufficient_evidence", "not_applicable"}
            or body.get("score") is not None
        ):
            _reject("nonscored_only")
        validate_single_answer(
            body,
            qid,
            checker.state.board,
            checker.state.evidence_cutoff_date,
            claim_kind_gate=True,
        )
        if checker.state._binding_findings(qid, body):
            _reject("original_answer_binding_rejected")
        item = next(i for i in items.values() if i["question_id"] == qid)
        if item["identity_state"] != "verified":
            _reject("original_identity_unverified")
        later = connection.execute(
            "SELECT 1 FROM work_item WHERE entity_id=? AND question_id=? AND scope=? AND scope_id=? AND generation>? LIMIT 1",
            (
                item["entity_id"],
                qid,
                item["scope"],
                item["scope_id"],
                item["generation"],
            ),
        ).fetchone()
        if later:
            _reject("generation_superseded")
        attempts = connection.execute(
            "SELECT * FROM attempt WHERE work_item_id=? ORDER BY ordinal DESC",
            (item["work_item_id"],),
        ).fetchall()
        if not attempts:
            _reject("original_attempt_required")
        attempt = attempts[0]
        physical = _physical(view, connection, item, attempt)
        if physical["execution_attempt_id"] != sdk_id:
            _reject("foreign_native_attempt")
        context = view.get_observation_context(item["work_item_id"])
        if context is None:
            _reject("frozen_context_required")
        urls = list(dict.fromkeys(e["url"] for e in body["evidence"]))
        normal = dict(
            entity_id=item["entity_id"],
            question_id=qid,
            status=body["status"],
            score=None,
            description=body["summary"],
        )
        metadata = context["context"]["questions"][qid]["metadata"]
        validate_standard_answer(
            body,
            metadata=metadata,
            normalized_answer=normal,
            source_urls=urls,
            research_only=True,
        )
        pre = connection.execute(
            "SELECT * FROM quick_scan_subject_send_receipt WHERE attempt_id=?",
            (attempt["attempt_id"],),
        ).fetchone()
        dispatch = receipt_record(connection, pre)
        cp = connection.execute(
            "SELECT * FROM answer_checkpoint WHERE work_item_id=?",
            (item["work_item_id"],),
        ).fetchone()
        if cp is None:
            if item["status"] not in {"uncertain", "leased"}:
                _reject("unpublished_work_state_required")
            if item["status"] == "leased" and item["lease_expires_at"] is None:
                _reject("lease_state_invalid")
            if item["status"] == "leased" and item["lease_expires_at"] > store._now():
                _reject("active_generation_lease_not_publication")
            if connection.execute(
                "SELECT 1 FROM quick_scan_result_delivery WHERE work_item_id=?",
                (item["work_item_id"],),
            ).fetchone():
                _reject("existing_delivery_without_checkpoint")
        else:
            saved = strict_json_loads(cp["payload_json"])
            if (
                cp["checkpoint_schema_version"] != 3
                or saved.get("publication", {}).get("request", {}).get("idempotency_key") != key
            ):
                _reject("checkpoint_conflict")
            if item["status"] not in {"result_ready", "delivered"}:
                _reject("published_work_state_invalid")
        members.append(
            dict(
                item=item,
                attempt=dict(attempt),
                physical=physical,
                standard_answer=copy.deepcopy(body),
                normalized_answer=normal,
                source_urls=urls,
                context=context,
                subject_dispatch_receipt=dispatch,
            )
        )
    publication = dict(
        protocol="stockqa.saved_research_publication/1.0.0",
        task_sha256=canonical_sha256(task.raw),
        task_bundle_sha256=hashlib.sha256(bundle).hexdigest(),
        execution_profile_sha256=task.raw["execution_profile_sha256"],
        native_sdk_id=sdk_id,
        native_intent_sha256=rows[0][0],
        safe_payload_sha256=connection.execute(
            "SELECT payload_sha256 FROM quick_scan_research_visible_output WHERE attempt_id=?",
            (visible["attempt_id"],),
        ).fetchone()[0],
        evidence_epoch_sha256=checker.state.board.epoch_sha256(),
        information_cutoff=task.raw["information_cutoff"],
        c06_authority_sha256=task.raw["native_inputs"]["authority.json"]["sha256"],
        question_ids=selected,
        subject_ref=members[0]["physical"]["subject_binding"]["subject_ref"],
        members=[
            dict(
                work_item_id=m["item"]["work_item_id"],
                attempt_id=m["attempt"]["attempt_id"],
                question_id=m["item"]["question_id"],
                generation=m["item"]["generation"],
                physical=m["physical"],
                answer_sha256=canonical_sha256(m["standard_answer"]),
                context_sha256=m["context"]["context_sha256"],
            )
            for m in members
        ],
    )
    request = dict(
        kind=KIND,
        purpose=PURPOSE,
        work_item_id=members[0]["item"]["work_item_id"],
        attempt_id=members[0]["attempt"]["attempt_id"],
        idempotency_key=key,
        physical=members[0]["physical"],
        publication=publication,
    )
    return dict(
        request=request,
        members=members,
        task=task,
        native_receipt=receipt,
        native_visible=visible,
    )


def derive_publication(store, bundle, *, sdk_id, idempotency_key):
    with closing(store._connect()) as con:
        con.execute("BEGIN")
        try:
            return _derive(store, con, bundle, sdk_id, idempotency_key)
        finally:
            con.rollback()


def _provenance(member, native, response):
    a = member["attempt"]
    return dict(
        attempt_lease_epoch=a["lease_epoch"],
        route_id=a["route_id"],
        route_provider=a["provider"],
        actual_provider=native["provider"],
        model_requested=a["model_requested"],
        actual_model=native["actual_model"],
        model_resolution_binding="durable-response-v1",
        response_sha256=response["response_sha256"],
        work_prompt_sha256=a["prompt_sha256"],
        provider_prompt_sha256=native["prompt_sha256"],
        request_id=native["request_id"],
        provider_attempt_id=native["attempt_id"],
        response_id=native["response_id"],
        search_receipt_id=None,
        response_status=native["response_status"],
        http_status_code=native["http_status_code"],
        search_status="unverified",
        source_urls=member["source_urls"],
        response_completed_at=native["completed_at"],
        receipt_sha256=response["receipt_sha256"],
        answer_contract="saved-research-nonscored-v1",
        execution_kind=PURPOSE,
    )


def publish_saved_research(
    store, bundle, *, sdk_id, idempotency_key, owner_authority, c06_authority
):
    before = derive_publication(store, bundle, sdk_id=sdk_id, idempotency_key=idempotency_key)
    if (
        not isinstance(c06_authority, dict)
        or c06_authority.get("authority_sha256")
        != before["request"]["publication"]["c06_authority_sha256"]
    ):
        _reject("original_c06_authority_required")
    now = store._now()
    grant = _authorize(owner_authority, before["request"], now)
    with store._transaction() as con:
        current = _derive(store, con, bundle, sdk_id, idempotency_key)
        if current["request"] != before["request"]:
            _reject("source_changed_before_commit")
        if _authorize(owner_authority, current["request"], store._now()) != grant:
            _reject("publisher_changed_before_commit")
        saved = []
        for m in current["members"]:
            item = m["item"]
            wid = item["work_item_id"]
            existing = con.execute(
                "SELECT * FROM answer_checkpoint WHERE work_item_id=?", (wid,)
            ).fetchone()
            if existing is not None:
                old = json.loads(existing["payload_json"])
                if old.get("publication") != dict(request=current["request"], grant=grant):
                    _reject("immutable_publication_conflict")
                saved.append(wid)
                continue
            response = store._response_for_checkpoint(
                con, m["attempt"], m["physical"]["receipt_sha256"]
            )
            payload = dict(
                checkpoint_schema="quick-scan-answer",
                checkpoint_schema_version=3,
                work_item_id=wid,
                attempt_id=m["attempt"]["attempt_id"],
                work={k: item[k] for k in WORK_KEYS},
                answer=m["normalized_answer"],
                provenance=_provenance(m, current["native_receipt"], response),
                checkpointed_at=now,
                publication=dict(request=current["request"], grant=grant),
            )
            side = store._prepare_standard_inputs(
                question_id=item["question_id"],
                normalized_answer=m["normalized_answer"],
                source_urls=m["source_urls"],
                observation_context=m["context"]["context"],
                standard_answer=m["standard_answer"],
                research_only=True,
            )
            store._store_side_tables(con, work_item_id=wid, item=item, now=now, **side)
            store._record_frozen_run_refs(
                con, work_item_id=wid, context_encoded=side["context_encoded"], now=now
            )
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            con.execute(
                "INSERT INTO answer_checkpoint VALUES (?,?,?,?,?,?,?)",
                (
                    "CHECKPOINT_" + uuid.uuid4().hex,
                    wid,
                    m["attempt"]["attempt_id"],
                    3,
                    encoded,
                    hashlib.sha256(encoded.encode()).hexdigest(),
                    now,
                ),
            )
            if (
                con.execute(
                    "UPDATE work_item SET status='result_ready',lease_token=NULL,lease_expires_at=NULL,uncertain_attempt_id=NULL,updated_at=? WHERE work_item_id=? AND status=? AND lease_epoch=?",
                    (now, wid, item["status"], item["lease_epoch"]),
                ).rowcount
                != 1
            ):
                _reject("work_changed_before_commit")
            store._event(
                con,
                wid,
                "saved_research_published",
                item["lease_epoch"],
                now,
                attempt_id=m["attempt"]["attempt_id"],
                old=item["status"],
                new="result_ready",
            )
            saved.append(wid)
        # Native checkpoints and sealed outbox items commit as one group.
        from .quick_scan_delivery_seal import _complete_package

        view = _read_view(con, store.path)
        packages = []
        for wid in saved:
            package, missing = _complete_package(view, wid, c06_authority)
            if missing or package is None:
                _reject("complete_publication_package_unavailable")
            delivered = store.get_result_delivery(wid)
            if delivered is not None and delivered["state"] == "delivered":
                if delivered["package"] != package:
                    _reject("published_delivery_conflict")
                packages.append(delivered["package_id"])
                continue
            outbox = store.prepare_result_delivery(
                wid, package, publisher_authority=owner_authority
            )
            store.bind_result_delivery_consumer(
                wid,
                dict(
                    component="StockWiki",
                    namespace="quick_scan",
                    store_id=grant["owner_store_id"],
                ),
                source_ref="native-owner-confirmation:" + grant["confirmation_id"],
            )
            packages.append(outbox["package_id"])
        return dict(
            package_ids=packages,
            protocol="stockqa.saved_research_publication_receipt/1.0.0",
            work_item_ids=saved,
            request_sha256=canonical_sha256(current["request"]),
            confirmation_id=grant["confirmation_id"],
            API_requests=0,
            formal_search_checkpoint_created=False,
        )


def validate_checkpoint(connection, row, payload, *, validate_status=True):
    """A new explicit checkpoint variant; old versions keep old validators."""
    from .quick_scan_work_store import QuickScanWorkStore

    if (
        payload.get("checkpoint_schema") != "quick-scan-answer"
        or payload.get("checkpoint_schema_version") != 3
        or row["checkpoint_schema_version"] != 3
        or payload.get("work_item_id") != row["work_item_id"]
        or payload.get("attempt_id") != row["attempt_id"]
        or payload.get("checkpointed_at") != row["checkpointed_at"]
    ):
        _reject("checkpoint_binding_invalid")
    item = dict(
        connection.execute(
            "SELECT * FROM work_item WHERE work_item_id=?", (row["work_item_id"],)
        ).fetchone()
    )
    attempt = connection.execute(
        "SELECT * FROM attempt WHERE attempt_id=? AND work_item_id=?",
        (row["attempt_id"], row["work_item_id"]),
    ).fetchone()
    if (
        attempt is None
        or payload.get("work") != {k: item[k] for k in WORK_KEYS}
        or item["identity_state"] != "verified"
        or validate_status
        and item["status"] not in {"result_ready", "delivered"}
    ):
        _reject("checkpoint_original_work_invalid")
    publication = payload.get("publication", {})
    request = publication.get("request", {})
    grant = publication.get("grant", {})
    if (
        request.get("kind") != KIND
        or request.get("purpose") != PURPOSE
        or grant.get("request_sha256") != canonical_sha256(request)
        or not isinstance(grant.get("confirmation_id"), str)
        or not grant["confirmation_id"].startswith("rc1_")
    ):
        _reject("checkpoint_publication_proof_invalid")
    members = request["publication"]["members"]
    member = next((m for m in members if m["work_item_id"] == item["work_item_id"]), None)
    if (
        member is None
        or member["attempt_id"] != attempt["attempt_id"]
        or member["generation"] != item["generation"]
    ):
        _reject("checkpoint_original_member_invalid")
    view = _read_view(connection, PathForRead(connection))
    if _physical(view, connection, item, attempt) != member["physical"]:
        _reject("checkpoint_native_proof_changed")
    native = view.get_research_visible_output(request["publication"]["native_sdk_id"])
    if (
        native is None
        or connection.execute(
            "SELECT payload_sha256 FROM quick_scan_research_visible_output WHERE attempt_id=?",
            (native["attempt_id"],),
        ).fetchone()[0]
        != request["publication"]["safe_payload_sha256"]
    ):
        _reject("checkpoint_safe_source_changed")
    original_output = native["visible_output"]
    calls = original_output["tool_calls"]
    if calls:
        if len(calls) != 1 or calls[0].get("name") != "submit_answers":
            _reject("checkpoint_original_carrier_invalid")
        args = calls[0]["arguments"]
        if isinstance(args, str):
            args = strict_json_loads(args)
        declared = args.get("question_ids")
        originals = args.get("answers")
    else:
        args = strict_json_loads(original_output["content"])
        declared = args.get("declared_question_ids")
        originals = args.get("answers")
    if not isinstance(originals, list) or declared != request["publication"]["question_ids"]:
        _reject("checkpoint_original_declarations_invalid")
    matching = [a for a in originals if a.get("question_id") == item["question_id"]]
    if len(matching) != 1:
        _reject("checkpoint_original_answer_changed")
    if canonical_sha256(matching[0]) != member["answer_sha256"]:
        _reject("checkpoint_original_answer_changed")
    standard = connection.execute(
        "SELECT * FROM quick_scan_standard_answer WHERE work_item_id=?",
        (item["work_item_id"],),
    ).fetchone()
    if standard is None:
        _reject("checkpoint_standard_body_required")
    body = strict_json_loads(standard["answer_json"])
    if not isinstance(body, dict):
        _reject("checkpoint_nonscored_body_invalid")
    if canonical_sha256(body) != member["answer_sha256"]:
        _reject("checkpoint_nonscored_body_invalid")
    if (
        body["status"] not in {"unknown", "insufficient_evidence", "not_applicable"}
        or body["score"] is not None
    ):
        _reject("checkpoint_nonscored_body_invalid")
    expected = dict(
        entity_id=item["entity_id"],
        question_id=item["question_id"],
        status=body["status"],
        score=None,
        description=body["summary"],
    )
    context = view.get_observation_context(item["work_item_id"])
    if context is None:
        _reject("checkpoint_body_context_changed")
    if context["context_sha256"] != member["context_sha256"] or payload.get("answer") != expected:
        _reject("checkpoint_body_context_changed")
    response = QuickScanWorkStore._response_for_checkpoint(
        connection, attempt, member["physical"]["receipt_sha256"]
    )
    original = response["receipt"]
    m = dict(
        attempt=dict(attempt),
        source_urls=list(dict.fromkeys(e["url"] for e in body["evidence"])),
    )
    if payload.get("provenance") != _provenance(m, original, response):
        _reject("checkpoint_provenance_invalid")
    return {
        k: row[k]
        for k in (
            "checkpoint_id",
            "work_item_id",
            "attempt_id",
            "checkpoint_schema_version",
            "payload_sha256",
            "checkpointed_at",
        )
    } | dict(payload=payload)


def PathForRead(connection):
    from pathlib import Path

    return Path(connection.execute("PRAGMA database_list").fetchone()[2])


def require_publisher(store, connection, work_item_id, authority):
    row = connection.execute(
        "SELECT payload_json,checkpoint_schema_version FROM answer_checkpoint WHERE work_item_id=?",
        (work_item_id,),
    ).fetchone()
    if row is None or row["checkpoint_schema_version"] != 3:
        return None
    request = strict_json_loads(row["payload_json"])["publication"]["request"]
    grant = _authorize(authority, request, store._now())
    if grant != strict_json_loads(row["payload_json"])["publication"]["grant"]:
        _reject("current_publisher_authority_changed")
    return request


def inspect_publication(path, request, *, policy, task_bundle, deployment=None):
    if deployment is not None:
        native_dependencies(deployment)
    from .quick_scan_work_decision_inspect import _policy_matches

    view = ReadOnlyWorkView(path)
    pub = request.get("publication", {})
    value = derive_publication(
        view,
        task_bundle,
        sdk_id=pub.get("native_sdk_id"),
        idempotency_key=request.get("idempotency_key"),
    )
    if value["request"] != request:
        _reject("executor_original_request_mismatch")
    with closing(view._connect()) as con:
        con.execute("BEGIN")
        for member in value["members"]:
            _policy_matches(con, policy, member["physical"])
    return dict(
        protocol="stockqa.work_decision_inspection/1.2.0",
        request=copy.deepcopy(request),
        request_sha256=canonical_sha256(request),
        subject_dispatch_receipts=[m["subject_dispatch_receipt"] for m in value["members"]],
        observed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        API_requests=0,
        database_writes=0,
    )


def native_dependencies(deployment):
    """Only two fixed original component roles; no factory or global install."""
    import stat
    import sys
    from importlib import import_module
    from pathlib import Path

    roles = {
        "company_agent_code_root": "iqs_company_agent_lab",
        "runtime_code_root": "iqs_agent_runtime_lab",
    }
    if not isinstance(deployment, dict) or set(deployment) != set(roles):
        _reject("fixed_component_roles_required")
    for key, package in roles.items():
        root = Path(deployment[key])
        if not root.is_absolute():
            _reject("absolute_component_root_required")
        init = root / "src" / package / "__init__.py"
        for path in (init, *init.parents):
            if path.exists():
                info = path.lstat()
                if (
                    path.is_symlink()
                    or getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                    or stat.S_ISREG(info.st_mode)
                    and info.st_nlink != 1
                ):
                    _reject("component_root_redirected")
        init = root / "src" / package / "__init__.py"
        if not init.is_file():
            _reject("original_component_package_missing")
        if not init.resolve().is_relative_to(root.resolve() / "src" / package):
            _reject("component_root_redirected")
        loaded = sys.modules.get(package)
        loaded_file = getattr(loaded, "__file__", None)
        if loaded is not None and (
            not isinstance(loaded_file, str) or Path(loaded_file).resolve() != init.resolve()
        ):
            _reject("component_import_origin_changed")
        sys.path.insert(0, str(root / "src"))
        module = import_module(package)
        module_file = getattr(module, "__file__", None)
        if not isinstance(module_file, str) or Path(module_file).resolve() != init.resolve():
            _reject("component_import_origin_changed")
    scan = import_module("iqs_company_agent_lab.scan_profile")
    mapping = import_module("iqs_agent_runtime_lab.receiptmap")
    if (
        getattr(scan, "TASK_PROTOCOL", None) != "iqs.company-scan-task/1.0.0"
        or not callable(getattr(scan, "validate_scan_task", None))
        or not callable(getattr(mapping, "project_batch_response", None))
    ):
        _reject("original_component_capability_missing")
