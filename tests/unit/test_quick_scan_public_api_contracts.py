"""Versioned public-interface contracts of the StockQA execution owner.

These checks describe the public surface a consumer may rely on: the
declared protocol version, the declared export names, and the lifecycle and
port contracts that make the owner replaceable.

They are data driven on purpose. A new owner operation is a deliberate act:
the owner publishes it in its versioned export contract, and a consumer may
then import it.
"""

import ast
import importlib
from pathlib import Path

import pytest

from src.utils import quick_scan_work_store as store
from src.utils import quick_scan_work_transport as transport

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"

#: Owner modules that publish an explicit, versioned export contract.
VERSIONED_OWNERS = {
    "src.utils.quick_scan_external_journal",
    "src.utils.quick_scan_known_outcome_journal",
    "src.utils.quick_scan_mcp_journal",
    "src.utils.quick_scan_owner_budget_continuation",
    "src.utils.quick_scan_owner_refresh_journal",
    "src.utils.quick_scan_provided_context",
    "src.utils.quick_scan_question_pack_journal",
    "src.utils.quick_scan_research_intent",
    "src.utils.quick_scan_research_output",
    "src.utils.quick_scan_research_session",
    "src.utils.quick_scan_work_store",
}

PUBLIC_API_CONTRACTS = {
    "src.config.quick_scan_search_policy": (
        "stockqa.quick-scan-search-policy-module/1.0.0",
        {"admit_search_route"},
    ),
    "src.providers.llm_client": (
        "stockqa.llm-client-module/1.1.0",
        {
            "SEARCH_MAX_OUTPUT_TOKENS",
            "canonical_http_json",
            "failed_attempt_receipt",
            "response_request_id",
            "response_status_code",
            "resolve_search_endpoint",
            "utc_now",
            "validate_search_generation_controls",
        },
    ),
    "src.providers.llm_provider": (
        "stockqa.llm-provider-module/1.0.0",
        {"final_answer_diagnostic_policy"},
    ),
    "src.providers.llm_response_parser": (
        "stockqa.llm-response-parser-module/1.0.0",
        {"reject_duplicate_json_keys"},
    ),
    "src.utils.quick_scan_external_context": (
        "stockqa.external-context-module/1.0.0",
        {"bind_external_candidate"},
    ),
    "src.utils.quick_scan_external_journal": (
        "stockqa.external-journal-module/1.1.0",
        {
            "DDL_V9_ADDITIONS",
            "DDL_V10_ADDITIONS",
            "ENDPOINTS",
            "MODEL_BUDGET_LABEL",
            "WORK_FINGERPRINT_KEYS",
            "begin",
            "consume",
            "get",
            "get_use",
            "lookup",
            "read_use",
            "record_result",
            "record_use",
            "strict_search_json",
            "assert_scope_resolved",
            "external_operation_cache_key",
            "canonical_external_json",
            "digest_external_json",
            "validate_sha256",
            "external_identity_basis",
            "validate_external_plan",
            "validate_external_receipt",
            "build_external_request_context",
            "can_resume_unsent_external_operation",
            "assert_external_operation_reusable",
            "validate_fetch_parent",
            "validate_tables",
        },
    ),
    "src.utils.quick_scan_mcp_journal": (
        "stockqa.mcp-journal-module/1.1.0",
        {
            "APPROVED_SEARCH_TOOLS",
            "DDL_V13_ADDITIONS",
            "STAGES",
            "SUPPORTED_PROTOCOLS",
            "assert_no_unknown_scope",
            "begin",
            "bind_search",
            "consume",
            "get",
            "get_binding",
            "read_search_binding",
            "record_result",
            "validate_tables",
            "validate_control_response",
            "is_mcp_stage_ready",
            "read_mcp_search_binding",
        },
    ),
    "src.utils.quick_scan_known_outcome_journal": (
        "stockqa.known-outcome-journal-module/1.2.0",
        {
            "DDL_V19_ADDITIONS",
            "OwnerWorkDecisionAuthority",
            "OwnerWorkDecisionGrant",
            "api_known_outcome_journal_authorize",
            "api_known_outcome_journal_physical",
            "api_known_outcome_journal_read",
            "derive_physical_receipt",
            "existing_owner_resolution",
            "get",
            "physical_attempt_projection",
            "projection",
            "read_owner_work_decision",
            "resolution_request",
            "resolve",
            "retry",
            "retry_request",
            "terminal_received_failure",
            "validate_comparison_target",
            "validate_tables",
        },
    ),
    "src.utils.quick_scan_owner_budget_continuation": (
        "stockqa.utils-quick_scan_owner_budget_continuation-module/1.1.0",
        {
            "ATTEMPT_FIELDS",
            "DDL_V24_ADDITIONS",
            "NO_EXTERNAL_SEARCH_POLICY",
            "accepted_control",
            "api_owner_budget_continuation_integer",
            "api_owner_budget_continuation_sha",
            "api_owner_budget_continuation_text",
            "api_owner_budget_continuation_utc",
            "assert_payload",
            "assert_research_task",
            "canonical",
            "cell_key",
            "digest",
            "exposure_snapshot",
            "future_request_core",
            "register_cells",
            "reserve_projection",
            "risk_hold_total",
            "validate_control",
            "validate_retained_rows",
        },
    ),
    "src.utils.quick_scan_question_pack_journal": (
        "stockqa.question-pack-journal-module/1.3.0",
        {
            "read_question_pack",
            "api_question_pack_journal_validates_operation_capability",
            "api_question_pack_journal_ddl_v17",
            "api_question_pack_journal_guard_individual",
            "api_question_pack_journal_get",
            "api_question_pack_journal_mark",
            "api_question_pack_journal_prepare",
            "api_question_pack_journal_read_question_pack",
            "api_question_pack_journal_record",
            "api_question_pack_journal_validate_for_post",
            "api_question_pack_journal_validate_tables",
        },
    ),
    "src.utils.quick_scan_owner_refresh_journal": (
        "stockqa.owner-refresh-journal-module/1.0.0",
        {
            "api_owner_refresh_journal_bind",
            "api_owner_refresh_journal_checkpoint_successor",
            "api_owner_refresh_journal_ddl_v14",
            "api_owner_refresh_journal_guard_send",
            "api_owner_refresh_journal_native_control",
            "api_owner_refresh_journal_ordered_targets",
            "api_owner_refresh_journal_read",
            "api_owner_refresh_journal_validate_tables",
        },
    ),
    "src.utils.quick_scan_provided_context": (
        "stockqa.quick-scan-provided-context-adapter/1.0.0",
        {
            "api_provided_context_assert_sent",
            "api_provided_context_prepare",
            "api_provided_context_read_sources",
        },
    ),
    "src.utils.quick_scan_research_intent": (
        "stockqa.utils-quick_scan_research_intent-module/1.1.0",
        {
            "api_research_intent_encode",
            "api_research_intent_hash",
            "api_research_intent_ddl_v23",
            "api_research_intent_bind",
            "api_research_intent_binding_from_row",
            "api_research_intent_get_response",
            "api_research_intent_bind_native_send",
            "api_research_intent_anchor_original_receipt",
        },
    ),
    "src.utils.quick_scan_research_output": (
        "stockqa.quick-scan-research-output-adapter/1.1.0",
        {
            "api_research_output_ddl_v23",
            "api_research_output_ddl_v27",
            "api_research_output_normalize",
            "api_research_output_persist",
            "api_research_output_public_final",
            "api_research_output_read",
            "api_research_output_stage",
            "api_research_output_visible_sha256",
        },
    ),
    "src.utils.quick_scan_research_session": (
        "stockqa.quick-scan-research-session-module/1.0.0",
        {
            "api_research_session_ddl_v23",
            "api_research_session_register",
            "api_research_session_bind",
            "api_research_session_active_id",
            "api_research_session_guard_turn",
            "api_research_session_attach_turn",
            "api_research_session_active_intent_sha256",
            "api_research_session_find_attempt_for_intent",
            "api_research_session_find_attempt_for_task_intent",
        },
    ),
    "src.utils.quick_scan_result_outbox": (
        "stockqa.result-outbox-module/1.1.0",
        {"CAPABILITIES", "CONTRACT_VERSION_KEYS"},
    ),
    "src.utils.quick_scan_work_store": (
        "stockqa.quick-scan-work-store-module/1.9.0",
        {
            "BudgetAdmissionError",
            "BudgetPolicyConflict",
            "Lease",
            "LeaseFencedError",
            "SendPermit",
            "SCHEMA_VERSION",
            "WorkConflictError",
            "quick_scan_receipt_sha256",
            "ENTITY_ID",
            "budget_policy_projection",
            "canonical_native_events",
            "canonical_source_urls",
            "cost_to_micros",
            "validate_safe_identifier",
            "validate_transport_text",
            "sanitize_transport_receipt",
            "validate_timestamp",
            "unmapped_run_scan_pairs",
            "QuickScanWorkStore",
            "WORK_STORE_API_METHODS",
        },
    ),
    "src.utils.quick_scan_work_transport": (
        "stockqa.quick-scan-work-transport-module/1.2.0",
        {
            "current_quick_scan_budget_binding",
            "current_quick_scan_work_binding",
            "current_quick_scan_question_pack_binding",
            "current_quick_scan_owner_refresh_guard",
            "project_quick_scan_search",
            "project_question_pack_transport",
        },
    ),
    "src.utils.quick_scan_refresh_inspect": (
        "stockqa.read-only-work-view/1.2.0",
        {
            "ReadOnlyWorkView",
            "READ_ONLY_SCHEMA_VERSIONS",
            "READ_ONLY_VIEW_API_PROTOCOL",
            "READ_ONLY_VIEW_API_METHODS",
        },
    ),
    "src.utils.quick_scan_result_proof": (
        "stockqa.quick-scan-result-proof-module/1.1.0",
        {
            "QUICK_SCAN_RESULT_PROOF_PROTOCOL",
            "QuickScanResultProofAdapter",
            "result_proof_port",
            "result_proof_port_for_store",
        },
    ),
    "src.providers.llm_research_client": (
        "stockqa.llm-research-client-module/1.0.0",
        {
            "research_channel_capabilities",
        },
    ),
}


def _imported_module(source_path, node):
    if not node.level:
        return node.module
    package_parts = list(source_path.relative_to(SRC_ROOT).with_suffix("").parts[:-1])
    remove = max(0, node.level - 1)
    if remove:
        package_parts = package_parts[:-remove]
    return ".".join(["src", *package_parts, *([node.module] if node.module else [])])


def _declared_public_callable_surface(owner_type):
    probe = object.__new__(owner_type)
    return {
        name
        for name in dir(owner_type)
        if not name.startswith("_") and callable(getattr(probe, name, None))
    }


def test_owner_module_exports_are_explicit_and_versioned():
    for module_name, (protocol, expected_exports) in PUBLIC_API_CONTRACTS.items():
        module = importlib.import_module(module_name)
        assert module.MODULE_API_PROTOCOL == protocol
        assert expected_exports <= module.MODULE_API_EXPORTS
        assert all(hasattr(module, name) for name in expected_exports)


def test_versioned_api_imports_are_declared_by_their_owners():
    """A consumer imports a versioned owner only through its declared exports."""
    violations = []
    for source_path in sorted(SRC_ROOT.rglob("*.py")):
        if "__pycache__" in source_path.parts:
            continue
        for node in ast.walk(ast.parse(source_path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ImportFrom):
                continue
            module_name = _imported_module(source_path, node)
            if module_name not in VERSIONED_OWNERS:
                continue
            owner = importlib.import_module(module_name)
            exports = getattr(owner, "MODULE_API_EXPORTS", frozenset())
            for alias in node.names:
                if alias.name == "*" or alias.name not in exports:
                    violations.append(
                        (
                            source_path.relative_to(SRC_ROOT).as_posix(),
                            node.lineno,
                            module_name,
                            alias.name,
                        )
                    )
    assert not violations, violations


def test_every_work_store_symbol_imported_by_a_peer_is_in_the_versioned_export_contract():
    owner_name = "src.utils.quick_scan_work_store"
    violations = []
    for source_path in sorted(SRC_ROOT.rglob("*.py")):
        if "__pycache__" in source_path.parts:
            continue
        for node in ast.walk(ast.parse(source_path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ImportFrom):
                continue
            if _imported_module(source_path, node) != owner_name:
                continue
            for alias in node.names:
                if alias.name != "*" and alias.name not in store.MODULE_API_EXPORTS:
                    violations.append(
                        (
                            source_path.relative_to(SRC_ROOT).as_posix(),
                            node.lineno,
                            alias.name,
                        )
                    )
    assert not violations, violations


def test_work_store_uses_only_declared_question_pack_journal_operations():
    owner_name = "src.utils.quick_scan_question_pack_journal"
    owner = importlib.import_module(owner_name)
    tree = ast.parse(Path(store.__file__).read_text(encoding="utf-8"))
    imports = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == owner_name
        for alias in node.names
        if alias.name != "*"
    ]
    assert owner.MODULE_API_PROTOCOL == "stockqa.question-pack-journal-module/1.3.0"
    assert imports and set(imports) <= owner.MODULE_API_EXPORTS, sorted(
        set(imports) - owner.MODULE_API_EXPORTS
    )


def test_transport_imports_only_public_store_contracts():
    tree = ast.parse(Path(transport.__file__).read_text(encoding="utf-8"))
    private_imports = [
        (node.lineno, alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "src.utils.quick_scan_work_store"
        for alias in node.names
        if alias.name.startswith("_")
    ]
    assert not private_imports, private_imports
    assert store.WORK_STORE_API_PROTOCOL == "stockqa.quick_scan_work_store/1.4.0"
    assert store.MODULE_API_PROTOCOL == "stockqa.quick-scan-work-store-module/1.9.0"
    assert {
        "connect",
        "validate_schema",
        "transaction",
        "now",
        "record_event",
        "item_in_transaction",
        "assert_lease",
        "response_record",
        "response_for_checkpoint",
        "ensure_budget_policy_tx",
        "reserve_budget_attempt_tx",
        "record_budget_outcome_tx",
        "external_checkpoint_projection",
        "checkpoint_record",
        "delivery_row",
        "result_delivery_record",
        "store_side_tables",
        "record_frozen_run_refs",
        "prepare_standard_inputs",
        "for_existing_ledger",
        "question_pack_operation",
        "current_question_pack_id",
        "count_route_in_flight_budget_attempts",
    } <= store.WORK_STORE_API_METHODS
    assert all(hasattr(store.QuickScanWorkStore, name) for name in store.WORK_STORE_API_METHODS)
    assert {
        "BudgetAdmissionError",
        "BudgetPolicyConflict",
        "Lease",
        "LeaseFencedError",
        "QuickScanWorkStore",
        "SendPermit",
        "WORK_STORE_API_METHODS",
        "WorkConflictError",
        "SCHEMA_VERSION",
        "quick_scan_receipt_sha256",
    } <= store.MODULE_API_EXPORTS
    assert {
        "create_or_attach",
        "prepare_attempt",
        "save_answer_checkpoint",
        "get_answer_checkpoint",
        "prepare_result_delivery",
    } <= store.WORK_STORE_API_METHODS


def test_shared_store_transaction_adapters_declare_versioned_contracts_and_owned_tables():
    from src.utils import quick_scan_provided_context as provided_context
    from src.utils import quick_scan_research_output as research_output

    assert research_output.MODULE_API_PROTOCOL == (
        "stockqa.quick-scan-research-output-adapter/1.1.0"
    )
    assert provided_context.MODULE_API_PROTOCOL == (
        "stockqa.quick-scan-provided-context-adapter/1.0.0"
    )
    for adapter in (research_output, provided_context):
        assert adapter.STORE_TRANSACTION_PROTOCOL == store.WORK_STORE_API_PROTOCOL
        assert adapter.STORE_TABLE_DEPENDENCIES
        assert adapter.MODULE_API_EXPORTS
        assert all(hasattr(adapter, name) for name in adapter.MODULE_API_EXPORTS)
    assert research_output.STORE_TABLE_DEPENDENCIES == {
        "quick_scan_attempt_response",
        "quick_scan_attempt_resolution",
        "quick_scan_native_search_events",
        "attempt",
        "quick_scan_budget_attempt",
        "quick_scan_budget_terminal",
        "quick_scan_research_visible_output",
        "quick_scan_research_visible_output_recovery",
        "quick_scan_research_session",
        "quick_scan_research_turn",
        "quick_scan_research_intent",
        "work_item",
    }
    assert {
        "quick_scan_research_intent",
        "attempt",
        "quick_scan_question_pack_member",
        "quick_scan_question_pack_send",
        "work_item",
        "quick_scan_owner_refresh_binding",
    } <= provided_context.STORE_TABLE_DEPENDENCIES


def test_work_store_and_read_view_contracts_cover_their_entire_public_method_surface():
    from src.utils.quick_scan_refresh_inspect import (
        READ_ONLY_VIEW_API_METHODS,
        ReadOnlyWorkView,
    )

    assert _declared_public_callable_surface(store.QuickScanWorkStore) == (
        set(store.WORK_STORE_API_METHODS) - {"current_question_pack_id"}
    )
    assert isinstance(store.QuickScanWorkStore.current_question_pack_id, property)
    assert _declared_public_callable_surface(ReadOnlyWorkView) == set(READ_ONLY_VIEW_API_METHODS)


def test_company_agent_facade_is_versioned_and_rejects_unpublished_operations(monkeypatch):
    import sys
    from types import ModuleType

    from src.utils import quick_scan_company_api as api

    assert api.PUBLIC_API_PROTOCOL == "stockqa.company-scan-public-api/1.8.0"
    assert "company_agent_public_api" in api.__all__
    facade = ModuleType("iqs_company_agent_lab.api")
    facade.PUBLIC_API_PROTOCOL = "iqs.company-agent-public-api/2.3.0"
    facade.__all__ = ["task_snapshot"]
    facade.task_snapshot = lambda task: {"task": task}
    facade.task_from_snapshot = lambda snapshot: snapshot
    monkeypatch.setitem(sys.modules, "iqs_company_agent_lab.api", facade)

    # The owner module object never crosses the package boundary.
    projection = api.company_agent_public_api("task_snapshot")
    assert projection is not facade
    assert projection.task_snapshot is facade.task_snapshot
    assert not hasattr(projection, "task_from_snapshot")
    with pytest.raises(ImportError, match="company_agent_public_api_operation_not_exported"):
        api.company_agent_public_api("task_from_snapshot")
    facade.PUBLIC_API_PROTOCOL = "iqs.company-agent-public-api/2.2.0"
    with pytest.raises(ImportError, match="company_agent_public_api_version_unsupported"):
        api.company_agent_public_api("task_snapshot")
    facade.PUBLIC_API_PROTOCOL = "iqs.company-agent-public-api/2.3.0"
    facade.__all__ = ["task_snapshot", "task_from_snapshot"]
    del facade.task_from_snapshot
    with pytest.raises(ImportError, match="company_agent_public_api_operation_missing"):
        api.company_agent_public_api("task_from_snapshot")


def test_saved_publication_rejects_callable_operations_missing_from_facade_exports():
    from types import SimpleNamespace

    from src.utils.quick_scan_saved_research_publication import (
        _supports_declared_public_facade,
    )

    facade = SimpleNamespace(
        PUBLIC_API_PROTOCOL="iqs.company-agent-public-api/2.3.0",
        __all__=["declared_operation"],
        declared_operation=lambda: None,
        callable_but_private=lambda: None,
    )
    assert _supports_declared_public_facade(
        facade,
        "iqs.company-agent-public-api",
        (2, 3, 0),
        ("declared_operation",),
    )
    assert not _supports_declared_public_facade(
        facade,
        "iqs.company-agent-public-api",
        (2, 3, 0),
        ("callable_but_private",),
    )


def test_owner_research_component_loader_rejects_unpublished_callable_operation(monkeypatch):
    import sys
    from types import ModuleType

    from src.utils.quick_scan_owner_research import (
        OwnerResearchRejected,
        _component_api,
    )

    facade = ModuleType("synthetic.component.api")
    facade.PUBLIC_API_PROTOCOL = "synthetic.component/1.0.0"
    facade.__all__ = ["published_operation"]
    facade.published_operation = lambda: None
    facade.callable_but_private = lambda: None
    monkeypatch.setitem(sys.modules, facade.__name__, facade)

    with pytest.raises(OwnerResearchRejected, match="component_public_api_operation_missing"):
        _component_api(
            facade.__name__,
            "synthetic.component",
            (1, 0, 0),
            ("callable_but_private",),
        )


def test_company_scan_lifecycle_contract_is_structural_and_explicit():
    from src.interfaces.quick_scan_lifecycle import (
        QUICK_SCAN_WORK_LIFECYCLE_METHODS,
        QUICK_SCAN_WORK_LIFECYCLE_PROTOCOL,
        QuickScanWorkLifecyclePort,
    )

    class AlternateLifecycle:
        def registered_research_work(self):
            pass

        def company_scan_progress(self, question_ids):
            pass

        def company_scan_uses_native_search(self, *, recovery_only=False):
            pass

        def for_approved_company_route(self, route, client):
            pass

        def reference_question(self, question):
            pass

        def hydrate_question(self, question):
            pass

        def before_question(self, question):
            pass

        def before_registered_questions(self, questions):
            pass

        def dispatch_context(self, handle, *, research_budget_managed=False):
            pass

        def seal_saved_delivery(self, work_item_id):
            pass

        def after_question(self, handle, result):
            pass

        def after_question_failed(self, handle, error):
            pass

        def assert_question_pack_ready(self, questions):
            pass

        def question_pack_group_key(self, question):
            pass

        def dispatch_question_pack_context(self, handles):
            pass

        def project_question_pack_failure(self, execution):
            pass

    class IncompleteLifecycle:
        def registered_research_work(self):
            pass

    assert isinstance(AlternateLifecycle(), QuickScanWorkLifecyclePort)
    assert not isinstance(IncompleteLifecycle(), QuickScanWorkLifecyclePort)
    assert QUICK_SCAN_WORK_LIFECYCLE_PROTOCOL == "stockqa.quick_scan_work_lifecycle/1.1.0"
    assert {
        "before_registered_questions",
        "seal_saved_delivery",
    } <= QUICK_SCAN_WORK_LIFECYCLE_METHODS
    import inspect

    parameters = inspect.signature(QuickScanWorkLifecyclePort.dispatch_context).parameters
    assert tuple(parameters) == ("self", "handle", "research_budget_managed")
    assert parameters["research_budget_managed"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["research_budget_managed"].default is False


def test_read_only_inspection_transaction_annotation_describes_the_borrowed_port():
    import sqlite3
    from typing import Iterator, get_type_hints

    from src.interfaces.sqlite_reader import SQLiteReadConnection
    from src.utils.quick_scan_refresh_inspect import ReadOnlyWorkView, _Borrowed

    assert (
        get_type_hints(ReadOnlyWorkView.inspection_transaction)["return"]
        == Iterator[SQLiteReadConnection]
    )
    connection = sqlite3.connect(":memory:")
    borrowed = _Borrowed(connection)
    try:
        assert isinstance(connection, SQLiteReadConnection)
        assert isinstance(borrowed, SQLiteReadConnection)
    finally:
        borrowed.close()
        connection.close()


def test_research_channel_capabilities_are_a_versioned_provider_export():
    from src.providers import llm_research_client

    assert llm_research_client.MODULE_API_PROTOCOL == "stockqa.llm-research-client-module/1.0.0"
    assert "research_channel_capabilities" in llm_research_client.MODULE_API_EXPORTS


def test_transport_public_context_accessors_never_export_mutable_contextvars():
    expected = {
        "current_quick_scan_work_binding",
        "current_quick_scan_question_pack_binding",
        "current_quick_scan_owner_refresh_guard",
    }
    assert expected <= transport.MODULE_API_EXPORTS
    assert not {
        name for name in transport.MODULE_API_EXPORTS if name.startswith("api_work_transport_")
    }
    for name in expected:
        getter = getattr(transport, name)
        assert callable(getter)
        assert not hasattr(getter, "set") and not hasattr(getter, "reset")
