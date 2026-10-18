"""Native single-work continuation fences; synthetic registrations, real SQLite.

The SQL fixture deliberately tests store guards below owner registration. It
does not claim public owner admission; that is tested by the owner adapter.
"""

import json
import sqlite3

import pytest

from src.providers.llm_research_client import ResearchChatSession
from src.providers.model_resolution import model_resolution_sha256
from src.utils.quick_scan_research_session import _encode, _sha, bind, register
from src.utils.quick_scan_work_store import (
    LeaseFencedError,
    QuickScanWorkStore,
    WorkConflictError,
)
from src.utils.quick_scan_work_transport import (
    QuickScanWorkPersistenceError,
    bind_quick_scan_work,
)
from tests.unit.test_llm_research_client import (
    TOOLS,
    budget,
    client,
    deny_network,
    response,
    transport,
)
from tests.unit.test_quick_scan_question_pack_store import _members


def seed(store, member, *, cap=3):
    registration = dict(
        protocol="stockqa.native-research-session/1.0.0",
        work_item_id=member["work_item_id"],
        task_sha256="a" * 64,
        owner_binding_sha256="b" * 64,
        observation_context_sha256="c" * 64,
        route_id="research",
        provider="deepseek",
        model_requested="deepseek-flash",
        model_resolution_sha256=model_resolution_sha256(None),
        max_turns=cap,
    )
    with store._transaction() as connection:
        connection.execute(
            "INSERT INTO quick_scan_research_session VALUES (?,?,?,?,?,?,?)",
            (
                "RSESSION_test",
                member["work_item_id"],
                member["lease"].lease_epoch,
                member["lease"].lease_token,
                _encode(registration),
                _sha(registration),
                store._now(),
            ),
        )
    return "RSESSION_test"


def test_actual_native_work_sdk_two_turns_and_zero_post_reopen(tmp_path, transport):
    wire, sent = transport
    wire.payloads = [response(tools=True), response()]
    with budget(tmp_path) as store:
        member = _members(store, count=1)[0]
        sid = seed(store, member)
        session = ResearchChatSession(client(), system_prefix="stable", tools=TOOLS)
        with bind_quick_scan_work(store, member["work_item_id"], member["lease"]), bind(
            store, member["work_item_id"], member["lease"], sid, intent_sha256="1" * 64
        ):
            first = session.step(user_context="first")
        with bind_quick_scan_work(store, member["work_item_id"], member["lease"]), bind(
            store, member["work_item_id"], member["lease"], sid, intent_sha256="2" * 64
        ):
            second = session.step(tool_results={first["tool_calls"][0]["id"]: "observed result"})
        attempts = store.list_attempts(member["work_item_id"])
        assert len(attempts) == 2 and all(a["phase"] == "response_available" for a in attempts)
        assert store.get_quick_scan_budget_status("research-test")["requests"] == 2
        reopened = QuickScanWorkStore(store.path)
        assert (
            reopened.get_research_visible_output(second["receipt"]["attempt_id"])["visible_output"][
                "content"
            ]
            == second["content"]
        )
        from src.utils.quick_scan_research_session import find_attempt_for_intent

        link = find_attempt_for_intent(reopened, sid, "2" * 64)
        assert link["attempt"]["attempt_id"] == attempts[1]["attempt_id"]
        assert link["visible_output"] == reopened.get_research_visible_output(
            second["receipt"]["attempt_id"]
        )
        assert find_attempt_for_intent(reopened, sid, "3" * 64) is None
        with sqlite3.connect(store.path) as connection:
            assert (
                connection.execute("SELECT count(*) FROM quick_scan_research_turn").fetchone()[0]
                == 2
            )
    assert len(sent) == 2


def test_legacy_single_work_still_refuses_second_generation(tmp_path, transport):
    wire, sent = transport
    wire.payloads = [response(), response()]
    with budget(tmp_path) as store:
        member = _members(store, count=1)[0]
        with bind_quick_scan_work(store, member["work_item_id"], member["lease"]):
            ResearchChatSession(client(), system_prefix="stable", tools=TOOLS).step(
                user_context="one"
            )
            with pytest.raises(QuickScanWorkPersistenceError):
                ResearchChatSession(client(), system_prefix="stable", tools=TOOLS).step(
                    user_context="two"
                )
    assert len(sent) == 1


@pytest.mark.parametrize(
    "kind",
    ["cap", "same_request", "same_intent", "unregistered", "foreign_work", "unknown", "resolution"],
)
def test_registered_continuation_cannot_become_a_retry_bypass(tmp_path, transport, kind):
    wire, sent = transport
    wire.payloads = [response(), response()]
    with budget(tmp_path) as store:
        member = _members(store, count=1)[0]
        sid = seed(store, member, cap=1 if kind == "cap" else 3)
        with bind_quick_scan_work(store, member["work_item_id"], member["lease"]), bind(
            store, member["work_item_id"], member["lease"], sid, intent_sha256="1" * 64
        ):
            first = ResearchChatSession(client(), system_prefix="stable", tools=TOOLS).step(
                user_context="one"
            )
        attempt = store.list_attempts(member["work_item_id"])[0]
        kwargs = dict(
            route_id="research",
            provider="deepseek",
            model_requested="deepseek-flash",
            request_cache_key="REQ_" + "d" * 64,
            prompt_sha256="e" * 64,
            research_session_id=sid,
            research_intent_sha256="2" * 64,
        )
        if kind == "same_request":
            kwargs["request_cache_key"] = attempt["request_cache_key"]
        if kind == "same_intent":
            kwargs["research_intent_sha256"] = "1" * 64
        if kind == "unregistered":
            kwargs["research_session_id"] = "RSESSION_missing"
        if kind == "resolution":
            kwargs["route_id"] = "other-route"
        if kind == "foreign_work":
            kwargs["research_session_id"] = "RSESSION_test"
            # The same original DB may hold another company; registration is not transferable.
            from tests.unit.test_quick_scan_work_store import _created

            foreign = _created(store, entity_id="ENT_OTHER", scope_id="ENT_OTHER")
            otherlease = store.claim(foreign["work_item_id"], lease_seconds=600)
            with pytest.raises(WorkConflictError):
                store.prepare_attempt(foreign["work_item_id"], otherlease, **kwargs)
            return
        if kind == "unknown":
            with store._transaction() as connection:
                connection.execute(
                    "UPDATE attempt SET phase='uncertain' WHERE attempt_id=?",
                    (attempt["attempt_id"],),
                )
        with pytest.raises(WorkConflictError):
            store.prepare_attempt(member["work_item_id"], member["lease"], **kwargs)
    assert len(sent) == 1


def test_registration_refuses_boolean_owner_authority(tmp_path):
    store = QuickScanWorkStore(tmp_path / "db.sqlite")
    member = _members(store, count=1)[0]
    with pytest.raises(WorkConflictError, match="actual_owner_required"):
        register(
            store,
            {"authorized": True},
            member["work_item_id"],
            member["lease"],
            task_sha256="a" * 64,
            route_id="research",
            provider="deepseek",
            model_requested="deepseek-flash",
        )


def test_original_lease_expiry_fence_is_not_relaxed(tmp_path, transport):
    wire, sent = transport
    wire.payloads = [response()]
    with budget(tmp_path) as store:
        member = _members(store, count=1)[0]
        sid = seed(store, member)
        store._now = lambda: 4_000_000_000
        with pytest.raises(LeaseFencedError):
            store.prepare_attempt(
                member["work_item_id"],
                member["lease"],
                route_id="research",
                provider="deepseek",
                model_requested="deepseek-flash",
                request_cache_key="REQ_" + "d" * 64,
                prompt_sha256="e" * 64,
                research_session_id=sid,
                research_intent_sha256="2" * 64,
            )
    assert not sent


def test_readonly_owner_preview_borrows_original_budget_snapshot(tmp_path):
    from contextlib import closing

    from src.utils.quick_scan_refresh_inspect import ReadOnlyWorkView

    with budget(tmp_path) as store:
        expected = store.get_quick_scan_budget_status("research-test")
        view = ReadOnlyWorkView(store.path)
        with closing(view._connect()) as connection:
            connection.execute("BEGIN")
            view._inspection_connection = connection
            try:
                assert view.get_quick_scan_budget_status("research-test") == expected
                assert connection.in_transaction
                with pytest.raises(sqlite3.OperationalError):
                    connection.execute("DELETE FROM quick_scan_budget_policy")
            finally:
                view._inspection_connection = None


def test_original_intent_join_survives_os_restart_without_network(tmp_path, transport):
    import os
    import subprocess
    import sys
    from pathlib import Path

    wire, sent = transport
    wire.payloads = [response()]
    with budget(tmp_path) as store:
        member = _members(store, count=1)[0]
        sid = seed(store, member)
        with bind_quick_scan_work(store, member["work_item_id"], member["lease"]), bind(
            store, member["work_item_id"], member["lease"], sid, intent_sha256="1" * 64
        ):
            result = ResearchChatSession(client(), system_prefix="stable", tools=TOOLS).step(
                user_context="one"
            )
    code = """import json,socket,sys
from src.utils.quick_scan_work_store import QuickScanWorkStore
from src.utils.quick_scan_research_session import find_attempt_for_task_intent
socket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError("network forbidden"))
socket.create_connection=lambda *a,**k: (_ for _ in ()).throw(AssertionError("network forbidden"))
record=find_attempt_for_task_intent(QuickScanWorkStore(sys.argv[1]),sys.argv[2],"a"*64,"1"*64)
assert record and record["visible_output"]
assert "lease_token" not in record["attempt"]
print(json.dumps(record))
"""
    # Child recovery needs no credentials. A failing pytest assertion must
    # never capture the parent process's secret-bearing environment.
    env = {
        key: os.environ[key]
        for key in ("SystemRoot", "WINDIR", "TEMP", "TMP", "PATH")
        if key in os.environ
    }
    env.update(
        PYTHONPATH=str(Path(__file__).resolve().parents[2]),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUTF8="1",
    )
    completed = subprocess.run(
        [sys.executable, "-B", "-c", code, str(store.path), member["work_item_id"]],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf8",
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    restored = json.loads(completed.stdout)
    assert restored["visible_output"]["provider_attempt_id"] == result["receipt"]["attempt_id"]
    assert len(sent) == 1


def test_readonly_v22_remains_supported_without_migration(tmp_path):
    import hashlib
    import re
    from contextlib import closing

    from src.utils.quick_scan_refresh_inspect import ReadOnlyWorkView
    from src.utils.quick_scan_research_output import DDL_V23_ADDITIONS

    with budget(tmp_path) as store:
        expected = store.get_quick_scan_budget_status("research-test")
    with sqlite3.connect(store.path) as connection:
        for statement in reversed(DDL_V23_ADDITIONS):
            kind, name = re.match(r"CREATE (TABLE|TRIGGER) ([A-Za-z_]+)", statement).groups()
            connection.execute("DROP " + kind + " " + name)
        connection.execute("PRAGMA user_version=22")
    before = hashlib.sha256(store.path.read_bytes()).hexdigest()
    view = ReadOnlyWorkView(store.path)
    with closing(view._connect()) as connection:
        connection.execute("BEGIN")
        view._inspection_connection = connection
        try:
            assert view.get_quick_scan_budget_status("research-test") == expected
        finally:
            view._inspection_connection = None
    assert hashlib.sha256(store.path.read_bytes()).hexdigest() == before
