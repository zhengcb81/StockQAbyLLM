"""Public main consumes ONE native-approved question from FULL release.

Real software/OS/native consent records; synthetic issuer/facts/approval only.
Only vendor HTTP is substituted. Not an accuracy or real-human-consent gold.
"""

import copy
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

if os.environ.get("IQS_C06_RUN_INTEGRATION") != "1":
    pytest.skip("requires explicitly owned three-component runtime", allow_module_level=True)

from stockwiki.quick_scan_import import import_package
from stockwiki.quick_scan_subject_authority import IqsSubjectContract
from stockwiki.quick_scan_subject_dispatch import SubjectBoundObservationStore

from src.config.llm_config import LLMConfig
from src.utils.quick_scan_refresh_admission import RefreshAdmissionService
from tests.integration.test_c06_refresh_admission import setup_admission
from tests.integration.test_c06_subject_public_e2e import OWN, actual, utc
from tests.unit.test_llm_config import _quick_scan_policy
from tests.unit.test_quick_scan_work_transport import _http_response


@pytest.mark.parametrize("ledger_layout", ["default", "registered_explicit"])
def test_public_main_only_dispatches_registered_scope_and_recovers_without_credentials(
    actual, tmp_path, monkeypatch, capsys, ledger_layout
):
    import main_with_llm
    from src.providers import llm_client

    routes, current, config, authority, manifest, identity_file = actual
    policy = _quick_scan_policy(order=("primary",))
    policy["models"][0]["model"] = "fixture-model"
    # Two available requests make the one-question scope assertion meaningful:
    # accidentally iterating the full release cannot hide behind a one-call cap.
    policy["budget"].update(max_cost=2, max_requests=2, max_cost_per_attempt=1)
    llm_file = tmp_path / "llm_apis.json"
    llm_file.write_text(
        json.dumps(
            dict(
                default_provider="primary",
                providers=dict(
                    primary=dict(
                        enabled=True,
                        api_key="synthetic-only",
                        model="fixture-model",
                        base_url="https://api.openai.com/v1/responses",
                        max_retries=1,
                        format_repair_budget=0,
                    )
                ),
                quick_scan_model_policy=policy,
            )
        ),
        encoding="utf-8",
    )
    normalized = LLMConfig(str(llm_file)).get_quick_scan_model_policy(default_provider="primary")
    ledger_path = (
        tmp_path / "quick_scan_work.sqlite"
        if ledger_layout == "default"
        else tmp_path / "approved-workspace" / "work.sqlite"
    )
    prepared = setup_admission(actual, tmp_path, policy=normalized, ledger_path=ledger_path)
    accepted = RefreshAdmissionService(
        prepared["deployment_file"], process_env=dict(os.environ)
    ).admit(prepared["consent"]["confirmation_id"])
    assert accepted["status"] == "accepted"
    store = prepared["ledger"]
    work_id = accepted["registration"]["items"][0]["work_item_id"]
    qid = prepared["q"]["id"]
    owner = SubjectBoundObservationStore(
        routes.identity.paths,
        contract=IqsSubjectContract(
            OWN / "iqs",
            release_root=Path(config["iqs_release_root"]),
            runs_dir=tmp_path / "contract-exec-runs",
            process_env=dict(os.environ),
        ),
    )
    session = Mock()
    sent = []

    def post(*args, **kwargs):
        actual_qid = re.search(
            r"Target question_id: ([A-Za-z0-9_.]+)\.", kwargs["json"]["input"]
        ).group(1)
        sent.append(actual_qid)
        assert sent == [qid], "unapproved or repeated model request"
        assert owner.dispatch_count() == 1
        body = copy.deepcopy(
            __import__(
                "tests.unit.test_quick_scan_observation_context", fromlist=["content"]
            ).content()
        )
        body.update(question_id=qid, information_as_of=utc()[:10])
        body["evidence"][0]["url"] = "https://example.org/source"
        outer = dict(
            entity_id=current["snapshot"]["entity_id"],
            company_name="Synthetic E2E issuer",
            question_id=qid,
            status="scored",
            score=8,
            description=json.dumps(body),
            information_as_of=utc()[:10],
        )
        response = _http_response(200)
        response.json.return_value["output"][1]["content"][0]["text"] = json.dumps(outer)
        return response

    session.post.side_effect = post
    monkeypatch.setattr(llm_client.http_client_manager, "get_sync_session", lambda: session)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()
    spend = tmp_path / "spend.json"
    spend.write_text(
        json.dumps(
            dict(
                schema_version="1.0.0",
                currency="USD",
                hard_cap=2,
                pricing_snapshot_ref="synthetic-cap",
                authorized_at=utc(),
            )
        ),
        encoding="utf-8",
    )
    bundle = prepared["bundle"]
    output = tmp_path / "result.json"
    argv = [
        "main_with_llm.py",
        "--company",
        "Synthetic E2E issuer",
        "--entity-id",
        current["snapshot"]["entity_id"],
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
    monkeypatch.setattr(sys, "argv", argv + ["--spend-authorization", str(spend)])
    assert main_with_llm.main() == 0, capsys.readouterr().out
    assert sent == [qid] and store.get_item(work_id)["status"] == "result_ready"
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM work_item").fetchone()[0] == 1
        assert (
            connection.execute("SELECT COUNT(*) FROM quick_scan_budget_attempt").fetchone()[0] == 1
        )
    report = json.loads(output.with_suffix(".json.scope.json").read_bytes())
    assert report["requested_question_ids"] == [qid]
    assert report["requested_scope_answered"] is True
    assert report["manifest_question_count"] == len(manifest["questions"]) > 1
    assert report["whole_manifest_answered"] is False
    package_before = copy.deepcopy(store.get_result_delivery(work_id))
    llm_file.unlink()  # Original pending delivery requires no model credentials.
    spend.unlink()
    monkeypatch.setattr(sys, "argv", argv)
    assert main_with_llm.main() == 0, capsys.readouterr().out
    recovered = json.loads(output.read_bytes())
    assert recovered["mode"] == "original_references_and_recovery"
    assert recovered["model_dispatch_started"] is False and recovered["API_requests"] == 0
    assert recovered["requested_scope_completed"] is False
    assert recovered["delivery_references"][0]["work_item_id"] == work_id
    assert recovered["delivery_references"][0]["package_sha256"] == package_before["package_sha256"]
    assert store.get_result_delivery(work_id) == package_before
    assert sent == [qid] and session.post.call_count == 1
    # The owner imports the actual original package and returns a real ACK.
    frozen = owner.contract.release_projection(json.loads(current["snapshot"]["manifest_raw"]))
    imported = import_package(
        owner, package_before["package"], frozen_release=frozen, identity_store=routes.identity
    )
    assert imported["summary"] == {"accepted": 1}
    consumer = dict(component="StockWiki", namespace="quick_scan", store_id=owner.store_id)
    store.bind_result_delivery_consumer(
        work_id, consumer, source_ref="owner-config:" + str(bundle / "owner-config.json")
    )
    assert store.apply_result_delivery_ack(work_id, imported["acks"][0])["state"] == "delivered"
    assert main_with_llm.main() == 0, capsys.readouterr().out
    reused = json.loads(output.read_bytes())
    reference = reused["observation_references"][qid]
    original = package_before["package"]["items"][0]["observation"]
    assert reference["observation_id"] == original["observation_id"]
    assert reference["payload_sha256"] == package_before["payload_sha256"]
    assert reference["original_observation"] == original
    assert reference["new_observation_created"] is False
    assert reused["requested_scope_completed"] is True and reused["whole_manifest_scanned"] is False
    assert sent == [qid] and session.post.call_count == 1
    assert store.get_result_delivery(work_id)["package_sha256"] == package_before["package_sha256"]
    # An old durable action is not enduring permission: revoke through owner
    # policy and require the PUBLIC original main to refuse with zero sends.
    prepared["owner_policy"]["policy_version"] += 1
    prepared["policy_file"].write_text(json.dumps(prepared["owner_policy"]), encoding="utf-8")
    assert main_with_llm.main() != 0
    assert sent == [qid] and session.post.call_count == 1
