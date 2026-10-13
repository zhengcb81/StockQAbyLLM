"""Actual current route / public CLIs / client / checkpoint / owner transaction.

Every company/evidence/HTTP result is synthetic. This is software E2E, never
a real-company golden or an accuracy finding. Only vendor HTTP is replaced.
"""

import copy
import hashlib
import json
import os
import runpy
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

if os.environ.get("IQS_C06_RUN_INTEGRATION") != "1":
    pytest.skip(
        "C06 integration requires an explicit owned three-component runtime",
        allow_module_level=True,
    )

from stockwiki.identity_snapshot import build_identity_snapshot, resolve_listing_binding
from stockwiki.quick_scan_analysis import (
    AnalysisSubjectStore,
    perimeter_sha256,
    receipt_key,
)
from stockwiki.quick_scan_route_validation import IqsRouteValidator
from stockwiki.quick_scan_routes import QuickScanRouteStore
from stockwiki.quick_scan_subject_authority import IqsSubjectContract
from stockwiki.quick_scan_subject_dispatch import SubjectBoundObservationStore

from src.utils import quick_scan_observation_context as context_api
from src.utils import quick_scan_work_transport as transport
from src.utils.quick_scan_c06_authority import load_c06_authority
from src.utils.quick_scan_question_manifest import load_question_manifest
from src.utils.quick_scan_result_outbox import canonical_sha256
from src.utils.quick_scan_subject_owner import register
from src.utils.quick_scan_work_store import QuickScanWorkStore

OWN = Path(os.environ["IQS_C06_OWN"])


def utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def owner_identity_request(identity, entity):
    """Real owner serializer over synthetic identity/receipt/registry stores."""
    from stockwiki.identity_g2b import build_identity_request
    from stockwiki.identity_receipts import IdentityReceiptStore, binding_projection
    from stockwiki.identity_snapshot import listing_v21_for_security
    from stockwiki.market_registry import MarketRegistryStore

    securities = identity.get_entity(entity["entity_id"])["securities"]
    listings = [listing_v21_for_security(s, entity["entity_id"]) for s in securities]
    attrs = {}
    for listing in listings:
        source = binding_projection(identity, listing["source_binding_ref"])
        attrs[listing["listing_id"]] = {
            **{
                k: listing[k]
                for k in (
                    "security_id",
                    "market",
                    "exchange_raw",
                    "exchange_mic",
                    "ticker_raw",
                    "ticker",
                    "currency",
                    "listing_status",
                    "source_binding_ref",
                    "valid_from",
                    "valid_to",
                )
            },
            **{
                k: source[k]
                for k in (
                    "source_namespace",
                    "source_record_id",
                    "source_canonical_name",
                )
            },
        }
    receipts = IdentityReceiptStore(identity.paths)
    receipts.migrate()
    receipts.record_receipt(
        {
            "kind": "verified_issuer",
            "receipt_id": entity["verified_issuer_receipt_id"],
            "entity_id": entity["entity_id"],
            "identity_revision": 1,
            "recorded_at": utc(),
            "same_legal_issuer": True,
            "incorporation_country": entity["incorporation_country"],
            "canonical_name": entity["canonical_name"],
            "security_ids": [s["security_id"] for s in securities],
            "listing_ids": [l["listing_id"] for l in listings],
            "security_attributes": {
                s["security_id"]: {
                    k: s.get(k)
                    for k in (
                        "security_type",
                        "share_class",
                        "adr_ratio",
                        "ordinary_security_ref",
                    )
                }
                for s in securities
            },
            "listing_attributes": attrs,
            "verified_adr_ratios": {},
            "evidence_ref": "https://example.invalid/synthetic-software-identity",
        },
        identity_store=identity,
    )
    registry = MarketRegistryStore(identity.paths)
    registry.import_official_csv(
        (OWN / "sw/tests/fixtures/market_registry/iso10383_sample.csv").read_bytes(),
        source_url="https://example.invalid/synthetic-mic-fixture.csv",
        fetched_at=utc(),
    )
    return build_identity_request(
        identity, receipts, registry, entity_id=entity["entity_id"], as_of=utc()
    )


def invoke(command, tmp_path, label, **env_extra):
    env = dict(os.environ, **env_extra)
    result = subprocess.run(command, capture_output=True, timeout=80, env=env, check=False)
    (tmp_path / (label + ".stdout.log")).write_bytes(result.stdout)
    (tmp_path / (label + ".stderr.log")).write_bytes(result.stderr)
    assert result.returncode == 0, (result.stdout + result.stderr).decode("utf-8", errors="replace")
    return json.loads(result.stdout)


@pytest.fixture
def actual(tmp_path, request):
    observation_version = getattr(request, "param", "1.1.0")
    seed = runpy.run_path(str(OWN / "sw/tests/test_c06_listing_authority.py"))["seeded"]
    identity, entity, listing = seed(tmp_path / "owner")
    initialized = invoke(
        [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            "-m",
            "stockwiki.quick_scan_subject_dispatch_cli",
            "--operation",
            "initialize",
            "--root",
            str(identity.paths.root),
            "--expected-schema-version",
            "0",
        ],
        tmp_path,
        "owner-explicit-first-setup",
    )
    assert initialized["status"] == "initialized" and initialized["schema_version"] == 3
    subjects = AnalysisSubjectStore(identity.paths)
    subjects.migrate()
    subject = {
        "analysis_subject_schema_version": "1.0.0",
        "analysis_subject_id": "ASJ_11111111-1111-4111-8111-111111111111",
        "analysis_subject_revision": 1,
        "display_name": "Synthetic E2E issuer",
        "primary_issuer_id": entity["entity_id"],
        "anchor_listing_id": None,
        "scope_kind": "standalone_issuer",
        "scope_as_of": utc(),
        "perimeter_coverage": "not_applicable",
        "memberships": [
            {
                "entity_id": entity["entity_id"],
                "role": "primary_issuer",
                "valid_from": None,
                "valid_to": None,
                "evidence_ref": "https://example.invalid/synthetic",
            }
        ],
    }
    receipt = {
        "receipt_id": "PRC_SYNTHETIC_E2E",
        "status": "verified",
        "analysis_subject_id": subject["analysis_subject_id"],
        "analysis_subject_revision": 1,
        "primary_issuer_id": entity["entity_id"],
        "evidence_ref": "https://example.invalid/synthetic",
        "perimeter_sha256": perimeter_sha256(subject),
        "decision_ref": "synthetic-software-test-only",
    }
    subjects.import_subject(identity, {"analysis_subject": subject, "perimeter_receipt": receipt})
    release = tmp_path / "release"
    validator = IqsRouteValidator(
        OWN / "iqs",
        runs_dir=identity.paths.runs_dir,
        release_root=release,
        process_env=dict(os.environ),
    )
    routes = QuickScanRouteStore(identity, validator=validator)
    key = receipt_key(subject)
    binding = routes.identity_binding(key)
    now = utc()
    request = {
        "root": str(release),
        "now": now,
        "answer_format": "standard-1",
        "identity": {
            "entity_id": entity["entity_id"],
            "identity_ref": binding["route_identity_ref"],
            "company": "Synthetic E2E issuer",
            "ticker": listing["ticker"],
            "exchange": listing["exchange_mic"],
            "security_id": listing["security_id"],
            "as_of": now[:10],
            "verified_facts": [],
        },
    }
    request_file = tmp_path / "producer-request.json"
    request_file.write_text(json.dumps(request), encoding="utf-8")
    invoke(
        [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            str(
                OWN
                / "iqs/docs/implementation/reviews/W15/module-readiness-2026-10-09/produce_bundle.py"
            ),
            str(request_file),
        ],
        tmp_path,
        "route-producer",
        IQS_ROUTE_TEST_OWNED_ROOT=str(tmp_path),
    )
    raw = (release / "run/route.json").read_bytes()
    manifest_raw = (release / "run/manifest.json").read_bytes()
    decision = json.loads(raw)["decision_id"]
    routes.record_snapshot(raw, manifest_raw, subject_key=key, expected_decision_id=decision)
    routes.set_current(decision, subject_key=key, expected_current_decision_id=None, now_utc=utc())
    current = routes.get_for_execution(
        subject_key=key, scope="entity", scope_id=entity["entity_id"], now_utc=utc()
    )
    manifest = load_question_manifest(release / "run/manifest.json")
    identity_file = tmp_path / "identity.json"
    identity_file.write_text(json.dumps(owner_identity_request(identity, entity)), encoding="utf-8")
    material = resolve_listing_binding(
        identity,
        entity_id=entity["entity_id"],
        security_id=listing["security_id"],
        listing_id=listing["listing_id"],
        identity_revision=1,
        as_of=utc(),
    )
    inputs = {
        "run": {
            "run_id": "RUN_E2E",
            "scan_id": "SCAN_E2E",
            "inputset_id": "INPUT_SYNTHETIC",
            "task_mode": "primary",
            "comparison_group_id": None,
        },
        "versions": {
            "identity_schema": "2.1.0",
            "answer_schema": "1.0.0",
            "observation_schema": observation_version,
            "question_catalog": manifest["template_version"],
            "model_policy_schema": "2.0.0",
        },
        "producer": {"component_version": "synthetic-e2e", "build_id": "synthetic-e2e"},
        "subject-binding": binding,
        "listing-bindings": {listing["security_id"]: material},
    }
    command = [
        sys.executable,
        "-B",
        "-X",
        "utf8",
        str(OWN / "iqs/scripts/c06_authority.py"),
        "--manifest",
        str(release / "run/manifest.json"),
        "--identity",
        str(identity_file),
        "--release-root",
        str(release),
        "--output",
        str(tmp_path / "authority.json"),
        "--observation-schema-version",
        observation_version,
    ]
    for name, value in inputs.items():
        path = tmp_path / (name + ".json")
        path.write_text(json.dumps(value), encoding="utf-8")
        command += ["--" + name, str(path)]
    invoke(command, tmp_path, "authority-public-export")
    authority = load_c06_authority(
        tmp_path / "authority.json",
        manifest=manifest,
        identity_snapshot_sha256=hashlib.sha256(identity_file.read_bytes()).hexdigest(),
    )
    config = {
        "stockwiki_code_root": str(OWN / "sw"),
        "stockwiki_workspace_root": str(identity.paths.root),
        "iqs_code_root": str(OWN / "iqs"),
        "iqs_release_root": str(release),
        "runs_dir": str(tmp_path / "qa-os-runs"),
        "subject_key": key,
        "scope": "entity",
        "scope_id": entity["entity_id"],
    }
    return routes, current, config, authority, manifest, identity_file


@pytest.mark.parametrize("scope", ["entity", "security"])
def test_actual_public_authority_owner_registration_client_checkpoint_and_import(
    actual, tmp_path, monkeypatch, scope
):
    routes, current, config, authority, manifest, identity_file = actual
    assert authority["schema_version"] == "3.0.0"
    context = authority["observation_context"]
    vendor_profile = authority["contract_versions"]["observation_schema"] == "1.2.0"
    provider = "mimo" if vendor_profile else "openai"
    model = "mimo-v2.6-pro" if vendor_profile else "fixture-model"
    q = next(q for q in manifest["questions"] if q["scope"] == scope)
    row = context["questions"][q["id"]]
    ref = row["subject_binding"]["subject_ref"]
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    item = store.create_or_attach(
        entity_id=ref["entity_id"],
        question_id=q["id"],
        generation=1,
        scope=scope,
        scope_id=ref["scope_id"],
        identity_revision=1,
        identity_state="verified",
        source_binding_version=1,
        source_binding_ref="BND_fixture_1",
        source_binding_refs=["BND_fixture_1"],
        identity_snapshot_sha256=hashlib.sha256(identity_file.read_bytes()).hexdigest(),
        question_fingerprint=row["work_prompt_sha256"],
        routing_fingerprint="e" * 64,
        run_id="RUN_E2E",
        scan_id="SCAN_E2E",
    )
    lease = store.claim(item["work_item_id"], lease_seconds=300)
    store.freeze_observation_context_before_send(item["work_item_id"], lease, context)
    from src.providers import llm_client
    from tests.unit.test_quick_scan_work_transport import _http_response

    response = _http_response(200)
    body = runpy.run_path(str(OWN / "qa/tests/unit/test_quick_scan_observation_context.py"))[
        "content"
    ]()
    body["question_id"] = q["id"]
    body["information_as_of"] = utc()[:10]
    body["summary"] = "Synthetic E2E supported body, no financial conclusion."
    body["evidence"][0]["url"] = "https://example.org/source"
    response.json.return_value["output"][1]["content"][0]["text"] = json.dumps(body)
    if vendor_profile:
        response.headers = {}
        response.json.return_value = {
            "id": "chatcmpl-SYNTHETIC-vendor-public-e2e",
            "model": model,
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(body),
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url": "https://example.org/source",
                                "title": "Synthetic citation",
                            }
                        ],
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 80,
                "total_tokens": 180,
                "web_search_usage": {"tool_usage": 1},
            },
        }
    owner = SubjectBoundObservationStore(
        routes.identity.paths,
        contract=IqsSubjectContract(
            OWN / "iqs",
            runs_dir=tmp_path / "contract-runs",
            release_root=config["iqs_release_root"],
            process_env=dict(os.environ),
        ),
    )
    session = Mock()

    def post(*args, **kwargs):
        attempt = store.list_attempts(item["work_item_id"])[0]
        pre = store.get_subject_dispatch_receipt(attempt["attempt_id"])["receipt"]
        assert owner.dispatch_count() == 1
        with closing(sqlite3.connect(owner.database_path)) as con:
            persisted = con.execute(
                "SELECT receipt_json FROM quick_scan_subject_dispatch WHERE receipt_id=?",
                (pre["receipt_id"],),
            ).fetchone()
        assert persisted is not None and json.loads(persisted[0]) == pre
        return response

    session.post = Mock(side_effect=post)
    monkeypatch.setattr(llm_client.http_client_manager, "get_sync_session", lambda: session)
    client = llm_client.LLMClient(
        api_key="synthetic-only",
        model=model,
        base_url=(
            "https://api.xiaomimimo.com/v1"
            if vendor_profile
            else "https://api.openai.com/v1/responses"
        ),
        provider_name=provider,
    )
    with transport.bind_quick_scan_work(
        store, item["work_item_id"], lease
    ), transport.bind_quick_scan_route(
        route_id="primary", provider="configured-provider-ref", model_requested=model
    ), transport.bind_subject_dispatch_registrar(
        lambda request: register(config, current, request, process_env=dict(os.environ))
    ):
        result = client.send_search_request(q["prompt"], "Synthetic system prompt")
    assert session.post.call_count == 1
    if vendor_profile:
        posted = session.post.call_args.kwargs["json"]
        assert result.execution_metadata["request_payload_sha256"] == canonical_sha256(posted)
    attempt = store.list_attempts(item["work_item_id"])[0]
    saved = store.save_answer_checkpoint(
        item["work_item_id"],
        lease,
        attempt["attempt_id"],
        answer={
            "entity_id": ref["entity_id"],
            "question_id": q["id"],
            "status": "scored",
            "score": 8,
            "description": body["summary"],
        },
        execution_receipt=result.execution_metadata,
        observation_context=context,
        standard_answer=body,
    )
    if vendor_profile:
        pre_request = store.get_subject_dispatch_receipt(attempt["attempt_id"])["receipt"][
            "dispatch_request"
        ]
        assert pre_request["protocol"] == "stockwiki.subject_dispatch_request/1.2.0"
        assert pre_request["execution_profile"]["profile_id"] == "vendor-http-identity-v1"
    from src.utils.quick_scan_delivery_seal import seal_result_delivery

    sealed = seal_result_delivery(store, item["work_item_id"], authority=authority)
    assert sealed["action"] == "sealed", sealed
    delivery = store.get_result_delivery(item["work_item_id"])
    package = delivery["package"]
    original = package["items"][0]["observation"]
    assert original["schema_version"] == ("1.2.0" if vendor_profile else "1.1.0")
    if vendor_profile:
        assert original["execution"]["request_id"] is None
        assert original["execution"]["response_id"] == "chatcmpl-SYNTHETIC-vendor-public-e2e"
    original_hash = canonical_sha256(original)
    from stockwiki.quick_scan_import import import_package, verify_package_integrity

    verify_package_integrity(package)
    frozen_release = owner.contract.release_projection(
        json.loads(current["snapshot"]["manifest_raw"])
    )
    if vendor_profile:
        from stockwiki.paths import WorkspacePaths
        from stockwiki.quick_scan_observations import ObservationImportError

        unregistered = SubjectBoundObservationStore(
            WorkspacePaths.from_root(tmp_path / "no-pre-owner"), contract=owner.contract
        )
        unregistered.migrate()
        before_empty = unregistered.database_path.read_bytes()
        with pytest.raises(ObservationImportError, match="subject_dispatch_preregister_required"):
            import_package(
                unregistered,
                package,
                frozen_release=frozen_release,
                identity_store=routes.identity,
            )
        assert unregistered.database_path.read_bytes() == before_empty
        assert unregistered.counts()["observations"] == unregistered.dispatch_count() == 0
        with closing(sqlite3.connect(unregistered.database_path)) as con:
            for table in ("quick_scan_import_item", "quick_scan_subject_binding"):
                assert con.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 0
    imported = import_package(
        owner, package, frozen_release=frozen_release, identity_store=routes.identity
    )
    assert imported["summary"] == {"accepted": 1}, imported
    if vendor_profile:
        from src.utils.quick_scan_execution_inspect import inspect_executions

        proof_request = {
            "protocol": "stockqa.execution_inspection_request/1.0.0",
            "owner_store_id": owner.store_id,
            "observations": [
                {
                    "work_item_id": item["work_item_id"],
                    "observation_id": original["observation_id"],
                    "payload_sha256": original_hash,
                    "subject_ref": ref,
                }
            ],
        }
        before_work = store.path.read_bytes()
        verified = inspect_executions(store.path, proof_request)
        assert (
            verified["observations"][original["observation_id"]]["execution"]
            == original["execution"]
        )
        assert verified["financial_accuracy_proven"] is False
        assert store.path.read_bytes() == before_work
        for key, value in (
            ("payload_sha256", "f" * 64),
            ("work_item_id", "WORK_SYNTHETIC_wrong"),
        ):
            wrong = copy.deepcopy(proof_request)
            wrong["observations"][0][key] = value
            with pytest.raises((ValueError, KeyError)):
                inspect_executions(store.path, wrong)
            assert store.path.read_bytes() == before_work
    binding = owner.binding_for_observation(original["observation_id"])
    assert binding["binding"]["subject_ref"] == ref
    assert binding["binding"]["payload_sha256"] == original_hash
    assert delivery["package_sha256"] == package["package_sha256"]
    assert original["execution"]["attempt_id"] == result.execution_metadata["attempt_id"]
    assert "analysis_subject" not in original
    assert list(Path(config["runs_dir"]).iterdir()) == []
    import query_contract as qc

    if vendor_profile:
        import query_vendor_contract as qc
    import subject_dispatch_contract as dc
    from stockwiki.quick_scan_query_v2 import QueryReader

    pre = store.get_subject_dispatch_receipt(attempt["attempt_id"])["receipt"]
    request = dc.query_request(pre["dispatch_request"], utc())
    if vendor_profile:
        from stockwiki.quick_scan_query_contract import IqsQueryContract

        fixed_contract = IqsQueryContract(
            OWN / "iqs",
            runs_dir=tmp_path / "query-contract-runs",
            process_env=dict(os.environ),
        )
        reader = QueryReader(
            routes.identity.paths,
            contract=fixed_contract,
            subject_contract=fixed_contract.subject_contract,
        )
    else:
        reader = QueryReader(routes.identity.paths, contract=qc, subject_contract=dc)
    response = reader.get_profiles(request)
    qc.validate_response(response, expected_request=request, expected_owner=reader.owner_context)
    assert (
        response["result"]["profiles"][0]["observations"][0]["observation_id"]
        == original["observation_id"]
    )
    # Public owner process does not import IQS Python. Its independently
    # captured references cross only the fixed offline contract CLI.
    query_input = tmp_path / "public-query.json"
    query_input.write_text(json.dumps(request), encoding="utf-8")
    public = invoke(
        [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            "-m",
            "stockwiki.quick_scan_query_v2_cli",
            "--root",
            config["stockwiki_workspace_root"],
            "--iqs-code-root",
            str(OWN / "iqs"),
            "--request",
            str(query_input),
        ],
        tmp_path,
        "public-bound-query",
        PYTHONPATH=os.pathsep.join(str(OWN / path) for path in ("guard", "sw")),
    )
    public_response = public
    public_row = public_response["result"]["profiles"][0]["observations"][0]
    assert public_row["observation_id"] == original["observation_id"]
    assert public_row["payload_sha256"] == original_hash
    request = copy.deepcopy(request)
    request.update(request_id="query-durable-replay", requested_at=utc())
    request["payload"]["snapshot_id"] = public_response["result"]["watermark"]["snapshot_id"]
    query_input.write_text(json.dumps(request), encoding="utf-8")
    replay = invoke(
        [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            "-m",
            "stockwiki.quick_scan_query_v2_cli",
            "--root",
            config["stockwiki_workspace_root"],
            "--iqs-code-root",
            str(OWN / "iqs"),
            "--request",
            str(query_input),
        ],
        tmp_path,
        "public-bound-replay",
        PYTHONPATH=os.pathsep.join(str(OWN / path) for path in ("guard", "sw")),
    )
    assert replay["result"] == public_response["result"]
    if vendor_profile:
        fresh = copy.deepcopy(request)
        fresh["payload"]["snapshot_id"] = None
        query_input.write_text(json.dumps(fresh), encoding="utf-8")
        before_owner = owner.database_path.read_bytes()
        assessment = invoke(
            [
                sys.executable,
                "-B",
                "-X",
                "utf8",
                "-m",
                "stockwiki.quick_scan_assessment_cli",
                "--root",
                config["stockwiki_workspace_root"],
                "--iqs-code-root",
                str(OWN / "iqs"),
                "--request",
                str(query_input),
            ],
            tmp_path,
            "public-vendor-field-assessment",
            PYTHONPATH=os.pathsep.join(str(OWN / path) for path in ("guard", "sw")),
        )
        assert (
            assessment["assessment"]["observations"][original["observation_id"]]["check_level"]
            == "unverified_model_output"
        )
        assert assessment["financial_accuracy_proven"] is False
        assert owner.database_path.read_bytes() == before_owner
    # Current owner refresh must use the retained PRE-dispatch sidecar, rather
    # than demanding subject fields forbidden by the original answer schema.
    refresh = invoke(
        [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            "-m",
            "stockwiki.quick_scan_routes_cli",
            "--root",
            config["stockwiki_workspace_root"],
            "quick-scan-route-refresh",
            "--iqs-code-root",
            str(OWN / "iqs"),
            "--iqs-release-root",
            config["iqs_release_root"],
            "--subject-key",
            config["subject_key"],
            "--scope",
            "entity",
            "--scope-id",
            ref["entity_id"],
            "--now-utc",
            utc(),
            "--provider",
            provider,
            "--model",
            model,
            "--ttl-hours",
            "48",
            "--c06-subject-bound",
        ],
        tmp_path,
        "public-c06-refresh",
        PYTHONPATH=os.pathsep.join(str(OWN / path) for path in ("guard", "sw")),
    )
    plan = refresh["payload"]
    action = next(f for f in plan["fields"] if f["field_id"] == q["id"])
    assert action["decision"] == "reuse", action
    assert action["reuse_observation_id"] == original["observation_id"]
    descriptor = plan["reused_observations"][original["observation_id"]]
    assert descriptor["payload"] == original and descriptor["payload_sha256"] == original_hash
    assert descriptor["reuse_subject_binding"]["binding"] == binding["binding"]
    assert descriptor["reuse_subject_binding"]["dispatch_receipt"] == pre
    assert descriptor["reuse_subject_binding"]["watermark"]["snapshot_id"].startswith("snap2_")
    assert owner.dispatch_count() == 1 and session.post.call_count == 1
    # The real QA consumer prepares through the owner CLI, then returns the
    # exact historical reference without creating work or calling the model.
    from src.core.models import Question
    from src.runners.llm_runner import load_identity_snapshot
    from src.utils.quick_scan_owner_refresh import (
        OwnerRefreshClient,
        OwnerRefreshRejected,
    )

    config_file = tmp_path / "owner-config.json"
    config_file.write_text(
        json.dumps(dict(config, protocol="stockqa.owner_refresh_config/1.0.0", ttl_hours=48)),
        encoding="utf-8",
    )
    clean_store = QuickScanWorkStore(tmp_path / "reuse-work.sqlite")
    prepared = OwnerRefreshClient(config_file, process_env=dict(os.environ)).prepare(
        manifest,
        clean_store,
        identity=load_identity_snapshot(identity_file, expected_entity_id=ref["entity_id"]),
        provider=provider,
        model=model,
        now=utc(),
        observation_context=context,
    )
    question = Question(q["prompt"], question_id=q["id"])
    reused = prepared.reference(question)
    assert reused.answer.source == "stockwiki_observation_reference"
    assert reused.answer.metadata["owner_observation_reference"]["payload"] == original
    assert reused.metadata["owner_observation_reference"]["new_work_created"] is False
    assert owner.dispatch_count() == 1 and session.post.call_count == 1
    untouched = copy.deepcopy(prepared.plan)
    for key, value in [
        ("identity_revision", 2),
        ("execution_attempt_id", "foreign-attempt"),
    ]:
        descriptor = prepared.plan["reused_observations"][original["observation_id"]]
        if key == "identity_revision":
            descriptor["reuse_subject_binding"]["dispatch_receipt"]["dispatch_request"][key] = value
        else:
            descriptor["reuse_subject_binding"]["binding"][key] = value
        with pytest.raises(OwnerRefreshRejected):
            prepared.reference(question)
        prepared.plan = copy.deepcopy(untouched)
    from datetime import timedelta

    from stockwiki.quick_scan_freshness import freshness_status, parse_utc
    from stockwiki.quick_scan_module_refresh import _matching_meta
    from stockwiki.quick_scan_route_validation import read_json

    args = dict(
        question=q,
        current=current["snapshot"],
        binding=current["snapshot"]["metadata"]["binding"],
        manifests=[
            (read_json(s["manifest_raw"]), s)
            for s in routes.snapshots_for_subject(config["subject_key"])
        ],
        provider=provider,
        model=model,
        ttl_hours=48,
    )
    meta = _matching_meta(descriptor, **args)
    assert meta["check_level"] == "unverified_model_output"  # Not a financial audit.
    deadline = parse_utc(meta["valid_until"])
    assert (
        freshness_status(
            meta,
            (deadline - timedelta(microseconds=1)).isoformat().replace("+00:00", "Z"),
        )
        == "fresh"
    )
    assert freshness_status(meta, meta["valid_until"]) == "stale"
    if vendor_profile:
        untouched_plan = copy.deepcopy(prepared.plan)
        for proof_field in (
            "execution_response_id",
            "execution_request_payload_sha256",
            "execution_response_sha256",
            "execution_receipt_sha256",
        ):
            prepared.plan = copy.deepcopy(untouched_plan)
            entry = prepared.plan["reused_observations"][original["observation_id"]]
            entry["reuse_subject_binding"]["binding"][proof_field] = "SYNTHETIC_FORGED"
            with pytest.raises(OwnerRefreshRejected):
                prepared.reference(question)
        prepared.plan = untouched_plan
    for changes in ({"model": "different-model"}, {"provider": "minimax"}):
        assert _matching_meta(descriptor, **dict(args, **changes)) is None
    assert descriptor["payload"] == original and canonical_sha256(original) == original_hash
    # Failed current source reads may not masquerade as empty coverage and
    # authorize a duplicate dispatch. Restore exact pre-test bytes afterwards.
    database = owner.database_path.resolve()
    assert database.is_relative_to(tmp_path.resolve())
    before = database.read_bytes()
    command = [
        sys.executable,
        "-B",
        "-X",
        "utf8",
        "-m",
        "stockwiki.quick_scan_routes_cli",
        "--root",
        config["stockwiki_workspace_root"],
        "quick-scan-route-refresh",
        "--iqs-code-root",
        str(OWN / "iqs"),
        "--iqs-release-root",
        config["iqs_release_root"],
        "--subject-key",
        config["subject_key"],
        "--scope",
        "entity",
        "--scope-id",
        ref["entity_id"],
        "--now-utc",
        utc(),
        "--provider",
        provider,
        "--model",
        model,
        "--ttl-hours",
        "48",
        "--c06-subject-bound",
    ]
    try:
        with closing(sqlite3.connect(database)) as con, con:
            con.execute("PRAGMA user_version=99")
        rejected = subprocess.run(
            command,
            capture_output=True,
            timeout=45,
            env=dict(
                os.environ,
                PYTHONPATH=os.pathsep.join(str(OWN / path) for path in ("guard", "sw")),
            ),
        )
        assert rejected.returncode == 2
        assert json.loads(rejected.stdout)["error_code"] == "refresh_subject_capture_unavailable"
    finally:
        database.write_bytes(before)
    assert database.read_bytes() == before
    away = database.with_name(database.name + ".owned-absent")
    assert not away.exists()
    database.rename(away)
    try:
        rejected = subprocess.run(
            command,
            capture_output=True,
            timeout=45,
            env=dict(
                os.environ,
                PYTHONPATH=os.pathsep.join(str(OWN / path) for path in ("guard", "sw")),
            ),
        )
        assert rejected.returncode == 2
        assert (
            json.loads(rejected.stdout)["error_code"]
            == "refresh_subject_observation_store_uninitialized"
        )
        assert not database.exists()
    finally:
        away.rename(database)
    assert database.read_bytes() == before and owner.dispatch_count() == 1
