"""Scan the published owner source and keep the standalone inventory complete."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "tests/contract"


@pytest.mark.skipif(sys.version_info < (3, 11), reason="S6 source scanner requires Python 3.11")
def test_runtime_scan_has_no_new_or_unscanned_legacy_violations():
    from tools.anti_trick_gate_spec.evidence import (
        baseline,
        changed_inventory,
        compare,
        renamed_waivers,
    )
    from tools.anti_trick_gate_spec.static_scan import scan

    result = scan(
        ROOT,
        ("src", "main.py", "main_with_llm.py", "scripts"),
        ("src/utils/quick_scan_work_store.py",),
        ("src/utils/quick_scan_feedback_owner.py",),
    )
    previous = json.loads((CONTRACT / "ratchet_baseline.json").read_bytes())
    current = {**baseline(result.findings), "constant_shapes": result.constant_shapes}
    assert not changed_inventory(result, previous, ROOT)
    assert not compare(current, previous)
    assert not renamed_waivers(current, previous)


def test_canonical_collection_roots_cover_all_standalone_test_directories(pytestconfig):
    tests_root = ROOT / "tests"
    external = {"live", "composition"}
    all_roots = {
        path.relative_to(ROOT).as_posix()
        for path in tests_root.iterdir()
        if path.is_dir() and path.name not in external and any(path.rglob("test_*.py"))
    }
    assert all_roots == set(pytestconfig.getini("testpaths"))
    assert not tuple(
        tests_root.glob("test_*.py")
    ), "root-level tests must use an owned test directory"
    policy = json.loads((CONTRACT / "test_policy.json").read_bytes())["platforms"]
    assert set(policy) == {"linux", "windows"}
    for entry in policy.values():
        assert set(entry["roots"]) == all_roots
        assert "not live" in entry["markexpr"]
        assert "not owner_composition" in entry["markexpr"]
