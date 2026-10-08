"""QA-C06-02 offline CLI E2E: the complete standard C06 through the public entry.

Runs ``main_with_llm.py`` with the REAL runner, work store, adapter and seal
path over a real frozen IQS manifest/authority pair. Only the HTTP boundary is
stubbed. Everything the v2 path must prove is asserted here: a complete
Observation (not a compact one) is sealed on the cold run, the full standard
body is durable and untruncated, restart sealing and warm runs cost zero model
calls, and an import ACK settles the work exactly once.

Every identity, manifest, prompt and answer in this file is SYNTHETIC — the
fixture manifest/authority come from ``tests/fixtures`` and the identity bytes
are generated here. Nothing here is an owner golden or a real StockWiki
positive example.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

import pytest

import main_with_llm
from src.utils.quick_scan_result_outbox import (
    canonical_sha256,
    validate_exchange_package,
)
from src.utils.quick_scan_work_store import QuickScanWorkStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ENTITY_ID = "ENT_CONTEXT_FIXTURE"
COMPANY = "Fixture Corp"
MODEL = "actual-fixture-model"
SOURCE_URL = "https://example.com/issuer"
SECURITY_SCOPE_ID = "SEC_CONTEXT_FIXTURE"
MANIFEST_BYTES = (FIXTURES / "quick_scan_c06_manifest_v2_fixture.json").read_bytes()
AUTHORITY_DOC = json.loads(
    (FIXTURES / "quick_scan_c06_authority_v2_fixture.json").read_text(encoding="utf-8")
)
MANIFEST_DOC = json.loads(MANIFEST_BYTES.decode("utf-8"))
CUTOFF = MANIFEST_DOC["profile"]["as_of"]


def _identity_bytes() -> bytes:
    package = {
        "object_type": "entity",
        "schema_version": "2.2.0",
        "payload": {
            "entity_id": ENTITY_ID,
            "identity_revision": 1,
            "identity_state": "verified",
            "listings": [{"source_binding_ref": "BND_QA_C06_02"}],
        },
    }
    return json.dumps(package, ensure_ascii=False).encode("utf-8")


def _authority_bytes(identity_sha256: str) -> bytes:
    """The fixture authority re-signed onto THIS run's identity bytes."""
    context = json.loads(json.dumps(AUTHORITY_DOC["observation_context"]))
    context["identity_snapshot_sha256"] = identity_sha256
    for question in context["questions"].values():
        metadata = question["metadata"]
        metadata["task_mode"] = "primary"
        metadata["comparison_group_id"] = None
    document = json.loads(json.dumps(AUTHORITY_DOC))
    document["observation_context"] = context
    document["observation_context_sha256"] = canonical_sha256(context)
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


def _standard_answer(question_id: str, *, large: bool = False) -> dict:
    evidence = [
        {
            "id": "e1",
            "title": "Synthetic primary source",
            "url": SOURCE_URL,
            "published_at": "2026-09-01",
            "claim": "Synthetic claim used only for offline validation.",
        }
    ]
    if large:
        evidence = [
            {
                "id": f"e{index}",
                "title": "Synthetic primary source",
                "url": SOURCE_URL,
                "published_at": "2026-09-01",
                "claim": "synthetic " + "x" * 380,
            }
            for index in range(18)
        ]
    return {
        "question_id": question_id,
        "response_kind": "score",
        "status": "scored",
        "score": 8,
        "summary": "Synthetic standard answer; not a real company conclusion.",
        "information_as_of": "2026-09-01",
        "period_start": "2026-01-01",
        "period_end": "2026-06-30",
        "basis": "current",
        "trend": "stable",
        "confidence": "medium",
        "metrics": [],
        "items": [],
        "evidence": evidence,
        "counterevidence": "Synthetic counterpoint kept for balance.",
        "watch_triggers": ["Synthetic watch trigger"],
        "missing_fields": ["customer concentration"],
        "coverage": {
            "status": "partial",
            "reason": "Synthetic fixture covers only part of the company.",
        },
    }


def _response_for(question_id: str) -> Mock:
    body = _standard_answer(question_id, large=question_id == MANIFEST_DOC["questions"][0]["id"])
    outer = {
        "entity_id": ENTITY_ID,
        "company_name": COMPANY,
        "question_id": question_id,
        "status": "scored",
        "score": 8,
        "information_as_of": "2026-09-01",
        "description": json.dumps(body, ensure_ascii=False),
    }
    output = [
        {
            "type": "web_search_call",
            "id": f"ws_{question_id}",
            "status": "completed",
            "action": {
                "type": "search",
                "sources": [{"type": "url", "url": SOURCE_URL}],
            },
        },
        {
            "type": "message",
            "content": [{"type": "output_text", "text": json.dumps(outer, ensure_ascii=False)}],
        },
    ]
    response = Mock()
    response.headers = {"x-request-id": f"req_{question_id}"}
    response.status_code = 200
    response.json.return_value = {
        "id": f"resp_{question_id}",
        "status": "completed",
        "model": MODEL,
        "output": output,
    }
    return response


def _setup(tmp_path: Path) -> dict[str, Path]:
    (tmp_path / "logs").mkdir(exist_ok=True)
    questions = [
        {"question_id": question["id"], "text": question["prompt"]}
        for question in MANIFEST_DOC["questions"]
    ]
    question_file = tmp_path / "questions.json"
    question_file.write_text(
        json.dumps({"questions": questions}, ensure_ascii=False), encoding="utf-8"
    )
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_bytes(MANIFEST_BYTES)
    identity_file = tmp_path / "identity.json"
    identity_file.write_bytes(_identity_bytes())
    authority_file = tmp_path / "quick_scan_c06_authority.json"
    authority_file.write_bytes(
        _authority_bytes(hashlib.sha256(identity_file.read_bytes()).hexdigest())
    )
    (tmp_path / "llm_apis.json").write_text(
        json.dumps(
            {
                "default_provider": "openai",
                "providers": {
                    "openai": {
                        "enabled": True,
                        "api_key": "offline-fixture-key",
                        "model": MODEL,
                        "base_url": "https://api.openai.com/v1/chat/completions",
                        "max_retries": 1,
                        "format_repair_budget": 0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "spend_authorization.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "currency": "USD",
                "hard_cap": 25,
                "pricing_snapshot_ref": "fixture-pricing-snapshot",
                "authorized_at": "2026-10-06T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    return {
        "questions": question_file,
        "manifest": manifest_file,
        "identity": identity_file,
        "authority": authority_file,
        "spend": tmp_path / "spend_authorization.json",
        "output": tmp_path / "result.json",
    }


def _install_http(monkeypatch) -> dict:
    session = Mock()
    seen: list[str] = []

    def _post(*args, **kwargs):
        text = kwargs["json"]["input"]
        match = re.search(r"Target question_id: ([A-Za-z0-9_.]+)\.", text)
        assert match is not None, text[:400]
        seen.append(match.group(1))
        return _response_for(match.group(1))

    session.post.side_effect = _post
    manager = Mock()
    manager.get_sync_session.return_value = session
    monkeypatch.setattr("src.providers.llm_client.http_client_manager", manager)
    return {"session": session, "sent": seen}


def _run(monkeypatch, tmp_path, files) -> int:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "main_with_llm.py",
            "--company",
            COMPANY,
            "--entity-id",
            ENTITY_ID,
            "--provider",
            "openai",
            "--config",
            str(files["questions"]),
            "--output",
            str(files["output"]),
            "--require-search",
            "--identity-snapshot",
            str(files["identity"]),
            "--spend-authorization",
            str(files["spend"]),
            "--question-manifest",
            str(files["manifest"]),
            "--security-scope-id",
            SECURITY_SCOPE_ID,
            "--c06-authority",
            str(files["authority"]),
        ],
    )
    return main_with_llm.main()


def _seal(monkeypatch, tmp_path, files, capsys) -> dict:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "main_with_llm.py",
            "--seal-deliveries",
            "--c06-authority",
            str(files["authority"]),
        ],
    )
    code = main_with_llm.main()
    out = capsys.readouterr().out
    assert code == 0, out
    lines = [line for line in out.splitlines() if line.startswith("{")]
    assert lines, out
    return json.loads(lines[-1])


def _ack_for(delivery: dict) -> dict:
    return {
        "schema_version": "1.0.0",
        "ack_id": "ack_qa_c06_02_0001",
        "package_id": delivery["package_id"],
        "item_id": delivery["item_id"],
        "observation_id": delivery["observation_id"],
        "payload_sha256": delivery["payload_sha256"],
        "status": "accepted",
        "error_code": None,
        "received_at": "2026-10-07T12:00:00Z",
        "consumer": {
            "component": "StockWiki",
            "namespace": "quick_scan",
            "store_id": "store-qa-c06-02",
        },
    }


def _utc(value: str):
    from datetime import datetime, timezone

    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _plans(captured: str) -> list[dict]:
    plans = []
    for line in captured.splitlines():
        line = line.strip()
        if line.startswith('{"contract_id": "stockqa.consumes_iqs_question_manifest'):
            plans.append(json.loads(line))
    return plans


def _work_ids(store: QuickScanWorkStore) -> list[str]:
    grouped = store.find_work_items(
        entity_id=ENTITY_ID,
        question_ids=[question["id"] for question in MANIFEST_DOC["questions"]],
    )
    ids = [rows[0]["work_item_id"] for rows in grouped.values() if rows]
    assert len(ids) == len(MANIFEST_DOC["questions"]), len(ids)
    return sorted(ids)


def test_cli_e2e_complete_standard_c06_seals_warms_and_settles(
    monkeypatch, tmp_path, capsys
) -> None:
    files = _setup(tmp_path)
    http = _install_http(monkeypatch)

    # cold run: every frozen question is dispatched exactly once
    assert _run(monkeypatch, tmp_path, files) == 0
    cold_out = capsys.readouterr().out
    assert len(http["sent"]) == len(MANIFEST_DOC["questions"])
    assert sorted(http["sent"]) == sorted(q["id"] for q in MANIFEST_DOC["questions"])
    cold_plan = _plans(cold_out)[-1]
    assert cold_plan["counts"] == {"dispatch": len(MANIFEST_DOC["questions"])}
    assert cold_plan["model_calls_planned"] == len(MANIFEST_DOC["questions"])

    store = QuickScanWorkStore(tmp_path / "quick_scan_work.sqlite")
    work_ids = _work_ids(store)
    authority = json.loads(files["authority"].read_text(encoding="utf-8"))
    largest = 0
    for work_item_id in work_ids:
        item = store.get_item(work_item_id)
        assert item["status"] == "result_ready"
        checkpoint = store.get_answer_checkpoint(work_item_id)
        assert checkpoint is not None
        context = store.get_observation_context(work_item_id)
        assert context is not None
        assert context["context_sha256"] == authority["observation_context_sha256"]
        standard = store.get_standard_answer(work_item_id)
        assert standard is not None
        largest = max(largest, len(json.dumps(standard["answer"], ensure_ascii=False)))

        delivery = store.get_result_delivery(work_item_id)
        assert delivery is not None and delivery["state"] == "ready"
        validate_exchange_package(delivery["package"])
        observation = delivery["package"]["items"][0]["observation"]
        # COMPLETE observation: the compact v1 package never carried these
        assert observation["information_cutoff"] == CUTOFF
        assert observation["entity_id"] == ENTITY_ID
        assert observation["execution"]["model_resolved"] == MODEL
        assert _utc(observation["execution"]["started_at"]) <= _utc(
            observation["execution"]["answered_at"]
        )
        assert observation["observed_at"] == observation["execution"]["answered_at"]
        assert observation["answer"]["response_kind"] == "score"
        assert observation["answer"]["evidence"]
        assert observation["observation_id"] == "obs_" + canonical_sha256(
            {k: v for k, v in observation.items() if k != "observation_id"}
        )
        # the stored body is identical to what the observation carries
        assert standard["answer"] == observation["answer"]
        revisions = store.list_delivery_revisions(work_item_id)
        assert len(revisions) == 1 and revisions[0]["revision"] == 1
    assert largest > 5000, "the full standard body must never be truncated"

    # restart seal entry: zero model calls, every package already sealed
    report = _seal(monkeypatch, tmp_path, files, capsys)
    assert report["schema"] == "stockqa.seal_deliveries/1.0.0"
    assert report["model_calls"] == 0
    assert len(report["already_sealed"]) == len(work_ids)
    assert report["sealed"] == [] and report["blocked"] == []

    # warm run: hydration reuses every answer, zero additional HTTP
    assert _run(monkeypatch, tmp_path, files) == 0
    warm_out = capsys.readouterr().out
    assert len(http["sent"]) == len(MANIFEST_DOC["questions"])
    warm_plan = _plans(warm_out)[-1]
    assert warm_plan["counts"] == {"reuse": len(MANIFEST_DOC["questions"])}
    assert warm_plan["model_calls_planned"] == 0

    # one exact ACK settles exactly one work item; replaying it is idempotent
    first = work_ids[0]
    delivery = store.get_result_delivery(first)
    assert delivery is not None
    ack = _ack_for(delivery)
    settled = store.apply_result_delivery_ack(first, ack)
    assert settled["state"] == "delivered"
    assert store.get_item(first)["status"] == "delivered"
    replay = store.apply_result_delivery_ack(first, ack)
    assert replay["state"] == "delivered"
    assert replay["ack_sha256"] == settled["ack_sha256"]
    events = store.list_result_delivery_events(first)
    assert [event["event_type"] for event in events] == ["package_prepared", "accepted"]

    # a forged hash or a foreign store can never settle a delivery
    forged = dict(ack)
    forged["payload_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        store.apply_result_delivery_ack(first, forged)
    foreign = _ack_for(delivery)
    foreign["ack_id"] = "ack_qa_c06_02_0002"
    foreign["consumer"] = dict(foreign["consumer"], store_id="someone-elses-store")
    with pytest.raises(ValueError):
        store.apply_result_delivery_ack(first, foreign)
    second_delivery = store.get_result_delivery(work_ids[1])
    assert second_delivery is not None
    mismatched = _ack_for(delivery)
    with pytest.raises(ValueError):
        store.apply_result_delivery_ack(work_ids[1], mismatched)
    # a delivered item is never re-dispatched
    with pytest.raises(ValueError):
        store.begin_result_delivery(first)

    # the settled delivery is out of the seal candidates; nothing changes
    final = _seal(monkeypatch, tmp_path, files, capsys)
    assert final["model_calls"] == 0
    assert final["sealed"] == [] and final["blocked"] == []
    assert all(item["work_item_id"] != first for item in final["already_sealed"])


def test_cli_e2e_tampered_v2_authority_is_refused_before_any_http(
    monkeypatch, tmp_path, capsys
) -> None:
    files = _setup(tmp_path)
    document = json.loads(files["authority"].read_text(encoding="utf-8"))
    question_id = MANIFEST_DOC["questions"][0]["id"]
    document["observation_context"]["questions"][question_id]["metadata"]["answer"] = (
        _standard_answer(question_id)
    )
    document["observation_context_sha256"] = canonical_sha256(document["observation_context"])
    files["authority"].write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    http = _install_http(monkeypatch)

    assert _run(monkeypatch, tmp_path, files) == 1
    capsys.readouterr()
    assert http["session"].post.call_count == 0
    assert not files["output"].exists()


def test_cli_e2e_authority_manifest_binding_is_checked_before_any_http(
    monkeypatch, tmp_path, capsys
) -> None:
    files = _setup(tmp_path)
    document = json.loads(files["manifest"].read_text(encoding="utf-8"))
    document["answer_format"] = "standard-legacy"
    files["manifest"].write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    http = _install_http(monkeypatch)

    assert _run(monkeypatch, tmp_path, files) == 1
    capsys.readouterr()
    assert http["session"].post.call_count == 0
    assert not files["output"].exists()
