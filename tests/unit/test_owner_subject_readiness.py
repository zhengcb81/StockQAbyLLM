"""Constructor/readiness units over a fixed protocol sample; never owner admission.

Only the public read-only CLI boundary is mocked. The validator reads the
original IQS schema from an explicitly configured checkout, never a run archive.
The fixed response contains no company, runtime path or real observation data.
"""

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.utils.quick_scan_owner_research import (
    OwnerResearchRejected,
    OwnerResearchServices,
)

IQS_ROOT = Path(
    os.environ.get("IQS_C06_IQS", str(Path(__file__).resolve().parents[3] / "invest-quick-scan"))
)
SCHEMA = IQS_ROOT / "schemas/quick_scan/query-capabilities.schema.json"
pytestmark = pytest.mark.skipif(
    not SCHEMA.is_file(), reason="configured original IQS capabilities schema required"
)

# Synthetic fixed sample, not an owner golden or a stored real response.
CAPABILITIES = {
    "message_type": "response",
    "operation": "capabilities",
    "protocol": "stockwiki.c06_query_capabilities/1.1.0",
    "request_id": "owner-capabilities",
    "response_at": "2020-01-01T00:00:00Z",
    "result": {
        "API_requests": 0,
        "answer_qualification_assessed": False,
        "capability_requirements": {
            "missing_market_filters": [],
            "missing_native_purposes": [],
            "missing_operations": [],
            "missing_query_versions": [],
            "status": "satisfied",
        },
        "coverage": {
            "basis": "availability_of_scope_bound_originals_not_accuracy_or_screening_qualification",
            "covered_market_domains": [],
            "field_scope_coverage": {},
            "reason_codes": ["selection_not_requested"],
            "scope_counts": None,
            "selection": None,
            "status": "not_requested",
            "unmapped_covered_market_labels": [],
        },
        "database_writes": 0,
        "dispatch_started": False,
        "facts_producer_ready": False,
        "financial_accuracy_proven": False,
        "full_C06_milestone_accepted": False,
        "identity_market_domains": ["CN", "HK", "US"],
        "identity_market_scan_status": "available",
        "identity_market_scope": "owner_namespace",
        "limits": {
            "page_size_max": 100,
            "pool_index_max_bytes": 67108864,
            "snapshot_max_bytes": 16777216,
            "snapshot_max_count": 32,
            "snapshot_total_max_bytes": 67108864,
            "snapshot_ttl_seconds": 3600,
            "source_max_bytes": 8388608,
            "source_max_rows": 5000,
        },
        "market_support": {
            "market_filter_domains": ["CN", "HK", "US"],
            "profile_listing_scope": "any_owner_attested_listing",
        },
        "native_interfaces": [
            {
                "component": "StockWiki",
                "entry_point": "stockwiki.quick_scan_refresh_owner_cli",
                "entry_type": "module",
                "permission_boundary": "fixed_owner_configuration_readonly_preview",
                "purpose": "refresh_preview",
                "selectors": ["--root", "--iqs-code-root", "--request"],
            },
            {
                "component": "StockWiki",
                "entry_point": "stockwiki.quick_scan_refresh_owner_cli",
                "entry_type": "module",
                "permission_boundary": "interactive_challenge_real_OS_principal_and_current_scope",
                "purpose": "native_confirmation",
                "selectors": ["--root", "--iqs-code-root", "--confirm", "--idempotency-key"],
            },
            {
                "component": "StockQA",
                "entry_point": "src.utils.quick_scan_refresh_admission_cli",
                "entry_type": "module",
                "permission_boundary": "fixed_native_confirmation_and_executor_configuration",
                "purpose": "qa_admission",
                "selectors": ["--config", "--confirmation-id"],
            },
            {
                "component": "StockQA",
                "entry_point": "main_with_llm.py",
                "entry_type": "script",
                "permission_boundary": "durable_admission_current_owner_permission_and_original_Q13_budget",
                "purpose": "qa_execution",
                "selectors": [
                    "--require-search",
                    "--identity-snapshot",
                    "--question-manifest",
                    "--c06-authority",
                    "--spend-authorization",
                    "--quick-scan-owner-config",
                    "--refresh-admission-config",
                    "--refresh-confirmation-id",
                ],
            },
            {
                "component": "StockWiki",
                "entry_point": "stockwiki.quick_scan_subject_dispatch_cli",
                "entry_type": "module",
                "permission_boundary": "independent_current_route_manifest_owner_anchor_before_send",
                "purpose": "pre_send_subject_registration",
                "selectors": [
                    "--root",
                    "--iqs-code-root",
                    "--iqs-release-root",
                    "--operation",
                    "--request",
                    "--subject-key",
                    "--scope",
                    "--scope-id",
                    "--expected-decision-id",
                    "--expected-anchor-version",
                ],
            },
            {
                "component": "StockWiki",
                "entry_point": "stockwiki.quick_scan_qualification_cli",
                "entry_type": "module",
                "permission_boundary": "fixed_review_authority_real_OS_principal_and_original_payload",
                "purpose": "operator_review",
                "selectors": ["--root", "--iqs-code-root", "--request"],
            },
            {
                "component": "StockQA",
                "entry_point": "src.utils.quick_scan_execution_inspect_cli",
                "entry_type": "module",
                "permission_boundary": "fixed_owner_executor_readonly_original_ledger",
                "purpose": "execution_inspection",
                "selectors": ["--ledger", "--request"],
            },
            {
                "component": "StockWiki",
                "entry_point": "stockwiki.quick_scan_execution_qualification_cli",
                "entry_type": "module",
                "permission_boundary": "fixed_execution_policy_real_OS_principal_and_native_execution_proof",
                "purpose": "execution_qualification",
                "selectors": ["--root", "--iqs-code-root", "--request"],
            },
        ],
        "store_id": "qsobs_0000000000000000",
        "supported_operations": [
            "get_profiles",
            "search",
            "candidate_set",
            "nominate",
            "report_conflict",
            "research_context",
        ],
        "supported_query_wires": [
            {"operations": ["get_profiles"], "schema_version": "2.0.0"},
            {"operations": ["search", "candidate_set"], "schema_version": "2.1.0"},
            {"operations": ["search", "candidate_set"], "schema_version": "2.2.0"},
            {"operations": ["nominate", "report_conflict"], "schema_version": "2.3.0"},
            {"operations": ["research_context"], "schema_version": "2.4.0"},
            {"operations": ["search", "candidate_set"], "schema_version": "2.5.0"},
        ],
        "unmapped_identity_market_labels": [],
        "watermark": {
            "observation_sequence": 0,
            "owner_context_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "read_at": "2020-01-01T00:00:00Z",
            "scope_inventory_sha256": None,
            "selected_index_sha256": None,
            "source_consistency": "sequential_owner_reads",
            "source_watermarks": [
                {
                    "content_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "namespace": "universe",
                    "read_at": "2020-01-01T00:00:00Z",
                    "schema_version": 6,
                    "sequence": 0,
                    "status": "available",
                },
                {
                    "content_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "namespace": "identity",
                    "read_at": "2020-01-01T00:00:00Z",
                    "schema_version": 6,
                    "sequence": 0,
                    "status": "available",
                },
                {
                    "content_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "namespace": "analysis_subjects",
                    "read_at": "2020-01-01T00:00:00Z",
                    "schema_version": 1,
                    "sequence": 0,
                    "status": "available",
                },
                {
                    "content_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "namespace": "observations",
                    "read_at": "2020-01-01T00:00:00Z",
                    "schema_version": 4,
                    "sequence": 0,
                    "status": "available",
                },
            ],
            "store_id": "qsobs_0000000000000000",
        },
    },
    "schema_version": "1.1.0",
}


@pytest.fixture
def readiness(tmp_path, monkeypatch):
    code = tmp_path / "iqs"
    schema = code / "schemas/quick_scan/query-capabilities.schema.json"
    schema.parent.mkdir(parents=True)
    schema.write_bytes(SCHEMA.read_bytes())
    wiki = tmp_path / "wiki-code"
    wiki.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sentinel = workspace / "unchanged.bin"
    sentinel.write_bytes(b"original-owned-workspace")
    runs = tmp_path / "runs"
    config = dict(
        iqs_code_root=str(code),
        stockwiki_code_root=str(wiki),
        stockwiki_workspace_root=str(workspace),
        runs_dir=str(runs),
    )
    checks, calls = [], []
    mapper = OwnerResearchServices.__new__(OwnerResearchServices)
    mapper._assert_registered_inputs = lambda: checks.append("checked")
    mapper.owner = SimpleNamespace(
        native=SimpleNamespace(
            client=SimpleNamespace(
                config=config,
                _environment={
                    "PATH": os.environ.get("PATH", ""),
                    "DEEPSEEK_API_KEY": "NEVER-IN-CHILD",
                },
            )
        )
    )
    payload = copy.deepcopy(CAPABILITIES)

    def run(command, **kwargs):
        calls.append((command, kwargs))
        kwargs["stdout"].write(json.dumps(payload).encode("utf-8"))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    return mapper, payload, calls, checks, runs, sentinel


def test_missing_subject_schema_rejected_before_search(readiness):
    mapper, payload, _, _, runs, sentinel = readiness
    payload["result"]["watermark"]["source_watermarks"][-1]["schema_version"] = 2
    with pytest.raises(OwnerResearchRejected, match="subject_dispatch_initialization_required"):
        mapper.preflight_subject_dispatch()
    assert not list(runs.iterdir())
    assert sentinel.read_bytes() == b"original-owned-workspace"


@pytest.mark.parametrize("version", [3, 4])
def test_initialized_schema_uses_fixed_readonly_cli_and_cleans_up(readiness, version):
    mapper, payload, calls, checks, runs, sentinel = readiness
    payload["result"]["watermark"]["source_watermarks"][-1]["schema_version"] = version
    result = mapper.preflight_subject_dispatch()
    assert result["observation_schema_version"] == version
    assert result["dispatch_started"] is False
    assert result["financial_accuracy_proven"] is False
    command, kwargs = calls[0]
    config = mapper.owner.native.client.config
    assert command == [
        sys.executable,
        "-B",
        "-X",
        "utf8",
        "-m",
        "stockwiki.quick_scan_query_v2_cli",
        "--root",
        config["stockwiki_workspace_root"],
        "--iqs-code-root",
        config["iqs_code_root"],
        "--capabilities",
        "--capabilities-version",
        "1.1.0",
    ]
    assert kwargs["cwd"] == config["stockwiki_code_root"]
    assert kwargs["timeout"] == 240 and kwargs.get("shell", False) is False
    assert "DEEPSEEK_API_KEY" not in kwargs["env"]
    assert kwargs["env"]["PYTHONPATH"] == config["stockwiki_code_root"]
    assert kwargs["env"]["TEMP"] == kwargs["env"]["TMP"]
    assert Path(kwargs["env"]["TEMP"]).parent == runs
    assert not Path(kwargs["env"]["TEMP"]).exists()
    assert not list(runs.iterdir())
    assert checks == ["checked", "checked"]
    assert sentinel.read_bytes() == b"original-owned-workspace"


@pytest.mark.parametrize("status", ["missing", "read_error", "schema_unavailable"])
def test_producer_unavailable_even_with_version_four_is_refused(readiness, status):
    mapper, payload, *_ = readiness
    mark = payload["result"]["watermark"]["source_watermarks"][-1]
    mark.update(status=status, schema_version=4, sequence=None, content_sha256=None)
    with pytest.raises(OwnerResearchRejected):
        mapper.preflight_subject_dispatch()


@pytest.mark.parametrize(
    "case",
    [
        "protocol",
        "schema",
        "message",
        "database_writes",
        "API_requests",
        "dispatch_started",
        "store",
        "duplicate",
        "missing_native",
        "wrong_native",
    ],
)
def test_envelope_or_dispatch_contract_mismatch_refused(readiness, case):
    mapper, payload, *_ = readiness
    if case in {"protocol", "schema", "message"}:
        payload[
            {"protocol": "protocol", "schema": "schema_version", "message": "message_type"}[case]
        ] = "wrong"
    elif case in {"database_writes", "API_requests", "dispatch_started"}:
        payload["result"][case] = True if case == "dispatch_started" else 1
    elif case == "store":
        payload["result"]["watermark"]["store_id"] = "qsobs_1111111111111111"
    elif case == "duplicate":
        payload["result"]["watermark"]["source_watermarks"][0] = copy.deepcopy(
            payload["result"]["watermark"]["source_watermarks"][-1]
        )
    else:
        native = next(
            i
            for i in payload["result"]["native_interfaces"]
            if i["purpose"] == "pre_send_subject_registration"
        )
        native["purpose" if case == "missing_native" else "entry_point"] = "other"
    with pytest.raises(OwnerResearchRejected):
        mapper.preflight_subject_dispatch()


@pytest.mark.parametrize(
    "case", ["timeout", "nonzero", "invalid_json", "duplicate_json", "too_large"]
)
def test_transport_failure_refused_and_owned_temp_removed(readiness, monkeypatch, case):
    mapper, _, _, _, runs, sentinel = readiness

    def run(command, **kwargs):
        if case == "timeout":
            raise subprocess.TimeoutExpired(command, 240)
        raw = {
            "invalid_json": b"not-json",
            "duplicate_json": b'{"protocol":1,"protocol":2}',
            "too_large": b" " * (8 * 1024 * 1024 + 1),
        }.get(case, b"{}")
        kwargs["stdout"].write(raw)
        return SimpleNamespace(returncode=1 if case == "nonzero" else 0)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(OwnerResearchRejected):
        mapper.preflight_subject_dispatch()
    assert not list(runs.iterdir())
    assert sentinel.read_bytes() == b"original-owned-workspace"


def test_constructor_calls_readiness_before_any_dispatch(monkeypatch):
    # Constructor wiring only: original classes with explicit boundary doubles.
    # This is not an owner-admission positive or actual sender evidence.
    from src.providers.llm_client import LLMClient
    from src.runners.llm_runner import QuickScanWorkLifecycle
    from src.utils.quick_scan_owner_refresh import OwnerRefreshSession
    from src.utils.quick_scan_refresh_execution import ApprovedRefreshSession
    from tests.unit.test_quick_scan_budget import _policy

    budget = _policy(model="deepseek-flash")
    route = budget["routes"][0]
    store = SimpleNamespace(get_research_visible_output=lambda *_: None)
    native = OwnerRefreshSession.__new__(OwnerRefreshSession)
    native.store = store
    owner = ApprovedRefreshSession.__new__(ApprovedRefreshSession)
    owner.native, owner.service = native, SimpleNamespace(store=store, budget=budget)
    owner.plan = {"provider": "deepseek"}
    owner.assert_provider_model = lambda *_: None
    owner.execution_routes = lambda *_: [route]
    owner.check = lambda: None
    lifecycle = QuickScanWorkLifecycle.__new__(QuickScanWorkLifecycle)
    for key, value in dict(
        _owner_refresh=owner,
        _store=store,
        _transport_managed=True,
        _budget_policy=budget,
        _model_requested="deepseek-flash",
        _route_id=route["id"],
        _budget_route=route,
        _provider_name=route["provider_config_ref"],
        _model_resolution=None,
        _observation_context={},
    ).items():
        setattr(lifecycle, key, value)
    task = SimpleNamespace(raw={}, questions=[], company_doc={}, company_ref={}, limits={})
    client = LLMClient(
        "SYNTHETIC-NOT-A-KEY",
        "deepseek-flash",
        "https://api.deepseek.com/chat/completions",
        provider_name="deepseek",
    )
    monkeypatch.setattr(OwnerResearchServices, "_validate_task", lambda _: None)
    calls = []

    def readiness(self):
        calls.append("readiness_before_dispatch")
        raise OwnerResearchRejected("synthetic_schema2_no_send")

    monkeypatch.setattr(OwnerResearchServices, "preflight_subject_dispatch", readiness)
    with pytest.raises(OwnerResearchRejected, match="synthetic_schema2_no_send"):
        OwnerResearchServices(lifecycle, client, task=task)
    assert calls == ["readiness_before_dispatch"]


def test_schema_cannot_trigger_implicit_network_resolution(readiness):
    mapper, _, _, _, runs, _ = readiness
    schema = (
        Path(mapper.owner.native.client.config["iqs_code_root"])
        / "schemas/quick_scan/query-capabilities.schema.json"
    )
    schema.write_text(
        '{"$ref":"https://never-request.invalid/foreign-schema.json"}', encoding="utf-8"
    )
    with pytest.raises(OwnerResearchRejected, match="readiness_unavailable"):
        mapper.preflight_subject_dispatch()
    assert not list(runs.iterdir())
