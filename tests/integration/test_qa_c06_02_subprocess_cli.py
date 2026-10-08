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

import json
import os
import subprocess
import sys
from pathlib import Path

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
        elif Path(os.fsdecode(args[0])).name == "llm_apis.json":
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
            json.dumps({"question_id": question_id, "synthetic_only": True}),
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
        assert store.get_item(work_item_id)["status"] == "result_ready"
        refs = {(row["run_id"], row["scan_id"]) for row in store.list_run_refs(work_item_id)}
        # the owner-frozen labels and the real dispatch run are BOTH durable
        assert ("fixture-run", "fixture-scan") in refs
        assert any(run.startswith("run-") and scan == "scan-l02" for run, scan in refs)
        delivery = store.get_result_delivery(work_item_id)
        assert delivery is not None and delivery["state"] == "ready"
        observation = delivery["package"]["items"][0]["observation"]
        assert observation["information_cutoff"] == base.CUTOFF
        assert store.get_standard_answer(work_item_id) is not None

    # another process with the HTTP stub WITHDRAWN still recovers, at zero HTTP
    sends_before = (tmp_path / "http-stub-sends.jsonl").read_text(encoding="utf-8")
    warm = _spawn(tmp_path, _cold_argv(files), stub=False)
    assert warm.returncode == 0, warm.stdout[-4000:] + warm.stderr[-3000:]
    warm_plans = _plans(warm.stdout)
    assert warm_plans[-1]["counts"] == {"reuse": len(QUESTIONS)}
    assert warm_plans[-1]["model_calls_planned"] == 0
    assert (tmp_path / "http-stub-sends.jsonl").read_text(encoding="utf-8") == sends_before
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
    # The ambiguous body is refused end to end: the provider falls back to an
    # honest-unknown checkpoint (NO score, NO durable standard answer) and the
    # v2 seal durably BLOCKS it — never a scored observation, never a re-ask.
    checkpoint = store.get_answer_checkpoint(rejected_id)
    assert checkpoint is not None
    assert checkpoint["payload"]["answer"]["status"] == "unknown"
    assert checkpoint["payload"]["answer"]["score"] is None
    assert "ambiguous summary" not in checkpoint["payload"]["answer"]["description"]
    assert store.get_standard_answer(rejected_id) is None
    delivery = store.get_result_delivery(rejected_id)
    assert delivery is not None and delivery["state"] == "blocked"
    assert delivery["block_code"] == "c06_standard_answer_unavailable"

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
