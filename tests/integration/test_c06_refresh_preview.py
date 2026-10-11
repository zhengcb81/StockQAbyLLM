"""Real public preview CLI/owner/OS account/Q09; all issuer inputs synthetic.

No model/HTTP stubs are necessary: every operation is read-only inspection.
This proves no confirmation, charged call, financial accuracy or real roster.
"""

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

if os.environ.get("IQS_C06_RUN_INTEGRATION") != "1":
    pytest.skip(
        "C06 integration requires explicit owned three-component runtime", allow_module_level=True
    )

from stockwiki.quick_scan_subject_authority import subject_ref_for_question

from src.utils.quick_scan_work_store import QuickScanWorkStore
from tests.integration.test_c06_subject_public_e2e import OWN, actual, invoke, utc
from tests.unit.test_quick_scan_budget import _policy


def files(paths):
    return {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def test_public_preview_reads_real_current_owner_capture_and_budget_then_refuses_unauthorized_scope(
    actual, tmp_path
):
    routes, current, config, authority, manifest, identity_file = actual
    paths = routes.identity.paths
    q = next(q for q in manifest["questions"] if q["scope"] == "entity")
    field = q.get("field_id", q["metric_id"])
    ref = subject_ref_for_question(
        routes,
        q,
        current["snapshot"],
        routes.identity_binding(config["subject_key"]),
        now_utc=utc(),
    )
    routes.identity.create_universe("preview-pool", "Synthetic test only", soft_target_capacity=1)
    member = dict(
        universe_id="preview-pool",
        entity_id=ref["entity_id"],
        membership_status="active",
        manual_pin=False,
        scan_enabled=True,
        priority=0,
        added_at=utc(),
        added_reason="synthetic fixture",
        version=1,
    )
    routes.identity.save_member(member)
    policy = _policy(model="fixture-model")
    ledger = QuickScanWorkStore(tmp_path / "executor.sqlite")
    ledger.configure_quick_scan_budget(policy)
    budget_file = tmp_path / "executor-budget.json"
    budget_file.write_text(json.dumps(policy), encoding="utf-8")
    principal = invoke(
        [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            "-m",
            "stockwiki.quick_scan_refresh_owner_cli",
            "--root",
            str(paths.root),
            "--iqs-code-root",
            str(OWN / "iqs"),
            "--principal",
        ],
        tmp_path,
        "native-principal",
    )["principal"]
    assert principal.startswith("windows-sid:" if os.name == "nt" else "uid:")
    owner_policy = dict(
        protocol="stockwiki.refresh_owner_policy/1.0.0",
        policy_id="owner-fixture",
        policy_version=1,
        owner_principal=principal,
        allowed_consumer_ids=["invest-quick-scan"],
        universe_id="preview-pool",
        field_ids=[field],
        provider="openai",
        model="fixture-model",
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
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    policy_file = paths.config_dir / "quick_scan_refresh.json"
    policy_file.write_text(json.dumps(owner_policy), encoding="utf-8")
    request = dict(
        message_type="request",
        schema_version="2.0.0",
        request_id="source-preview-fixture",
        consumer_id="invest-quick-scan",
        operation="get_profiles",
        requested_at=utc(),
        payload=dict(
            subject_refs=[ref],
            field_ids=[field],
            information_cutoff=utc()[:10],
            include_history=True,
            model_filter=None,
            snapshot_id=None,
        ),
    )
    file = tmp_path / "query.json"
    file.write_text(json.dumps(request), encoding="utf-8")
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
            str(file),
        ],
        tmp_path,
        "preview-capture",
    )
    request["payload"]["snapshot_id"] = capture["result"]["watermark"]["snapshot_id"]
    refresh = dict(
        protocol="iqs.refresh_request/1.0.0",
        operation="request_refresh",
        request_id="preview-test",
        consumer_id="invest-quick-scan",
        requested_at=utc(),
        source_query=request,
        subject_refs=[ref],
        field_ids=[field],
        origin_task_id="synthetic-test",
        reason="missing field",
    )
    file = tmp_path / "refresh.json"
    file.write_text(json.dumps(refresh), encoding="utf-8")
    command = [
        sys.executable,
        "-B",
        "-X",
        "utf8",
        "-m",
        "stockwiki.quick_scan_refresh_owner_cli",
        "--root",
        str(paths.root),
        "--iqs-code-root",
        str(OWN / "iqs"),
        "--iqs-release-root",
        config["iqs_release_root"],
        "--request",
        str(file),
    ]
    databases = [ledger.path, *paths.data_dir.rglob("*.sqlite"), *paths.data_dir.rglob("*.sqlite3")]
    before = files(databases)
    preview = invoke(command, tmp_path, "owner-preview")
    assert preview["status"] == "ready" and preview["dispatch_started"] is False
    assert preview["requires_user_confirmation"] is True and preview["API_requests"] == 0
    assert preview["items"] == [
        dict(
            subject_ref=ref,
            field_id=field,
            question_id=q["id"],
            decision="dispatch_new_work",
            generation=1,
            reuse_observation_id=None,
            existing_work_status=None,
        )
    ]
    assert preview["quote"]["max_cost_micros"] == 10000000
    assert before == files(databases)
    from stockwiki.quick_scan_refresh_records import RefreshRecordStore

    record = RefreshRecordStore(paths).load(preview["preview_id"])
    assert record["preview"] == preview and record["request"] == refresh
    assert record["owner_principal"] == principal
    # Piped input is not authenticated human confirmation, even with the
    # expected phrase. Test the real entry point without inventing a golden.
    confirm_command = [
        sys.executable,
        "-B",
        "-X",
        "utf8",
        "-m",
        "stockwiki.quick_scan_refresh_owner_cli",
        "--root",
        str(paths.root),
        "--iqs-code-root",
        str(OWN / "iqs"),
        "--iqs-release-root",
        config["iqs_release_root"],
        "--confirm",
        preview["preview_id"],
        "--idempotency-key",
        "synthetic-piped-action",
    ]
    denied = subprocess.run(
        confirm_command,
        input=("CONFIRM " + preview["preview_id"][-12:] + "\n").encode(),
        env=dict(os.environ),
        capture_output=True,
        timeout=80,
    )
    (tmp_path / "confirmation-noninteractive.stdout.log").write_bytes(denied.stdout)
    (tmp_path / "confirmation-noninteractive.stderr.log").write_bytes(denied.stderr)
    assert denied.returncode == 2
    assert json.loads(denied.stdout)["dispatch_started"] is False
    assert not (paths.state_dir / "quick_scan_refresh/confirmations").exists()
    assert before == files(databases)
    # Synthetic privileged-owner consent fixture for the real read bridge.
    # No human typed this; it is not a real consent/financial golden.
    from stockwiki.quick_scan_refresh_records import RefreshConfirmationStore

    consent = RefreshConfirmationStore(paths).save_confirmation(
        record,
        owner_principal=principal,
        idempotency_key="synthetic-inspection-fixture",
        confirmed_at=utc(),
    )
    inspection_command = [
        sys.executable,
        "-B",
        "-X",
        "utf8",
        "-m",
        "stockwiki.quick_scan_refresh_owner_cli",
        "--root",
        str(paths.root),
        "--iqs-code-root",
        str(OWN / "iqs"),
        "--iqs-release-root",
        config["iqs_release_root"],
        "--inspect-confirmation",
        consent["confirmation_id"],
    ]
    inspection = invoke(inspection_command, tmp_path, "synthetic-consent-native-inspection")
    assert inspection["protocol"] == "stockwiki.refresh_approval_inspection/1.0.0"
    assert inspection["preview"] == preview and inspection["confirmation"] == consent
    assert inspection["registered_with_executor"] is False and inspection["API_requests"] == 0
    assert before == files(databases)
    # Actual fixed StockQA -> native StockWiki read, not a JSON confirmation
    # callback. Consent is still the explicit synthetic fixture above.
    from src.runners.llm_runner import load_identity_snapshot, routing_fingerprint_for
    from src.utils.quick_scan_owner_refresh import (
        OwnerRefreshClient,
        OwnerRefreshRejected,
    )
    from src.utils.quick_scan_refresh_batch import register_refresh_action
    from src.utils.quick_scan_refresh_consent import RefreshConsentClient
    from src.utils.quick_scan_result_outbox import canonical_sha256

    owner_config = tmp_path / "owner-config.json"
    owner_config.write_text(
        json.dumps(dict(config, protocol="stockqa.owner_refresh_config/1.0.0", ttl_hours=48)),
        encoding="utf-8",
    )
    owner = OwnerRefreshClient(owner_config, process_env=dict(os.environ))
    bridge = RefreshConsentClient(
        owner,
        work_store=ledger.path,
        budget_policy_file=budget_file,
        consumer_id=refresh["consumer_id"],
    )
    actual_consent = bridge.inspect(consent["confirmation_id"], mode="fresh_registration")
    assert (
        actual_consent["confirmation"] == consent and actual_consent["owner_policy"] == owner_policy
    )
    assert actual_consent["inspection_mode"] == "fresh_registration" and before == files(databases)
    identity = load_identity_snapshot(identity_file, expected_entity_id=ref["entity_id"])
    session = owner.prepare(
        manifest,
        ledger,
        identity=identity,
        provider=preview["provider"],
        model=preview["model"],
        now=utc(),
        observation_context=authority["observation_context"],
    )
    assert authority["schema_version"] == "3.0.0"
    question_context = authority["observation_context"]["questions"][q["id"]]
    assert question_context["subject_binding"]["subject_ref"] == ref
    assert question_context["metadata"]["field_id"] == field
    spec = dict(
        identity,
        question_id=q["id"],
        generation=preview["items"][0]["generation"],
        scope=ref["scope"],
        scope_id=ref["entity_id"],
        question_fingerprint=hashlib.sha256(q["prompt"].strip().encode()).hexdigest(),
        routing_fingerprint=routing_fingerprint_for([r["prompt"] for r in manifest["questions"]]),
        owner_refresh_binding=session.binding(q["id"]),
    )
    action = dict(
        confirmation_id=consent["confirmation_id"],
        confirmation_sha256=canonical_sha256(consent),
        preview_id=preview["preview_id"],
        scope_sha256=preview["scope_sha256"],
        policy_sha256=preview["policy_sha256"],
        execution_inputs_sha256=canonical_sha256(
            dict(
                manifest=manifest["manifest_sha256"],
                identity=identity["identity_snapshot_sha256"],
                context=authority["observation_context_sha256"],
            )
        ),
    )

    def recheck(mode):
        returned = bridge.inspect(consent["confirmation_id"], mode=mode)
        assert returned["confirmation"] == consent and returned["preview"] == preview
        session.check()

    result = register_refresh_action(
        ledger, [spec], action=action, run_id="RUN_E2E", scan_id="SCAN_E2E", recheck=recheck
    )
    assert result["permission_authenticated"] is False and result["API_requests"] == 0
    assert result["dispatch_started"] is False and len(result["items"]) == 1
    work = ledger.get_item(result["items"][0]["work_item_id"])
    assert work["question_id"] == q["id"] and work["status"] == "pending"
    assert ledger.get_owner_refresh_binding(work["work_item_id"])["binding"] == session.binding(
        q["id"]
    )
    # Only the original ledger changed. No model attempt, budget reservation,
    # other company question, observation or owner data was written.
    assert all(files(databases)[p] == h for p, h in before.items() if p != ledger.path)
    with ledger._transaction() as con:
        assert con.execute("SELECT COUNT(*) FROM work_item").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM attempt").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM quick_scan_budget_attempt").fetchone()[0] == 0
    before = files(databases)
    with pytest.raises(OwnerRefreshRejected):
        bridge.inspect(consent["confirmation_id"], mode="fresh_registration")
    assert (
        bridge.inspect(consent["confirmation_id"], mode="current_permissions")["confirmation"]
        == consent
    )
    again = register_refresh_action(
        QuickScanWorkStore(ledger.path),
        [spec],
        action=action,
        run_id="RUN_E2E",
        scan_id="SCAN_E2E",
        recheck=recheck,
    )
    assert again == result and before == files(databases)
    # An explicit owner policy opt-in uses the real current membership and
    # module anchors, so expanding a user-approved pool does not require
    # hand-writing a 2,000-row subject/field allowlist. No name-based joining.
    owner_policy.update(
        policy_version=2,
        route_resolution="current_owner_members",
        field_permission="current_manifest",
        routes=[],
        field_ids=[],
    )
    policy_file.write_text(json.dumps(owner_policy), encoding="utf-8")
    dynamic = invoke(command, tmp_path, "owner-dynamic-preview")
    assert dynamic["status"] == "ready" and dynamic["items"] == [
        dict(preview["items"][0], decision="resume_existing_work", existing_work_status="pending")
    ]
    assert (
        dynamic["policy_version"] == 2
        and dynamic["owner_state_sha256"] != preview["owner_state_sha256"]
    )
    assert before == files(databases)
    stale = subprocess.run(
        inspection_command, env=dict(os.environ), capture_output=True, timeout=80
    )
    assert stale.returncode == 2 and json.loads(stale.stdout)["dispatch_started"] is False
    assert before == files(databases)
    with pytest.raises(OwnerRefreshRejected):
        bridge.inspect(consent["confirmation_id"], mode="current_permissions")
    # Consumer JSON cannot manufacture an owner confirmation or model choice.
    for key, value in [("confirmed", True), ("model", "self-selected-model")]:
        bad = copy.deepcopy(refresh)
        bad[key] = value
        file.write_text(json.dumps(bad), encoding="utf-8")
        result = subprocess.run(command, env=dict(os.environ), capture_output=True, timeout=30)
        assert result.returncode == 2 and json.loads(result.stdout)["dispatch_started"] is False
        assert before == files(databases)
    # Permission comes from the original membership, not the snapshot.
    file.write_text(json.dumps(refresh), encoding="utf-8")
    member.update(
        membership_status="logically_removed",
        version=2,
        removed_at=utc(),
        removal_reason="fixture removal",
    )
    routes.identity.save_member(member)
    removed = files(databases)
    result = subprocess.run(command, env=dict(os.environ), capture_output=True, timeout=30)
    assert result.returncode == 2
    assert removed == files(databases)
    assert not (paths.state_dir / "refresh-confirmations").exists()
