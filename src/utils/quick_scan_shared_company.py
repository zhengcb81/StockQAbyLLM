"""Thin original-runner entry into the shared company engine; no client or queue."""

from __future__ import annotations

import copy
import importlib
import sys
from pathlib import Path

from src.utils.quick_scan_result_outbox import canonical_sha256, strict_json_loads


def read_shared_company_config(path):
    raw = Path(path).read_bytes()
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("shared_company_config_too_large")
    config = strict_json_loads(raw.decode("utf-8-sig"))
    required = {
        "schema_version",
        "iqs_root",
        "company_agent_root",
        "runtime_bridge_root",
        "state_root",
    }
    if (
        not isinstance(config, dict)
        or not required <= set(config)
        or set(config) - required - {"evidence_boards", "limits", "pack_by"}
        or config.get("schema_version") != "1.0.0"
    ):
        raise ValueError("shared_company_config_invalid")
    for key in required - {"schema_version"}:
        if not isinstance(config[key], str) or not Path(config[key]).is_absolute():
            raise ValueError("shared_company_absolute_path_required:" + key)
    for key, member in (
        ("iqs_root", "scripts/shared_company_engine.py"),
        ("company_agent_root", "src/iqs_company_agent_lab/task.py"),
        ("runtime_bridge_root", "src/iqs_agent_runtime_lab/port.py"),
    ):
        if not (Path(config[key]) / member).is_file():
            raise ValueError("shared_company_dependency_missing:" + key)
    boards = config.get("evidence_boards", [])
    if not isinstance(boards, list) or any(
        not isinstance(b, dict)
        or set(b) != {"subject_ref", "records"}
        or not isinstance(b["subject_ref"], dict)
        or not isinstance(b["records"], list)
        for b in boards
    ):
        raise ValueError("shared_company_evidence_boards_invalid")
    return config


def _modules(config):
    paths = [
        ("shared_company_engine", Path(config["iqs_root"]) / "scripts"),
        ("shared_company_task", Path(config["iqs_root"]) / "scripts"),
        ("iqs_company_agent_lab", Path(config["company_agent_root"]) / "src"),
        ("iqs_agent_runtime_lab", Path(config["runtime_bridge_root"]) / "src"),
    ]
    for _, root in reversed(paths):
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
    modules = []
    for name, root in paths:
        module = importlib.import_module(name)
        module_path = getattr(module, "__file__", None)
        if not isinstance(module_path, str) or not Path(module_path).resolve().is_relative_to(
            root.resolve()
        ):
            raise ValueError("shared_company_dependency_shadowed:" + name)
        modules.append(module)
    return modules[0], modules[1]


def _bound_board(config, *, subject, company_ref, excerpt_cap):
    matches = [b for b in config.get("evidence_boards", []) if b["subject_ref"] == subject]
    if len(matches) > 1:
        raise ValueError("shared_company_duplicate_subject_evidence")
    board = {"records": copy.deepcopy(matches[0]["records"] if matches else [])}
    # The subject comes from the original owner. Only its transport reference
    # is derived here; source text, numbers, dates and scope are never rewritten.
    if not board["records"]:
        return board
    evidence_api = importlib.import_module("iqs_company_agent_lab.evidence")

    for record in board["records"]:
        if not isinstance(record, dict):
            raise ValueError("shared_company_evidence_record_invalid")
        if "issuer_ref" in record and record["issuer_ref"] != company_ref:
            raise ValueError("shared_company_evidence_issuer_mismatch")
        record.setdefault("issuer_ref", copy.deepcopy(company_ref))
        evidence_api.validate_evidence_record(record, excerpt_cap)
    return board


def run_shared_company_scan(
    lifecycle,
    client,
    *,
    config,
    questions,
    cost_resolver=None,
    question_pack_size=1,
    thinking_enabled=True,
    max_output_tokens=8192,
    transport_origin="stockqa_live",
):
    """Reuse original services, durable engine checkpoint/export and finalizer.

    The caller hydrates the resulting original checkpoints into its normal
    output. An interrupted or rejected engine never falls back to asking again.
    """
    if (
        thinking_enabled is False
        or type(max_output_tokens) is not int
        or not 1 <= max_output_tokens <= 16384
    ):
        raise ValueError("shared_company_requires_thinking_and_output_at_most_16384")
    native_search = lifecycle._external_retrieval is None
    if native_search and (client.provider_name != "mimo" or not client.supports_web_search):
        raise ValueError("shared_company_native_search_endpoint_not_qualified")
    engine, factory = _modules(config)
    owner = lifecycle._owner_refresh
    ids = [q.question_id for q in questions]
    if not ids or len(set(ids)) != len(ids) or any(qid not in owner.entries for qid in ids):
        raise ValueError("shared_company_original_question_selection_required")
    selected = [qid for qid in ids if owner.entries[qid]["kind"] == "work"]
    if not selected:
        return None  # The original hydrator handles references without a send.
    # Output file names change on UI resume. The original admitted scope does
    # not, so they must never form a new paid company task/session identity.
    identity = canonical_sha256(
        {
            "confirmation_id": owner.proof["confirmation"]["confirmation_id"],
            "question_ids": selected,
        }
    )
    run_id = "scan-" + identity[:48]
    limits = {
        **config.get("limits", {}),
        "logical_max_output_tokens_including_reasoning_per_request": max_output_tokens,
    }
    kwargs = dict(
        experiment_id="company-scan",
        run_id=run_id,
        question_ids=selected,
        limits=limits,
        pack_by=config.get("pack_by", "module"),
        max_questions_per_pack=question_pack_size,
    )
    # Project the original issuer with the same public serializer as the
    # task factory. Do not build/validate the entire admitted task twice just
    # to obtain this reference before attaching short source records.
    canonical = importlib.import_module("iqs_company_agent_lab.canonical")
    profiles = importlib.import_module("iqs_company_agent_lab.scan_profile")

    identity_payload = strict_json_loads(owner.input_raw["identity.json"].decode("utf8"))["payload"]
    subject = lifecycle._observation_context["questions"][selected[0]]["subject_binding"][
        "subject_ref"
    ]
    company = profiles.native_company_projection(identity_payload, subject)
    company_bytes = canonical.canonical_json(company).encode("utf8")
    reference = dict(
        path="native-derived/company.json",
        bytes=len(company_bytes),
        sha256=canonical.sha256_hex(company_bytes),
    )
    board = _bound_board(
        config,
        subject=subject,
        company_ref=reference,
        excerpt_cap=profiles.scan_limits(limits)["excerpt_chars_per_source"],
    )
    task = factory.create_company_scan_task(lifecycle, evidence_board=board, **kwargs)
    from src.utils.quick_scan_owner_research import OwnerResearchServices

    services = OwnerResearchServices(
        lifecycle,
        client,
        task=task,
        cost_resolver=cost_resolver,
        owner_batch_claim=True,
        native_search=lifecycle._external_retrieval is None,
    )
    session_config = {
        "protocol": "iqs.agent-runtime-lab.session-config/1.1.0",
        "planned_model": client.model,
        "endpoint_profile_id": client.provider_name + "@" + client.base_url,
        "max_output_tokens": max_output_tokens,
        "session_input_tokens": task.limits["session_input_tokens"],
        "session_output_tokens": task.limits["session_output_tokens_including_reasoning"],
        "wall_seconds": task.limits["company_wall_seconds"],
        "logical_clock": False,
    }
    root = Path(config["state_root"]) / run_id
    export = root / "export"
    if not export.exists():
        root.mkdir(parents=True, exist_ok=True)
        sessions = sorted(root.glob("session-[0-9][0-9][0-9][0-9]"))
        sequence = len(sessions) + 1
        next_session = root / ("session-%04d" % sequence)
        candidate_export = root / ("export-%04d" % sequence)
        if sessions:
            checkpoint = sessions[-1] / "checkpoint.json"
            if not checkpoint.is_file():
                raise ValueError("shared_company_original_checkpoint_missing")
            outcome = engine.resume_shared_engine(
                task,
                services,
                session_config,
                checkpoint,
                candidate_export,
                session_dir=next_session,
                transport_origin=transport_origin,
            )
        else:
            outcome = engine.execute_shared_engine(
                task,
                services,
                session_config,
                candidate_export,
                session_dir=next_session,
                transport_origin=transport_origin,
            )
        if outcome.get("all_slots_completed") is not True:
            raise ValueError("shared_company_scan_incomplete")
        # Only publish a fully written original export. A failed candidate is
        # retained; recovery uses the existing native checkpoint in a new
        # segment and never mistakes a half-written directory for completion.
        candidate_export.rename(export)
    return engine.finalize_shared_engine(task, services, export)
