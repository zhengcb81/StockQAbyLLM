"""Actual public controls -> consent -> original admission, synthetic data only.

No model HTTP or real-human approval/financial gold. Native records are made
by the original software in owned temporary SQL stores, never hand-built DTOs.
"""

import copy
import json
import os
import sys
from contextlib import closing

import pytest
from stockwiki.quick_scan_refresh_records import (
    RefreshConfirmationStore,
    RefreshRecordStore,
)
from stockwiki.quick_scan_work_decision_owner import (
    WorkDecisionConfirmationStore,
    WorkDecisionPreviewStore,
)

from src.utils.quick_scan_refresh_execution import prepare_execution
from src.utils.quick_scan_work_decision_inspect import derive_work_decision
from tests.integration.test_c06_subject_public_e2e import OWN, actual, invoke, utc
from tests.integration.test_owner_work_control_public import resolved_public_fixture


def controlled_public_fixture(
    actual,
    tmp_path,
    monkeypatch,
    capsys,
    *,
    policy=None,
    ledger_path=None,
    pack_size=1,
    comparison_target=None,
):
    material = resolved_public_fixture(actual, tmp_path, policy=policy, ledger_path=ledger_path)
    prepared, store, old, policy = (material[k] for k in ("prepared", "store", "item", "policy"))
    control_options = dict(pack_size=pack_size, thinking_enabled=True, max_output_tokens=32768)
    current_policy = material["owner_policy"]
    dialect = "1.3.0" if comparison_target is not None else "1.2.0"
    current_policy.update(protocol="stockwiki.refresh_owner_policy/" + dialect, policy_version=2)
    current_policy["qa"]["quote_schema_version"] = dialect
    if comparison_target is not None:
        current_policy.update(
            provider=comparison_target["provider"], model=comparison_target["model"]
        )
        current_policy["qa"].update(
            route_id=comparison_target["route_id"],
            provider_config_ref=comparison_target["provider_config_ref"],
        )
    material["owner_policy_file"].write_text(json.dumps(current_policy), encoding="utf-8")
    intent = dict(
        kind="retry_decision",
        work_item_id=old["work_item_id"],
        purpose="comparison",
        retry_after_seconds=0,
        idempotency_key="synthetic-public-comparison",
        comparison_controls=control_options,
    )
    derived = derive_work_decision(store.path, intent, policy=policy)
    outer = copy.deepcopy(material["outer"])
    outer["decision_request"] = derived["request"]
    file = tmp_path / "comparison-control.json"
    file.write_text(json.dumps(outer), encoding="utf-8")
    preview = invoke(
        material["base"] + ["--work-decision-request", str(file)],
        tmp_path,
        "comparison-intent-preview",
    )
    record = WorkDecisionPreviewStore(prepared["paths"]).load(preview["preview_id"])
    consent = WorkDecisionConfirmationStore(prepared["paths"]).save_control(
        record, idempotency_key=intent["idempotency_key"], confirmed_at=utc()
    )
    command = material["command"].copy()
    command[command.index("--confirmation-id") + 1] = consent["confirmation_id"]
    command[command.index("--refresh-schema-version") + 1] = dialect
    applied = invoke(command, tmp_path, "comparison-native-intent", timeout=180)
    decision = applied["decision"]
    assert decision["request"]["comparison_controls"] == control_options
    assert store.get_item(old["work_item_id"])["status"] == "failed"
    assert material["physical"]() == material["before"]

    refresh = copy.deepcopy(outer["permission_request"])
    refresh.update(
        protocol="iqs.refresh_request/" + dialect,
        request_id="synthetic-control-refresh",
        requested_at=utc(),
        cohort_id="B01_SYNTHETIC_PACK" + str(pack_size),
        work_decision_refs=[
            dict(work_item_id=old["work_item_id"], decision_id=decision["decision_id"])
        ],
    )
    file = tmp_path / "controlled-refresh.json"
    file.write_text(json.dumps(refresh), encoding="utf-8")
    preview = invoke(
        material["base"] + ["--request", str(file)],
        tmp_path,
        "controlled-refresh-preview",
    )
    record = RefreshRecordStore(prepared["paths"]).load(preview["preview_id"])
    assert record["preview"]["items"][0]["owner_control"]["decision_id"] == decision["decision_id"]
    assert record["preview"]["items"][0]["generation"] == 2
    confirmation = RefreshConfirmationStore(prepared["paths"]).save_confirmation(
        record,
        owner_principal=current_policy["owner_principal"],
        idempotency_key="synthetic-controlled-registration",
        confirmed_at=utc(),
    )
    command = [
        sys.executable,
        "-B",
        "-X",
        "utf8",
        "-m",
        "src.utils.quick_scan_refresh_admission_cli",
        "--config",
        str(prepared["deployment_file"]),
        "--confirmation-id",
        confirmation["confirmation_id"],
    ]
    accepted = invoke(command, tmp_path, "controlled-public-admission", timeout=180)
    assert accepted["status"] == "accepted" and accepted["API_requests"] == 0
    work_id = accepted["registration"]["items"][0]["work_item_id"]
    assert (
        store.get_item(work_id)["generation"] == 2
        and store.get_item(work_id)["status"] == "pending"
    )
    assert store.get_owner_refresh_binding(work_id)["binding"][
        "protocol"
    ] == "stockqa.owner_refresh_binding/" + ("4.0.0" if comparison_target is not None else "3.0.0")
    replay = invoke(command, tmp_path, "controlled-public-replay", timeout=180)
    assert replay["registration"] == accepted["registration"]
    assert material["physical"]() == material["before"]
    with closing(store._connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM work_item").fetchone()[0] == 2

    bundle = prepared["bundle"]
    inputs = {
        name: bundle / name
        for name in (
            "manifest.json",
            "identity.json",
            "authority.json",
            "owner-config.json",
            "questions.json",
        )
    }
    session = prepare_execution(
        prepared["deployment_file"],
        confirmation["confirmation_id"],
        input_paths=inputs,
        process_env=dict(os.environ),
    )
    session.assert_execution_controls(**control_options)
    session.check()
    assert session.existing_work(prepared["q"]["id"])["work_item_id"] == work_id
    before = store.path.read_bytes()
    import main_with_llm

    argv = [
        "main_with_llm.py",
        "--company",
        "Synthetic E2E issuer",
        "--entity-id",
        old["entity_id"],
        "--require-search",
        "--provider",
        "mimo-config",
        "--config",
        str(bundle / "questions.json"),
        "--output",
        str(tmp_path / "not-an-answer.json"),
        "--identity-snapshot",
        str(bundle / "identity.json"),
        "--question-manifest",
        str(bundle / "manifest.json"),
        "--c06-authority",
        str(bundle / "authority.json"),
        "--quick-scan-owner-config",
        str(bundle / "owner-config.json"),
        "--refresh-admission-config",
        str(prepared["deployment_file"]),
        "--refresh-confirmation-id",
        confirmation["confirmation_id"],
        "--question-pack-size",
        "1",
        "--thinking-enabled",
        "true",
        "--max-output-tokens",
        "8192",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    assert main_with_llm.main() != 0
    assert not (tmp_path / "not-an-answer.json").exists()
    assert store.path.read_bytes() == before and store.list_attempts(work_id) == []
    assert material["physical"]() == material["before"]

    return dict(
        material=material,
        prepared=prepared,
        session=session,
        confirmation=confirmation,
        work_id=work_id,
    )


@pytest.mark.parametrize("actual", ["1.2.0"], indirect=True)
def test_public_independent_control_registers_exact_new_generation_and_main_rejects_wrong_options(
    actual, tmp_path, monkeypatch, capsys
):
    controlled_public_fixture(actual, tmp_path, monkeypatch, capsys)
