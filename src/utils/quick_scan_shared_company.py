"""Thin original-runner entry into the shared company engine; no client or queue."""

from __future__ import annotations

import copy
import importlib
import sys
from pathlib import Path

from src.utils.quick_scan_company_api import (
    company_agent_public_api,
    supports_public_api,
)
from src.utils.quick_scan_result_outbox import canonical_sha256, strict_json_loads

SHARED_ENGINE_PROTOCOL_FAMILY = "iqs.shared-company-engine"
SHARED_ENGINE_MINIMUM_VERSION = (1, 0, 0)


def _compatible_public_protocol(value, family, minimum_version):
    return supports_public_api(value, family, minimum_version)


def require_shared_engine_capabilities(
    capabilities, required_operations, *, available_operations=None
):
    """Check the versioned public engine contract before using its operations."""
    if not isinstance(capabilities, dict) or not _compatible_public_protocol(
        capabilities.get("protocol"),
        SHARED_ENGINE_PROTOCOL_FAMILY,
        SHARED_ENGINE_MINIMUM_VERSION,
    ):
        raise ValueError("shared_company_engine_protocol_unsupported")
    if available_operations is not None and not set(required_operations) <= set(
        available_operations
    ):
        raise ValueError("shared_company_engine_operations_incomplete")
    return capabilities


def read_shared_company_config(path, *, recovery_only=False):
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
    optional = {"evidence_boards", "limits", "pack_by"}
    # An explicit import root names the directory that holds the importable
    # companion package, so a consumer need not assume a source layout.
    optional |= {
        "company_agent_import_root",
        "runtime_import_root",
    }
    if (
        not isinstance(config, dict)
        or not required <= set(config)
        or not recovery_only
        and set(config) - required - optional
        or config.get("schema_version") != "1.0.0"
    ):
        raise ValueError("shared_company_config_invalid")
    for key in required - {"schema_version"}:
        if not isinstance(config[key], str) or not Path(config[key]).is_absolute():
            raise ValueError("shared_company_absolute_path_required:" + key)
    for key in ("company_agent_import_root", "runtime_import_root"):
        if key in config and (
            not isinstance(config[key], str) or not Path(config[key]).is_absolute()
        ):
            raise ValueError("shared_company_absolute_path_required:" + key)
    # Root configuration identifies provider installations; implementation file
    # layout is private and may change while the public facades stay compatible.
    for key in required - {"schema_version", "state_root"}:
        if not Path(config[key]).is_dir():
            raise ValueError("shared_company_root_missing:" + key)
    if recovery_only:
        return {key: config[key] for key in required}
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


#: The companion packages this composition root consumes, with their public
#: facade family and the minimum version this owner requires.
COMPANION_FACADES = {
    "iqs_company_agent_lab": ("iqs.company-agent-public-api", (2, 3, 0)),
    "iqs_agent_runtime_lab": ("iqs.agent-runtime-public-api", (1, 4, 0)),
}


def _companion_import_roots(config):
    """Return the import roots that make the companion packages importable.

    The composition root selects the paths; the layout behind a root belongs
    to its owner. An explicit ``*_import_root`` names the directory that
    holds the importable package, so no source layout has to be assumed.
    """
    roots = []
    for key, import_key, package in (
        ("company_agent_root", "company_agent_import_root", "iqs_company_agent_lab"),
        ("runtime_bridge_root", "runtime_import_root", "iqs_agent_runtime_lab"),
    ):
        explicit = config.get(import_key)
        if isinstance(explicit, str) and explicit:
            roots.append((package, Path(explicit)))
            continue
        root = Path(config[key])
        nested = root / "src"
        roots.append((package, nested if nested.is_dir() else root))
    return roots


def _modules(config):
    """Load the shared engine and the companion public facades.

    Only the consumer's own engine layout is asserted. A companion is
    consumed through its versioned public facade, never through its
    implementation file layout, so a companion may move its files freely.
    """
    paths = [
        ("shared_company_engine", Path(config["iqs_root"]) / "scripts"),
        *_companion_import_roots(config),
    ]
    for _, root in reversed(paths):
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
    engine = None
    for name, root in paths:
        target = name if name == "shared_company_engine" else name + ".api"
        try:
            module = importlib.import_module(target)
        except ImportError as error:
            raise ValueError("shared_company_dependency_unavailable:" + name) from error
        if name == "shared_company_engine":
            module_path = getattr(module, "__file__", None)
            if not isinstance(module_path, str) or not Path(module_path).resolve().is_relative_to(
                root.resolve()
            ):
                raise ValueError("shared_company_dependency_shadowed:" + name)
            engine = module
            continue
        family, minimum_version = COMPANION_FACADES[name]
        if not _compatible_public_protocol(
            getattr(module, "PUBLIC_API_PROTOCOL", None), family, minimum_version
        ):
            raise ValueError("shared_company_dependency_protocol_unsupported:" + name)
    return engine


def _company_scan_task_api():
    """Resolve required owner operations from the versioned StockQA facade."""
    try:
        api = importlib.import_module("src.utils.quick_scan_company_api")
    except ImportError as error:
        raise ValueError("shared_company_owner_api_unavailable") from error
    if not _compatible_public_protocol(
        getattr(api, "PUBLIC_API_PROTOCOL", None),
        "stockqa.company-scan-public-api",
        (1, 7, 0),
    ):
        raise ValueError("shared_company_owner_api_version_unsupported")
    required = {"company_scan_contract", "company_scan_capabilities", "create_company_scan_task"}
    exports = getattr(api, "__all__", ())
    if (
        not isinstance(exports, (list, tuple))
        or any(not isinstance(name, str) or not name for name in exports)
        or not required <= set(exports)
    ):
        raise ValueError("shared_company_owner_api_operations_incomplete")
    if any(not callable(getattr(api, operation, None)) for operation in required):
        raise ValueError("shared_company_owner_api_operation_missing")
    return api


def _bound_board(config, *, subject, company_ref, excerpt_cap):
    matches = [b for b in config.get("evidence_boards", []) if b["subject_ref"] == subject]
    if len(matches) > 1:
        raise ValueError("shared_company_duplicate_subject_evidence")
    board = {"records": copy.deepcopy(matches[0]["records"] if matches else [])}
    # The subject comes from the original owner. Only its transport reference
    # is derived here; source text, numbers, dates and scope are never rewritten.
    if not board["records"]:
        return board
    company_api = company_agent_public_api("validate_evidence_record")

    for record in board["records"]:
        if not isinstance(record, dict):
            raise ValueError("shared_company_evidence_record_invalid")
        if "issuer_ref" in record and record["issuer_ref"] != company_ref:
            raise ValueError("shared_company_evidence_issuer_mismatch")
        record.setdefault("issuer_ref", copy.deepcopy(company_ref))
        company_api.validate_evidence_record(record, excerpt_cap)
    return board


def registered_recovery_routes(session, policy, *, store):
    """Read the route of the original received attempt, never current key rank."""
    snapshot = session.snapshot()
    if not session.is_bound_to(store):
        raise ValueError("shared_recovery_original_store_required")
    bindings: set[tuple[str, str, str]] = set()
    for qid in snapshot.requested_question_ids:
        entry = snapshot.entries[qid]
        if entry["kind"] != "work":
            continue
        work = session.existing_work(qid)
        rows = [
            row
            for row in store.list_attempts(work["work_item_id"])
            if row["phase"] == "response_available"
            and store.get_attempt_response(row["attempt_id"])
        ]
        bindings.update((row["route_id"], row["provider"], row["model_requested"]) for row in rows)
    selected = snapshot.ordered_route_targets
    if selected is not None:
        available = session.execution_routes(policy)
        routes = []
        for target in selected:
            binding = (target["route_id"], target["provider_config_ref"], target["model"])
            if binding not in bindings:
                continue
            matches = [
                r for r in available if (r["id"], r["provider_config_ref"], r["model"]) == binding
            ]
            if len(matches) != 1:
                raise ValueError("shared_recovery_original_route_unavailable")
            routes.append(matches[0])
        if not bindings or len(routes) != len(bindings):
            raise ValueError("shared_recovery_original_route_unavailable")
        return routes
    if len(bindings) != 1:
        raise ValueError("shared_recovery_original_route_required")
    binding = next(iter(bindings))
    routes = [
        r
        for r in session.execution_routes(policy)
        if (r["id"], r["provider_config_ref"], r["model"]) == binding
    ]
    if len(routes) != 1:
        raise ValueError("shared_recovery_original_route_unavailable")
    return routes


def load_original_company_task(checkpoint):
    company_api = company_agent_public_api("load_checkpoint_task")
    return company_api.load_checkpoint_task(checkpoint)


class SharedCompanyIncomplete(ValueError):
    """Original export is partial; only native receipts may permit fallback."""

    def __init__(self):
        super().__init__("shared_company_scan_incomplete")


def ordered_task_root(config, owner, scope, route_id):
    snapshot = owner.snapshot()
    identity = canonical_sha256(
        dict(
            confirmation_id=snapshot.proof["confirmation"]["confirmation_id"],
            question_ids=scope,
            route_id=route_id,
        )
    )
    return Path(config["state_root"]) / ("scan-" + identity[:48])


def original_task_checkpoint(root):
    if (root / "export" / "checkpoint.json").is_file():
        return root / "export" / "checkpoint.json"
    return next(
        iter(sorted(root.glob("session-[0-9][0-9][0-9][0-9]/checkpoint.json"), reverse=True)), None
    )


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
    recovery_only=False,
    ordered_scope=None,
    request_scope=None,
):
    """Reuse original services, durable engine checkpoint/export and finalizer.

    The caller hydrates the resulting original checkpoints into its normal
    output. An interrupted or rejected engine never falls back to asking again.
    """
    work = lifecycle.registered_research_work()
    native_search = lifecycle.company_scan_uses_native_search(recovery_only=recovery_only)
    engine = _modules(config)
    owner_api = _company_scan_task_api()
    engine_operations = {
        name
        for name in ("execute_shared_engine", "resume_shared_engine", "finalize_shared_engine")
        if callable(getattr(engine, name, None))
    }
    require_shared_engine_capabilities(
        engine.capabilities(),
        {"execute_shared_engine", "resume_shared_engine", "finalize_shared_engine"},
        available_operations=engine_operations,
    )
    company_api = company_agent_public_api(
        "task_snapshot",
        "native_company_projection",
        "canonical_json",
        "sha256_hex",
        "scan_limits",
        "validate_evidence_record",
        "load_checkpoint_task",
    )
    owner = work.owner
    owner_snapshot = owner.snapshot()
    scan_contract = owner_api.company_scan_contract(lifecycle)
    ids = [q.question_id for q in questions]
    if (
        not ids
        or len(set(ids)) != len(ids)
        or any(qid not in scan_contract.requested_question_ids for qid in ids)
    ):
        raise ValueError("shared_company_original_question_selection_required")
    selected = [qid for qid in ids if qid in scan_contract.eligible_question_ids]
    if not selected:
        return None  # The original hydrator handles references without a send.
    # Output file names change on UI resume. The original admitted scope does
    # not, so they must never form a new paid company task/session identity.
    identity = canonical_sha256(
        {
            "confirmation_id": owner_snapshot.proof["confirmation"]["confirmation_id"],
            "question_ids": selected,
        }
    )
    run_id = "scan-" + identity[:48]
    # Read the original task before inspecting NEW control/evidence settings.
    # A paid scan retains its frozen identity, board and session limits.
    root = Path(config["state_root"]) / run_id
    if ordered_scope is not None:
        if owner_snapshot.ordered_route_targets is None or not set(selected) <= set(ordered_scope):
            raise ValueError("shared_company_ordered_scope_not_approved")
        root = ordered_task_root(config, owner, ordered_scope, work.route_id)
        run_id = root.name
    checkpoints = (
        [root / "export" / "checkpoint.json"]
        if (root / "export").exists()
        else sorted(root.glob("session-[0-9][0-9][0-9][0-9]/checkpoint.json"), reverse=True)
    )
    if checkpoints:
        task = load_original_company_task(checkpoints[0])
        task_contract = company_api.task_snapshot(task)
        task_questions = task_contract["question_ids"]
        task_limits = task_contract["limits"]
        task_raw = task_contract["raw"]
        if (
            task_questions != selected
            if ordered_scope is None
            else not set(selected) <= set(task_questions) <= set(ordered_scope)
        ):
            raise ValueError("shared_company_original_question_selection_changed")
        max_output_tokens = task_limits["logical_max_output_tokens_including_reasoning_per_request"]
        question_pack_size = max(map(len, task_raw["execution_profile"]["packs"]))
        thinking_enabled = True  # Original one-shot scan protocol always requires thinking.
    else:
        if recovery_only:
            raise ValueError("shared_company_original_checkpoint_required")
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
        identity_payload = dict(scan_contract.identity_payload)
        subject = scan_contract.observation_context["questions"][selected[0]]["subject_binding"][
            "subject_ref"
        ]
        company = company_api.native_company_projection(identity_payload, subject)
        company_bytes = company_api.canonical_json(company).encode("utf8")
        reference = dict(
            path="native-derived/company.json",
            bytes=len(company_bytes),
            sha256=company_api.sha256_hex(company_bytes),
        )
        board = _bound_board(
            config,
            subject=subject,
            company_ref=reference,
            excerpt_cap=company_api.scan_limits(limits)["excerpt_chars_per_source"],
        )
        task = owner_api.create_company_scan_task(lifecycle, evidence_board=board, **kwargs)
        task_contract = company_api.task_snapshot(task)
        task_limits = task_contract["limits"]
    if not recovery_only:
        capabilities = owner_api.company_scan_capabilities(client, native_search=native_search)
        if (
            thinking_enabled is False
            or type(max_output_tokens) is not int
            or not 1 <= max_output_tokens <= capabilities["max_output_tokens"]
        ):
            raise ValueError("shared_company_requires_thinking_and_supported_output")
        if native_search and not capabilities["native_web_search"]:
            raise ValueError("shared_company_native_search_endpoint_not_qualified")
        if question_pack_size > 1 and not capabilities["question_packs"]:
            raise ValueError("shared_company_question_packs_not_supported")
    from src.utils.quick_scan_owner_research import OwnerResearchServices

    services = OwnerResearchServices(
        lifecycle,
        client,
        task=task,
        cost_resolver=cost_resolver,
        owner_batch_claim=True,
        native_search=native_search,
        request_scope=(
            None
            if request_scope is None
            else (
                request_scope[0],
                (
                    request_scope[1]
                    if request_scope[1] is not None
                    else task_limits["max_model_requests_per_session"]
                ),
            )
        ),
    )
    session_config = {
        "protocol": "iqs.agent-runtime-lab.session-config/1.1.0",
        "planned_model": client.model,
        "endpoint_profile_id": client.provider_name + "@" + client.base_url,
        "max_output_tokens": max_output_tokens,
        "session_input_tokens": task_limits["session_input_tokens"],
        "session_output_tokens": task_limits["session_output_tokens_including_reasoning"],
        "wall_seconds": task_limits["company_wall_seconds"],
        "logical_clock": False,
    }
    export = root / "export"
    if not export.exists():
        if recovery_only and not root.is_dir():
            raise ValueError("shared_company_original_session_required")
        root.mkdir(parents=True, exist_ok=True)
        sessions = sorted(root.glob("session-[0-9][0-9][0-9][0-9]"))
        sequence = len(sessions) + 1
        next_session = root / ("session-%04d" % sequence)
        candidate_export = root / ("export-%04d" % sequence)
        if sessions:
            checkpoint = sessions[-1] / "checkpoint.json"
            if recovery_only and not (checkpoint.exists() or checkpoint.is_symlink()):
                # A rejected replay can export diagnostics without creating a
                # new send checkpoint. Re-read the latest original pending
                # checkpoint under the same zero-send guard; keep diagnostics.
                checkpoint = next(
                    (
                        folder / "checkpoint.json"
                        for folder in reversed(sessions)
                        if (folder / "checkpoint.json").exists()
                        or (folder / "checkpoint.json").is_symlink()
                    ),
                    checkpoint,
                )
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
                replay_only=recovery_only,
            )
        else:
            if recovery_only:
                raise ValueError("shared_company_original_checkpoint_required")
            outcome = engine.execute_shared_engine(
                task,
                services,
                session_config,
                candidate_export,
                session_dir=next_session,
                transport_origin=transport_origin,
            )
        if outcome.get("all_slots_completed") is not True:
            # Earlier packs may already have a paid, verified answer. Preserve
            # only those exact original completed members through the same
            # native finalizer before reporting the remaining scan incomplete.
            # The candidate stays partial; never publish it as the full export.
            completed = [
                row["question_id"]
                for row in outcome.get("run", {}).get("slots", [])
                if row.get("execution_status") == "completed" and row.get("answer") is not None
            ]
            if completed:
                engine.finalize_shared_engine(
                    task, services, candidate_export, question_ids=completed
                )
            raise SharedCompanyIncomplete()
        # Only publish a fully written original export. A failed candidate is
        # retained; recovery uses the existing native checkpoint in a new
        # segment and never mistakes a half-written directory for completion.
        candidate_export.rename(export)
    return engine.finalize_shared_engine(task, services, export)
