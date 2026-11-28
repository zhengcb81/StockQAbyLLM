"""Run the public StockQA command through native TCP and HTTP on loopback."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_TOKEN = "offline-stockqa-native-http-fixture"
FIXTURE_ANSWER = "Synthetic response from the actual HTTP exchange"
SYSTEM_VARIABLES = {
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "SYSTEMDRIVE",
    "COMSPEC",
    "TEMP",
    "TMP",
    "TMPDIR",
}


def test_public_cli_reads_config_and_posts_once_to_loopback(tmp_path):
    calls = []

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, self.headers.get("Authorization"), request))
            answer = json.dumps({"score": 8, "description": FIXTURE_ANSWER})
            body = json.dumps({"choices": [{"message": {"content": answer}}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    questions = {
        "categories": [
            {
                "category": "quality",
                "questions": [{"question_id": "CORE_01", "text": "What did the fixture prove?"}],
            }
        ]
    }
    (tmp_path / "questions.json").write_text(json.dumps(questions), encoding="utf-8")
    env = {name: value for name, value in os.environ.items() if name.upper() in SYSTEM_VARIABLES}
    env.update(
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
        NO_PROXY="*",
        RESEARCH_FEEDBACK_DIR=str(tmp_path / "feedback"),
        STOCKQA_RUN_LIVE_E2E="0",
        HOME=str(tmp_path),
        USERPROFILE=str(tmp_path),
    )
    with ThreadingHTTPServer(("127.0.0.1", 0), Provider) as server:
        config = {
            "default_provider": "fixture",
            "providers": {
                "fixture": {
                    "enabled": True,
                    "api_key": FIXTURE_TOKEN,
                    "model": "s6-loopback",
                    "base_url": f"http://127.0.0.1:{server.server_port}/chat/completions",
                    "max_retries": 1,
                }
            },
        }
        (tmp_path / "llm_apis.json").write_text(json.dumps(config), encoding="utf-8")
        worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        worker.start()
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(ROOT / "main_with_llm.py"),
                    "--company",
                    "Fixture Company",
                    "--provider",
                    "fixture",
                    "--config",
                    "questions.json",
                    "--output",
                    "output.json",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                stdin=subprocess.DEVNULL,
                cwd=tmp_path,
                env=env,
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        finally:
            server.shutdown()
            worker.join(timeout=5)
            assert not worker.is_alive()

    assert result.returncode == 0, result.stderr
    ((path, authorization, body),) = calls
    assert path == "/chat/completions"
    assert authorization == f"Bearer {FIXTURE_TOKEN}"
    assert body["model"] == "s6-loopback"
    assert "What did the fixture prove?" in body["messages"][-1]["content"]
    report = (tmp_path / "output.json").read_text(encoding="utf-8")
    assert FIXTURE_ANSWER in report
    assert FIXTURE_TOKEN not in report + result.stdout + result.stderr
