"""QA-C06-02 remediation: the v2 authority binds every per-question metadata
field to the real frozen manifest, and the loader reads STRICT JSON only.

Covers the shared manifest rule set (field / construct / scope / definition /
semantic / rubric / template / module / method / cohort / cutoff / entity) at
the real ``load_c06_authority`` entry — before any key is read or HTTP is
sent — plus duplicate-key and non-finite JSON refusals at every nesting level.

Every identity, prompt and answer here is SYNTHETIC and derived from
``tests/fixtures/quick_scan_c06_*`` — never an owner golden.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.utils.quick_scan_c06_authority import AuthorityUnavailable, load_c06_authority
from src.utils.quick_scan_question_manifest import load_question_manifest
from src.utils.quick_scan_result_outbox import canonical_sha256

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
AUTHORITY = json.loads(
    (FIXTURES / "quick_scan_c06_authority_v2_fixture.json").read_text(encoding="utf-8")
)
CONTEXT = AUTHORITY["observation_context"]
MANIFEST = load_question_manifest(FIXTURES / "quick_scan_c06_manifest_v2_fixture.json")
QID = MANIFEST["questions"][0]["id"]
LAST_QID = MANIFEST["questions"][-1]["id"]

V1_AUTHORITY = {
    "schema_version": "1.0.0",
    "contract_versions": {
        "identity_schema": "2.1.0",
        "answer_schema": "quick-scan-answer-v1",
        "observation_schema": "1.0.0",
        "question_catalog": "iqs-2026-10",
        "model_policy_schema": "2.0.0",
    },
    "capabilities": [
        "entity_security_identity_v1",
        "standard_observation_v1",
        "score_v1",
    ],
    "producer": {"component_version": "1.0.0", "build_id": "stockqa-offline-binding"},
}


def _signed(document: dict) -> dict:
    document["observation_context_sha256"] = canonical_sha256(document["observation_context"])
    return document


def _write(tmp_path: Path, document: dict) -> Path:
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def _load(tmp_path: Path, document: dict) -> dict:
    return load_c06_authority(
        _write(tmp_path, _signed(document)),
        manifest=MANIFEST,
        identity_snapshot_sha256=CONTEXT["identity_snapshot_sha256"],
    )


def test_pure_metadata_v2_authority_still_loads_unchanged(tmp_path):
    loaded = _load(tmp_path, copy.deepcopy(AUTHORITY))
    assert loaded["schema_version"] == "2.0.0"
    assert loaded["observation_context"] == CONTEXT
    assert loaded["observation_context_sha256"] == AUTHORITY["observation_context_sha256"]


def test_v1_authority_keeps_its_historical_load_path(tmp_path):
    loaded = load_c06_authority(_write(tmp_path, dict(V1_AUTHORITY)))
    assert loaded["schema_version"] == "1.0.0"
    assert loaded["producer_build_id"] == "stockqa-offline-binding"


@pytest.mark.parametrize(
    "changes",
    [
        {"question_definition_sha256": "0" * 64},
        {"question_semantic_sha256": "1" * 64},
        {"template_version": "99.0.0"},
        {"field_id": "score.not_the_metric"},
        {"construct_id": "NOT_THE_CONSTRUCT"},
        {"question_version": "9.9.9"},
        {"module_package_id": "pkg_" + "a" * 64},
        {"module_release_id": "modrel_" + "b" * 64},
        {"method_id": "module-locked-v1/not-a-module/0000000000000000"},
        {"information_cutoff": "2001-01-01"},
        {"entity_id": "ENT_SOMEONE_ELSE"},
        {"scope": "security", "security_id": "SEC_MISMATCH"},
        {"cycle_sensitive": False},
        {
            "cohort": {
                "company_type": "seed",
                "industries": ["semiconductors"],
                "stage": "seed",
                "subtype": "tools",
            }
        },
    ],
    ids=lambda value: "-".join(str(item)[:16] for item in value.values())[:60],
)
def test_per_question_metadata_is_bound_to_the_frozen_manifest(tmp_path, changes):
    document = copy.deepcopy(AUTHORITY)
    document["observation_context"]["questions"][QID]["metadata"].update(changes)
    with pytest.raises(AuthorityUnavailable):
        _load(tmp_path, document)


def test_a_tampered_question_beyond_the_first_is_also_refused(tmp_path):
    document = copy.deepcopy(AUTHORITY)
    document["observation_context"]["questions"][LAST_QID]["metadata"][
        "template_version"
    ] = "99.0.0"
    with pytest.raises(AuthorityUnavailable):
        _load(tmp_path, document)


def test_identity_bytes_binding_is_still_checked(tmp_path):
    document = copy.deepcopy(AUTHORITY)
    document["observation_context"]["identity_snapshot_sha256"] = "c" * 64
    with pytest.raises(AuthorityUnavailable):
        load_c06_authority(
            _write(tmp_path, document),
            manifest=MANIFEST,
            identity_snapshot_sha256=CONTEXT["identity_snapshot_sha256"],
        )


def test_manifest_field_tamper_still_breaks_the_manifest_hash_binding(tmp_path):
    document = copy.deepcopy(AUTHORITY)
    manifest = copy.deepcopy(MANIFEST)
    manifest["questions"][0]["prompt"] = manifest["questions"][0]["prompt"] + " "
    with pytest.raises(AuthorityUnavailable):
        load_c06_authority(
            _write(tmp_path, _signed(document)),
            manifest=manifest,
            identity_snapshot_sha256=CONTEXT["identity_snapshot_sha256"],
        )


def test_authority_with_a_duplicate_schema_version_is_refused(tmp_path):
    path = tmp_path / "authority.json"
    path.write_text(
        json.dumps(AUTHORITY, ensure_ascii=False).replace(
            '"schema_version": "2.0.0"',
            '"schema_version": "1.0.0", "schema_version": "2.0.0"',
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(AuthorityUnavailable, match="strict"):
        load_c06_authority(path)


def test_authority_with_a_nested_duplicate_key_is_refused(tmp_path):
    raw = json.dumps(_signed(copy.deepcopy(AUTHORITY)), ensure_ascii=False).replace(
        '"template_version": "3.2.0"',
        '"template_version": "3.2.0", "template_version": "9.9.9"',
        1,
    )
    path = tmp_path / "authority.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(AuthorityUnavailable, match="strict"):
        load_c06_authority(path)


def test_authority_with_a_non_finite_number_is_refused(tmp_path):
    raw = json.dumps(_signed(copy.deepcopy(AUTHORITY)), ensure_ascii=False).replace(
        '"identity_schema": "2.2.0"', '"identity_schema": NaN', 1
    )
    path = tmp_path / "authority.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(AuthorityUnavailable, match="strict"):
        load_c06_authority(path)
