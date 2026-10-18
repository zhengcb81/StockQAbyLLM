"""Original public owner + SDK + Runner + process-death/cache recovery.

Synthetic company/evidence and scripted vendor wire only. The approval, native
SQLite, SDK, pack transport, intent binding and Runner are the original code.
Run only in an explicitly owned three-component runtime, never production DBs.
"""

import importlib.util
import json
import os
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

if os.environ.get("IQS_C06_RUN_INTEGRATION") != "1":
    pytest.skip("requires explicitly owned three-component runtime", allow_module_level=True)

_ENV_NAMES = (
    "SystemRoot",
    "SYSTEMROOT",
    "WINDIR",
    "PATH",
    "TEMP",
    "TMP",
    "PYTHONUTF8",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONPATH",
    "IQS_C06_RUN_INTEGRATION",
    "IQS_C06_OWN",
    "IQS_C06_COMPOSITION_CODE_ROOT",
    "IQS_C06_COMPANY_ROOT",
    "IQS_C06_BRIDGE_ROOT",
    "IQS_CONTINUATION_OWN",
    "IQS_CONTINUATION_METRICS",
    "IQS_V15_ORIGINAL101",
)
SAFE_ENV = {name: os.environ[name] for name in _ENV_NAMES if name in os.environ}
# This is an explicitly isolated test process. Original fixture code also uses
# dict(os.environ); constrain that process before importing any fixture/client.
os.environ.clear()
os.environ.update(SAFE_ENV)

OWN = Path(os.environ["IQS_C06_OWN"])
CODE = Path(os.environ["IQS_C06_COMPOSITION_CODE_ROOT"])
COMPANY = Path(os.environ["IQS_C06_COMPANY_ROOT"])
BRIDGE = Path(os.environ["IQS_C06_BRIDGE_ROOT"])
sys.path.insert(0, str(COMPANY / "src"))
sys.path.insert(0, str(BRIDGE / "src"))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PHASE = "fixture"
_NATIVE_SUBPROCESS_RUN = subprocess.run
_METRICS = Path(os.environ.get("IQS_CONTINUATION_METRICS", os.environ["TEMP"])) / (
    "continuation-metrics-" + str(os.getpid()) + ".jsonl"
)
_METRICS.parent.mkdir(parents=True, exist_ok=True)


def metric(kind, **data):
    with _METRICS.open("a", encoding="utf8") as stream:
        stream.write(
            json.dumps(dict(kind=kind, phase=_PHASE, observed_epoch=time.time(), **data)) + "\n"
        )


def observed_subprocess(command, *args, **kwargs):
    started = time.perf_counter()
    try:
        result = _NATIVE_SUBPROCESS_RUN(command, *args, **kwargs)
        return result
    finally:
        module = command[command.index("-m") + 1] if "-m" in command else "python-script"
        operation = next(
            (
                flag
                for flag in (
                    "--permissions-request-stdin",
                    "--inspect-confirmation",
                    "--request",
                    "--install-delegation",
                    "--confirm-delegated",
                    "--principal",
                )
                if flag in command
            ),
            "other",
        )
        metric(
            "original_subprocess",
            module=module,
            operation=operation,
            seconds=time.perf_counter() - started,
        )


subprocess.run = observed_subprocess

engine = load("native_shared_engine", CODE / "scripts/shared_company_engine.py")
factory = load("native_shared_task", CODE / "scripts/shared_company_task.py")
from src.providers.llm_client import LLMClient
from src.runners.llm_runner import QuickScanWorkLifecycle
from src.utils.quick_scan_owner_research import OwnerResearchServices
from src.utils.quick_scan_refresh_admission import RefreshAdmissionService
from src.utils.quick_scan_refresh_execution import prepare_execution
from tests.continuation_fixture import setup_continuation
from tests.integration.test_c06_subject_public_e2e import actual
from tests.unit.test_quick_scan_budget import _policy


def construct(config):
    global _PHASE
    _PHASE = "prepare_execution"
    owner = prepare_execution(
        config["deployment"],
        config["confirmation_id"],
        input_paths={
            name: Path(config["bundle"]) / name
            for name in (
                "manifest.json",
                "identity.json",
                "authority.json",
                "owner-config.json",
                "questions.json",
            )
        },
        process_env=dict(SAFE_ENV),
    )
    first = owner.requested_question_ids[0]
    ref = owner.native.observation_context["questions"][first]["subject_binding"]["subject_ref"]
    policy = config["policy"]
    route = next(row for row in policy["routes"] if row["id"] == config["route_id"])
    lifecycle = QuickScanWorkLifecycle(
        owner.service.store,
        entity_id=ref["entity_id"],
        run_id="NATIVE_SHARED_12",
        scan_id="NATIVE_SHARED_12_SCAN",
        identity=owner.bundle["identity"],
        transport_managed=True,
        route_id=route["id"],
        provider_name=route["provider_config_ref"],
        model_requested="mimo-v2.6-pro",
        budget_policy=policy,
        budget_route=route,
        owner_refresh_session=owner,
        observation_context=owner.native.observation_context,
        scope_by_question=owner.scope_bindings,
        generation_by_question={
            qid: owner.existing_work(qid)["generation"] for qid in owner.requested_question_ids
        },
    )
    original_before = lifecycle.before_question

    def observed_before(question):
        started = time.perf_counter()
        try:
            return original_before(question)
        finally:
            metric("original_claim", seconds=time.perf_counter() - started)

    lifecycle.before_question = observed_before

    original_batch = lifecycle.before_registered_questions

    def observed_batch(questions):
        started = time.perf_counter()
        try:
            return original_batch(questions)
        finally:
            metric(
                "original_batch_claim", count=len(questions), seconds=time.perf_counter() - started
            )

    lifecycle.before_registered_questions = observed_batch
    task = factory.create_company_scan_task(
        lifecycle,
        experiment_id="native-shared-software-e2e",
        run_id="NATIVE_SHARED_12",
        max_questions_per_pack=2,
    )
    from iqs_company_agent_lab.scan_profile import task_validation_context

    assert task_validation_context(task)["information_cutoff"] == config["information_cutoff"]
    assert task.raw["execution_profile"] == config["execution_profile"]
    assert task.raw["execution_profile_sha256"] == config["execution_profile_sha256"]
    client = LLMClient(
        "SCRIPTED-NOT-A-SECRET",
        "mimo-v2.6-pro",
        "https://api.xiaomimimo.com/v1/chat/completions",
        provider_name="mimo",
        timeout=10,
    )
    services = OwnerResearchServices(
        lifecycle,
        client,
        task=task,
        owner_batch_claim=True,
        cost_resolver=lambda _: dict(
            actual_cost=0.001,
            pricing_ref="scripted-native-rate",
            source_ref="synthetic-software-e2e",
        ),
    )
    return task, services


def child_run(config_path, phase):
    global _PHASE
    config = json.loads(Path(config_path).read_text(encoding="utf8"))
    base = Path(config["root"])
    task, services = construct(config)
    _PHASE = "cold_claim_sdk" if phase == "crash" else "cache_recovery"
    from iqs_company_agent_lab import synthfixtures as sf

    from src.providers import llm_research_client as sdk

    posts = []

    def lease_measurement():
        import sqlite3

        with closing(
            sqlite3.connect(services.store.path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as observed:
            observed.execute("PRAGMA query_only=ON")
            work_ids = [
                entry["work_item_id"]
                for entry in services.owner.entries.values()
                if entry["kind"] == "work"
            ]
            if not work_ids:
                return {"lease_active": False}
            expiry = observed.execute(
                "SELECT MIN(lease_expires_at) FROM work_item WHERE status='leased' AND work_item_id IN ("
                + ",".join("?" for _ in work_ids)
                + ")",
                work_ids,
            ).fetchone()[0]
        if expiry is None:
            return {"lease_active": False}
        first_claim = expiry - 600
        return {
            "lease_active": True,
            "original_lease_seconds": 600,
            "earliest_claim_epoch": first_claim,
            "earliest_expiry_epoch": expiry,
            "elapsed_since_first_claim": time.time() - first_claim,
        }

    class Response:
        status_code = 200
        headers = {"x-request-id": "scripted-native-owner-12"}

        def __init__(self):
            answers = [sf.gen_insufficient(row["question"]) for row in task.questions]
            for answer in answers:
                answer["status"] = "not_applicable"
                answer["information_as_of"] = config["information_cutoff"]
                answer["summary"] = "[Synthetic software fixture] No real company finding."
            self.content = json.dumps(
                dict(
                    id="scripted-native-owner-response",
                    model="mimo-v2.6-pro",
                    choices=[
                        dict(
                            message=dict(
                                role="assistant",
                                content=json.dumps(
                                    {"answers": answers, "declared_question_ids": task.question_ids}
                                ),
                                reasoning_content="PRIVATE-NATIVE-REASONING-NEVER-PERSIST",
                            ),
                            finish_reason="stop",
                        )
                    ],
                    usage=dict(
                        prompt_tokens=1200,
                        prompt_tokens_details=dict(cached_tokens=400),
                        completion_tokens=1200,
                        completion_tokens_details=dict(reasoning_tokens=200),
                    ),
                )
            ).encode("utf8")

        def raise_for_status(self):
            pass

    class Wire:
        def post(self, *args, **kwargs):
            posts.append(1)
            metric("scripted_vendor_post", posts=len(posts), **lease_measurement())
            if phase != "crash":
                raise AssertionError("native saved response must recover without another POST")
            assert kwargs["json"]["thinking"] == {"type": "enabled"}
            assert len(kwargs["json"]["messages"]) >= 2
            return Response()

    sdk.http_client_manager.get_metered_sync_session = lambda: Wire()
    session = dict(
        protocol="iqs.agent-runtime-lab.session-config/1.0.0",
        planned_model="mimo-v2.6-pro",
        endpoint_profile_id="mimo-v2.6-pro@https://api.xiaomimimo.com/v1/chat/completions",
        max_output_tokens=16384,
        session_input_tokens=240000,
        session_output_tokens=48000,
        wall_seconds=900,
        logical_clock=False,
    )
    if phase == "crash":
        original = services.observe_native_step

        def die_after_native_commit(step):
            ref = original(step)
            metric("native_safe_output_committed", **lease_measurement())
            (base / "crash-native-proof.json").write_text(
                json.dumps({"native_reference": ref, "scripted_posts": len(posts)}), encoding="utf8"
            )
            import sitecustomize

            sitecustomize._report()
            os._exit(86)

        services.observe_native_step = die_after_native_commit
        engine.execute_shared_engine(
            task,
            services,
            session,
            base / "unused-export",
            session_dir=base / "crashed-session",
            transport_origin="scripted_wire",
        )
        raise AssertionError("child did not die at native response boundary")
    result = engine.resume_shared_engine(
        task,
        services,
        session,
        base / "crashed-session/checkpoint.json",
        base / "recovered-export",
        session_dir=base / "recovered-session",
        transport_origin="scripted_wire",
    )
    metric("cache_recovery_complete", **lease_measurement())
    from iqs_company_agent_lab.reporter import generate_report

    report = generate_report(base / "recovered-export", base / "recovered-report")
    assert result["all_slots_completed"] and not posts
    summary = result["runtime_summary"]
    assert summary["new_model_http"] == 0 and summary["replayed_native_attempts"] == 1
    assert summary["runtime_origin"] == "scripted_wire"
    assert (
        not summary["owner_authority_verified"] and not summary["financial_qualification_granted"]
    )
    assert summary["cash_cost"] is None
    assert report["runtime_observations"] == summary
    with closing(services.store._connect()) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM quick_scan_budget_attempt").fetchone()[0]
            == 102
        )
    for qid in task.question_ids:
        assert (
            services.store.get_answer_checkpoint(services.owner.existing_work(qid)["work_item_id"])
            is None
        )
    result_payload = dict(
        scripted_posts=len(posts),
        slots=len(result["run"]["slots"]),
        runtime_summary=summary,
        original_owner_rechecked=True,
        official_search_checkpoint_created=False,
    )
    (base / "recovery-result.json").write_text(json.dumps(result_payload), encoding="utf8")


@pytest.mark.parametrize("actual", ["1.2.0"], indirect=True)
def test_v15(actual, tmp_path):
    import hashlib
    import sqlite3

    from src.utils.quick_scan_owner_budget_continuation import exposure_snapshot

    original = Path(os.environ["IQS_V15_ORIGINAL101"])
    policy = json.loads(
        (original.parent / "original-budget-policy.json").read_text(encoding="utf8")
    )
    route = next(row for row in policy["routes"] if row["model"] == "mimo-v2.6-pro")
    ledger_path = tmp_path / "retained-original101.sqlite"
    before_source = hashlib.sha256(original.read_bytes()).hexdigest()
    with closing(sqlite3.connect(original.resolve().as_uri() + "?mode=ro", uri=True)) as source:
        source.execute("PRAGMA query_only=ON")
        with closing(sqlite3.connect(ledger_path)) as target:
            source.backup(target)

    def rows(db, table):
        return [dict(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY 1")]

    with closing(sqlite3.connect(ledger_path)) as db:
        db.row_factory = sqlite3.Row
        old_budget = rows(db, "quick_scan_budget_attempt")
        old_work = rows(db, "work_item")
        old_attempt = rows(db, "attempt")
    assert len(old_budget) == 101 and len(old_work) == 12
    prepared = setup_continuation(
        actual,
        tmp_path,
        policy=policy,
        ledger_path=ledger_path,
        refresh_version="1.5.0",
        provider="mimo",
        selected_question_count=2,
        route_id=route["id"],
    )
    assert prepared["preview"]["status"] == "ready"
    quote = prepared["preview"]["quote"]
    assert quote["planned_physical_requests"] == quote["max_http_requests"] == 1
    assert quote["retained_unreconciled_attempts"] == 3 and not quote["cash_actual_complete"]
    assert prepared["consent"]["method"] == "native_owner_delegated_agent"
    accepted = RefreshAdmissionService(
        prepared["deployment_file"], process_env=dict(SAFE_ENV)
    ).admit(prepared["consent"]["confirmation_id"])
    assert accepted["status"] == "accepted"
    cell = prepared["control"]["cells"][0]
    config = dict(
        deployment=str(prepared["deployment_file"]),
        bundle=str(prepared["bundle"]),
        confirmation_id=prepared["consent"]["confirmation_id"],
        policy=policy,
        route_id=route["id"],
        root=str(tmp_path),
        execution_profile=cell["execution_profile"],
        execution_profile_sha256=cell["execution_profile_sha256"],
        information_cutoff=cell["execution_profile"]["information_cutoff"],
    )
    config_file = tmp_path / "owned-native-test.json"
    config_file.write_text(json.dumps(config), encoding="utf8")
    for phase, expected in (("crash", 86), ("resume", 0)):
        completed = subprocess.run(
            [sys.executable, "-B", str(Path(__file__)), phase, str(config_file)],
            cwd=OWN / "qa",
            capture_output=True,
            env=dict(SAFE_ENV, PYTHONDONTWRITEBYTECODE="1"),
            timeout=600,
        )
        (tmp_path / (phase + ".stdout.log")).write_bytes(completed.stdout[-16000:])
        (tmp_path / (phase + ".stderr.log")).write_bytes(completed.stderr[-16000:])
        assert completed.returncode == expected, completed.stderr[-4000:].decode(errors="replace")
    with closing(sqlite3.connect(ledger_path)) as db:
        db.row_factory = sqlite3.Row
        current = {row["budget_attempt_id"]: row for row in rows(db, "quick_scan_budget_attempt")}
        assert all(current[row["budget_attempt_id"]] == row for row in old_budget)
        current_work = {row["work_item_id"]: row for row in rows(db, "work_item")}
        assert all(current_work[row["work_item_id"]] == row for row in old_work)
        current_attempt = {row["attempt_id"]: row for row in rows(db, "attempt")}
        assert all(current_attempt[row["attempt_id"]] == row for row in old_attempt)
        assert len(current) == 102
    snapshot = exposure_snapshot(ledger_path, policy, dialect="1.5.0")
    assert len(snapshot["attempts"]) == 4
    new = next(
        row
        for row in snapshot["attempts"]
        if row["budget_attempt_id"] not in {x["budget_attempt_id"] for x in old_budget}
    )
    assert new["status"] == "response_unpriced" and new["actual_cost_micros"] is None
    assert new["native"]["usable_visible_output"] is True
    # New POST is not authorized by the old accepted three-exposure snapshot.
    from src.utils.quick_scan_owner_budget_continuation_v15 import (
        validate_retained_rows,
    )

    with closing(sqlite3.connect(ledger_path)) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(ValueError, match="complete_snapshot_changed"):
            validate_retained_rows(db, prepared["control"], policy["policy_id"])
    payload = json.loads((tmp_path / "recovery-result.json").read_text())
    assert payload["slots"] == 2 and payload["scripted_posts"] == 0
    assert json.loads((tmp_path / "crash-native-proof.json").read_text())["scripted_posts"] == 1
    assert hashlib.sha256(original.read_bytes()).hexdigest() == before_source
    (tmp_path / "continuation-proof.json").write_text(
        json.dumps(
            dict(
                original_requests=101,
                total_requests=102,
                original_budget_work_attempt_rows_unchanged=True,
                retained_exposures_before=3,
                retained_exposures_after=4,
                new_response_unpriced_and_cache_readable=True,
                old_native_members_not_retried=True,
                slots=2,
                scripted_posts=1,
                recovery_posts=0,
                lease_seconds=600,
            )
        ),
        encoding="utf8",
    )


if __name__ == "__main__":
    child_run(sys.argv[2], sys.argv[1])
