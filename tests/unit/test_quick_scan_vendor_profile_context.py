"""Synthetic context interoperability, not owner execution or financial gold."""

import copy

import pytest
import test_c06_authority as iqs_fixture

from src.utils import quick_scan_observation_context as api
from src.utils.quick_scan_result_outbox import canonical_sha256


@pytest.fixture(scope="module")
def exported():
    iqs_fixture.C06AuthorityTests.setUpClass()
    fixture = iqs_fixture.C06AuthorityTests()
    authority = fixture.create(
        contract_versions={**fixture.versions, "observation_schema": "1.2.0"},
        observation_schema_version="1.2.0",
    )
    manifest = copy.deepcopy(fixture.manifest)
    manifest["manifest_sha256"] = "a" * 64
    return authority, manifest


def test_actual_exported_new_context_accepted_and_detached(exported):
    authority, _ = exported
    original = authority["observation_context"]
    result = api.validate_context_document(
        original, expected_sha256=authority["observation_context_sha256"]
    )
    assert result == original
    result["questions"][next(iter(result["questions"]))]["metadata"]["run_id"] = "changed"
    assert result != original


def test_actual_manifest_binding_uses_new_explicit_schema(exported):
    authority, manifest = exported
    context = authority["observation_context"]
    question = next(q for q in manifest["questions"] if q["scope"] == "entity")
    bound = api.bind_question_context(
        context,
        manifest=manifest,
        identity_snapshot_sha256="b" * 64,
        entity_id=manifest["profile"]["entity_id"],
        question_id=question["id"],
        scope="entity",
        scope_id=manifest["profile"]["entity_id"],
    )
    assert bound["metadata"]["schema_version"] == "1.2.0"
    assert bound["observation_schema_sha256"] == context["observation_schema_sha256"]
    assert bound["schema"] == api.QUESTION_CONTEXT_SCHEMA


@pytest.mark.parametrize(
    "kind",
    [
        "mixed_version",
        "all_legacy_version",
        "future",
        "wrong_hash",
        "bad_review",
        "missing_metadata",
    ],
)
def test_resigned_new_context_cannot_select_conflicting_or_unknown_profile(exported, kind):
    authority, _ = exported
    bad = copy.deepcopy(authority["observation_context"])
    first = next(iter(bad["questions"].values()))
    if kind == "mixed_version":
        first["metadata"]["schema_version"] = "1.1.0"
    elif kind == "all_legacy_version":
        for question in bad["questions"].values():
            question["metadata"]["schema_version"] = "1.1.0"
    elif kind == "future":
        first["metadata"]["schema_version"] = "1.3.0"
    elif kind == "wrong_hash":
        bad["observation_schema_sha256"] = "0" * 64
    elif kind == "bad_review":
        first["metadata"]["evidence_review_status"] = "accepted"
    else:
        first.pop("metadata")
    with pytest.raises(ValueError):
        api.validate_context_document(bad, expected_sha256=canonical_sha256(bad))


def test_legacy_context_cannot_adopt_new_metadata_only(exported):
    legacy = iqs_fixture.C06AuthorityTests().create()["observation_context"]
    bad = copy.deepcopy(legacy)
    for question in bad["questions"].values():
        question["metadata"]["schema_version"] = "1.2.0"
    with pytest.raises(ValueError):
        api.validate_context_document(bad, expected_sha256=canonical_sha256(bad))
