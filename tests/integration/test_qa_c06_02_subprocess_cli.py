"""QA-C06-02 remediation: REAL subprocess public-CLI E2E.

Every run here launches ``main_with_llm.py`` as its own operating-system
process. Only the HTTP boundary is stubbed (``requests.Session.post`` inside a
``sitecustomize`` guard the children inherit); the runner, work store, budget
reservation, authority loader, adapter and seal path are the real components.
The warm and seal processes run WITHOUT the HTTP stub — recovery must succeed
with the stub withdrawn, at zero HTTP, zero key-read surprises and zero budget
reservation for rejected authorities.

Every identity, prompt and answer in this file is SYNTHETIC and derived from
``tests/fixtures/quick_scan_c06_*`` — never an owner golden or a real
StockWiki consumer example.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.utils.quick_scan_result_outbox import canonical_sha256
from src.utils.quick_scan_work_store import QuickScanWorkStore
from tests.integration import test_qa_c06_02_e2e as base

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN = REPO_ROOT / "main_with_llm.py"
QUESTIONS = [question["id"] for question in base.MANIFEST_DOC["questions"]]
PLAN_PREFIX = '{"contract_id": "stockqa.consumes_iqs_question_manifest'

# The children inherit this guard through PYTHONPATH; it blocks every network
# audit event, records key-file reads, restricts writes to the owned root and
# (only when QA100_STUB_HTTP=1) stubs the HTTP boundary itself.
_GUARD = '''"""QA-C06-02 subprocess guard: synthetic only, no provider calls."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

OWNED = Path(os.environ["E97_OWNED_ROOT"]).resolve()
KEY_LEDGER = OWNED / "key-opens.jsonl"
NET_LEDGER = OWNED / "network-attempts.jsonl"
_pair_sockets = []


def _inside(raw):
    if isinstance(raw, int):
        return True
    path = Path(os.fsdecode(raw)).resolve()
    if path == Path(os.devnull).resolve():
        return True
    return path.is_relative_to(OWNED)


def _append(ledger, payload):
    with Path(ledger).open("a", encoding="utf-8") as stream:
        stream.write(payload + "\\n")


def audit(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.bind", "socket.sendto"}:
        # Windows asyncio's internal socketpair: the exact ephemeral loopback
        # bind (127.0.0.1 or ::1, port 0) and connects to exactly that socket.
        # No external address, listener or model/search HTTP is admitted, so
        # the pair never counts as a network attempt.
        if event == "socket.bind" and args[1] in {("127.0.0.1", 0), ("::1", 0)}:
            _pair_sockets.append(args[0])
            return
        if event == "socket.connect":
            for sock in _pair_sockets:
                try:
                    if sock.getsockname() == args[1]:
                        return
                except OSError:
                    pass
        _append(NET_LEDGER, event)
        raise PermissionError("network is forbidden in this test root")
    if event == "open":
        mode, flags = args[1], args[2]
        writing = (isinstance(mode, str) and any(ch in mode for ch in "wax+")) or (
            isinstance(flags, int)
            and flags
            & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        )
        if writing:
            if not _inside(args[0]):
                raise PermissionError("write outside the owned test root")
        elif not isinstance(args[0], int) and Path(os.fsdecode(args[0])).name == "llm_apis.json":
            _append(KEY_LEDGER, os.fsdecode(args[0]))
    if event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.utime"}:
        if not _inside(args[0]):
            raise PermissionError("write outside the owned test root")
    if event in {"os.rename", "os.replace", "os.link", "os.symlink"}:
        if not (_inside(args[0]) and _inside(args[1])):
            raise PermissionError("write outside the owned test root")
    if event in {"os.system", "os.exec", "os.posix_spawn"}:
        raise PermissionError("shell execution is forbidden in this test root")
    if event == "subprocess.Popen":
        executable, argv, cwd, env = args
        same_python = (
            (Path(executable).resolve() == Path(sys.executable).resolve())
            if executable
            else (
                isinstance(argv, str)
                and argv.startswith(subprocess.list2cmdline([sys.executable]) + " ")
            )
        )
        if not same_python:
            raise PermissionError("only Python child processes are allowed")
        if cwd is None or not Path(cwd).resolve().is_relative_to(OWNED):
            raise PermissionError("child cwd outside the owned test root")
        if env is None or env.get("E97_OWNED_ROOT") != str(OWNED):
            raise PermissionError("child guard not inherited")
        if any(key.upper().endswith("_API_KEY") for key in env):
            raise PermissionError("credentials in the child environment")


sys.addaudithook(audit)

if os.environ.get("QA100_STUB_HTTP") == "1":
    import requests

    responses = json.loads((OWNED / "stub-responses.json").read_text(encoding="utf-8"))

    class _StubResponse:
        status_code = 200

        def __init__(self, payload, question_id):
            self._payload = payload
            self.headers = {"x-request-id": "req_" + question_id}

        def json(self):
            return self._payload

        def raise_for_status(self):
            return None

    def _post(self, *args, **kwargs):
        match = re.search(r"Target question_id: ([A-Za-z0-9_.]+)\\.", kwargs["json"]["input"])
        if match is None:
            raise AssertionError("dispatch text is missing its target question id")
        question_id = match.group(1)
        _append(
            OWNED / "http-stub-sends.jsonl",
            json.dumps({"question_id": question_id, "model_requested": kwargs["json"]["model"], "synthetic_only": True}),
        )
        return _StubResponse(json.loads(responses[question_id]), question_id)

    requests.Session.post = _post
'''


def _prepare_root(tmp_path: Path, *, corrupt_question: str | None = None) -> dict:
    files = base._setup(tmp_path)
    (tmp_path / "temp").mkdir()
    guard = tmp_path / "guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(_GUARD, encoding="utf-8", newline="\n")
    responses = {}
    for question_id in QUESTIONS:
        body = base._standard_answer(question_id)
        description = json.dumps(body, ensure_ascii=False)
        if question_id == corrupt_question:
            description = description.replace(
                '"summary": "Synthetic standard answer; not a real company conclusion."',
                '"summary": "first summary", "summary": "second ambiguous summary"',
                1,
            )
        outer = {
            "entity_id": base.ENTITY_ID,
            "company_name": base.COMPANY,
            "question_id": question_id,
            "status": "scored",
            "score": 8,
            "information_as_of": "2026-09-01",
            "description": description,
        }
        responses[question_id] = json.dumps(
            {
                "id": f"resp_{question_id}",
                "status": "completed",
                "model": base.MODEL,
                "output": [
                    {
                        "type": "web_search_call",
                        "id": f"ws_{question_id}",
                        "status": "completed",
                        "action": {
                            "type": "search",
                            "sources": [{"type": "url", "url": base.SOURCE_URL}],
                        },
                    },
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(outer, ensure_ascii=False),
                            }
                        ],
                    },
                ],
            },
            ensure_ascii=False,
        )
    (tmp_path / "stub-responses.json").write_text(
        json.dumps(responses, ensure_ascii=False), encoding="utf-8"
    )
    return files


def _child_env(tmp_path: Path, *, stub: bool) -> dict:
    env = os.environ.copy()
    for key in [key for key in list(env) if key.upper().endswith("_API_KEY")]:
        env.pop(key)
    env["E97_OWNED_ROOT"] = str(tmp_path)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(tmp_path / "guard")] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    env["QA100_STUB_HTTP"] = "1" if stub else "0"
    env["TEMP"] = env["TMP"] = env["TMPDIR"] = str(tmp_path / "temp")
    env["STOCKQA_RUN_LIVE_E2E"] = "0"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _spawn(tmp_path: Path, argv: list[str], *, stub: bool) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", "-X", "utf8", str(MAIN), *argv],
        cwd=tmp_path,
        env=_child_env(tmp_path, stub=stub),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=240,
    )


def _cold_argv(files: dict) -> list[str]:
    return [
        "--company",
        base.COMPANY,
        "--entity-id",
        base.ENTITY_ID,
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
        base.SECURITY_SCOPE_ID,
        "--c06-authority",
        str(files["authority"]),
    ]


def _plans(output: str) -> list[dict]:
    return [json.loads(line) for line in output.splitlines() if line.startswith(PLAN_PREFIX)]


def _sends(tmp_path: Path) -> list[dict]:
    path = tmp_path / "http-stub-sends.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _no_network(tmp_path: Path) -> None:
    assert not (tmp_path / "network-attempts.jsonl").exists()


def _lifecycle_snapshot(store: QuickScanWorkStore, work_ids: list[str]) -> dict:
    """Durable per-question states that a public warm run must only read."""
    return {
        work_id: {
            "attempts": store.list_attempts(work_id),
            "checkpoint": store.get_answer_checkpoint(work_id),
            "delivery": store.get_result_delivery(work_id),
        }
        for work_id in work_ids
    }


def _assert_rejected_before_key_http_budget(tmp_path: Path, files: dict, result) -> None:
    assert result.returncode == 1, result.stdout[-3000:] + result.stderr[-3000:]
    assert _sends(tmp_path) == []
    assert not (tmp_path / "key-opens.jsonl").exists(), "key file was read"
    assert not (tmp_path / "quick_scan_work.sqlite").exists(), "work/budget rows exist"
    assert not files["output"].exists()
    combined = result.stdout + result.stderr
    assert "LLMProvider initialized" not in combined
    _no_network(tmp_path)


def test_real_subprocess_cold_warm_and_seal_with_the_stub_only_at_http(tmp_path):
    files = _prepare_root(tmp_path)

    cold = _spawn(tmp_path, _cold_argv(files), stub=True)
    assert cold.returncode == 0, cold.stdout[-4000:] + cold.stderr[-3000:]
    sends = _sends(tmp_path)
    assert len(sends) == len(QUESTIONS)
    assert sorted({entry["question_id"] for entry in sends}) == sorted(QUESTIONS)
    assert all(entry["synthetic_only"] for entry in sends)
    assert (tmp_path / "key-opens.jsonl").exists()
    _no_network(tmp_path)

    cold_plans = _plans(cold.stdout)
    assert cold_plans[-1]["counts"] == {"dispatch": len(QUESTIONS)}
    assert cold_plans[-1]["model_calls_planned"] == len(QUESTIONS)
    assert files["output"].exists()

    store = QuickScanWorkStore(tmp_path / "quick_scan_work.sqlite")
    work_ids = base._work_ids(store)
    assert len(work_ids) == len(QUESTIONS)
    for work_item_id in work_ids:
        item = store.get_item(work_item_id)
        assert item["status"] == "result_ready"
        question_id = item["question_id"]
        attempts = store.list_attempts(work_item_id)
        assert len(attempts) == 1
        attempt = attempts[0]
        assert attempt["work_item_id"] == work_item_id
        assert attempt["phase"] == "response_available"
        checkpoint = store.get_answer_checkpoint(work_item_id)
        assert checkpoint is not None and checkpoint["attempt_id"] == attempt["attempt_id"]
        checkpoint_answer = checkpoint["payload"]["answer"]
        standard = store.get_standard_answer(work_item_id)
        assert standard is not None
        assert standard["answer"] == base._standard_answer(question_id)
        assert checkpoint_answer["question_id"] == question_id
        assert checkpoint_answer["description"] == standard["answer"]["summary"]
        response = store.get_attempt_response(attempt["attempt_id"])
        assert response is not None
        assert response["attempt_id"] == attempt["attempt_id"]
        assert response["model_requested"] == attempt["model_requested"]
        assert response["receipt"]["response_id"] == f"resp_{question_id}"
        refs = {(row["run_id"], row["scan_id"]) for row in store.list_run_refs(work_item_id)}
        # the owner-frozen labels and the real dispatch run are BOTH durable
        assert ("fixture-run", "fixture-scan") in refs
        assert any(run.startswith("run-") and scan == "scan-l02" for run, scan in refs)
        delivery = store.get_result_delivery(work_item_id)
        assert delivery is not None and delivery["state"] == "ready"
        observation = delivery["package"]["items"][0]["observation"]
        assert observation["question_id"] == question_id
        assert observation["execution"]["model_requested"] == attempt["model_requested"]
        assert observation["execution"]["model_resolved"] == response["model_resolved"]
        assert observation["information_cutoff"] == base.CUTOFF

    lifecycle_before_warm = _lifecycle_snapshot(store, work_ids)

    # another process with the HTTP stub WITHDRAWN still recovers, at zero HTTP
    sends_before = (tmp_path / "http-stub-sends.jsonl").read_text(encoding="utf-8")
    warm = _spawn(tmp_path, _cold_argv(files), stub=False)
    assert warm.returncode == 0, warm.stdout[-4000:] + warm.stderr[-3000:]
    warm_plans = _plans(warm.stdout)
    assert warm_plans[-1]["counts"] == {"reuse": len(QUESTIONS)}
    assert warm_plans[-1]["model_calls_planned"] == 0
    assert (tmp_path / "http-stub-sends.jsonl").read_text(encoding="utf-8") == sends_before
    assert _lifecycle_snapshot(store, work_ids) == lifecycle_before_warm
    _no_network(tmp_path)

    seal = _spawn(
        tmp_path,
        ["--seal-deliveries", "--c06-authority", str(files["authority"])],
        stub=False,
    )
    assert seal.returncode == 0, seal.stdout[-4000:] + seal.stderr[-3000:]
    report = json.loads([line for line in seal.stdout.splitlines() if line.startswith("{")][-1])
    assert report["model_calls"] == 0
    assert len(report["already_sealed"]) == len(QUESTIONS)
    assert report["sealed"] == [] and report["blocked"] == []
    assert (tmp_path / "http-stub-sends.jsonl").read_text(encoding="utf-8") == sends_before
    _no_network(tmp_path)


def test_real_subprocess_shared_recovery_restores_saved_receipt_then_pauses_unknown(
    tmp_path, monkeypatch
):
    """The public CLI restores one safe native receipt, pauses an unknown send,
    and reports never-sent work without posting again.
    """
    if os.environ.get("IQS_C06_RUN_INTEGRATION") != "1" or not os.environ.get("IQS_C06_OWN"):
        pytest.skip("requires an explicitly owned synthetic three-component runtime")

    import time

    phase_path = Path(os.environ.get("IQS_C06_PHASE_PATH", str(tmp_path / "i3-seed-phases.jsonl")))

    def phase(name, **fields):
        with phase_path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {"phase": name, "monotonic": time.monotonic(), **fields},
                    sort_keys=True,
                )
                + "\n"
            )
            stream.flush()

    from iqs_company_agent_lab import synthfixtures as sf

    from src.core.models import Question
    from src.providers import llm_research_client as sdk_module
    from src.providers.llm_client import LLMClient
    from src.runners.llm_runner import QuickScanWorkLifecycle, load_identity_snapshot
    from src.utils.quick_scan_company_api import company_agent_public_api
    from src.utils.quick_scan_owner_refresh_api import adapt_owner_refresh
    from src.utils.quick_scan_refresh_admission import RefreshAdmissionService
    from src.utils.quick_scan_refresh_execution import prepare_execution
    from src.utils.quick_scan_shared_company import (
        SharedCompanyIncomplete,
        _modules,
        original_endpoint_profiles,
        run_shared_company_scan,
    )
    from tests.integration import test_c06_subject_public_e2e as native_fixture
    from tests.integration.test_c06_refresh_admission import setup_admission
    from tests.unit.test_quick_scan_budget import _policy

    phase("native_fixture.start")
    native = native_fixture.actual.__wrapped__(tmp_path, SimpleNamespace(param="1.1.0"))
    phase("native_fixture.end")
    policy = _policy(model="mimo-v2.6-pro", max_cost_per_attempt=1)
    phase("setup_admission.start")
    prepared = setup_admission(
        native,
        tmp_path,
        policy=policy,
        refresh_version="1.6.0",
        provider="mimo",
        selected_question_count=4,
    )
    phase("setup_admission.end")
    phase("admit.start")
    accepted = RefreshAdmissionService(
        prepared["deployment_file"], process_env=dict(os.environ)
    ).admit(prepared["consent"]["confirmation_id"])
    phase("admit.end", status=accepted.get("status"))
    assert accepted["status"] == "accepted"
    items = accepted["registration"]["items"]
    qids = [item["question_id"] for item in items]
    assert len(qids) == 4
    work_ids = {item["question_id"]: item["work_item_id"] for item in items}
    entity_id = items[0]["entity_id"]
    route = policy["routes"][0]
    input_paths = {
        name: prepared["bundle"] / name
        for name in (
            "manifest.json",
            "identity.json",
            "authority.json",
            "owner-config.json",
            "questions.json",
        )
    }
    phase("prepare_execution.start")
    prepared_session = prepare_execution(
        prepared["deployment_file"],
        prepared["consent"]["confirmation_id"],
        input_paths=input_paths,
        process_env=dict(os.environ),
    )
    phase("prepare_execution.end")
    approved = adapt_owner_refresh(prepared_session)
    store = prepared_session.service.store
    owner_snapshot = approved.snapshot()
    information_cutoff = owner_snapshot.proof["request"]["source_query"]["payload"][
        "information_cutoff"
    ]
    assert isinstance(information_cutoff, str) and len(information_cutoff) == 10
    assert owner_snapshot.proof["request"]["protocol"] == "iqs.refresh_request/1.6.0"
    route_target_fields = (
        "provider",
        "model",
        "route_id",
        "provider_config_ref",
        "quota_group",
    )
    assert [
        tuple(target[field] for field in route_target_fields)
        for target in owner_snapshot.ordered_route_targets
    ] == [
        tuple(target[field] for field in route_target_fields)
        for target in owner_snapshot.proof["preview"]["ordered_route_targets"]
    ]
    assert owner_snapshot.ordered_route_targets is not None
    assert [
        (target["provider"], target["model"], target["route_id"])
        for target in owner_snapshot.ordered_route_targets
    ] == [("mimo", route["model"], route["id"])]
    assert list(owner_snapshot.entries) == qids
    assert all(owner_snapshot.entries[qid]["kind"] == "work" for qid in qids)
    assert [owner_snapshot.entries[qid]["work_item_id"] for qid in qids] == [
        work_ids[qid] for qid in qids
    ]
    identity = load_identity_snapshot(
        str(input_paths["identity.json"]), expected_entity_id=entity_id
    )
    lifecycle = QuickScanWorkLifecycle(
        store,
        entity_id=entity_id,
        run_id=owner_snapshot.record["inputs"]["run_id"],
        scan_id=owner_snapshot.record["inputs"]["scan_id"],
        identity=identity,
        transport_managed=True,
        owner_refresh_session=approved,
        c06_authority=json.loads(input_paths["authority.json"].read_text(encoding="utf-8")),
        route_id=route["id"],
        provider_name=route["provider_config_ref"],
        model_requested=route["model"],
        budget_policy=policy,
        budget_route=route,
        observation_context=owner_snapshot.observation_context,
        scope_by_question=dict(owner_snapshot.scope_bindings),
        generation_by_question={
            question_id: approved.existing_work(question_id)["generation"]
            for question_id in owner_snapshot.entries
            if owner_snapshot.entries[question_id]["kind"] == "work"
        },
    )
    client = LLMClient(
        "SYNTHETIC-NOT-A-SECRET",
        route["model"],
        "https://api.xiaomimimo.com/v1/chat/completions",
        provider_name="mimo",
    )
    seed_provider_config = tmp_path / "seed-provider-config.json"
    seed_provider_config.write_text(
        json.dumps({"providers": {route["provider_config_ref"]: {"base_url": client.base_url}}}),
        encoding="utf-8",
    )
    # Mirror the public runner's final route-binding check with a synthetic,
    # credential-free config. Direct service setup must not skip this seam.
    phase("assert_executor_policy.start")
    approved.assert_executor_policy(seed_provider_config, policy, executor_store=store)
    assert [item["id"] for item in approved.execution_routes(policy)] == [route["id"]]
    phase("assert_executor_policy.end")
    shared_state = tmp_path / "shared-state"
    shared_config_doc = {
        "schema_version": "1.0.0",
        "iqs_root": str(Path(os.environ["IQS_C06_OWN"]) / "iqs"),
        "company_agent_root": str(Path(os.environ["IQS_C06_OWN"]) / "company"),
        "runtime_bridge_root": str(Path(os.environ["IQS_C06_OWN"]) / "bridge"),
        "state_root": str(shared_state),
    }
    shared_config = tmp_path / "shared-company.json"
    shared_config.write_text(json.dumps(shared_config_doc), encoding="utf-8")
    questions = [
        Question(row["prompt"], question_id=row["id"]) for row in prepared["selected_questions"]
    ]

    posts = []
    http_session_gets = []

    from tests.unit.test_llm_client import _b01_thinking_response

    class SyntheticResponse:
        status_code = 200

        def __init__(self, question_id):
            answer = sf.gen_insufficient({"id": question_id})
            answer.update(
                status="not_applicable",
                information_as_of=information_cutoff,
                summary="[Synthetic] Runtime receipt recovery fixture.",
            )
            body = {
                "answers": [answer],
                "declared_question_ids": [question_id],
            }
            response = _b01_thinking_response(
                "mimo_chat_completions",
                route["model"],
                final=json.dumps(body, ensure_ascii=False),
            )
            self.content = json.dumps(response.json.return_value, ensure_ascii=False).encode(
                "utf-8"
            )
            self.headers = {"x-request-id": "synthetic-native-response"}
            self._payload = response.json.return_value

        def json(self):
            return self._payload

        def raise_for_status(self):
            return None

    class ScriptedWire:
        def __init__(self):
            self.calls = 0

        def post(self, *args, **kwargs):
            index = self.calls
            self.calls += 1
            request_body = kwargs["json"]
            posts.append(request_body["model"])
            phase("wire.post", index=index, model=request_body["model"])
            messages = request_body.get("messages")
            assert isinstance(messages, list) and messages
            requested = json.loads(messages[-1]["content"].splitlines()[-1])[
                "requested_question_ids"
            ]
            assert requested == [qids[index]]
            if index == 1:
                import requests

                raise requests.Timeout("synthetic original native send outcome unknown")
            if index != 0:
                raise AssertionError("the original run must stop after its unknown second send")
            return SyntheticResponse(qids[index])

    wire = ScriptedWire()
    from src.providers import llm_client as llm_client_module
    from src.utils.http_client import http_client_manager as owner_http_client_manager

    qa_root = (Path(os.environ["IQS_C06_OWN"]).resolve() / "qa").resolve()
    assert Path(sdk_module.__file__).resolve().is_relative_to(qa_root)
    assert sdk_module.http_client_manager is owner_http_client_manager
    assert llm_client_module.http_client_manager is owner_http_client_manager
    assert not any(key.upper().endswith("_API_KEY") for key in os.environ)

    def get_seed_wire():
        http_session_gets.append(sdk_module.__file__)
        phase("wire.getter", module=sdk_module.__file__)
        return wire

    monkeypatch.setattr(llm_client_module.http_client_manager, "get_sync_session", get_seed_wire)
    from src.providers.llm_client import LLMTransportAttemptError
    from src.utils.quick_scan_work_transport import QuickScanWorkUncertainError

    class SyntheticProcessStop(BaseException):
        pass

    original_send_search_request = LLMClient.send_search_request

    def stop_after_persisted_unknown(self, *args, **kwargs):
        try:
            return original_send_search_request(self, *args, **kwargs)
        except (LLMTransportAttemptError, QuickScanWorkUncertainError) as error:
            assert len(posts) == wire.calls == 2
            uncertain_attempts = store.list_attempts(work_ids[qids[1]])
            assert uncertain_attempts and uncertain_attempts[-1]["phase"] == "uncertain"
            uncertain_attempt_id = uncertain_attempts[-1]["attempt_id"]
            assert store.get_attempt_response(uncertain_attempt_id) is None
            if isinstance(error, LLMTransportAttemptError):
                assert error.failure_type == "Timeout"
                assert error.attempt_receipt["failure_type"] == "Timeout"
                assert error.attempt_receipt["attempt_id"] == uncertain_attempt_id
            else:
                assert error.attempt_id == uncertain_attempt_id
            raise SyntheticProcessStop(
                "synthetic abrupt stop after the uncertain send was persisted"
            ) from error

    monkeypatch.setattr(LLMClient, "send_search_request", stop_after_persisted_unknown)
    engine = _modules(shared_config_doc)
    original_execute = engine.execute_shared_engine
    captured_engine_outcomes = []

    def capture_engine_execute(*args, **kwargs):
        phase("shared_engine.execute.start")
        try:
            outcome = original_execute(*args, **kwargs)
        except Exception as error:
            phase("shared_engine.execute.error", error_type=type(error).__name__)
            raise
        run = outcome.get("run") if isinstance(outcome, dict) else None
        slots = run.get("slots", []) if isinstance(run, dict) else []
        summaries = []
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            summary = {"question_id": slot.get("question_id")}
            status = slot.get("execution_status")
            if isinstance(status, str):
                summary["execution_status"] = status
            for field in ("reason_code", "failure_reason", "error_code"):
                reason = slot.get(field)
                if isinstance(reason, str):
                    summary["reason"] = reason[:160]
                    break
            summaries.append(summary)
        captured_engine_outcomes.append(
            {
                "all_slots_completed": outcome.get("all_slots_completed"),
                "slots": summaries,
            }
        )
        phase(
            "shared_engine.execute.end",
            all_slots_completed=outcome.get("all_slots_completed"),
            slot_count=len(summaries),
        )
        return outcome

    monkeypatch.setattr(engine, "execute_shared_engine", capture_engine_execute)

    def synthetic_cost_resolver(receipt):
        # Settle only responses with normalized, verified usage. Unknown
        # transport outcomes retain their reservation and remain unpriced.
        if not isinstance(receipt, dict) or not isinstance(receipt.get("usage"), dict):
            return None
        return {
            "actual_cost": 0.001,
            "pricing_ref": policy["cost_policy"]["pricing_ref"],
            "source_ref": "synthetic-software-e2e",
        }

    interruption = None
    phase("run_shared_company_scan.start")
    try:
        run_shared_company_scan(
            lifecycle,
            client,
            config=shared_config_doc,
            questions=questions,
            cost_resolver=synthetic_cost_resolver,
            question_pack_size=1,
            ordered_scope=qids,
            request_scope=([work_ids[qid] for qid in qids], None),
            max_output_tokens=8192,
            transport_origin="scripted_wire",
        )
    except SyntheticProcessStop as error:
        interruption = error
    except Exception as error:
        diagnostic = {
            "exception_chain": [],
            "wire_posts": list(posts),
            "engine_outcomes": captured_engine_outcomes,
            "phase_log": str(phase_path),
        }
        current = error
        for _ in range(5):
            if current is None:
                break
            diagnostic["exception_chain"].append(
                {
                    "type": type(current).__name__,
                    "code": getattr(current, "code", None),
                    "reason": str(current)[:200],
                }
            )
            current = current.__cause__
        try:
            diagnostic_api = company_agent_public_api(
                "load_checkpoint_task",
                "load_verified_recovery",
                "recovery_event_journals",
                "event_journal_projection",
            )
            checkpoint_paths = [
                *shared_state.glob("scan-*/session-*/checkpoint.json"),
                *shared_state.glob("scan-*/export/checkpoint.json"),
            ]
            checkpoint_paths.sort(key=lambda candidate: candidate.stat().st_mtime)
            if checkpoint_paths:
                checkpoint_path = checkpoint_paths[-1]
                task = diagnostic_api.load_checkpoint_task(checkpoint_path)
                recovery = diagnostic_api.load_verified_recovery(checkpoint_path, task)
                events = []
                for _, journal in diagnostic_api.recovery_event_journals(recovery):
                    projection = diagnostic_api.event_journal_projection(journal)
                    for event in projection.get("events", []):
                        if not isinstance(event, dict):
                            continue
                        data = event.get("data")
                        data = data if isinstance(data, dict) else {}
                        reason = next(
                            (
                                data[field]
                                for field in ("reason_code", "reason", "decision", "error_code")
                                if isinstance(data.get(field), str)
                            ),
                            None,
                        )
                        events.append(
                            {
                                "event_type": event.get("event_type"),
                                "reason": reason[:160] if isinstance(reason, str) else None,
                                "question_ids": (
                                    [qid for qid in data.get("question_ids", []) if qid in qids]
                                    if isinstance(data.get("question_ids"), list)
                                    else []
                                ),
                            }
                        )
                diagnostic["latest_native_events"] = events[-12:]
        except Exception as diagnostic_error:
            diagnostic["journal_diagnostic_error"] = type(diagnostic_error).__name__
        diagnostic_path = tmp_path / "i3-seed-failure.json"
        diagnostic_path.write_text(
            json.dumps(diagnostic, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        phase(
            "run_shared_company_scan.unexpected",
            error_type=type(error).__name__,
            diagnostic=str(diagnostic_path),
        )
        raise
    else:
        pytest.fail("expected the synthetic abrupt stop after the uncertain send")
    finally:
        monkeypatch.setattr(engine, "execute_shared_engine", original_execute)
        phase(
            "run_shared_company_scan.end",
            error_type=None if interruption is not None else "unexpected_success",
        )
    assert isinstance(interruption, SyntheticProcessStop)
    assert str(interruption) == ("synthetic abrupt stop after the uncertain send was persisted")
    assert isinstance(
        interruption.__cause__, (LLMTransportAttemptError, QuickScanWorkUncertainError)
    )
    if isinstance(interruption.__cause__, LLMTransportAttemptError):
        assert interruption.__cause__.failure_type == "Timeout"
    assert http_session_gets == [sdk_module.__file__, sdk_module.__file__]
    assert posts == [
        route["model"],
        route["model"],
    ], "seed did not reach the synthetic wire; posts=" + repr(posts)

    matching = sorted(shared_state.glob("scan-*/session-*/checkpoint.json"), reverse=True)
    assert matching
    company_api = company_agent_public_api(
        "load_checkpoint_task",
        "task_snapshot",
        "load_verified_recovery",
        "recovery_projection",
        "recovery_event_journals",
        "event_journal_projection",
    )
    checkpoint = matching[0]
    original_task = company_api.load_checkpoint_task(checkpoint)
    original_task_contract = company_api.task_snapshot(original_task)
    original_recovery = company_api.load_verified_recovery(checkpoint, original_task)
    original_projection = company_api.recovery_projection(original_recovery)
    native_events = [
        event
        for _, journal in company_api.recovery_event_journals(original_recovery)
        for event in company_api.event_journal_projection(journal)["events"]
    ]
    assert original_task_contract["question_ids"] == qids
    assert original_task_contract["runtime_profile"]["model"] == route["model"]
    assert original_projection["pending_intent"]["question_ids"] == [qids[1]]
    assert original_projection["pending_receipt"] is None
    original_profiles = original_endpoint_profiles(
        checkpoint, original_task, original_task_contract
    )
    original_attempts = {qid: store.list_attempts(work_ids[qid]) for qid in qids}
    assert original_attempts[qids[0]][-1]["phase"] == "response_available"
    original_q1_response = store.get_attempt_response(original_attempts[qids[0]][-1]["attempt_id"])
    assert original_q1_response is not None
    assert store.get_answer_checkpoint(work_ids[qids[0]]) is None
    q1_native_ref = "stockqa:" + original_q1_response["receipt"]["attempt_id"]
    q1_receipt_event = next(
        event
        for event in native_events
        if event["event_type"] == "receipt_model" and event["data"]["attempt_ref"] == q1_native_ref
    )
    expected_endpoint = original_profiles[q1_native_ref]
    from src.utils.quick_scan_research_intent import (
        api_research_intent_is_endpoint_profile_id,
    )

    assert api_research_intent_is_endpoint_profile_id(expected_endpoint)
    assert original_q1_response["receipt"]["endpoint_profile_id"] == expected_endpoint
    assert q1_receipt_event["data"]["endpoint_profile_id"] == expected_endpoint
    q1_intent = next(
        event["data"]
        for event in native_events
        if event["event_type"] == "intent"
        and event["data"]["action_id"] == q1_receipt_event["data"]["action_id"]
    )
    assert q1_intent["question_ids"] == [qids[0]]
    assert any(
        event["event_type"] == "receipt_model" and event["data"]["attempt_ref"] == q1_native_ref
        for event in native_events
    )
    assert original_profiles[q1_native_ref] == expected_endpoint
    original_intents_path = tmp_path / "verified-native-intents.json"
    original_intents_path.write_text(
        json.dumps({q1_native_ref: q1_intent}, ensure_ascii=False), encoding="utf-8"
    )
    assert original_attempts[qids[1]][-1]["phase"] == "uncertain"
    assert store.get_attempt_response(original_attempts[qids[1]][-1]["attempt_id"]) is None
    assert original_attempts[qids[2]] == []
    assert original_attempts[qids[3]] == []

    # A prepared attempt with no durable send intent is still never-sent.
    prepared_lease = store.claim(work_ids[qids[3]], lease_seconds=300)
    store.prepare_attempt(
        work_ids[qids[3]],
        prepared_lease,
        route_id=route["id"],
        provider=route["provider_config_ref"],
        model_requested=route["model"],
        request_cache_key="REQ_" + hashlib.sha256(qids[3].encode("utf-8")).hexdigest(),
        prompt_sha256="f" * 64,
    )
    original_attempts[qids[3]] = store.list_attempts(work_ids[qids[3]])
    assert original_attempts[qids[3]][-1]["phase"] == "prepared"
    assert (
        store.get_attempt_transmission(original_attempts[qids[3]][-1]["attempt_id"])[
            "send_intent_at"
        ]
        is None
    )

    guard = tmp_path / "recovery-guard"
    guard.mkdir()
    recovery_audit = """
from src.utils.quick_scan_company_runtime import CompanyResearchRuntime

_supports_intent_recovery = CompanyResearchRuntime.supports_intent_recovery
_recover_intent = CompanyResearchRuntime.recover_intent
_inspect_attempt = CompanyResearchRuntime.inspect_attempt
_request_resume = CompanyResearchRuntime.request_resume
_runtime_ledger = OWNED / "runtime-recovery.jsonl"
_original_intents = json.loads(Path(os.environ["E97_ORIGINAL_NATIVE_INTENTS"]).read_text(encoding="utf-8"))

def _runtime_record(record):
    _append(_runtime_ledger, json.dumps(record, ensure_ascii=False, sort_keys=True))

def _audited_supports_intent_recovery(self):
    supported = _supports_intent_recovery(self)
    _runtime_record({
        "operation": "supports_intent_recovery",
        "task_contract_sha256": self.describe()["task_contract_sha256"],
        "supported": supported,
    })
    return supported

def _audited_recover_intent(self, intent):
    result = _recover_intent(self, intent)
    _runtime_record({
        "operation": "recover_intent",
        "task_contract_sha256": self.describe()["task_contract_sha256"],
        "intent": intent,
        "result": result,
    })
    return result

def _audited_inspect_attempt(self, attempt_ref):
    result = _inspect_attempt(self, attempt_ref)
    _runtime_record({
        "operation": "inspect_attempt",
        "task_contract_sha256": self.describe()["task_contract_sha256"],
        "attempt_ref": attempt_ref,
        "result": result,
    })
    if attempt_ref in _original_intents and result["response_saved"] is not None:
        resumed = _request_resume(self, attempt_ref)
        _runtime_record({
            "operation": "request_resume",
            "task_contract_sha256": self.describe()["task_contract_sha256"],
            "attempt_ref": attempt_ref,
            "intent": _original_intents[attempt_ref],
            "result": resumed,
        })
    return result

CompanyResearchRuntime.supports_intent_recovery = _audited_supports_intent_recovery
CompanyResearchRuntime.recover_intent = _audited_recover_intent
CompanyResearchRuntime.inspect_attempt = _audited_inspect_attempt
"""
    own = Path(os.environ["IQS_C06_OWN"]).resolve()
    # Keep the outer guard across sibling CLI calls. Add observations only to
    # the top-level recovery process without narrowing its subprocess cwd.
    observer = """
import json
import os
import sys
from pathlib import Path

OWNED = Path(os.environ["E97_OWNED_ROOT"]).resolve()
assert OWNED.is_relative_to(Path(os.environ["IQS_C06_OWN"]).resolve())

def _append(path, payload):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(payload + chr(10))

def _observe_recovery_io(event, args):
    # The outer guard denies all sockets. A Windows event loop can still
    # attempt to create its own ephemeral loopback listener at initialization.
    if event == "socket.bind" and args[1] in {("127.0.0.1", 0), ("::1", 0)}:
        return
    if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo", "socket.bind", "socket.sendto"}:
        _append(OWNED / "network-attempts.jsonl", event)
    if event == "open" and not isinstance(args[0], int):
        if Path(os.fsdecode(args[0])).name == "llm_apis.json":
            _append(OWNED / "key-opens.jsonl", "model config opened")

sys.addaudithook(_observe_recovery_io)
"""
    outer_guard = (own / "guard" / "sitecustomize.py").read_text(encoding="utf-8")
    (guard / "sitecustomize.py").write_text(
        observer + outer_guard + recovery_audit, encoding="utf-8"
    )
    env = {
        name: os.environ[name]
        for name in (
            "SystemRoot",
            "SYSTEMROOT",
            "WINDIR",
            "PATH",
            "IQS_C06_RUN_INTEGRATION",
            "IQS_C06_OWN",
            "IQS_C06_COMPOSITION_CODE_ROOT",
            "IQS_C06_COMPANY_ROOT",
            "IQS_C06_BRIDGE_ROOT",
        )
        if name in os.environ
    }
    env["E97_OWNED_ROOT"] = str(tmp_path)
    env["E97_ORIGINAL_NATIVE_INTENTS"] = str(original_intents_path)
    env["PYTHONPATH"] = os.pathsep.join(
        str(own / component)
        for component in (guard, "sw", "iqs", "qa", "iqs/scripts", "company/src", "bridge/src")
    )
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["QA100_STUB_HTTP"] = "0"
    env["PYTHONIOENCODING"] = "utf-8"
    env["TEMP"] = env["TMP"] = env["TMPDIR"] = str(tmp_path)
    output = tmp_path / "shared-recovery.json"
    bundle = prepared["bundle"]
    command = [
        sys.executable,
        "-B",
        "-X",
        "utf8",
        str(MAIN),
        "--company",
        "Synthetic E2E issuer",
        "--entity-id",
        entity_id,
        "--provider",
        "primary",
        "--config",
        str(bundle / "questions.json"),
        "--output",
        str(output),
        "--require-search",
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
        "--shared-company-config",
        str(shared_config),
        "--recover-checkpoints",
        "--seal-deliveries",
    ]
    completed = subprocess.run(
        command,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    combined = completed.stdout + completed.stderr
    assert completed.returncode == 0, combined[-6000:]
    report = json.loads(
        output.with_suffix(output.suffix + ".scope.json").read_text(encoding="utf-8")
    )
    assert report["recovery_mode"] == "received_plus_unknown"
    assert report["status"] == "paused_unknown"
    assert report["saved_question_ids"] == [qids[0]]
    assert report["unknown_question_ids"] == [qids[1]]
    assert report["unknown_intent_question_ids"] == [qids[1]]
    assert report["unknown_dispatch_receipt_question_ids"] == [qids[1]]
    assert report["never_sent_question_ids"] == qids[2:]
    assert report["dispatch_started"] is False and report["API_requests"] == 0
    network_attempts = tmp_path / "network-attempts.jsonl"
    assert not network_attempts.exists(), network_attempts.read_text(encoding="utf-8")
    assert not (tmp_path / "key-opens.jsonl").exists()
    for qid in qids:
        assert store.list_attempts(work_ids[qid]) == original_attempts[qid]
    assert store.get_answer_checkpoint(work_ids[qids[0]]) is not None
    assert store.get_answer_checkpoint(work_ids[qids[1]]) is None
    assert store.get_answer_checkpoint(work_ids[qids[2]]) is None
    assert store.get_answer_checkpoint(work_ids[qids[3]]) is None

    runtime_calls = [
        json.loads(line)
        for line in (tmp_path / "runtime-recovery.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    recoveries = [row for row in runtime_calls if row["operation"] == "recover_intent"]
    assert recoveries
    assert all(
        row["task_contract_sha256"] == original_task_contract["contract_sha256"]
        for row in recoveries
    )
    inspections = [row for row in runtime_calls if row["operation"] == "inspect_attempt"]
    q1_recovered = [
        row
        for row in inspections
        if row["attempt_ref"] == q1_native_ref
        and row["result"]["response_saved"]["endpoint_profile_id"] == expected_endpoint
    ]
    q1_resumable = [
        row
        for row in runtime_calls
        if row["operation"] == "request_resume"
        and row["intent"]["question_ids"] == [qids[0]]
        and row["result"]["decision"] == "recover_saved_receipt"
        and row["result"]["saved_receipt"]["endpoint_profile_id"] == expected_endpoint
    ]
    q2_paused = [
        row
        for row in recoveries
        if row["intent"]["question_ids"] == [qids[1]]
        and (row["result"] or {}).get("decision") == "pause_for_inspection"
        and (row["result"] or {}).get("reason") == "original_send_exists_without_replay_proof"
    ]
    assert q1_recovered and q1_resumable and q2_paused, [
        (row["intent"]["question_ids"], (row["result"] or {}).get("decision")) for row in recoveries
    ]
    assert all(row["intent"]["question_ids"] != [qids[2]] for row in recoveries)

    # Continue the same owned mixed-state database through the public IQS
    # export, StockWiki import, and StockQA ACK seams. Replays must preserve the
    # exact package, observation, native checkpoint, and delivery receipt.
    q0_work_id = work_ids[qids[0]]
    original_delivery = store.get_result_delivery(q0_work_id)
    assert original_delivery is not None and original_delivery["state"] == "ready"
    original_package = original_delivery["package"]
    original_package_bytes = original_delivery["package_bytes"]
    assert isinstance(original_package_bytes, bytes)
    original_observation = original_package["items"][0]["observation"]
    original_observation_sha256 = canonical_sha256(original_observation)
    assert (
        original_delivery["package_bytes_sha256"]
        == hashlib.sha256(original_package_bytes).hexdigest()
    )

    delivery_request = tmp_path / "i3-q0-delivery-request.json"
    delivery_request.write_text(
        json.dumps(
            {
                "entity_id": entity_id,
                "question_ids": [qids[0]],
                "scope_bindings": {qids[0]: owner_snapshot.scope_bindings[qids[0]]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    execution_bundle = {
        "entity_id": entity_id,
        "admission_config": str(prepared["deployment_file"]),
        "inspection_request": str(delivery_request),
        "manifest": str(input_paths["manifest.json"]),
        "confirmation_id": prepared["consent"]["confirmation_id"],
    }
    iqs_root = own / "iqs"
    iqs_scripts = iqs_root / "scripts"
    assert (iqs_scripts / "dashboard_execution.py").is_file()
    assert iqs_root.resolve().is_relative_to(own.resolve())
    import importlib

    added_import_paths = [
        str(candidate) for candidate in (iqs_scripts, iqs_root) if str(candidate) not in sys.path
    ]
    sys.path[:0] = added_import_paths
    try:
        dashboard_execution = importlib.import_module("dashboard_execution")
        dashboard_owner = importlib.import_module("dashboard_owner")
    finally:
        for import_path in added_import_paths:
            sys.path.remove(import_path)

    execution = dashboard_execution.ExecutionGateway(
        {
            "stockqa_root": str(qa_root),
            "stockqa_python": sys.executable,
            "iqs_root": str(iqs_root),
            "iqs_release_root": str(native[2]["iqs_release_root"]),
            "state_root": str(tmp_path / "i3-dashboard-execution-state"),
            "bundles": {"i3-q0": execution_bundle},
        }
    )
    # Use the outer isolation guard for delivery: it owns the sibling IQS
    # and StockWiki CLI roots as well as the test workspace.
    delivery_env = {
        name: os.environ[name]
        for name in ("SystemRoot", "SYSTEMROOT", "WINDIR", "PATH")
        if name in os.environ
    }
    delivery_env.update(
        PYTHONPATH=os.pathsep.join(
            str(own / component) for component in ("guard", "sw", "qa", "iqs/scripts")
        ),
        PYTHONUTF8="1",
        PYTHONDONTWRITEBYTECODE="1",
        IQS_C06_RUN_INTEGRATION="1",
        IQS_C06_OWN=str(own),
        TEMP=str(tmp_path),
        TMP=str(tmp_path),
        TMPDIR=str(tmp_path),
    )
    monkeypatch.setattr(execution, "_environment", lambda root, paid=False: dict(delivery_env))
    exported = execution.export_delivery({"bundle_id": "i3-q0"})
    assert exported["status"] == "exported"
    assert exported["API_requests"] == exported["model_calls"] == 0
    assert len(exported["import_bundles"]) == 1
    imported_bundle_id, import_bundle = next(iter(exported["import_bundles"].items()))
    assert import_bundle["work_item_ids"] == {original_observation["observation_id"]: q0_work_id}
    package_path = Path(import_bundle["package"])
    assert package_path.resolve().is_relative_to(tmp_path.resolve())
    assert package_path.read_bytes() == original_package_bytes
    assert import_bundle["package_sha256"] == original_delivery["package_bytes_sha256"]

    stockwiki_root = Path(native[2]["stockwiki_code_root"]).resolve()
    stockwiki_workspace = Path(native[2]["stockwiki_workspace_root"]).resolve()
    assert stockwiki_root.is_dir()
    assert stockwiki_workspace.is_relative_to(tmp_path.resolve())
    owner_gateway = dashboard_owner.OwnerGateway(
        {
            "stockwiki_root": str(stockwiki_root),
            "workspace_root": str(stockwiki_workspace),
            "stockwiki_python": sys.executable,
            "iqs_root": str(iqs_root),
            "iqs_release_root": str(native[2]["iqs_release_root"]),
            "bundles": {imported_bundle_id: import_bundle},
        }
    )
    monkeypatch.setattr(dashboard_owner, "_env", lambda: dict(delivery_env))
    owner_status = owner_gateway.status()
    assert owner_status["status"] == "available"
    assert owner_status["network_calls"] == owner_status["llm_calls"] == 0
    target = {
        "component": "StockWiki",
        "namespace": "quick_scan",
        "store_id": owner_status["store_id"],
    }
    bound = execution.bind_delivery_consumer(
        import_bundle, target, "synthetic-i3:" + prepared["consent"]["confirmation_id"]
    )
    assert bound["status"] == "bound" and bound["API_requests"] == 0

    preview = owner_gateway.import_preview({"bundle_id": imported_bundle_id})
    assert preview["can_apply"] is True and preview["replay"] is False
    applied = owner_gateway.import_apply(
        {"bundle_id": imported_bundle_id, "preview_token": preview["preview_token"]}
    )
    receipt = applied["receipt"]
    assert applied["status"] == "imported"
    assert receipt["summary"] == {"accepted": 1}
    ack = receipt["acks"][0]
    package_item = original_package["items"][0]
    assert ack["status"] == "accepted"
    assert ack["package_id"] == original_package["package_id"]
    assert ack["item_id"] == package_item["item_id"]
    assert ack["observation_id"] == original_observation["observation_id"]
    assert ack["payload_sha256"] == package_item["payload_sha256"]
    first_ack = execution.acknowledge_delivery(import_bundle, receipt)
    assert first_ack["status"] == "delivered" and first_ack["API_requests"] == 0
    assert store.get_result_delivery(q0_work_id)["state"] == "delivered"
    owner_ack = owner_gateway.delivery_status(receipt["package_id"])
    assert owner_ack["count"] == 1
    assert owner_ack["receipts"][0]["ack"] == ack

    def mixed_state_snapshot():
        return {
            "attempts": {qid: store.list_attempts(work_ids[qid]) for qid in qids},
            "checkpoints": {qid: store.get_answer_checkpoint(work_ids[qid]) for qid in qids},
            "deliveries": {
                qid: (
                    None
                    if store.get_result_delivery(work_ids[qid]) is None
                    else {
                        "state": store.get_result_delivery(work_ids[qid])["state"],
                        "package_sha256": store.get_result_delivery(work_ids[qid])[
                            "package_sha256"
                        ],
                        "package_bytes_sha256": store.get_result_delivery(work_ids[qid])[
                            "package_bytes_sha256"
                        ],
                        "ack_json": store.get_result_delivery(work_ids[qid])["ack_json"],
                        "ack_sha256": store.get_result_delivery(work_ids[qid])["ack_sha256"],
                        "consumer_store_id": store.get_result_delivery(work_ids[qid])[
                            "consumer_store_id"
                        ],
                    }
                )
                for qid in qids
            },
        }

    post_ack_snapshot = mixed_state_snapshot()
    assert post_ack_snapshot["attempts"] == original_attempts
    assert post_ack_snapshot["checkpoints"][qids[0]] is not None
    assert all(post_ack_snapshot["checkpoints"][qid] is None for qid in qids[1:])

    warm_output = tmp_path / "shared-recovery-after-ack.json"
    warm_command = list(command)
    warm_command[warm_command.index("--output") + 1] = str(warm_output)
    warm = subprocess.run(
        warm_command,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    combined = warm.stdout + warm.stderr
    assert warm.returncode == 0, combined[-6000:]
    warm_report = json.loads(
        warm_output.with_suffix(warm_output.suffix + ".scope.json").read_text(encoding="utf-8")
    )
    for field in (
        "recovery_mode",
        "status",
        "saved_question_ids",
        "unknown_question_ids",
        "unknown_intent_question_ids",
        "unknown_dispatch_receipt_question_ids",
        "never_sent_question_ids",
        "dispatch_started",
        "API_requests",
    ):
        assert warm_report[field] == report[field]
    assert warm_report["status"] == "paused_unknown"
    assert warm_report["never_sent_question_ids"] == qids[2:]
    assert posts == [route["model"], route["model"]]
    network_attempts = tmp_path / "network-attempts.jsonl"
    assert not network_attempts.exists(), network_attempts.read_text(encoding="utf-8")
    assert not (tmp_path / "key-opens.jsonl").exists()
    assert mixed_state_snapshot() == post_ack_snapshot

    replay_preview = owner_gateway.import_preview({"bundle_id": imported_bundle_id})
    assert replay_preview["replay"] is True
    assert all(item["exact_ack_replay"] is True for item in replay_preview["items"])
    replayed = owner_gateway.import_apply(
        {
            "bundle_id": imported_bundle_id,
            "preview_token": replay_preview["preview_token"],
        }
    )
    assert replayed["receipt"] == receipt
    replay_ack = execution.acknowledge_delivery(import_bundle, replayed["receipt"])
    assert replay_ack["status"] == "delivered"
    assert (
        store.get_result_delivery(q0_work_id)["ack_json"]
        == post_ack_snapshot["deliveries"][qids[0]]["ack_json"]
    )
    assert mixed_state_snapshot() == post_ack_snapshot
    assert package_path.read_bytes() == original_package_bytes
    final_delivery = store.get_result_delivery(q0_work_id)
    assert final_delivery["package"] == original_package
    assert final_delivery["package"]["items"][0]["observation"] == original_observation
    assert (
        canonical_sha256(final_delivery["package"]["items"][0]["observation"])
        == original_observation_sha256
    )
    assert (
        hashlib.sha256(package_path.read_bytes()).hexdigest()
        == final_delivery["package_bytes_sha256"]
    )
    final_owner_ack = owner_gateway.delivery_status(receipt["package_id"])
    assert final_owner_ack["receipts"][0]["ack"] == ack
    assert not (tmp_path / "network-attempts.jsonl").exists()


def test_shared_recovery_report_state_classifies_never_sent_only():
    from src.runners.llm_runner import shared_recovery_report_state

    assert shared_recovery_report_state([], [], ["Q1", "Q2"], ["Q1", "Q2"]) == {
        "recovery_mode": "never_sent_only",
        "status": "partial",
        "pause_reason": "original_scope_never_sent",
    }


def test_real_subprocess_wrong_template_metadata_is_rejected_before_key_http(tmp_path):
    files = _prepare_root(tmp_path)
    document = json.loads(files["authority"].read_text(encoding="utf-8"))
    document["observation_context"]["questions"][QUESTIONS[0]]["metadata"][
        "template_version"
    ] = "99.0.0"
    document["observation_context_sha256"] = canonical_sha256(document["observation_context"])
    files["authority"].write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    result = _spawn(tmp_path, _cold_argv(files), stub=True)
    _assert_rejected_before_key_http_budget(tmp_path, files, result)


def test_real_subprocess_duplicate_authority_json_key_is_rejected_before_key_http(
    tmp_path,
):
    files = _prepare_root(tmp_path)
    raw = (
        files["authority"]
        .read_text(encoding="utf-8")
        .replace(
            '"schema_version": "2.0.0"',
            '"schema_version": "1.0.0", "schema_version": "2.0.0"',
            1,
        )
    )
    files["authority"].write_text(raw, encoding="utf-8")
    result = _spawn(tmp_path, _cold_argv(files), stub=True)
    _assert_rejected_before_key_http_budget(tmp_path, files, result)


def test_real_subprocess_corrupt_standard_body_is_rejected_without_reasking(tmp_path):
    corrupt = QUESTIONS[0]
    files = _prepare_root(tmp_path, corrupt_question=corrupt)
    result = _spawn(tmp_path, _cold_argv(files), stub=True)
    assert result.returncode == 1, result.stdout[-4000:] + result.stderr[-3000:]
    _no_network(tmp_path)

    sends = _sends(tmp_path)
    counts: dict[str, int] = {}
    for entry in sends:
        counts[entry["question_id"]] = counts.get(entry["question_id"], 0) + 1
    assert counts == {question_id: 1 for question_id in QUESTIONS}, "a question was re-asked"

    store = QuickScanWorkStore(tmp_path / "quick_scan_work.sqlite")
    grouped = store.find_work_items(entity_id=base.ENTITY_ID, question_ids=[corrupt])
    rejected_id = grouped[corrupt][0]["work_item_id"]
    # Complete C06 refuses the unusable body before saving any checkpoint.
    # The physical request remains recoverable without a false ready item,
    # compact fallback checkpoint, newly created outbox head or repeat POST.
    assert store.get_answer_checkpoint(rejected_id) is None
    assert store.get_standard_answer(rejected_id) is None
    assert store.get_result_delivery(rejected_id) is None
    assert store.get_item(rejected_id)["status"] != "result_ready"

    work_ids = base._work_ids(store)
    healthy = [work_item_id for work_item_id in work_ids if work_item_id != rejected_id]
    assert len(healthy) == len(QUESTIONS) - 1
    for work_item_id in healthy:
        healthy_checkpoint = store.get_answer_checkpoint(work_item_id)
        assert healthy_checkpoint is not None
        assert healthy_checkpoint["payload"]["answer"]["status"] == "scored"
        assert store.get_item(work_item_id)["status"] == "result_ready"
        healthy_delivery = store.get_result_delivery(work_item_id)
        assert healthy_delivery is not None and healthy_delivery["state"] == "ready"


def test_q10_real_subprocess_alias_cold_then_independent_warm_and_seal(tmp_path):
    files = _prepare_root(tmp_path)
    config_path = tmp_path / "llm_apis.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    resolved = "synthetic-resolved-v1"
    config["quick_scan_model_resolution"] = {
        "schema_version": "1.0.0",
        "aliases": [
            {
                "provider": "openai",
                "protocol": "responses",
                "requested_model": base.MODEL,
                "resolved_model": resolved,
            }
        ],
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")
    response_path = tmp_path / "stub-responses.json"
    responses = json.loads(response_path.read_text(encoding="utf-8"))
    for question_id, raw in list(responses.items()):
        payload = json.loads(raw)
        payload["model"] = resolved
        responses[question_id] = json.dumps(payload)
    response_path.write_text(json.dumps(responses), encoding="utf-8")
    cold = _spawn(tmp_path, _cold_argv(files), stub=True)
    assert cold.returncode == 0, cold.stdout[-4000:] + cold.stderr[-3000:]
    store = QuickScanWorkStore(tmp_path / "quick_scan_work.sqlite")
    work_ids = base._work_ids(store)
    assert len(work_ids) == len(QUESTIONS)
    packages = {}
    for work_id in work_ids:
        checkpoint = store.get_answer_checkpoint(work_id)
        receipt = store.get_attempt_response(checkpoint["attempt_id"])
        assert receipt["model_requested"] == base.MODEL
        assert receipt["model_resolved"] == resolved
        delivery = store.get_result_delivery(work_id)
        execution = delivery["package"]["items"][0]["observation"]["execution"]
        assert execution["model_requested"] == base.MODEL
        assert execution["model_resolved"] == resolved
        packages[work_id] = delivery["package_bytes"]
    before = (tmp_path / "http-stub-sends.jsonl").read_bytes()
    assert all(item["model_requested"] == base.MODEL for item in _sends(tmp_path))
    warm = _spawn(tmp_path, _cold_argv(files), stub=False)
    assert warm.returncode == 0, warm.stdout[-4000:] + warm.stderr[-3000:]
    assert _plans(warm.stdout)[-1]["model_calls_planned"] == 0
    # Seal reconstructs the original response without reading current aliases.
    config.pop("quick_scan_model_resolution")
    config_path.write_text(json.dumps(config), encoding="utf-8")
    seal = _spawn(
        tmp_path, ["--seal-deliveries", "--c06-authority", str(files["authority"])], stub=False
    )
    assert seal.returncode == 0, seal.stdout[-4000:] + seal.stderr[-3000:]
    assert (tmp_path / "http-stub-sends.jsonl").read_bytes() == before
    for work_id in work_ids:
        assert store.get_result_delivery(work_id)["package_bytes"] == packages[work_id]
    _no_network(tmp_path)
