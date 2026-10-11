"""Original SQLite control record tests; callbacks are synthetic, not consent."""

import copy
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from src.utils.quick_scan_refresh_admission_journal import DDL_V16_ADDITIONS, read
from src.utils.quick_scan_refresh_batch import register_refresh_action
from src.utils.quick_scan_work_store import SCHEMA_VERSION, QuickScanWorkStore
from tests.unit.test_quick_scan_refresh_batch import counts, specs


def action():
    return dict(
        confirmation_id="rc1_" + "a" * 64,
        confirmation_sha256="b" * 64,
        preview_id="rv1_" + "c" * 64,
        scope_sha256="d" * 64,
        policy_sha256="e" * 64,
        execution_inputs_sha256="f" * 64,
    )


def register(store, rows=None, claim=None, recheck=lambda mode: None):
    return register_refresh_action(
        store,
        rows or specs(),
        action=claim or action(),
        run_id="RUN_SYNTHETIC",
        scan_id="SCAN_SYNTHETIC",
        recheck=recheck,
    )


def test_action_and_work_commit_atomically_then_restart_reuses_original_rows(tmp_path):
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    checks = []
    first = register(store, recheck=checks.append)
    assert checks[0] == "fresh_registration" and checks[-1] == "current_permissions"
    assert first["permission_authenticated"] is False and first["API_requests"] == 0
    assert first["dispatch_started"] is False
    before = counts(store.path)
    checks.clear()
    again = register(QuickScanWorkStore(store.path), recheck=checks.append)
    assert again == first and set(checks) == {"current_permissions"}
    assert counts(store.path) == before
    with store._transaction() as con:
        assert read(con, action()["confirmation_id"]) == first
        assert con.execute("SELECT COUNT(*) FROM quick_scan_refresh_admission").fetchone()[0] == 1


@pytest.mark.parametrize("fault", ["first_check", "post_check", "second_spec", "record_write"])
def test_admission_fault_rolls_back_action_work_run_refs_and_no_attempt(
    tmp_path, fault, monkeypatch
):
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    before = counts(store.path)
    checks = []
    rows = specs()
    if fault == "second_spec":
        rows[1]["routing_fingerprint"] = "bad"
    if fault == "record_write":

        def fail(*args, **kwargs):
            raise ValueError("synthetic record publication failed")

        monkeypatch.setattr("src.utils.quick_scan_refresh_admission_journal.publish", fail)

    def check(mode):
        checks.append(mode)
        if fault == "first_check" or fault == "post_check" and mode == "current_permissions":
            raise ValueError("synthetic permission revoked")

    with pytest.raises(ValueError):
        register(store, rows, recheck=check)
    assert counts(store.path) == before
    with store._transaction() as con:
        assert con.execute("SELECT COUNT(*) FROM quick_scan_refresh_admission").fetchone()[0] == 0


@pytest.mark.parametrize("fault", ["scope", "inputs", "specification", "run"])
def test_existing_action_never_rebinds_input_and_current_permission_still_required(tmp_path, fault):
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    first = register(store)
    claim = action()
    rows = specs()
    if fault == "scope":
        claim["scope_sha256"] = "9" * 64
    elif fault == "inputs":
        claim["execution_inputs_sha256"] = "8" * 64
    elif fault == "specification":
        rows[0]["routing_fingerprint"] = "7" * 64
    checks = []
    with pytest.raises(ValueError, match="idempotency_conflict"):
        register_refresh_action(
            store,
            rows,
            action=claim,
            run_id="RUN_CHANGED" if fault == "run" else "RUN_SYNTHETIC",
            scan_id="SCAN_SYNTHETIC",
            recheck=checks.append,
        )
    assert not checks

    def denied(mode):
        raise ValueError("permission revoked now")

    with pytest.raises(ValueError, match="permission revoked"):
        register(store, recheck=denied)
    with store._transaction() as con:
        assert read(con, claim["confirmation_id"]) == first


def test_two_process_like_connections_share_one_atomic_action(tmp_path):
    path = tmp_path / "work.sqlite"
    QuickScanWorkStore(path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        out = list(pool.map(lambda _: register(QuickScanWorkStore(path)), range(2)))
    assert out[0] == out[1] and counts(path)["work_item"] == 2


def test_v15_migration_preserves_existing_work_and_future_or_broken_schema_rejected(tmp_path):
    path = tmp_path / "work.sqlite"
    store = QuickScanWorkStore(path)
    from src.utils.quick_scan_refresh_batch import attach_owner_refresh_batch

    old = attach_owner_refresh_batch(
        store, specs(), run_id="RUN_OLD", scan_id="SCAN_OLD", recheck=lambda: None
    )
    with store._transaction() as con:
        for statement in reversed(DDL_V16_ADDITIONS):
            tokens = statement.split()
            con.execute("DROP " + tokens[1] + " " + tokens[2])
        con.execute("PRAGMA user_version=15")
    migrated = QuickScanWorkStore(path)
    assert SCHEMA_VERSION == 16
    assert all(migrated.get_item(i["work_item_id"])["status"] == "pending" for i in old["items"])
    with migrated._transaction() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 16
        assert con.execute("SELECT COUNT(*) FROM quick_scan_refresh_admission").fetchone()[0] == 0
        con.execute("PRAGMA user_version=17")
    with pytest.raises(ValueError, match="unsupported"):
        QuickScanWorkStore(path)


def test_immutable_record_cannot_be_changed_and_resealed_row_cannot_hide_wrong_original_work(
    tmp_path,
):
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    first = register(store)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store._transaction() as con:
            con.execute("UPDATE quick_scan_refresh_admission SET input_sha256=?", ("0" * 64,))
    # Controlled owned-DB corruption: even a new JSON digest does not bind
    # another original work row. No production DB or statement is touched.
    with store._transaction() as con:
        from src.utils.quick_scan_result_outbox import canonical_bytes, canonical_sha256

        wrong = copy.deepcopy(first)
        wrong["items"][0]["work_item_id"] = first["items"][1]["work_item_id"]
        con.execute("DROP TRIGGER quick_scan_refresh_admission_no_update")
        con.execute(
            "UPDATE quick_scan_refresh_admission SET receipt_json=?,receipt_sha256=?",
            (canonical_bytes(wrong).decode(), canonical_sha256(wrong)),
        )
        con.execute(DDL_V16_ADDITIONS[1])
    with pytest.raises(ValueError, match="admission_work_mismatch"):
        QuickScanWorkStore(store.path)


def test_failed_v15_migration_rolls_back_schema_and_preserves_original_bytes(tmp_path, monkeypatch):
    path = tmp_path / "work.sqlite"
    store = QuickScanWorkStore(path)
    with store._transaction() as con:
        for statement in reversed(DDL_V16_ADDITIONS):
            tokens = statement.split()
            con.execute("DROP " + tokens[1] + " " + tokens[2])
        con.execute("PRAGMA user_version=15")
    before = path.read_bytes()
    original = QuickScanWorkStore._validate_schema

    def validate(cls, connection, *, schema_version=SCHEMA_VERSION):
        if schema_version == 16:
            raise ValueError("synthetic migration validation fault")
        return original(connection, schema_version=schema_version)

    monkeypatch.setattr(QuickScanWorkStore, "_validate_schema", classmethod(validate))
    with pytest.raises(ValueError, match="synthetic migration"):
        QuickScanWorkStore(path)
    assert path.read_bytes() == before
    with closing(sqlite3.connect(path)) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 15
        assert (
            con.execute(
                "SELECT 1 FROM sqlite_master WHERE name='quick_scan_refresh_admission'"
            ).fetchone()
            is None
        )


@pytest.mark.parametrize("fault", ["receipt_false_integer", "spec_true_integer"])
def test_resealed_json_boolean_cannot_impersonate_original_numeric_contract(tmp_path, fault):
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    first = register(store)
    from src.utils.quick_scan_result_outbox import (
        canonical_bytes,
        canonical_sha256,
        strict_json_loads,
    )

    with store._transaction() as con:
        row = con.execute("SELECT * FROM quick_scan_refresh_admission").fetchone()
        value = strict_json_loads(row["input_json"])
        receipt = copy.deepcopy(first)
        if fault == "receipt_false_integer":
            receipt["API_requests"] = False
        else:
            value["specifications"][0]["source_binding_version"] = True
            receipt["input_sha256"] = canonical_sha256(value)
        con.execute("DROP TRIGGER quick_scan_refresh_admission_no_update")
        con.execute(
            "UPDATE quick_scan_refresh_admission SET input_json=?,input_sha256=?,receipt_json=?,receipt_sha256=?",
            (
                canonical_bytes(value).decode(),
                canonical_sha256(value),
                canonical_bytes(receipt).decode(),
                canonical_sha256(receipt),
            ),
        )
        con.execute(DDL_V16_ADDITIONS[1])
    with pytest.raises(ValueError, match="admission_"):
        QuickScanWorkStore(store.path)


def references():
    return [
        dict(
            subject_ref=dict(
                entity_id="ENT_BYD",
                analysis_subject_id="ASJ_11111111-1111-4111-8111-111111111111",
                analysis_subject_revision=1,
                primary_issuer_id="ENT_BYD",
                perimeter_sha256="a" * 64,
                scope="entity",
                scope_id="ENT_BYD",
                security_id=None,
                segment_id=None,
                listing_id=None,
            ),
            question_id="IQS_05",
            field_id="synthetic-field",
            observation_id="OBS_SYNTHETIC",
            payload_sha256="9" * 64,
        )
    ]


def test_reference_only_action_keeps_original_ids_without_work_attempt_or_budget(tmp_path):
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    before = counts(store.path)
    result = register_refresh_action(
        store,
        [],
        action=action(),
        references=references(),
        run_id="RUN_REF",
        scan_id="SCAN_REF",
        recheck=lambda mode: None,
    )
    assert result["protocol"] == "stockqa.refresh_action_registration/1.1.0"
    assert result["items"] == [] and result["observation_references"] == references()
    assert counts(store.path) == before and result["permission_authenticated"] is False
    again = register_refresh_action(
        QuickScanWorkStore(store.path),
        [],
        action=action(),
        references=references(),
        run_id="RUN_REF",
        scan_id="SCAN_REF",
        recheck=lambda mode: None,
    )
    assert again == result


@pytest.mark.parametrize("fault", ["empty", "duplicate", "wrong_hash"])
def test_empty_or_invalid_reference_action_is_never_registered(tmp_path, fault):
    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    refs = references()
    if fault == "empty":
        refs = []
    elif fault == "duplicate":
        refs *= 2
    else:
        refs[0]["payload_sha256"] = "not-a-hash"
    with pytest.raises(ValueError):
        register_refresh_action(
            store,
            [],
            action=action(),
            references=refs,
            run_id="RUN_REF",
            scan_id="SCAN_REF",
            recheck=lambda mode: None,
        )
    with store._transaction() as con:
        assert con.execute("SELECT COUNT(*) FROM quick_scan_refresh_admission").fetchone()[0] == 0
