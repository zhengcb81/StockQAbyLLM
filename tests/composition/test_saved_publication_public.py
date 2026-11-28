"""Actual original CLI software, copied source data and synthetic fresh permission.

No claim of real owner consent; source namespace and all actual ledgers readonly.
"""

import copy
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("IQS_SAVED_PUBLICATION_ROOT") is None,
    reason="owned original-software fixture only",
)


def test_public_native_publication_and_stockwiki_import(tmp_path):
    from src.utils.quick_scan_refresh_consent import OwnerConsentEndpoint
    from src.utils.quick_scan_result_outbox import canonical_sha256
    from src.utils.quick_scan_saved_research_publication import derive_publication
    from src.utils.quick_scan_work_decision_authority import (
        StockWikiWorkDecisionAuthority,
    )
    from src.utils.quick_scan_work_store import QuickScanWorkStore

    root = Path(os.environ["IQS_SAVED_PUBLICATION_ROOT"])
    projects = root.parents[3]
    actual = root.parent / "actual-native-catl-profile-01"
    admission = json.loads((actual / "admission-config.json").read_bytes())
    original = Path(admission["stockwiki_workspace_root"])
    workspace = tmp_path / "owner-workspace"
    sources = []
    for rel in [
        "data/quick_scan/analysis_subjects.sqlite",
        "data/quick_scan/identity_receipts.sqlite",
        "data/quick_scan/market_registry.sqlite",
        "data/quick_scan/scan_observations.sqlite",
        "data/quick_scan/scan.sqlite",
        "runs/c06-query-captures/captures.sqlite3",
    ]:
        source = original / rel
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        with closing(
            sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
        ) as src, closing(sqlite3.connect(target)) as dst:
            src.execute("PRAGMA query_only=ON")
            src.backup(dst)
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before
        sources.append((source, before))
    native = tmp_path / "native.sqlite"
    shutil.copyfile(root / "fixtures/actual102-uncertain.sqlite", native)
    store = QuickScanWorkStore(native)
    config = json.loads((original / "config/quick_scan_refresh.json").read_bytes())
    config["qa"]["code_root"] = str(root / "qa")
    config["qa"]["work_store"] = str(native)
    (workspace / "config").mkdir()

    def write(name, value):
        path = tmp_path / name
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf8")
        return path

    (workspace / "config/quick_scan_refresh.json").write_text(
        json.dumps(config, ensure_ascii=False), "utf8"
    )
    bundle_path = actual / "cold-session/task-bundle.json"
    bundle = bundle_path.read_bytes()
    source_config = json.loads((actual / "run-config.json").read_bytes())
    sdk = "4d46a825-728f-4372-8947-26ec3737ff3f"
    key = "synthetic-publication-native-01"
    request = derive_publication(store, bundle, sdk_id=sdk, idempotency_key=key)["request"]
    cap = dict(
        protocol="stockwiki.saved_research_publication_capability/1.0.0",
        purpose="research_no_verified_search",
        enabled=True,
        owner_principal=config["owner_principal"],
        consumer_id=admission["consumer_id"],
        action_id=key,
        task_bundle=dict(path=str(bundle_path), sha256=hashlib.sha256(bundle).hexdigest()),
        native_sdk_id=sdk,
        expected_request_sha256=canonical_sha256(request),
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=2))
        .isoformat()
        .replace("+00:00", "Z"),
        confirmation_method="native_owner_delegated_agent",
        c06_authority=dict(
            path=str(Path(source_config["bundle"]) / "authority.json"),
            sha256=request["publication"]["c06_authority_sha256"],
        ),
        deployment=dict(
            company_agent_code_root=str(projects / "iqs-company-agent-lab"),
            runtime_code_root=str(projects / "iqs-agent-runtime-lab"),
        ),
    )
    cap_path = workspace / "config/quick_scan_saved_research_publication.json"
    cap_path.write_text(json.dumps(cap, ensure_ascii=False), "utf8")
    outer = dict(
        protocol="stockwiki.work_decision_request/1.1.0",
        consumer_id=admission["consumer_id"],
        decision_request=request,
        permission_request=json.loads((actual / "refresh-request.json").read_bytes()),
    )
    outer_path = write("publication-request.json", outer)
    env = {
        k: os.environ[k]
        for k in ["SYSTEMROOT", "SystemRoot", "WINDIR", "PATH", "PATHEXT"]
        if k in os.environ
    }
    # The actual child starts without parent Company/Runtime PYTHONPATH.
    env.update(
        PYTHONPATH=os.pathsep.join([str(root / "guard"), str(root / "qa"), str(root / "sw")]),
        PYTHONUTF8="1",
        PYTHONDONTWRITEBYTECODE="1",
        IQS_C06_RUN_INTEGRATION="1",
        IQS_C06_OWN=str(root),
        IQS_CONTINUATION_OWN=str(root),
        TEMP=str(root / "temp"),
        TMP=str(root / "temp"),
    )

    def invoke(args, label, timeout=240):
        done = subprocess.run(
            [sys.executable, "-B", *args],
            env=env,
            cwd=root,
            capture_output=True,
            timeout=timeout,
        )
        (tmp_path / (label + ".stdout.json")).write_bytes(done.stdout)
        (tmp_path / (label + ".stderr.log")).write_bytes(done.stderr)
        assert done.returncode == 0, (
            label,
            done.returncode,
            done.stdout[-1000:].decode("utf8", errors="replace"),
            done.stderr[-1000:].decode("utf8", errors="replace"),
        )
        return json.loads(done.stdout)

    common = [
        "-m",
        "stockwiki.quick_scan_refresh_owner_cli",
        "--root",
        str(workspace),
        "--iqs-code-root",
        admission["iqs_code_root"],
        "--iqs-release-root",
        admission["iqs_release_root"],
    ]
    preview = invoke([*common, "--work-decision-request", str(outer_path)], "preview")
    confirmation = invoke(
        [
            *common,
            "--confirm-work-decision",
            preview["preview_id"],
            "--idempotency-key",
            key,
        ],
        "confirm",
    )
    inspected = invoke(
        [
            *common,
            "--inspect-work-decision",
            confirmation["confirmation_id"],
            "--admission-inspection",
            "current_permissions",
        ],
        "inspect",
    )
    assert (
        inspected["request"] == outer
        and inspected["confirmation"]["method"] == "native_owner_delegated_agent"
    )
    applied = invoke(
        [
            "-m",
            "src.utils.quick_scan_work_decision_apply_cli",
            "--ledger",
            str(native),
            "--policy",
            admission["budget_policy_file"],
            "--owner-code-root",
            str(root / "sw"),
            "--owner-workspace",
            str(workspace),
            "--iqs-code-root",
            admission["iqs_code_root"],
            "--iqs-release-root",
            admission["iqs_release_root"],
            "--consumer-id",
            admission["consumer_id"],
            "--confirmation-id",
            confirmation["confirmation_id"],
            "--refresh-schema-version",
            "1.5.0",
        ],
        "apply",
        timeout=600,
    )
    assert applied["decision"]["formal_search_checkpoint_created"] is False
    endpoint = OwnerConsentEndpoint(
        dict(
            stockwiki_code_root=str(root / "sw"),
            stockwiki_workspace_root=str(workspace),
            iqs_code_root=admission["iqs_code_root"],
            iqs_release_root=admission["iqs_release_root"],
        ),
        process_env=env,
    )
    publisher = StockWikiWorkDecisionAuthority(
        endpoint,
        work_store=native,
        budget_policy_file=admission["budget_policy_file"],
        consumer_id=admission["consumer_id"],
        confirmation_id=confirmation["confirmation_id"],
        refresh_schema_version="1.5.0",
    )
    ids = applied["decision"]["work_item_ids"]
    # Native source module decides delivery bytes; no response/body supplied by caller.
    for index, wid in enumerate(ids):
        outgoing = store.begin_result_delivery(wid, publisher_authority=publisher)
        package = tmp_path / ("package-" + str(index) + ".json")
        package.write_bytes(outgoing["request_bytes"])
        # Release projection was frozen by original owner software before dispatch.
        from stockwiki.quick_scan_subject_authority import IqsSubjectContract

        from src.utils.quick_scan_question_manifest import load_question_manifest

        # Use exact original source authority context as published module definition source.
        release_file = tmp_path / "release.json"
        if not release_file.exists():
            contract = IqsSubjectContract(
                Path(admission["iqs_code_root"]),
                runs_dir=workspace / "runs/publication-release",
                release_root=Path(admission["iqs_release_root"]),
            )
            projection = contract.release_projection(
                load_question_manifest(Path(source_config["bundle"]) / "manifest.json")
            )
            release_file.write_text(json.dumps(projection, ensure_ascii=False), "utf8")
        imported = invoke(
            [
                "-m",
                "stockwiki.cli",
                "--root",
                str(workspace),
                "quick-scan",
                "observation-import",
                "--package",
                str(package),
                "--release",
                str(release_file),
                "--iqs-code-root",
                admission["iqs_code_root"],
                "--iqs-release-root",
                admission["iqs_release_root"],
            ],
            "import-" + str(index),
        )
        assert len(imported["acks"]) == 1 and imported["acks"][0]["status"] in {
            "accepted",
            "already_present",
        }
        store.apply_result_delivery_ack(wid, imported["acks"][0])
        assert store.get_item(wid)["status"] == "delivered"
        replay = invoke(
            [
                "-m",
                "stockwiki.cli",
                "--root",
                str(workspace),
                "quick-scan",
                "observation-import",
                "--package",
                str(package),
                "--release",
                str(release_file),
                "--iqs-code-root",
                admission["iqs_code_root"],
                "--iqs-release-root",
                admission["iqs_release_root"],
            ],
            "replay-" + str(index),
        )
        assert replay["acks"] == imported["acks"]
    for source, digest in sources:
        assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    with closing(store._connect()) as con:
        assert con.execute("SELECT COUNT(*) FROM quick_scan_budget_attempt").fetchone()[0] == 102
        assert (
            con.execute(
                "SELECT COUNT(*) FROM answer_checkpoint WHERE checkpoint_schema_version=3"
            ).fetchone()[0]
            == 2
        )
