"""Pure projection of a previously authenticated native journal decision.

This function authenticates no owner and executes no work. Callers must read
the decision with the native journal validator, recheck its configured owner,
and bind this projection into a fresh approved refresh request before dispatch.
"""

import copy
import math
import re
from datetime import datetime, timezone

from .quick_scan_result_outbox import canonical_sha256


def project_owner_control(document, *, source_work, subject_ref, provider, model, now, cohort_id):
    if type(now) not in (int, float) or not math.isfinite(now):
        raise ValueError("work_control_time_invalid")
    if not isinstance(cohort_id, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", cohort_id) is None:
        raise ValueError("work_control_cohort_invalid")
    if (
        not isinstance(document, dict)
        or document.get("protocol")
        not in {
            "stockqa.owner_work_decision/1.0.0",
            "stockqa.owner_work_decision/1.1.0",
        }
        or document.get("decision_id")
        != "owd1_" + canonical_sha256({k: v for k, v in document.items() if k != "decision_id"})
    ):
        raise ValueError("work_control_document_invalid")
    request = document["request"]
    physical = request["physical"]
    fingerprint = physical["work_fingerprint"]
    target = document["grant"].get("comparison_target")
    if document["protocol"] == "stockqa.owner_work_decision/1.1.0":
        from .quick_scan_known_outcome_journal import validate_comparison_target

        validate_comparison_target(target, request)
        if provider != target["provider"] or model != target["model"]:
            raise ValueError("work_control_target_mismatch")
    elif target is not None:
        raise ValueError("work_control_target_version_mismatch")
    if (
        request["kind"] != "retry_decision"
        or request["purpose"] not in {"comparison", "retry_unknown_answer", "retry_unusable_answer"}
        or request["work_item_id"] != source_work["work_item_id"]
        or request["target_generation"] != source_work["generation"] + 1
        or type(request["target_generation"]) is not int
        or any(source_work.get(k) != v for k, v in fingerprint.items())
        or physical["subject_binding"]["subject_ref"] != subject_ref
        or target is None
        and (physical["provider"] != provider or physical["model_requested"] != model)
        or source_work["status"] not in {"failed", "result_ready", "delivered"}
    ):
        raise ValueError("work_control_source_mismatch")
    controls = request["comparison_controls"]
    if request["purpose"] == "comparison":
        if (
            not isinstance(controls, dict)
            or set(controls) != {"pack_size", "thinking_enabled", "max_output_tokens"}
            or type(controls["pack_size"]) is not int
            or not 1 <= controls["pack_size"] <= 30
            or type(controls["thinking_enabled"]) is not bool
            or type(controls["max_output_tokens"]) is not int
            or not 1 <= controls["max_output_tokens"] <= 32768
        ):
            raise ValueError("work_control_generation_options_invalid")
    elif controls is not None:
        raise ValueError("work_control_unexpected_generation_options")
    retry_at = datetime.fromisoformat(document["next_retry_at"].replace("Z", "+00:00"))
    decided_at = datetime.fromisoformat(document["decided_at"].replace("Z", "+00:00"))
    if (
        retry_at.utcoffset() != timezone.utc.utcoffset(None)
        or decided_at.utcoffset() != timezone.utc.utcoffset(None)
        or retry_at < decided_at
        or now < decided_at.timestamp()
    ):
        raise ValueError("work_control_clock_invalid")
    projection = dict(
        decision_id=document["decision_id"],
        document_sha256=canonical_sha256(document),
        source_work_item_id=source_work["work_item_id"],
        source_generation=source_work["generation"],
        confirmation_id=document["grant"]["confirmation_id"],
        purpose=request["purpose"],
        next_retry_at=document["next_retry_at"],
        target_generation=request["target_generation"],
        cohort_id=cohort_id,
        comparison_controls=copy.deepcopy(controls),
    )
    if target is not None:
        projection["comparison_target"] = copy.deepcopy(target)
    if now < retry_at.timestamp():
        decision, generation = "deferred_unknown", None
    elif request["requires_prior_delivery"] and source_work["status"] != "delivered":
        decision, generation = "manual_refresh_required", None
    else:
        decision, generation = "dispatch_new_generation", request["target_generation"]
    return dict(
        decision=decision,
        generation=generation,
        reuse_observation_id=None,
        owner_control=projection,
    )
