"""Continue an ORIGINAL stopped synthetic fixture, without another model call.

Setup/one HTTP substitution/pending-delivery checks are previous terminal
evidence. This bounded lane only imports/ACKs that SAME original package,
uses the real public OS main for references and then verifies revocation.
No bootstrap, new identity, new model fixture or fabricated financial gold.
"""

import copy
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

if os.environ.get("IQS_C06_RUN_INTEGRATION") != "1":
    pytest.skip("requires explicitly owned three-component runtime", allow_module_level=True)

from stockwiki.paths import WorkspacePaths
from stockwiki.quick_scan_import import import_package
from stockwiki.quick_scan_store import QuickScanStore
from stockwiki.quick_scan_subject_authority import IqsSubjectContract
from stockwiki.quick_scan_subject_dispatch import SubjectBoundObservationStore

from src.utils.quick_scan_refresh_admission import RefreshAdmissionService
from src.utils.quick_scan_result_outbox import strict_json_loads
from src.utils.quick_scan_work_store import QuickScanWorkStore
from tests.integration.test_c06_refresh_admission import setup_admission
from tests.integration.test_c06_subject_public_e2e import actual

OWN = Path(os.environ["IQS_C06_OWN"]).resolve()


@pytest.mark.skipif(
    not os.environ.get("IQS_C06_RESUME_SOURCE"),
    reason="once-only original stopped experiment requires IQS_C06_RESUME_SOURCE",
)
def test_public_os_main_references_original_ack_then_refuses_revoked_permission(tmp_path):
    source = Path(os.environ["IQS_C06_RESUME_SOURCE"]).resolve()
    assert source.is_relative_to(OWN) and source.name == "test_public_main_only_dispatch0"
    assert not (source / "llm_apis.json").exists() and not (source / "spend.json").exists()
    deployment_file = source / "admission-config.json"
    deployment = strict_json_loads(deployment_file.read_bytes())
    inputs = Path(deployment["execution_inputs_root"])
    bundles = list(inputs.iterdir())
    assert len(bundles) == 1 and bundles[0].is_dir()
    bundle = bundles[0]
    manifest = strict_json_loads((bundle / "manifest.json").read_bytes())
    authority = strict_json_loads((bundle / "authority.json").read_bytes())
    assert all(
        q["metadata"]["inputset_id"] == "INPUT_SYNTHETIC"
        for q in authority["observation_context"]["questions"].values()
    )
    config = strict_json_loads((bundle / "owner-config.json").read_bytes())
    paths = WorkspacePaths.from_root(Path(config["stockwiki_workspace_root"]))
    identity = QuickScanStore(paths)
    store = QuickScanWorkStore(Path(deployment["work_store"]))
    with closing(sqlite3.connect(store.path.as_uri() + "?mode=ro", uri=True)) as con:
        works = con.execute("SELECT work_item_id,question_id,entity_id FROM work_item").fetchall()
        confirms = con.execute(
            "SELECT confirmation_id FROM quick_scan_refresh_admission"
        ).fetchall()
    assert len(works) == len(confirms) == 1
    work_id, qid, entity_id = works[0]
    assert identity.get_entity(entity_id)["entity_id"] == entity_id
    assert all(
        q["subject_binding"]["subject_ref"]["analysis_subject_id"]
        == "ASJ_11111111-1111-4111-8111-111111111111"
        for q in authority["observation_context"]["questions"].values()
    )
    before_attempts = copy.deepcopy(store.list_attempts(work_id))
    assert len(before_attempts) == 1
    delivery = store.get_result_delivery(work_id)
    assert delivery is not None and delivery["package"] is not None
    original = copy.deepcopy(delivery["package"]["items"][0]["observation"])
    # Preserve the stopped fixture's database bytes before continuing it.
    backups = tmp_path / "stopped-fixture-before"
    backups.mkdir()
    hashes = {}
    for path in [store.path, *paths.data_dir.rglob("*.sqlite")]:
        assert not any(
            path.with_name(path.name + suffix).exists() for suffix in ("-wal", "-shm", "-journal")
        )
        raw = path.read_bytes()
        relative = path.relative_to(source)
        target = backups / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        hashes[relative.as_posix()] = hashlib.sha256(raw).hexdigest()
    (tmp_path / "stopped-fixture-before.json").write_text(json.dumps(hashes), encoding="utf-8")

    def stage(value):
        with (tmp_path / "stages.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"stage": value, "work_item_id": work_id}) + "\n")

    stage("original_task_" + store.get_item(work_id)["status"])
    owner = SubjectBoundObservationStore(
        paths,
        contract=IqsSubjectContract(
            OWN / "iqs",
            release_root=Path(config["iqs_release_root"]),
            runs_dir=tmp_path / "contract-runs",
            process_env=dict(os.environ),
        ),
    )
    if delivery["state"] != "delivered":
        assert store.get_item(work_id)["status"] == "result_ready"
        frozen = owner.contract.release_projection(manifest)
        imported = import_package(
            owner, delivery["package"], frozen_release=frozen, identity_store=identity
        )
        assert imported["summary"] in ({"accepted": 1}, {"already_present": 1})
        consumer = dict(component="StockWiki", namespace="quick_scan", store_id=owner.store_id)
        store.bind_result_delivery_consumer(
            work_id, consumer, source_ref="owner-config:" + str(bundle / "owner-config.json")
        )
        assert store.apply_result_delivery_ack(work_id, imported["acks"][0])["state"] == "delivered"
    stage("original_package_delivered")
    output = tmp_path / "original-reference.json"
    command = [
        sys.executable,
        "-B",
        "-X",
        "utf8",
        str(OWN / "qa/main_with_llm.py"),
        "--company",
        "Synthetic E2E issuer",
        "--entity-id",
        entity_id,
        "--provider",
        "primary",
        "--require-search",
        "--config",
        str(bundle / "questions.json"),
        "--output",
        str(output),
        "--identity-snapshot",
        str(bundle / "identity.json"),
        "--question-manifest",
        str(bundle / "manifest.json"),
        "--c06-authority",
        str(bundle / "authority.json"),
        "--quick-scan-owner-config",
        str(bundle / "owner-config.json"),
        "--refresh-admission-config",
        str(deployment_file),
        "--refresh-confirmation-id",
        confirms[0][0],
    ]
    stage("public_main_reference_started")
    completed = subprocess.run(
        command, cwd=source, env=dict(os.environ), capture_output=True, timeout=180
    )
    (tmp_path / "reference.stdout.log").write_bytes(completed.stdout)
    (tmp_path / "reference.stderr.log").write_bytes(completed.stderr)
    assert completed.returncode == 0, (completed.stdout + completed.stderr).decode(
        "utf-8", errors="replace"
    )
    response = strict_json_loads(output.read_bytes())
    reference = response["observation_references"][qid]
    assert reference["observation_id"] == original["observation_id"]
    assert reference["payload_sha256"] == delivery["payload_sha256"]
    assert (
        reference["original_observation"] == original
        and reference["new_observation_created"] is False
    )
    assert (
        response["requested_scope_completed"] is True
        and response["whole_manifest_scanned"] is False
    )
    assert response["API_requests"] == 0 and response["model_dispatch_started"] is False
    assert store.list_attempts(work_id) == before_attempts
    assert store.get_result_delivery(work_id)["package_sha256"] == delivery["package_sha256"]
    assert not (source / "llm_apis.json").exists()
    stage("original_reference_verified")
    owner_policy_file = paths.config_dir / "quick_scan_refresh.json"
    policy = strict_json_loads(owner_policy_file.read_bytes())
    original_policy = owner_policy_file.read_bytes()
    policy["policy_version"] += 1
    try:
        owner_policy_file.write_text(json.dumps(policy), encoding="utf-8")
        denied = subprocess.run(
            command, cwd=source, env=dict(os.environ), capture_output=True, timeout=80
        )
        (tmp_path / "revoked.stdout.log").write_bytes(denied.stdout)
        (tmp_path / "revoked.stderr.log").write_bytes(denied.stderr)
        assert denied.returncode != 0
        assert store.list_attempts(work_id) == before_attempts
        assert store.get_result_delivery(work_id)["package_sha256"] == delivery["package_sha256"]
        stage("revoked_permission_refused_without_send")
    finally:
        owner_policy_file.write_bytes(original_policy)  # Restore the owned test policy.


@pytest.mark.parametrize("state", ["active_lease", "uncertain"])
def test_public_main_defers_original_active_or_unknown_work_without_credentials(
    actual, tmp_path, state
):
    import time

    from src.utils.quick_scan_observation_context import bind_context_to_work_item
    from src.utils.quick_scan_subject_journal import dispatch_request
    from src.utils.quick_scan_subject_owner import register
    from tests.unit.test_quick_scan_budget import _policy
    from tests.unit.test_quick_scan_work_store import _prepared

    prepared = setup_admission(actual, tmp_path)
    accepted = RefreshAdmissionService(
        prepared["deployment_file"], process_env=dict(os.environ)
    ).admit(prepared["consent"]["confirmation_id"])
    work_id = accepted["registration"]["items"][0]["work_item_id"]
    clock = [time.time()]
    store = QuickScanWorkStore(prepared["ledger"].path, clock=lambda: clock[0])
    lease = store.claim(work_id, lease_seconds=600)
    if state == "uncertain":
        routes, current, config, authority, manifest, identity_file = actual
        policy = _policy(model="fixture-model")
        route = policy["routes"][0]
        # Use the ORIGINAL pre-dispatch context and native owner registrar;
        # a synthetic receipt or bypass of context2 would weaken this test.
        context = authority["observation_context"]
        store.freeze_observation_context_before_send(work_id, lease, context)
        qid = store.get_item(work_id)["question_id"]
        attempt = _prepared(
            store,
            work_id,
            lease,
            provider=route["provider_config_ref"],
            model="fixture-model",
            route_id=route["id"],
            prompt_sha256=context["questions"][qid]["work_prompt_sha256"],
        )
        bound = bind_context_to_work_item(
            context, work_item=store.get_item(work_id), question_id=qid
        )
        request = dispatch_request(
            store.get_item(work_id),
            attempt,
            bound,
            execution_attempt_id="synthetic-unknown-send-boundary",
            provider_actual="openai",
            provider_prompt_sha256=hashlib.sha256(
                prepared["q"]["prompt"].encode("utf-8")
            ).hexdigest(),
        )
        receipt = register(config, current, request, process_env=dict(os.environ))
        clock[0] = time.time()  # The real native receipt precedes admission.
        store.persist_subject_dispatch_receipt(
            work_id, lease, attempt["attempt_id"], receipt, expected_request=request
        )
        # Crash boundary: original durable send intent/Q09 reservation, before
        # any HTTP. Recovery must remain conservative and never resend.
        store.mark_send_intent(
            work_id,
            lease,
            attempt["attempt_id"],
            budget_policy=policy,
            budget_route=dict(
                route_id=route["id"],
                provider=route["provider_config_ref"],
                model_requested=route["model"],
                quota_group=route["quota_group"],
            ),
        )
        store.record_attempt_outcome(work_id, lease, attempt["attempt_id"], outcome="unknown")
        assert store.get_item(work_id)["status"] == "uncertain"
        assert store.get_subject_dispatch_receipt(attempt["attempt_id"])["receipt"] == receipt
    attempts = copy.deepcopy(store.list_attempts(work_id))
    item = store.get_item(work_id)
    assert item["status"] == ("leased" if state == "active_lease" else "uncertain")
    assert not (tmp_path / "llm_apis.json").exists()
    bundle = prepared["bundle"]
    output = tmp_path / "deferred.json"
    command = [
        sys.executable,
        "-B",
        "-X",
        "utf8",
        str(OWN / "qa/main_with_llm.py"),
        "--company",
        "Synthetic E2E issuer",
        "--entity-id",
        item["entity_id"],
        "--provider",
        "primary",
        "--require-search",
        "--config",
        str(bundle / "questions.json"),
        "--output",
        str(output),
        "--identity-snapshot",
        str(bundle / "identity.json"),
        "--question-manifest",
        str(bundle / "manifest.json"),
        "--c06-authority",
        str(bundle / "authority.json"),
        "--quick-scan-owner-config",
        str(bundle / "owner-config.json"),
        "--refresh-admission-config",
        str(prepared["deployment_file"]),
        "--refresh-confirmation-id",
        prepared["consent"]["confirmation_id"],
    ]
    completed = subprocess.run(
        command, cwd=tmp_path, env=dict(os.environ), capture_output=True, timeout=180
    )
    (tmp_path / "defer.stdout.log").write_bytes(completed.stdout)
    (tmp_path / "defer.stderr.log").write_bytes(completed.stderr)
    assert completed.returncode == 0, (completed.stdout + completed.stderr).decode(
        "utf-8", errors="replace"
    )
    report = strict_json_loads(output.read_bytes())
    assert report["API_requests"] == 0 and report["model_dispatch_started"] is False
    assert (
        report["requested_scope_completed"] is False and report["whole_manifest_scanned"] is False
    )
    assert report["deferred_work"][0]["work_item_id"] == work_id
    assert report["deferred_work"][0]["status"] == item["status"]
    assert store.list_attempts(work_id) == attempts
    assert store.get_item(work_id)["generation"] == item["generation"]
    assert not (tmp_path / "llm_apis.json").exists()
