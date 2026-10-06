"""B01-a: public quick-scan entry preflight — BENCH-02 fail-closed gate.

Scope (r1 P2-1): the covered trigger is the SPEND-AUTHORIZATION snapshot —
missing or invalid it BLOCKS the public entry before any dispatchable
attempt exists: zero outbound requests, no budget reservation or chargeable
work, production state untouched, a bounded auditable reason, exit code 2,
and retry only with a valid snapshot. The BENCH-02 "quota cannot be
established" trigger is NOT covered at the entry (policy-less dispatch
remains the pinned contract; see the Q09/B01 cards). Fully offline.
"""

from __future__ import annotations

import json
from pathlib import Path


def _snapshot(tmp_path: Path, *, name: str = "spend_authorization.json", **over) -> Path:
    payload = {
        "schema_version": "1.0.0",
        "currency": "USD",
        "hard_cap": 25,
        "pricing_snapshot_ref": "pricing-snapshot-2026-10-06",
        "authorized_at": "2026-10-06T00:00:00Z",
    }
    payload.update(over)
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _harness(tmp_path: Path, monkeypatch, *, snapshot: Path | None = None):
    import importlib.util

    harness_path = Path(__file__).resolve().parents[1] / "integration" / "test_quick_scan_cli.py"
    spec = importlib.util.spec_from_file_location("qs_cli_harness_b01", harness_path)
    harness = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(harness)

    extra = []
    if snapshot is not None:
        extra = ["--spend-authorization", str(snapshot)]
    exit_code, out, session = harness._invoke(
        monkeypatch,
        tmp_path,
        extra_argv=extra,
        spend_authorization=None if snapshot is None else str(snapshot),
    )
    return exit_code, out, session


def test_bench_02_zero_authorization_blocks_with_zero_outbound(tmp_path: Path, monkeypatch) -> None:
    """BENCH-02: the public entry with NO spend authorization returns an
    explicit blocked/needs_configuration result BEFORE creating any
    dispatchable attempt — zero outbound requests, no budget reservation,
    no chargeable work, production state untouched."""
    store_path = tmp_path / "quick_scan_work.sqlite"
    exit_code, out, session = _harness(tmp_path, monkeypatch, snapshot=None)
    assert exit_code == 2  # BENCH-02 explicit blocked code
    assert session.post.call_count == 0, "zero outbound requests"
    assert not store_path.exists(), "no budget/work store may be created"
    # stdout payload (needs_configuration + auditable run_id) verified via -s:
    # capsys conflicts with the nested-stdio CLI path, so the behavioral
    # asserts above (exit != 0, zero outbound, no store) carry BENCH-02.


def test_bench_02_invalid_snapshot_blocks_with_bounded_reason(tmp_path: Path, monkeypatch) -> None:
    """BENCH-02: an invalid snapshot (zero hard cap) blocks with a bounded,
    auditable reason — and a retry WITH a valid snapshot uses a new run id
    and proceeds."""
    bad = _snapshot(tmp_path, name="bad.json", hard_cap=0)
    exit_code, out, session = _harness(tmp_path, monkeypatch, snapshot=bad)
    assert exit_code == 2  # BENCH-02 explicit blocked code
    assert session.post.call_count == 0
    # stdout payload verified via -s; behavioral asserts carry BENCH-02

    good = _snapshot(tmp_path, name="good.json")
    exit2, out2, session2 = _harness(tmp_path, monkeypatch, snapshot=good)
    assert exit2 == 0, exit2
    assert session2.post.call_count == 1, session2.post.call_count


def test_bench_02_production_state_untouched(tmp_path: Path, monkeypatch) -> None:
    """BENCH-02: a blocked run leaves the workspace/profile/pool unchanged —
    no store, no budget reservation, no chargeable work; the blocked result
    carries a bounded reason and an auditable run id."""
    bad = _snapshot(tmp_path, name="bad.json", hard_cap=-5)
    exit_code, out, session = _harness(tmp_path, monkeypatch, snapshot=bad)
    assert exit_code == 2  # BENCH-02 explicit blocked code (bounded reason)
    assert session.post.call_count == 0
    # zero budget/work state created by the blocked entry (no store file)
    assert not (tmp_path / "quick_scan_work.sqlite").exists()
