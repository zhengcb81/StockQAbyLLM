"""Real public main, owner refresh, authority3 and bounded restart software E2E.

Full frozen 24-question release is retained. A one-request local test policy
limits dispatch, not a fabricated short release. Only vendor HTTP is replaced.
No real issuer golden, investment conclusion, API charge or production data.
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
    pytest.skip(
        "C06 integration requires an explicit owned three-component runtime",
        allow_module_level=True,
    )

from stockwiki.quick_scan_import import import_package
from stockwiki.quick_scan_subject_authority import IqsSubjectContract
from stockwiki.quick_scan_subject_dispatch import SubjectBoundObservationStore

from src.utils.quick_scan_work_store import QuickScanWorkStore
from tests.integration.test_c06_subject_public_e2e import OWN, actual, invoke, utc
from tests.unit.test_llm_config import _quick_scan_policy
from tests.unit.test_quick_scan_work_transport import _http_response


def test_public_main_authority3_dispatch_import_ack_then_current_owner_reuse(
    actual, tmp_path, monkeypatch, capsys
):
    import main_with_llm
    from src.providers import llm_client

    routes, current, config, authority, manifest, identity_file = actual
    release = Path(config["iqs_release_root"])
    question_input = tmp_path / "question-config-input.json"
    question_input.write_text(
        json.dumps({"manifest": json.loads(current["snapshot"]["manifest_raw"])}), encoding="utf-8"
    )
    exported = invoke(
        [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            str(OWN / "iqs/scripts/subject_dispatch_cli.py"),
            "--operation",
            "questions",
            "--release-root",
            str(release),
            "--input",
            str(question_input),
        ],
        tmp_path,
        "identified-question-export",
    )
    identified = tmp_path / "questions-identified.json"
    identified.write_text(json.dumps(exported["result"]), encoding="utf-8")
    policy = _quick_scan_policy(order=("primary",))
    policy["models"][0]["model"] = "fixture-model"
    policy["budget"].update(max_cost=1, max_requests=1, max_cost_per_attempt=1)
    (tmp_path / "llm_apis.json").write_text(
        json.dumps(
            {
                "default_provider": "primary",
                "providers": {
                    "primary": {
                        "enabled": True,
                        "api_key": "synthetic-only",
                        "model": "fixture-model",
                        "base_url": "https://api.openai.com/v1/responses",
                        "max_retries": 1,
                        "format_repair_budget": 0,
                    }
                },
                "quick_scan_model_policy": policy,
            }
        ),
        encoding="utf-8",
    )
    spend = tmp_path / "spend.json"
    spend.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "currency": "USD",
                "hard_cap": 1,
                "pricing_snapshot_ref": "synthetic-test-cap",
                "authorized_at": utc(),
            }
        ),
        encoding="utf-8",
    )
    owner_config = tmp_path / "owner-config.json"
    owner_config.write_text(
        json.dumps(dict(config, protocol="stockqa.owner_refresh_config/1.0.0", ttl_hours=48)),
        encoding="utf-8",
    )
    owner = SubjectBoundObservationStore(
        routes.identity.paths,
        contract=IqsSubjectContract(
            OWN / "iqs",
            release_root=release,
            runs_dir=tmp_path / "contract-runs",
            process_env=dict(os.environ),
        ),
    )
    # The fixture performed first setup through the public owner CLI.
    assert owner.database_path.exists()
    sent = []
    session = Mock()

    def post(*args, **kwargs):
        text = kwargs["json"]["input"]
        qid = re.search(r"Target question_id: ([A-Za-z0-9_.]+)\.", text).group(1)
        sent.append(qid)
        assert len(sent) == 1  # Any accidental restart/second send fails here.
        assert owner.dispatch_count() >= 1
        body = copy.deepcopy(
            __import__(
                "tests.unit.test_quick_scan_observation_context", fromlist=["content"]
            ).content()
        )
        body.update(question_id=qid, information_as_of=utc()[:10])
        body["evidence"][0]["url"] = "https://example.org/source"
        outer = {
            "entity_id": current["snapshot"]["entity_id"],
            "company_name": "Synthetic E2E issuer",
            "question_id": qid,
            "status": "scored",
            "score": 8,
            "description": json.dumps(body),
            "information_as_of": utc()[:10],
        }
        response = _http_response(200)
        response.json.return_value["output"][1]["content"][0]["text"] = json.dumps(outer)
        return response

    session.post.side_effect = post
    monkeypatch.setattr(llm_client.http_client_manager, "get_sync_session", lambda: session)
    (tmp_path / "logs").mkdir()
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "result.json"

    def run():
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "main_with_llm.py",
                "--company",
                "Synthetic E2E issuer",
                "--entity-id",
                current["snapshot"]["entity_id"],
                "--provider",
                "primary",
                "--config",
                str(identified),
                "--output",
                str(output),
                "--require-search",
                "--identity-snapshot",
                str(identity_file),
                "--spend-authorization",
                str(spend),
                "--question-manifest",
                str(release / "run/manifest.json"),
                "--c06-authority",
                str(tmp_path / "authority.json"),
                "--quick-scan-owner-config",
                str(owner_config),
            ],
        )
        return main_with_llm.main()

    # Deliberately bounded partial run: remaining questions are deferred,
    # not a successful whole-company scan and not fabricated free answers.
    assert run() == 1, capsys.readouterr().out
    assert len(sent) == 1
    store = QuickScanWorkStore(tmp_path / "quick_scan_work.sqlite")
    with sqlite3.connect(store.path) as con:
        ready = con.execute(
            "SELECT work_item_id FROM work_item WHERE status='result_ready'"
        ).fetchall()
    assert len(ready) == 1
    work_id = ready[0][0]
    delivery = store.get_result_delivery(work_id)
    assert delivery["state"] == "ready"
    package = delivery["package"]
    original = package["items"][0]["observation"]
    assert original["execution"]["provider"] == "openai"  # Not config ref primary.
    assert original["execution"]["model_requested"] == "fixture-model"
    frozen = owner.contract.release_projection(json.loads(current["snapshot"]["manifest_raw"]))
    imported = import_package(owner, package, frozen_release=frozen, identity_store=routes.identity)
    assert imported["summary"] == {"accepted": 1}
    consumer = {"component": "StockWiki", "namespace": "quick_scan", "store_id": owner.store_id}
    store.bind_result_delivery_consumer(
        work_id, consumer, source_ref="owner-config:" + str(owner_config)
    )
    assert store.apply_result_delivery_ack(work_id, imported["acks"][0])["state"] == "delivered"
    original_package_hash = delivery["package_sha256"]
    restart_code = run()
    assert restart_code in (0, 1), capsys.readouterr().out
    resumed = json.loads(output.read_bytes())
    reference = resumed["observation_references"][sent[0]]
    assert reference["observation_id"] == original["observation_id"]
    assert reference["payload_sha256"] == delivery["package"]["items"][0]["payload_sha256"]
    assert reference["new_observation_created"] is False
    assert store.get_result_delivery(work_id)["package_sha256"] == original_package_hash
    assert len(sent) == session.post.call_count == 1
