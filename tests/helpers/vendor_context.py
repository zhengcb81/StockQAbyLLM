"""Read the IQS public export stored with its original synthetic manifest."""

import json
from pathlib import Path

from src.utils.quick_scan_question_manifest import load_question_manifest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def read_vendor_context(*, legacy=False):
    name = (
        "quick_scan_c06_authority_v2_fixture.json"
        if legacy
        else "quick_scan_c06_authority_observation_v12_fixture.json"
    )
    authority = json.loads((FIXTURES / name).read_bytes())
    manifest = load_question_manifest(FIXTURES / "quick_scan_c06_manifest_v2_fixture.json")
    return authority, manifest
