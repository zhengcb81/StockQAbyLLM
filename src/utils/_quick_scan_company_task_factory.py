"""StockQA-owned task construction for the cross-project company scan."""

from __future__ import annotations

import copy

from src.utils.quick_scan_company_context import (
    company_scan_contract,
    company_scan_pack_group_key,
    validate_company_scan_task,
)


class CompanyScanTaskRejected(ValueError):
    """The original owner refused a task request outside its registered scope."""


def _required_text(value, error):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 80
        or any(ord(char) < 32 for char in value)
    ):
        raise CompanyScanTaskRejected(error)


def _selected_ids(contract, question_ids, *, allow_empty=False):
    if question_ids is not None and not isinstance(question_ids, (list, tuple)):
        raise CompanyScanTaskRejected("explicit_ordered_question_subset_required")
    selected = list(contract.eligible_question_ids if question_ids is None else question_ids)
    if (
        (not allow_empty and not selected)
        or any(not isinstance(qid, str) or not qid for qid in selected)
        or len(set(selected)) != len(selected)
        or not set(selected) <= set(contract.eligible_question_ids)
    ):
        raise CompanyScanTaskRejected("nonempty_unique_original_authorized_questions_required")
    return selected


def _question_projection(contract, selected, company_api):
    manifest_rows = contract.question_contracts
    locks = {row["module_id"]: row for row in contract.module_locks}
    observation_questions = contract.observation_context["questions"]
    questions, subject = [], None
    for question_id in selected:
        row = manifest_rows.get(question_id)
        binding = (observation_questions.get(question_id) or {}).get("subject_binding") or {}
        body, ref = binding.get("question_contract"), binding.get("subject_ref")
        if (
            not isinstance(row, dict)
            or not isinstance(body, dict)
            or body.get("id") != question_id
            or body != row
            or not isinstance(ref, dict)
            or row.get("module_id") not in locks
        ):
            raise CompanyScanTaskRejected("full_original_manifest_question_required")
        if subject is not None and ref != subject:
            raise CompanyScanTaskRejected("one_exact_native_subject_required")
        subject = copy.deepcopy(ref)
        version = locks[row["module_id"]].get("version")
        if not isinstance(version, str) or not version:
            raise CompanyScanTaskRejected("original_module_version_required")
        questions.append(
            dict(
                question_id=question_id,
                question=copy.deepcopy(body),
                question_object_sha256=company_api.sha256_hex(body),
                module_id=row["module_id"],
                module_version=version,
            )
        )
    if subject is None:
        raise CompanyScanTaskRejected("original_native_subject_required")
    return questions, subject


def _owner_inputs(contract, subject):
    native_inputs = copy.deepcopy(dict(contract.native_input_references))
    if set(native_inputs) != {
        "manifest.json",
        "identity.json",
        "authority.json",
        "owner-config.json",
        "questions.json",
    }:
        raise CompanyScanTaskRejected("complete_original_native_inputs_required")
    identity = copy.deepcopy(dict(contract.identity_payload))
    if identity.get("entity_id") != subject.get("entity_id"):
        raise CompanyScanTaskRejected("original_identity_subject_required")
    listings = identity.get("listings")
    if (
        not isinstance(listings, list)
        or not listings
        or any(
            not isinstance(row, dict)
            or not isinstance(row.get("market"), str)
            or not isinstance(row.get("ticker"), str)
            or not row["ticker"]
            for row in listings
        )
    ):
        raise CompanyScanTaskRejected("original_identity_listings_required")
    return native_inputs, identity


def _evidence_board(evidence_board):
    board = copy.deepcopy(evidence_board if evidence_board is not None else {"records": []})
    if not isinstance(board, dict) or not isinstance(board.get("records"), list):
        raise CompanyScanTaskRejected("explicit_evidence_board_required")
    return board


def _company_reference(company, company_api):
    encoded = company_api.canonical_json(company).encode("utf8")
    return {
        "path": "native-derived/company.json",
        "bytes": len(encoded),
        "sha256": company_api.sha256_hex(encoded),
    }


def _binding(subject, native_inputs, company_api):
    source_hash = company_api.sha256_hex(native_inputs)
    return dict(
        protocol="iqs.native-company-owner-binding/1.0.0",
        synthetic=False,
        subject_ref=copy.deepcopy(subject),
        authority_ref=copy.deepcopy(native_inputs["authority.json"]),
        native_scope={"scope": subject["scope"], "scope_id": subject["scope_id"]},
        original_inputs_sha256=source_hash,
    )


def _assert_unchanged(lifecycle, original_contract, original_owner):
    current = company_scan_contract(lifecycle)
    if current != original_contract:
        raise CompanyScanTaskRejected("original_owner_changed_during_task_creation")
    if lifecycle.registered_research_work().owner is not original_owner:
        raise CompanyScanTaskRejected("original_owner_changed_during_task_creation")


def _capture_admitted_owner(lifecycle):
    """Bind task construction to the registered owner and its original inputs.

    This only constructs data. Claim and transport enforce current permission.
    """
    work = lifecycle.registered_research_work()
    owner = work.owner
    contract = company_scan_contract(lifecycle)
    _assert_unchanged(lifecycle, contract, owner)
    return contract, owner


def create_native_task(
    lifecycle,
    *,
    experiment_id,
    run_id,
    strategy_id="free_agent",
    question_ids=None,
    evidence_board=None,
    limits=None,
):
    """Build the native full-pack carrier using only the StockQA owner contract."""
    from src.utils.quick_scan_company_api import company_agent_public_api

    contract, owner = _capture_admitted_owner(lifecycle)
    if not contract.information_cutoff:
        raise CompanyScanTaskRejected("registered_information_cutoff_required")
    if strategy_id != "free_agent":
        raise CompanyScanTaskRejected("native_full_pack_strategy_required")
    _required_text(experiment_id, "bounded_experiment_and_run_ids_required")
    _required_text(run_id, "bounded_experiment_and_run_ids_required")
    selected = _selected_ids(contract, question_ids)
    if len(selected) > 12:
        raise CompanyScanTaskRejected("bounded_original_authorized_questions_required")

    company_api = company_agent_public_api(
        "canonical_json",
        "sha256_hex",
        "LIMIT_CAPS",
        "QUESTION_IDS",
        "limits_within_registration",
        "create_owner_task",
    )
    questions, subject = _question_projection(contract, selected, company_api)
    native_inputs, identity = _owner_inputs(contract, subject)
    company = dict(
        protocol="iqs.company-agent-lab.company-identity/2.0.0",
        origin="owner_public",
        company_key=subject["entity_id"],
        label=identity.get("canonical_name") or subject["entity_id"],
        subject={
            "issuer_id": subject["primary_issuer_id"],
            "subject_ref": subject,
            "share_classes": [
                {"market": row["market"], "ticker": row["ticker"]} for row in identity["listings"]
            ],
        },
    )
    profile = dict(
        protocol="iqs.native-company-runtime-profile/1.0.0",
        synthetic=False,
        question_projection=dict(zip(company_api.QUESTION_IDS, selected)),
        provider=contract.route_provider,
        model=contract.route_model,
        native_inputs_sha256=company_api.sha256_hex(native_inputs),
    )
    selected_limits = copy.deepcopy(company_api.LIMIT_CAPS)
    selected_limits["max_search_queries_per_session"] = 0
    if limits is not None:
        if not isinstance(limits, dict):
            raise CompanyScanTaskRejected("bounded_registered_limits_required")
        selected_limits.update(copy.deepcopy(limits))
    if (
        company_api.limits_within_registration(selected_limits)
        or any(type(value) is not int or value < 0 for value in selected_limits.values())
        or selected_limits["max_search_queries_per_session"] != 0
    ):
        raise CompanyScanTaskRejected("bounded_registered_limits_required")

    board = _evidence_board(evidence_board)
    binding = _binding(subject, native_inputs, company_api)
    raw = dict(
        protocol="iqs.company-agent-task/3.0.0",
        experiment_id=experiment_id,
        run_id=run_id,
        company_ref=_company_reference(company, company_api),
        information_cutoff=contract.information_cutoff,
        question_refs=[
            dict(
                copy.deepcopy(native_inputs["manifest.json"]),
                question_id=question["question_id"],
                question_object_sha256=question["question_object_sha256"],
            )
            for question in questions
        ],
        evidence_epoch_ref={
            "path": "native-derived/evidence-board.json",
            "bytes": len(company_api.canonical_json(board).encode("utf8")),
            "sha256": company_api.sha256_hex(company_api.canonical_json(board).encode("utf8")),
        },
        strategy_id=strategy_id,
        limits=selected_limits,
        runtime_profile_ref={
            "path": "native-derived/runtime-profile.json",
            "bytes": len(company_api.canonical_json(profile).encode("utf8")),
            "sha256": company_api.sha256_hex(company_api.canonical_json(profile).encode("utf8")),
        },
        owner_binding=binding,
        native_inputs=native_inputs,
        module_locks=[copy.deepcopy(lock) for lock in contract.module_locks],
    )
    _assert_unchanged(lifecycle, contract, owner)
    task = company_api.create_owner_task(
        raw,
        questions,
        company,
        board,
        profile,
        {
            "protocol": "iqs.native-company-targets/1.0.0",
            "native_manifest_ref": native_inputs["manifest.json"],
        },
        owner_binding=binding,
    )
    _assert_unchanged(lifecycle, contract, owner)
    return task


def create_company_scan_task(
    lifecycle,
    *,
    experiment_id,
    run_id,
    question_ids=None,
    evidence_board=None,
    limits=None,
    pack_by="module",
    max_questions_per_pack=12,
    search_exposure=None,
):
    """Build the shared scan task while identity, source refs and scope stay owner-side."""
    from src.utils.quick_scan_company_api import company_agent_public_api

    contract, owner = _capture_admitted_owner(lifecycle)
    _required_text(experiment_id, "bounded_experiment_and_run_ids_required")
    _required_text(run_id, "bounded_experiment_and_run_ids_required")
    selected = _selected_ids(contract, question_ids)
    company_api = company_agent_public_api(
        "canonical_json",
        "sha256_hex",
        "create_owner_task",
        "build_scan_profile",
        "validate_scan_task",
        "native_company_projection",
        "scan_limits",
    )
    questions, subject = _question_projection(contract, selected, company_api)
    native_inputs, identity = _owner_inputs(contract, subject)
    company = company_api.native_company_projection(identity, subject)
    profile = dict(
        protocol="iqs.company-scan-runtime/1.0.0",
        synthetic=False,
        provider=contract.route_provider,
        model=contract.route_model,
        native_inputs_sha256=company_api.sha256_hex(native_inputs),
    )
    selected_limits = company_api.scan_limits(limits, search_exposure=search_exposure)
    board = _evidence_board(evidence_board)
    binding = _binding(subject, native_inputs, company_api)
    raw = dict(
        protocol="iqs.company-scan-task/1.0.0",
        experiment_id=experiment_id,
        run_id=run_id,
        company_ref=_company_reference(company, company_api),
        information_cutoff=contract.information_cutoff,
        question_refs=[
            dict(
                copy.deepcopy(native_inputs["manifest.json"]),
                question_id=question["question_id"],
                question_object_sha256=question["question_object_sha256"],
            )
            for question in questions
        ],
        evidence_epoch_ref={
            "path": "native-derived/evidence-board.json",
            "bytes": len(company_api.canonical_json(board).encode("utf8")),
            "sha256": company_api.sha256_hex(company_api.canonical_json(board).encode("utf8")),
        },
        strategy_id="module_packs",
        limits=selected_limits,
        runtime_profile_ref={
            "path": "native-derived/runtime-profile.json",
            "bytes": len(company_api.canonical_json(profile).encode("utf8")),
            "sha256": company_api.sha256_hex(company_api.canonical_json(profile).encode("utf8")),
        },
        owner_binding=binding,
        native_inputs=native_inputs,
        module_locks=[copy.deepcopy(lock) for lock in contract.module_locks],
    )
    group_keys = {
        question["question_id"]: company_scan_pack_group_key(
            lifecycle,
            question["question_id"],
            question["question"]["prompt"],
        )
        for question in questions
    }
    execution = company_api.build_scan_profile(
        questions,
        information_cutoff=contract.information_cutoff,
        route_id=contract.route_id,
        group_keys=group_keys,
        pack_by=pack_by,
        max_questions_per_pack=max_questions_per_pack,
        limits=limits,
        search_exposure=search_exposure,
    )
    raw["limits"] = execution["resource_limits"]
    raw["execution_profile"] = execution
    raw["execution_profile_sha256"] = company_api.sha256_hex(execution)
    _assert_unchanged(lifecycle, contract, owner)
    task = company_api.create_owner_task(
        raw,
        questions,
        company,
        board,
        profile,
        {
            "protocol": "iqs.native-company-targets/1.0.0",
            "native_manifest_ref": native_inputs["manifest.json"],
        },
        owner_binding=binding,
    )
    company_api.validate_scan_task(task)
    validate_company_scan_task(lifecycle, task)
    _assert_unchanged(lifecycle, contract, owner)
    return task
