"""Original native state, isolated copies; injected authority is synthetic only."""

import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("IQS_SAVED_PUBLICATION_ROOT") is None,
    reason="owned original-software fixture only",
)


@pytest.fixture
def owned_store(tmp_path):
    from src.utils.quick_scan_work_store import QuickScanWorkStore

    root = Path(os.environ["IQS_SAVED_PUBLICATION_ROOT"])
    path = tmp_path / "native.sqlite"
    shutil.copyfile(root / "fixtures/actual102-uncertain.sqlite", path)
    return QuickScanWorkStore(path)


def test_original24_migrates_without_rewriting_historical_rows(owned_store):
    from src.utils.quick_scan_work_store import SCHEMA_VERSION

    assert SCHEMA_VERSION == 25
    with closing(owned_store._connect()) as db:
        policy_id = db.execute("SELECT policy_id FROM quick_scan_budget_policy").fetchone()[0]
    assert owned_store.get_quick_scan_budget_status(policy_id)["requests"] == 102
    with closing(owned_store._connect()) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 25
        assert c.execute("SELECT COUNT(*) FROM answer_checkpoint").fetchone()[0] == 0


def test_derive_requires_original_saved_native_and_no_caller_answer(owned_store):
    from src.utils.quick_scan_saved_research_publication import derive_publication

    root = Path(os.environ["IQS_SAVED_PUBLICATION_ROOT"]).parent
    bundle = (root / "actual-native-catl-profile-01/cold-session/task-bundle.json").read_bytes()
    value = derive_publication(
        owned_store,
        bundle,
        sdk_id="4d46a825-728f-4372-8947-26ec3737ff3f",
        idempotency_key="publish-test",
    )
    assert value["request"]["kind"] == "publish_saved_research"
    assert value["request"]["purpose"] == "research_no_verified_search"
    assert [r["question_id"] for r in value["request"]["publication"]["members"]] == [
        "IQS_12",
        "IQS_16",
    ]
    assert all(r["standard_answer"]["score"] is None for r in value["members"])
    with pytest.raises(Exception):
        derive_publication(owned_store, bundle, sdk_id="foreign", idempotency_key="publish-test")


def source_inputs():
    from src.utils.quick_scan_c06_authority import load_c06_authority

    root = Path(os.environ["IQS_SAVED_PUBLICATION_ROOT"]).parent / "actual-native-catl-profile-01"
    config = json.loads((root / "run-config.json").read_bytes())
    return (root / "cold-session/task-bundle.json").read_bytes(), load_c06_authority(
        Path(config["bundle"]) / "authority.json"
    )


def synthetic_authority():
    from datetime import datetime, timezone

    from src.utils.quick_scan_known_outcome_journal import (
        OwnerWorkDecisionAuthority,
        OwnerWorkDecisionGrant,
    )
    from src.utils.quick_scan_result_outbox import canonical_sha256

    class Synthetic(OwnerWorkDecisionAuthority):
        revoked = False
        calls = 0

        def authorize(self, request):
            self.calls += 1
            if self.revoked:
                raise ValueError("synthetic_current_permission_revoked")
            return OwnerWorkDecisionGrant(
                owner_store_id="synthetic-owner",
                principal_id="synthetic-principal",
                confirmation_id="rc1_" + "a" * 64,
                action_id=request["idempotency_key"],
                request_sha256=canonical_sha256(request),
                confirmed_at="2026-10-18T00:00:00Z",
            )

    return Synthetic()


def test_two_uncertain_members_publish_and_seal_atomically_without_changing_budget(
    owned_store,
):
    from src.utils.quick_scan_saved_research_publication import publish_saved_research

    bundle, authority = source_inputs()
    owner = synthetic_authority()
    with closing(owned_store._connect()) as c:
        original = [
            tuple(row)
            for row in c.execute(
                "SELECT * FROM quick_scan_budget_attempt ORDER BY budget_attempt_id"
            )
        ]
        work = [
            tuple(row)
            for row in c.execute(
                "SELECT * FROM work_item WHERE status='leased' ORDER BY work_item_id"
            )
        ]
    result = publish_saved_research(
        owned_store,
        bundle,
        sdk_id="4d46a825-728f-4372-8947-26ec3737ff3f",
        idempotency_key="publish-test",
        owner_authority=owner,
        c06_authority=authority,
    )
    assert len(result["work_item_ids"]) == len(result["package_ids"]) == 2
    assert result["formal_search_checkpoint_created"] is False
    for wid in result["work_item_ids"]:
        cp = owned_store.get_answer_checkpoint(wid, allow_research=True)
        with pytest.raises(Exception):
            owned_store.get_answer_checkpoint(wid)
        assert cp["checkpoint_schema_version"] == 3
        assert (
            cp["payload"]["answer"]["score"] is None
            and cp["payload"]["provenance"]["search_status"] == "unverified"
        )
        assert cp["payload"]["provenance"]["search_receipt_id"] is None
        assert owned_store.get_item(wid)["status"] == "result_ready"
        assert owned_store.get_result_delivery(wid)["state"] == "ready"
    again = publish_saved_research(
        owned_store,
        bundle,
        sdk_id="4d46a825-728f-4372-8947-26ec3737ff3f",
        idempotency_key="publish-test",
        owner_authority=owner,
        c06_authority=authority,
    )
    assert again == result
    with closing(owned_store._connect()) as c:
        assert [
            tuple(row)
            for row in c.execute(
                "SELECT * FROM quick_scan_budget_attempt ORDER BY budget_attempt_id"
            )
        ] == original
        assert [
            tuple(row)
            for row in c.execute(
                "SELECT * FROM work_item WHERE status='leased' ORDER BY work_item_id"
            )
        ] == work
        assert c.execute("SELECT COUNT(*) FROM answer_checkpoint").fetchone()[0] == 2
    owner.revoked = True
    with pytest.raises(ValueError):
        publish_saved_research(
            owned_store,
            bundle,
            sdk_id="4d46a825-728f-4372-8947-26ec3737ff3f",
            idempotency_key="publish-test",
            owner_authority=owner,
            c06_authority=authority,
        )


def test_consumer_dictionary_never_authorizes_native_publication(owned_store):
    from src.utils.quick_scan_saved_research_publication import publish_saved_research

    bundle, authority = source_inputs()
    with pytest.raises(TypeError):
        publish_saved_research(
            owned_store,
            bundle,
            sdk_id="4d46a825-728f-4372-8947-26ec3737ff3f",
            idempotency_key="publish-test",
            owner_authority={"approved": True},
            c06_authority=authority,
        )
    with closing(owned_store._connect()) as c:
        assert c.execute("SELECT COUNT(*) FROM answer_checkpoint").fetchone()[0] == 0
        assert (
            c.execute("SELECT COUNT(*) FROM work_item WHERE status='uncertain'").fetchone()[0] == 2
        )


def test_original24_readonly_derive_does_not_migrate_or_change_bytes():
    from src.utils.quick_scan_refresh_inspect import (
        READ_ONLY_SCHEMA_VERSIONS,
        ReadOnlyWorkView,
    )
    from src.utils.quick_scan_saved_research_publication import derive_publication

    root = Path(os.environ["IQS_SAVED_PUBLICATION_ROOT"])
    path = root / "fixtures/actual102-uncertain.sqlite"
    before = path.read_bytes()
    assert 24 in READ_ONLY_SCHEMA_VERSIONS
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        assert db.execute("PRAGMA user_version").fetchone()[0] == 24
    bundle, _ = source_inputs()
    result = derive_publication(
        ReadOnlyWorkView(path),
        bundle,
        sdk_id="4d46a825-728f-4372-8947-26ec3737ff3f",
        idempotency_key="readonly24-test",
    )
    assert len(result["members"]) == 2
    assert path.read_bytes() == before
    assert not any(path.with_name(path.name + s).exists() for s in ("-wal", "-shm", "-journal"))


def test_preauthorized_grant_must_equal_the_stored_publication_grant(owned_store):
    from src.utils.quick_scan_saved_research_publication import (
        publish_saved_research,
        require_publisher,
    )

    bundle, authority = source_inputs()
    owner = synthetic_authority()
    result = publish_saved_research(
        owned_store,
        bundle,
        sdk_id="4d46a825-728f-4372-8947-26ec3737ff3f",
        idempotency_key="publish-preauthorized",
        owner_authority=owner,
        c06_authority=authority,
    )
    work_item_id = result["work_item_ids"][0]
    with closing(owned_store._connect()) as connection:
        connection.execute("BEGIN")
        try:
            with pytest.raises(Exception, match="current_publisher_authority_changed"):
                require_publisher(
                    owned_store,
                    connection,
                    work_item_id,
                    owner,
                    preauthorized_grant={"confirmation_id": "rc1_" + "c" * 64},
                )
        finally:
            connection.rollback()


def test_require_publisher_without_a_checkpoint_returns_none(owned_store):
    from src.utils.quick_scan_saved_research_publication import require_publisher

    with closing(owned_store._connect()) as connection:
        assert require_publisher(owned_store, connection, "WORK_missing", object()) is None


def test_apply_cli_exposes_no_preauthorized_grant_option():
    import inspect

    from src.utils import quick_scan_work_decision_apply_cli as cli

    source = inspect.getsource(cli.main)
    assert "preauthorized_grant" not in source
    assert "--grant" not in source
