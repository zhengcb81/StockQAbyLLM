"""Read original public-main ledger; synthetic HTTP only, no repeated dispatch.

The checked-in SQL reconstructs the immutable mocked E2E unit input.
Each corruption uses its own copied SQLite and restores exact trigger DDL.
Execution integrity is not source correctness or whole-company coverage.
"""

import copy
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from src.utils.quick_scan_result_outbox import canonical_bytes, canonical_sha256

SOURCE_SHA = "1318ffc7012fcb1715d127469dc048827f712252b44b8aa625db27eecd4594d2"


def fixture(tmp_path):
    global SOURCE_SHA
    from tests.c06_execution_fixture import execution_fixture

    source, SOURCE_SHA = execution_fixture(tmp_path / "baseline")
    raw = source.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SOURCE_SHA
    path = tmp_path / "execution-source.sqlite"
    path.write_bytes(raw)
    # Migrate only this mutable test copy via the released native algorithm.
    # The immutable schema16 SQL/source and its hash remain unchanged.
    from src.utils.quick_scan_work_store import QuickScanWorkStore

    QuickScanWorkStore(path)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM quick_scan_result_delivery WHERE package_json IS NOT NULL LIMIT 1"
        ).fetchone()
        package = json.loads(row["package_json"])
        original = package["items"][0]["observation"]
        pre = json.loads(
            con.execute(
                "SELECT receipt_json FROM quick_scan_subject_send_receipt LIMIT 1"
            ).fetchone()[0]
        )
    target = dict(
        work_item_id=row["work_item_id"],
        observation_id=original["observation_id"],
        payload_sha256=package["items"][0]["payload_sha256"],
        subject_ref=pre["dispatch_request"]["subject_ref"],
    )
    request = dict(
        protocol="stockqa.execution_inspection_request/1.0.0",
        owner_store_id=pre["store_id"],
        observations=[target],
    )
    return source, path, request, original, package


def inspect(path, request):
    from src.utils.quick_scan_execution_inspect import inspect_executions

    return inspect_executions(path, request)


def tamper(path, table, sql, args=()):
    with closing(sqlite3.connect(path)) as con:
        triggers = con.execute(
            "SELECT name,sql FROM sqlite_master WHERE type=? AND tbl_name=?",
            ("trigger", table),
        ).fetchall()
        for name, _ in triggers:
            con.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        con.execute(sql, args)
        for _, ddl in triggers:
            con.execute(ddl)
        con.commit()


def test_native_ledger_inspection_verifies_exact_complete_original_without_credentials_or_writes(
    tmp_path,
):
    source, path, request, original, package = fixture(tmp_path)
    before = path.read_bytes()
    result = inspect(path, request)
    assert result["protocol"] == "stockqa.execution_inspection/1.0.0"
    assert result["source_consistency"] == "one_native_read_transaction"
    assert result["request_sha256"] == canonical_sha256(request)
    assert result["owner_store_id"] == request["owner_store_id"]
    proof = result["observations"][original["observation_id"]]
    assert proof["payload_sha256"] == request["observations"][0]["payload_sha256"]
    assert proof["subject_ref"] == request["observations"][0]["subject_ref"]
    assert (
        proof["execution"] == original["execution"]
        and proof["package_sha256"] == package["package_sha256"]
    )
    assert proof["maximum_check_level"] == "execution_verified"
    assert (
        proof["status"] == "verified"
        and proof["checkpoint_payload_sha256"]
        and proof["dispatch_receipt_sha256"]
    )
    assert result["financial_accuracy_proven"] is False and result["screening_audited"] is False
    assert result["database_writes"] == result["API_requests"] == 0
    assert '"answer"' not in json.dumps(result) and "original_json_base64" not in json.dumps(result)
    assert (
        path.read_bytes() == before
        and hashlib.sha256(source.read_bytes()).hexdigest() == SOURCE_SHA
    )


@pytest.mark.parametrize(
    "fault",
    [
        "store",
        "entity",
        "subject_revision",
        "perimeter",
        "observation",
        "payload",
        "work",
        "duplicate",
        "oversized",
        "self_receipt",
        "self_answer",
    ],
)
def test_wrong_original_or_consumer_self_attestation_refuses_without_writes(tmp_path, fault):
    source, path, req, original, package = fixture(tmp_path)
    target = req["observations"][0]
    if fault == "store":
        req["owner_store_id"] = "qsobs_foreign"
    elif fault == "entity":
        target["subject_ref"]["entity_id"] = "ENT_FOREIGN"
    elif fault == "subject_revision":
        target["subject_ref"]["analysis_subject_revision"] += 1
    elif fault == "perimeter":
        target["subject_ref"]["perimeter_sha256"] = "0" * 64
    elif fault == "observation":
        target["observation_id"] = "obs_" + "0" * 64
    elif fault == "payload":
        target["payload_sha256"] = "0" * 64
    elif fault == "work":
        target["work_item_id"] = "WORK_FOREIGN"
    elif fault == "duplicate":
        req["observations"].append(copy.deepcopy(target))
    elif fault == "oversized":
        req["observations"] = [copy.deepcopy(target) for _ in range(101)]
    elif fault == "self_receipt":
        req["receipt"] = {"check_level": "screening_audited"}
    else:
        target["answer"] = {"score": 10}
    before = path.read_bytes()
    with pytest.raises((ValueError, KeyError)):
        inspect(path, req)
    assert (
        path.read_bytes() == before
        and hashlib.sha256(source.read_bytes()).hexdigest() == SOURCE_SHA
    )


@pytest.mark.parametrize(
    "fault",
    [
        "missing_pre_send",
        "missing_checkpoint",
        "missing_context",
        "missing_standard_body",
        "standard_body_rehashed",
        "wrong_resolved_model",
        "wrong_attempt_phase",
        "missing_run_ref",
        "unknown_schema",
    ],
)
def test_durable_provenance_corruption_never_becomes_execution_verified(tmp_path, fault):
    source, path, req, original, package = fixture(tmp_path)
    if fault == "missing_pre_send":
        tamper(
            path,
            "quick_scan_subject_send_receipt",
            "DELETE FROM quick_scan_subject_send_receipt",
        )
    elif fault == "missing_checkpoint":
        tamper(path, "answer_checkpoint", "DELETE FROM answer_checkpoint")
    elif fault == "missing_context":
        tamper(path, "quick_scan_work_context", "DELETE FROM quick_scan_work_context")
    elif fault == "missing_standard_body":
        tamper(path, "quick_scan_standard_answer", "DELETE FROM quick_scan_standard_answer")
    elif fault == "standard_body_rehashed":
        with closing(sqlite3.connect(path)) as con:
            body = json.loads(
                con.execute(
                    "SELECT answer_json FROM quick_scan_standard_answer LIMIT 1"
                ).fetchone()[0]
            )
        body["score"] = 9
        tamper(
            path,
            "quick_scan_standard_answer",
            "UPDATE quick_scan_standard_answer SET answer_json=?,answer_sha256=?",
            (canonical_bytes(body).decode(), canonical_sha256(body)),
        )
    elif fault == "wrong_resolved_model":
        tamper(
            path,
            "quick_scan_attempt_response",
            "UPDATE quick_scan_attempt_response SET model_resolved=?",
            ("foreign-model",),
        )
    elif fault == "wrong_attempt_phase":
        tamper(path, "attempt", "UPDATE attempt SET phase=?", ("uncertain",))
    elif fault == "missing_run_ref":
        tamper(path, "work_run_ref", "DELETE FROM work_run_ref")
    else:
        with closing(sqlite3.connect(path)) as con:
            con.execute("PRAGMA user_version=999")
            con.commit()
    before = path.read_bytes()
    with pytest.raises((ValueError, KeyError)):
        inspect(path, req)
    assert (
        path.read_bytes() == before
        and hashlib.sha256(source.read_bytes()).hexdigest() == SOURCE_SHA
    )


def test_missing_ledger_and_uncheckpointed_sidecar_are_not_created_or_repaired(
    tmp_path,
):
    source, path, req, original, package = fixture(tmp_path)
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(ValueError):
        inspect(missing, req)
    assert not missing.exists()
    journal = path.with_name(path.name + "-wal")
    journal.write_bytes(b"owned busy marker")
    before = path.read_bytes()
    with pytest.raises(ValueError):
        inspect(path, req)
    assert journal.read_bytes() == b"owned busy marker" and path.read_bytes() == before


def test_actual_os_cli_reopens_original_ledger_and_never_opens_model_config(tmp_path):
    source, path, req, original, package = fixture(tmp_path)
    file = tmp_path / "inspection.json"
    file.write_text(json.dumps(req), encoding="utf-8")
    before = path.read_bytes()
    process = subprocess.run(
        [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            "-m",
            "src.utils.quick_scan_execution_inspect_cli",
            "--ledger",
            str(path),
            "--request",
            str(file),
        ],
        cwd=tmp_path,
        env=dict(os.environ),
        capture_output=True,
        timeout=45,
    )
    assert process.returncode == 0, (process.stdout, process.stderr)
    result = json.loads(process.stdout)
    assert result["observations"][original["observation_id"]]["status"] == "verified"
    assert (
        result["API_requests"] == result["database_writes"] == 0
        and result["financial_accuracy_proven"] is False
    )
    assert (
        before == path.read_bytes()
        and hashlib.sha256(source.read_bytes()).hexdigest() == SOURCE_SHA
    )


def test_batch_with_valid_first_original_and_missing_second_refuses_all_without_public_partial_proof(
    tmp_path,
):
    source, path, req, original, package = fixture(tmp_path)
    other = copy.deepcopy(req["observations"][0])
    other.update(work_item_id="WORK_MISSING", observation_id="obs_" + "0" * 64)
    req["observations"].append(other)
    file = tmp_path / "partial-batch.json"
    file.write_text(json.dumps(req), encoding="utf-8")
    before = path.read_bytes()
    process = subprocess.run(
        [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            "-m",
            "src.utils.quick_scan_execution_inspect_cli",
            "--ledger",
            str(path),
            "--request",
            str(file),
        ],
        cwd=tmp_path,
        env=dict(os.environ),
        capture_output=True,
        timeout=45,
    )
    assert process.returncode == 2, (process.stdout, process.stderr)
    result = json.loads(process.stdout)
    assert result["status"] == "rejected" and "observations" not in result
    assert result["database_writes"] == 0 and path.read_bytes() == before
    assert hashlib.sha256(source.read_bytes()).hexdigest() == SOURCE_SHA


def test_actual_windows_junction_does_not_make_foreign_ledger_a_trusted_inspection_source(
    tmp_path,
):
    if os.name != "nt":
        pytest.skip("actual Windows junction behavior")
    source, path, req, original, package = fixture(tmp_path)
    target = tmp_path / "owned-target"
    target.mkdir()
    target_file = target / "ledger.sqlite"
    target_file.write_bytes(path.read_bytes())
    sentinel = target / "keep.txt"
    sentinel.write_bytes(b"preserve target")
    junction = tmp_path / "inspection-link"
    # Native mount-point creation avoids spawning an unconstrained shell in
    # the isolated suite. Both paths are checked before the OS mutation.
    import ctypes
    import struct
    from ctypes import wintypes

    assert junction.parent.resolve().is_relative_to(tmp_path.resolve())
    assert target.resolve().is_relative_to(tmp_path.resolve())
    junction.mkdir()
    substitute = ("\\??\\" + str(target.resolve())).encode("utf-16-le")
    printed = str(target.resolve()).encode("utf-16-le")
    path_buffer = substitute + b"\x00\x00" + printed + b"\x00\x00"
    buffer = (
        struct.pack(
            "<LHHHHHH",
            0xA0000003,
            len(path_buffer) + 8,
            0,
            0,
            len(substitute),
            len(substitute) + 2,
            len(printed),
        )
        + path_buffer
    )
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(junction), 0x40000000, 0, None, 3, 0x02200000, None)
    assert handle != wintypes.HANDLE(-1).value, ctypes.get_last_error()
    returned = wintypes.DWORD()
    try:
        storage = ctypes.create_string_buffer(buffer)
        assert kernel.DeviceIoControl(
            handle, 0x900A4, storage, len(buffer), None, 0, ctypes.byref(returned), None
        ), ctypes.get_last_error()
    finally:
        kernel.CloseHandle(handle)
    before = target_file.read_bytes()
    try:
        with pytest.raises(ValueError, match="store_unavailable"):
            inspect(junction / "ledger.sqlite", req)
        assert target_file.read_bytes() == before and sentinel.read_bytes() == b"preserve target"
    finally:
        assert junction.parent.resolve().is_relative_to(tmp_path.resolve())
        assert junction.resolve() == target.resolve()
        junction.rmdir()
    assert not junction.exists() and sentinel.read_bytes() == b"preserve target"
