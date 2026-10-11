"""Batched native proof of EXACT original C06 executions, no receipts issued.

Uses the original read-only Q09/Q13 view and original full-observation rebuild.
Only the fixed owner adapter may convert this native inspection to an
execution_verified receipt. A caller-supplied model receipt/answer is rejected.
Execution integrity does not establish source correctness or scoring accuracy.
"""

import copy
import re
from contextlib import closing
from datetime import datetime, timezone

from src.utils.quick_scan_refresh_inspect import ReadOnlyWorkView
from src.utils.quick_scan_result_outbox import canonical_bytes, canonical_sha256
from src.utils.quick_scan_work_store import SCHEMA_VERSION, WorkConflictError


def validate_request(request):
    if (
        not isinstance(request, dict)
        or set(request) != {"protocol", "owner_store_id", "observations"}
        or request["protocol"] != "stockqa.execution_inspection_request/1.0.0"
        or not isinstance(request["owner_store_id"], str)
        or not 1 <= len(request["owner_store_id"]) <= 160
        or not isinstance(request["observations"], list)
        or not 1 <= len(request["observations"]) <= 100
        or len(canonical_bytes(request)) > 1024 * 1024
    ):
        raise ValueError("execution_inspection_request_invalid")
    seen_work = set()
    seen_observations = set()
    for target in request["observations"]:
        if (
            not isinstance(target, dict)
            or set(target) != {"work_item_id", "observation_id", "payload_sha256", "subject_ref"}
            or not isinstance(target["work_item_id"], str)
            or not re.fullmatch("[A-Za-z0-9_.:-]{1,160}", target["work_item_id"])
            or not isinstance(target["observation_id"], str)
            or not re.fullmatch("obs_[a-f0-9]{64}", target["observation_id"])
            or not isinstance(target["payload_sha256"], str)
            or not re.fullmatch("[a-f0-9]{64}", target["payload_sha256"])
            or not isinstance(target["subject_ref"], dict)
        ):
            raise ValueError("execution_inspection_target_invalid")
        if target["work_item_id"] in seen_work or target["observation_id"] in seen_observations:
            raise ValueError("execution_inspection_duplicate_original")
        seen_work.add(target["work_item_id"])
        seen_observations.add(target["observation_id"])
    return copy.deepcopy(request)


def inspect_executions(path, request):
    request = validate_request(request)
    view = ReadOnlyWorkView(path)
    proofs = {}
    read_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        with closing(view._connect()) as connection:
            connection.execute("BEGIN")
            if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise ValueError("execution_inspection_schema_unavailable")
            view._validate_schema(connection, schema_version=SCHEMA_VERSION)
            view._inspection_connection = connection
            try:
                for target in request["observations"]:
                    work = target["work_item_id"]
                    item = view.get_item(work)
                    checkpoint = view.get_answer_checkpoint(work)
                    delivery = view.get_result_delivery(work)
                    if (
                        item["status"] not in {"result_ready", "delivered"}
                        or checkpoint is None
                        or delivery is None
                        or delivery["package"] is None
                        or delivery["state"] == "blocked"
                    ):
                        raise ValueError("execution_inspection_original_unavailable")
                    original = delivery["package"]["items"][0]["observation"]
                    context = view.get_observation_context(work)
                    if (
                        original.get("schema_version") != "1.1.0"
                        or context is None
                        or context["context"].get("schema")
                        != "stockqa.quick_scan_observation_context/2.0.0"
                        or checkpoint["payload"]["provenance"].get("model_resolution_binding")
                        != "durable-response-v1"
                        or original["observation_id"] != target["observation_id"]
                        or canonical_sha256(original) != target["payload_sha256"]
                        or delivery["payload_sha256"] != target["payload_sha256"]
                    ):
                        raise ValueError("execution_inspection_original_mismatch")
                    # Original algorithm: durable standard body, context,
                    # run/scan map and pre-POST instant must rebuild exactly.
                    view._assert_complete_observation_binding(
                        connection,
                        work_item_id=work,
                        item=item,
                        checkpoint=checkpoint,
                        package=delivery["package"],
                    )
                    pre = view.get_subject_dispatch_receipt(checkpoint["attempt_id"])
                    response = view.get_attempt_response(checkpoint["attempt_id"])
                    if pre is None or response is None:
                        raise ValueError("execution_inspection_durable_receipt_missing")
                    receipt = pre["receipt"]
                    dispatch = receipt["dispatch_request"]
                    execution = original["execution"]
                    observed = datetime.fromisoformat(
                        original["observed_at"].replace("Z", "+00:00")
                    )
                    if (
                        receipt["store_id"] != request["owner_store_id"]
                        or dispatch["subject_ref"] != target["subject_ref"]
                        or dispatch["work_item_id"] != work
                        or any(
                            dispatch[k] != execution[v]
                            for k, v in (
                                ("execution_attempt_id", "attempt_id"),
                                ("provider", "provider"),
                                ("model_requested", "model_requested"),
                                ("dispatch_prompt_sha256", "prompt_sha256"),
                            )
                        )
                        or checkpoint["payload"]["provenance"]["response_sha256"]
                        != response["response_sha256"]
                        or checkpoint["payload"]["provenance"]["receipt_sha256"]
                        != response["receipt_sha256"]
                        or observed.utcoffset() is None
                        or observed > datetime.fromisoformat(read_at.replace("Z", "+00:00"))
                    ):
                        raise ValueError("execution_inspection_scope_or_receipt_mismatch")
                    proofs[original["observation_id"]] = dict(
                        status="verified",
                        maximum_check_level="execution_verified",
                        work_item_id=work,
                        observation_id=original["observation_id"],
                        payload_sha256=target["payload_sha256"],
                        subject_ref=copy.deepcopy(dispatch["subject_ref"]),
                        field_id=original["field_id"],
                        question_id=original["question_id"],
                        checkpoint_id=checkpoint["checkpoint_id"],
                        checkpoint_payload_sha256=checkpoint["payload_sha256"],
                        work_attempt_id=checkpoint["attempt_id"],
                        response_sha256=response["response_sha256"],
                        execution_receipt_sha256=response["receipt_sha256"],
                        package_sha256=delivery["package_sha256"],
                        package_bytes_sha256=delivery["package_bytes_sha256"],
                        dispatch_receipt_id=receipt["receipt_id"],
                        dispatch_receipt_sha256=pre["receipt_sha256"],
                        dispatch_manifest_sha256=dispatch["dispatch_manifest_sha256"],
                        dispatch_identity_snapshot_sha256=dispatch[
                            "dispatch_identity_snapshot_sha256"
                        ],
                        frozen_question_work_fingerprint=item["question_fingerprint"],
                        actual_work_prompt_sha256=dispatch["dispatch_work_prompt_sha256"],
                        observed_at=original["observed_at"],
                        execution=copy.deepcopy(execution),
                        delivery_state=delivery["state"],
                        financial_accuracy_proven=False,
                    )
            finally:
                view._inspection_connection = None
                connection.rollback()
    except WorkConflictError as error:
        raise ValueError("execution_inspection_durable_original_mismatch") from error
    body = dict(
        protocol="stockqa.execution_inspection/1.0.0",
        owner_store_id=request["owner_store_id"],
        request_sha256=canonical_sha256(request),
        ledger_ref_sha256=canonical_sha256(str(view.path)),
        read_at=read_at,
        source_consistency="one_native_read_transaction",
        observations=proofs,
        financial_accuracy_proven=False,
        screening_audited=False,
        API_requests=0,
        database_writes=0,
    )
    result = dict(body, inspection_sha256=canonical_sha256(body))
    if len(canonical_bytes(result)) > 1024 * 1024:
        raise ValueError("execution_inspection_output_too_large")
    return result
