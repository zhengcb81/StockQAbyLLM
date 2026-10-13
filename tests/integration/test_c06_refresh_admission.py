"""Native public admission, synthetic owner consent, no HTTP/model fixture.

All company facts are synthetic. This authenticates real OS/native records
in an owned test workspace, not real human consent or financial accuracy.
"""

import hashlib
import json
import os
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

if os.environ.get("IQS_C06_RUN_INTEGRATION") != "1":
    pytest.skip("requires explicitly owned three-component runtime", allow_module_level=True)

from stockwiki.quick_scan_refresh_records import (
    RefreshConfirmationStore,
    RefreshRecordStore,
)

from src.utils.quick_scan_result_outbox import canonical_sha256
from src.utils.quick_scan_work_store import QuickScanWorkStore
from tests.integration.test_c06_subject_public_e2e import OWN, actual, invoke, utc
from tests.unit.test_quick_scan_budget import _policy


def setup_admission(actual, tmp_path, *, policy=None, ledger_path=None, refresh_version="1.0.0"):
    assert refresh_version in {"1.0.0", "1.1.0"}
    routes, current, config, authority, manifest, identity_file = actual
    q = next(row for row in manifest["questions"] if row["scope"] == "entity")
    metadata = authority["observation_context"]["questions"][q["id"]]
    ref, field = (
        metadata["subject_binding"]["subject_ref"],
        metadata["metadata"]["field_id"],
    )
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
    if refresh_version == "1.1.0":
        owner_policy["qa"]["quote_schema_version"] = "1.1.0"
    paths = routes.identity.paths
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    policy_file = paths.config_dir / "quick_scan_refresh.json"
    policy_file.write_text(json.dumps(owner_policy), encoding="utf-8")
    request = dict(
        message_type="request",
        schema_version="2.6.0" if refresh_version == "1.1.0" else "2.0.0",
        request_id="admit-query",
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
        field_ids=[field],
        origin_task_id="synthetic-admission",
        reason="missing field",
    )
    request_file.write_text(json.dumps(refresh), encoding="utf-8")
    preview = invoke(base + ["--request", str(request_file)], tmp_path, "preview")
    record = RefreshRecordStore(paths).load(preview["preview_id"])
    consent = RefreshConfirmationStore(paths).save_confirmation(
        record,
        owner_principal=principal,
        idempotency_key="synthetic-admission-consent",
        confirmed_at=utc(),
    )
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
    inputs.mkdir()
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
    runtime.mkdir()
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
        q=q,
        ref=ref,
        ledger=ledger,
        consent=consent,
        bundle=bundle,
        deployment_file=deployment_file,
        owner_policy=owner_policy,
        policy_file=policy_file,
        paths=paths,
    )


def test_public_admission_uses_exact_native_scope_and_replays_original_work(actual, tmp_path):
    prepared = setup_admission(actual, tmp_path)
    q, ledger, consent = (prepared[k] for k in ("q", "ledger", "consent"))
    deployment_file, paths = (prepared[k] for k in ("deployment_file", "paths"))
    owner_policy, policy_file = (prepared[k] for k in ("owner_policy", "policy_file"))
    command = [
        sys.executable,
        "-B",
        "-X",
        "utf8",
        "-m",
        "src.utils.quick_scan_refresh_admission_cli",
        "--config",
        str(deployment_file),
        "--confirmation-id",
        consent["confirmation_id"],
    ]

    def admission(label):
        # One service aggregates several separately bounded native calls.
        # The general fixture's 80s is a single-call ceiling, not this batch.
        completed = subprocess.run(command, capture_output=True, env=dict(os.environ), timeout=180)
        (tmp_path / (label + ".stdout.log")).write_bytes(completed.stdout)
        (tmp_path / (label + ".stderr.log")).write_bytes(completed.stderr)
        assert completed.returncode == 0, (completed.stdout + completed.stderr).decode(
            "utf-8", errors="replace"
        )
        return json.loads(completed.stdout)

    owner_dbs = list(paths.data_dir.rglob("*.sqlite")) + list(paths.data_dir.rglob("*.sqlite3"))
    owner_before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in owner_dbs}
    accepted = admission("admission-cold")
    assert accepted["status"] == "accepted" and accepted["permission_authenticated"] is True
    assert (
        accepted["registered_with_executor"] is True
        and accepted["dispatch_started"] is False
        and accepted["API_requests"] == 0
    )
    assert accepted["registration"]["permission_authenticated"] is False
    assert len(accepted["registration"]["items"]) == 1
    work_id = accepted["registration"]["items"][0]["work_item_id"]
    assert ledger.get_item(work_id)["question_id"] == q["id"]
    again = admission("admission-restart")
    assert (
        again["status"] == "already_registered"
        and again["registration"] == accepted["registration"]
    )
    assert owner_before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in owner_dbs}
    with closing(ledger._connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM work_item").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM attempt").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM quick_scan_budget_attempt").fetchone()[0] == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM quick_scan_refresh_admission").fetchone()[0]
            == 1
        )
    # New policy revokes the same action; durable registration is not consent.
    owner_policy["policy_version"] = 2
    policy_file.write_text(json.dumps(owner_policy), encoding="utf-8")
    denied = subprocess.run(command, capture_output=True, env=dict(os.environ), timeout=80)
    (tmp_path / "admission-revoked.stdout.log").write_bytes(denied.stdout)
    (tmp_path / "admission-revoked.stderr.log").write_bytes(denied.stderr)
    assert denied.returncode == 2 and json.loads(denied.stdout)["registered_with_executor"] is False
