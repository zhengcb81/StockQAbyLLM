"""Controlled-scope mapping and original runner options; no authority in fixtures."""

import copy

import pytest

from src.utils.quick_scan_owner_refresh import OwnerRefreshRejected, OwnerRefreshSession
from src.utils.quick_scan_refresh_admission import AdmissionRejected
from src.utils.quick_scan_refresh_execution import (
    ApprovedRefreshSession,
    registered_scope,
)
from tests.unit.test_quick_scan_refresh_execution import materials


def controls():
    return dict(
        decision_id="owd1_" + "a" * 64,
        document_sha256="b" * 64,
        source_work_item_id="WORK_SOURCE_SYNTHETIC",
        source_generation=1,
        confirmation_id="rc1_" + "c" * 64,
        purpose="comparison",
        next_retry_at="2026-10-13T08:00:00Z",
        target_generation=2,
        cohort_id="B01_SYNTHETIC_PACK3",
        comparison_controls=dict(pack_size=3, thinking_enabled=True, max_output_tokens=32768),
    )


def mapped():
    item, bundle, record = materials()
    spec = record["inputs"]["specifications"][0]
    binding = spec["owner_refresh_binding"]
    control = controls()
    item.update(
        generation=2, owner_control=copy.deepcopy(control), decision="dispatch_new_generation"
    )
    spec["generation"] = 2
    binding.update(
        protocol="stockqa.owner_refresh_binding/3.0.0",
        owner_control=copy.deepcopy(control),
        target_generation=2,
    )
    record["receipt"]["items"][0]["generation"] = 2
    return item, bundle, record


def test_registered_mapping_preserves_controlled_scope_without_mutating_original_manifest():
    item, bundle, record = mapped()
    original = copy.deepcopy(bundle["manifest"])
    result = registered_scope([item], bundle, record, information_cutoff="2026-10-10")
    assert result["IQS_05"]["specification"]["owner_refresh_binding"]["owner_control"] == controls()
    assert bundle["manifest"] == original


@pytest.mark.parametrize("fault", ["old_dialect", "missing", "pack", "cohort"])
def test_resealed_controlled_spec_must_equal_native_preview_item(fault):
    item, bundle, record = mapped()
    binding = record["inputs"]["specifications"][0]["owner_refresh_binding"]
    if fault == "old_dialect":
        binding["protocol"] = "stockqa.owner_refresh_binding/2.0.0"
    elif fault == "missing":
        binding.pop("owner_control")
    elif fault == "pack":
        binding["owner_control"]["comparison_controls"]["pack_size"] = 1
    else:
        binding["owner_control"]["cohort_id"] = "OTHER_COHORT"
    with pytest.raises(AdmissionRejected):
        registered_scope([item], bundle, record, information_cutoff="2026-10-10")


def option_session():
    session = ApprovedRefreshSession.__new__(ApprovedRefreshSession)
    item, bundle, record = mapped()
    session.entries = {
        "IQS_05": dict(kind="work", specification=record["inputs"]["specifications"][0])
    }
    session.proof = dict(
        request={"protocol": "iqs.refresh_request/1.2.0", "cohort_id": "B01_SYNTHETIC_PACK3"}
    )
    return session


def test_original_runner_must_supply_the_exact_approved_generation_options():
    session = option_session()
    session.assert_execution_controls(pack_size=3, thinking_enabled=True, max_output_tokens=32768)
    assert session._execution_controls == dict(
        pack_size=3, thinking_enabled=True, max_output_tokens=32768
    )


@pytest.mark.parametrize(
    "change",
    [
        dict(pack_size=1),
        dict(pack_size=True),
        dict(thinking_enabled=False),
        dict(thinking_enabled=None),
        dict(thinking_enabled=1),
        dict(max_output_tokens=8192),
        dict(max_output_tokens=None),
        dict(max_output_tokens=True),
    ],
)
def test_default_or_changed_options_never_silently_execute_an_approved_experiment(change):
    options = dict(pack_size=3, thinking_enabled=True, max_output_tokens=32768)
    options.update(change)
    with pytest.raises(AdmissionRejected, match="approved_controls"):
        option_session().assert_execution_controls(**options)
