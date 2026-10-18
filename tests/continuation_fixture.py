"""Actual public delegated owner admission over synthetic identity, no fake TTY.

All company facts are synthetic. This authenticates real OS/native records
in an owned test workspace, not real human consent or financial accuracy.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

if os.environ.get("IQS_C06_RUN_INTEGRATION") != "1":
    pytest.skip("requires explicitly owned three-component runtime", allow_module_level=True)

from stockwiki.quick_scan_refresh_records import RefreshRecordStore

from src.utils.quick_scan_result_outbox import canonical_sha256
from src.utils.quick_scan_work_store import QuickScanWorkStore
from tests.integration.test_c06_subject_public_e2e import OWN, invoke, utc
from tests.unit.test_quick_scan_budget import _policy


def setup_continuation(
    actual,
    tmp_path,
    *,
    policy=None,
    ledger_path=None,
    refresh_version="1.4.0",
    provider="openai",
    selected_question_count=1,
    information_cutoff=None,
):
    assert refresh_version == "1.4.0"
    if information_cutoff not in (None, "2026-10-13"):
        raise ValueError("synthetic fixture unsupported information cutoff")
    routes, current, config, authority, manifest, identity_file = actual
    selected = [row for row in manifest["questions"] if row["scope"] == "entity"][
        :selected_question_count
    ]
    if (
        type(selected_question_count) is not int
        or not 1 <= selected_question_count <= 12
        or len(selected) != selected_question_count
    ):
        raise ValueError("synthetic fixture selected question count invalid")
    q = selected[0]
    selected_fields = [
        authority["observation_context"]["questions"][row["id"]]["metadata"]["field_id"]
        for row in selected
    ]
    metadata = authority["observation_context"]["questions"][q["id"]]
    ref = metadata["subject_binding"]["subject_ref"]
    routes.identity.create_universe("admit-pool", "Synthetic test only", soft_target_capacity=1)
    routes.identity.save_member(
        dict(
            universe_id="admit-pool",
            entity_id=ref["entity_id"],
            membership_status="active",
            manual_pin=False,
            scan_enabled=True,
            priority=0,
            added_at=utc(),
            added_reason="synthetic",
            version=1,
        )
    )
    policy = policy or _policy(model="fixture-model")
    ledger = QuickScanWorkStore(ledger_path or tmp_path / "executor.sqlite")
    ledger.configure_quick_scan_budget(policy)
    from decimal import Decimal

    for ordinal in range(98):
        route = policy["routes"][0]
        original_id = "DISPATCH_settled_" + str(ordinal)
        ledger.reserve_budget_attempt(
            policy,
            budget_attempt_id=original_id,
            route_id=route["id"],
            provider=route["provider_config_ref"],
            model_requested=route["model"],
            quota_group=route["quota_group"],
        )
        ledger.record_budget_outcome(
            original_id,
            outcome="response_available",
            http_status_code=200,
            actual_cost=Decimal("0.001"),
            cost_source_ref="synthetic-original-rate",
        )
    for suffix, route in (("unknown", policy["routes"][1]), ("response", policy["routes"][0])):
        ledger.reserve_budget_attempt(
            policy,
            budget_attempt_id="DISPATCH_legacy_" + suffix,
            route_id=route["id"],
            provider=route["provider_config_ref"],
            model_requested=route["model"],
            quota_group=route["quota_group"],
        )
    ledger.record_budget_outcome(
        "DISPATCH_legacy_unknown", outcome="unknown", http_status_code=None
    )
    ledger.record_budget_outcome(
        "DISPATCH_legacy_response", outcome="response_available", http_status_code=200
    )
    budget_file = tmp_path / "budget.json"
    budget_file.write_text(json.dumps(policy), encoding="utf-8")
    base = [
        sys.executable,
        "-B",
        "-X",
        "utf8",
        "-m",
        "stockwiki.quick_scan_refresh_owner_cli",
        "--root",
        str(routes.identity.paths.root),
        "--iqs-code-root",
        str(OWN / "iqs"),
        "--iqs-release-root",
        config["iqs_release_root"],
    ]
    principal = invoke(base + ["--principal"], tmp_path, "principal")["principal"]
    owner_policy = dict(
        protocol="stockwiki.refresh_owner_policy/" + refresh_version,
        policy_id="admit-fixture",
        policy_version=1,
        owner_principal=principal,
        allowed_consumer_ids=["invest-quick-scan"],
        universe_id="admit-pool",
        field_ids=selected_fields,
        provider=provider,
        model=policy["routes"][0]["model"],
        ttl_hours=48,
        routes=[
            dict(
                subject_ref=ref,
                subject_key=config["subject_key"],
                scope=config["scope"],
                scope_id=config["scope_id"],
            )
        ],
        qa=dict(
            code_root=str(OWN / "qa"),
            work_store=str(ledger.path),
            budget_policy_file=str(budget_file),
            policy_id=policy["policy_id"],
            route_id=policy["routes"][0]["id"],
            provider_config_ref=policy["routes"][0]["provider_config_ref"],
        ),
    )
    if refresh_version in {"1.1.0", "1.4.0"}:
        owner_policy["qa"]["quote_schema_version"] = refresh_version
    paths = routes.identity.paths
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    policy_file = paths.config_dir / "quick_scan_refresh.json"
    policy_file.write_text(json.dumps(owner_policy), encoding="utf-8")
    request = dict(
        message_type="request",
        schema_version="2.6.0" if refresh_version in {"1.1.0", "1.4.0"} else "2.0.0",
        request_id="admit-query",
        consumer_id="invest-quick-scan",
        operation="get_profiles",
        requested_at=utc(),
        payload=dict(
            subject_refs=[ref],
            field_ids=selected_fields,
            information_cutoff=information_cutoff or utc()[:10],
            include_history=True,
            model_filter=None,
            snapshot_id=None,
        ),
    )
    request_file = tmp_path / "query.json"
    request_file.write_text(json.dumps(request), encoding="utf-8")
    capture = invoke(
        [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            "-m",
            "stockwiki.quick_scan_query_v2_cli",
            "--root",
            str(paths.root),
            "--iqs-code-root",
            str(OWN / "iqs"),
            "--request",
            str(request_file),
        ],
        tmp_path,
        "capture",
    )
    request["payload"]["snapshot_id"] = capture["result"]["watermark"]["snapshot_id"]
    refresh = dict(
        protocol="iqs.refresh_request/" + refresh_version,
        operation="request_refresh",
        request_id="admit-refresh",
        consumer_id="invest-quick-scan",
        requested_at=utc(),
        source_query=request,
        subject_refs=[ref],
        field_ids=selected_fields,
        origin_task_id="synthetic-admission",
        reason="missing field",
    )
    # The original owner-controlled policy itself accepts the precise retained
    # exposure BEFORE quote. No ready-preview grant is invented to bootstrap it.
    from datetime import datetime, timedelta, timezone

    from src.utils.quick_scan_owner_budget_continuation import (
        NO_EXTERNAL_SEARCH_POLICY,
        digest,
        exposure_snapshot,
        future_request_core,
    )

    inputs = tmp_path / "inputs"
    inputs.mkdir(exist_ok=True)
    runtime = tmp_path / "admission-runs"
    runtime.mkdir(exist_ok=True)
    deployment = dict(
        {
            k: config[k]
            for k in (
                "stockwiki_code_root",
                "stockwiki_workspace_root",
                "iqs_code_root",
                "iqs_release_root",
            )
        },
        protocol="stockqa.refresh_admission_config/1.0.0",
        execution_inputs_root=str(inputs),
        work_store=str(ledger.path),
        budget_policy_file=str(budget_file),
        consumer_id="invest-quick-scan",
        runs_dir=str(runtime),
    )
    deployment_file = tmp_path / "admission-config.json"
    deployment_file.write_text(json.dumps(deployment), encoding="utf8")
    owner_policy["qa"]["admission_config"] = str(deployment_file)
    refresh.update(
        cohort_id="NATIVE_CONTINUATION_12",
        continuation_decision_ref="ACCEPT_SYNTHETIC_LEGACY_PAIR",
        question_ids=[row["id"] for row in selected],
    )
    raw_manifest = Path(config["iqs_release_root"]) / "run/manifest.json"
    native_questions = {
        row["id"]: row for row in json.loads(raw_manifest.read_bytes())["questions"]
    }
    clock = datetime.now(timezone.utc)
    control = dict(
        protocol="stockqa.owner_budget_continuation/1.0.0",
        mode="explicit_repeat_exposure",
        reference_scope="unknown_reference_scope",
        purpose="independent_comparison",
        consumer_id="invest-quick-scan",
        cohort_id=refresh["cohort_id"],
        ledger_path=str(ledger.path.resolve()),
        snapshot=exposure_snapshot(
            ledger.path, policy, ["DISPATCH_legacy_unknown", "DISPATCH_legacy_response"]
        ),
        future_request_core=future_request_core(refresh),
        cells=[
            dict(
                cell_id="MIMO_NATIVE_FULL12",
                question_ids=refresh["question_ids"],
                question_body_sha256={
                    qid: digest(native_questions[qid]) for qid in refresh["question_ids"]
                },
                subject_ref=ref,
                provider=provider,
                model=policy["routes"][0]["model"],
                route_id=policy["routes"][0]["id"],
                quota_group=policy["routes"][0]["quota_group"],
                generation=1,
                manifest_raw_sha256=hashlib.sha256(raw_manifest.read_bytes()).hexdigest(),
                controls=dict(
                    thinking_enabled=True,
                    max_output_tokens=4096,
                    strategy_id="free_agent",
                    search_policy_sha256=digest(NO_EXTERNAL_SEARCH_POLICY),
                    replicate_ordinal=1,
                ),
            )
        ],
        max_new_http_requests=1,
        valid_from=(clock - timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
        expires_at=(clock + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        no_retry_original_intents=True,
        unknown_cash_remains_unknown=True,
        retained_original_requests=True,
        original_process_stopped=True,
        stop_evidence_ref="synthetic-source-local-process-stopped-not-real-provider-proof",
        additional_risk_hold_micros=0,
    )
    owner_policy["budget_exposure_acceptance"] = dict(
        continuation_decision_ref=refresh["continuation_decision_ref"], control=control
    )
    policy_file.write_text(json.dumps(owner_policy), encoding="utf8")
    request_file.write_text(json.dumps(refresh), encoding="utf-8")
    preview = invoke(base + ["--request", str(request_file)], tmp_path, "preview")
    record = RefreshRecordStore(paths).load(preview["preview_id"])
    from datetime import datetime, timedelta, timezone

    install = dict(
        protocol="stockwiki.refresh_delegation_install/1.0.0",
        workspace_root=str(paths.root),
        preview_id=preview["preview_id"],
        preview_record_sha256=record["record_sha256"],
        agent=dict(
            profile_id="delegated-native-e2e",
            profile_sha256="c" * 64,
            harness_ref="scripted-software-test",
        ),
        authorization_reference="synthetic-test-operator-install-not-real-user-grant",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1))
        .isoformat()
        .replace("+00:00", "Z"),
    )
    (paths.config_dir / "quick_scan_delegation_install.json").write_text(
        json.dumps(install), encoding="utf-8"
    )
    grant = invoke(base + ["--install-delegation"], tmp_path, "delegated-install", timeout=180)
    confirmed = invoke(
        base
        + [
            "--confirm-delegated",
            preview["preview_id"],
            "--delegation-id",
            grant["grant_id"],
            "--idempotency-key",
            "delegated-e2e-action",
        ],
        tmp_path,
        "delegated-confirm",
        timeout=180,
    )
    from stockwiki.quick_scan_refresh_delegation import DelegatedConfirmationStore

    consent = DelegatedConfirmationStore(paths).load(confirmed["confirmation_id"])
    # Original public IQS producer exports the full frozen question set.
    producer_input = tmp_path / "producer-input.json"
    raw_manifest = Path(config["iqs_release_root"]) / "run/manifest.json"
    producer_input.write_text(
        json.dumps({"manifest": json.loads(raw_manifest.read_bytes())}),
        encoding="utf-8",
    )
    questions = invoke(
        [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            str(OWN / "iqs/scripts/subject_dispatch_cli.py"),
            "--operation",
            "questions",
            "--release-root",
            config["iqs_release_root"],
            "--input",
            str(producer_input),
        ],
        tmp_path,
        "original-questions",
    )["result"]
    inputs = tmp_path / "inputs"
    inputs.mkdir(exist_ok=True)
    bundle = inputs / ("subject_" + canonical_sha256(ref))
    bundle.mkdir()
    (bundle / "manifest.json").write_bytes(raw_manifest.read_bytes())
    (bundle / "identity.json").write_bytes(identity_file.read_bytes())
    (bundle / "authority.json").write_bytes((tmp_path / "authority.json").read_bytes())
    (bundle / "questions.json").write_text(json.dumps(questions), encoding="utf-8")
    (bundle / "owner-config.json").write_text(
        json.dumps(dict(config, protocol="stockqa.owner_refresh_config/1.0.0", ttl_hours=48)),
        encoding="utf-8",
    )
    runtime = tmp_path / "admission-runs"
    runtime.mkdir(exist_ok=True)
    deployment = dict(
        {
            k: config[k]
            for k in (
                "stockwiki_code_root",
                "stockwiki_workspace_root",
                "iqs_code_root",
                "iqs_release_root",
            )
        },
        protocol="stockqa.refresh_admission_config/1.0.0",
        execution_inputs_root=str(inputs),
        work_store=str(ledger.path),
        budget_policy_file=str(budget_file),
        consumer_id="invest-quick-scan",
        runs_dir=str(runtime),
    )
    deployment_file = tmp_path / "admission-config.json"
    deployment_file.write_text(json.dumps(deployment), encoding="utf-8")
    return dict(
        refresh_request=refresh,
        preview=preview,
        control=control,
        q=q,
        selected_questions=selected,
        ref=ref,
        ledger=ledger,
        consent=consent,
        bundle=bundle,
        deployment_file=deployment_file,
        owner_policy=owner_policy,
        policy_file=policy_file,
        native_owner_command=base,
        grant_id=grant["grant_id"],
        paths=paths,
    )
