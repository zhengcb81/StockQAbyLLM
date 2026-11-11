"""Domain direction rules for the StockQA execution owner.

Every check here answers one question about a declared capability domain:
does the production source respect the direction the architecture requires?

They deliberately stay on plain AST facts - imports, attribute names and
declared owner exports. They are not a Python sandbox: they do not chase
dynamic aliases, ``getattr`` tricks, ``exec`` or call-stack authorisation.
A consumer that must not reach undeclared state is proven by the public
boundary tests instead, where the owner hands out a real handle.
"""

import ast
from decimal import Decimal
from pathlib import Path

from src.utils import quick_scan_work_store as store
from src.utils import quick_scan_work_transport as transport

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"

#: Modules that own a raw SQLite connection or an owner transaction seam.
STORE_SQL_ADAPTERS = {
    "utils/quick_scan_delivery_seal.py",
    "utils/quick_scan_external_continuation.py",
    "utils/quick_scan_external_journal.py",
    "utils/quick_scan_known_outcome_journal.py",
    "utils/quick_scan_mcp_journal.py",
    "utils/quick_scan_owner_budget_continuation.py",
    "utils/quick_scan_owner_research.py",
    "utils/quick_scan_prepaid_quota.py",
    "utils/quick_scan_question_pack_journal.py",
    "utils/quick_scan_refresh_admission.py",
    "utils/quick_scan_refresh_batch.py",
    "utils/quick_scan_refresh_execution.py",
    "utils/quick_scan_refresh_inspect.py",
    "utils/quick_scan_provided_context.py",
    "utils/quick_scan_research_intent.py",
    "utils/quick_scan_research_output.py",
    "utils/quick_scan_research_session.py",
    "utils/quick_scan_saved_research_publication.py",
    "utils/quick_scan_uncertain_seal.py",
    "utils/quick_scan_work_store.py",
    "utils/quick_scan_work_transport.py",
}

#: The single module allowed to drive the raw question-pack transaction seam.
QUESTION_PACK_SEAM_OWNER = "utils/quick_scan_question_pack_journal.py"

#: Names that expose the raw question-pack transaction seam.
QUESTION_PACK_SEAM_NAMES = (
    "question_pack_operation",
    "current_question_pack_id",
    "_QUESTION_PACK_OPERATION_CAPABILITY",
)

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

#: Owner modules whose consumers may only use their declared exports.
EXPORT_BOUND_OWNERS = {
    "src.utils.quick_scan_external_journal",
    "src.utils.quick_scan_mcp_journal",
    "src.utils.quick_scan_owner_refresh_journal",
    "src.utils.quick_scan_provided_context",
    "src.utils.quick_scan_question_pack_journal",
    "src.utils.quick_scan_research_intent",
    "src.utils.quick_scan_research_output",
    "src.utils.quick_scan_research_session",
    "src.utils.quick_scan_work_store",
}


def _source_paths():
    for source_path in sorted(SRC_ROOT.rglob("*.py")):
        if "__pycache__" in source_path.parts:
            continue
        yield source_path


def _relative(source_path):
    return source_path.relative_to(SRC_ROOT).as_posix()


def _tree(source_path):
    return ast.parse(source_path.read_text(encoding="utf-8"))


def _imported_module(source_path, node):
    """Return the absolute module name of an ``ImportFrom`` node."""
    if not node.level:
        return node.module
    package_parts = list(source_path.relative_to(SRC_ROOT).with_suffix("").parts[:-1])
    remove = max(0, node.level - 1)
    if remove:
        package_parts = package_parts[:-remove]
    return ".".join(["src", *package_parts, *([node.module] if node.module else [])])


def test_quick_scan_core_does_not_import_persistence_or_transport_implementations():
    forbidden = {
        "src.utils.quick_scan_work_store",
        "src.utils.quick_scan_work_transport",
    }
    violations = []
    for relative in ("core/qa_engine.py", "core/models.py"):
        source_path = SRC_ROOT / relative
        for node in ast.walk(_tree(source_path)):
            module = node.module if isinstance(node, ast.ImportFrom) else None
            if module in forbidden:
                violations.append((relative, node.lineno, module))
    assert not violations, violations


def test_lower_level_company_scan_modules_depend_on_lifecycle_protocol_not_runner_class():
    violations = []
    for relative in (
        "utils/quick_scan_company_context.py",
        "utils/quick_scan_shared_routes.py",
    ):
        source_path = SRC_ROOT / relative
        for node in ast.walk(_tree(source_path)):
            if isinstance(node, ast.ImportFrom) and node.module == "src.runners.llm_runner":
                violations.append((relative, node.lineno, node.module))
    assert not violations, violations


def test_stockqa_source_does_not_import_private_names_from_peer_modules():
    violations = []
    for source_path in _source_paths():
        for node in ast.walk(_tree(source_path)):
            if not isinstance(node, ast.ImportFrom):
                continue
            module_name = _imported_module(source_path, node)
            if not module_name or not module_name.startswith("src."):
                continue
            for alias in node.names:
                if alias.name.startswith("_"):
                    violations.append(
                        (_relative(source_path), node.lineno, module_name, alias.name)
                    )
    assert not violations, violations


def test_owner_refresh_consumers_use_the_versioned_port_instead_of_session_internals():
    consumers = {
        "runners/llm_runner.py",
        "core/qa_engine.py",
        "utils/quick_scan_company_context.py",
        "utils/quick_scan_company_runtime.py",
        "utils/quick_scan_owner_budget_continuation.py",
        "utils/quick_scan_owner_research.py",
        "utils/quick_scan_research_session.py",
        "utils/quick_scan_shared_company.py",
        "utils/quick_scan_work_store.py",
        "utils/quick_scan_work_transport.py",
    }
    violations = []
    for relative in sorted(consumers):
        source_path = SRC_ROOT / relative
        for node in ast.walk(_tree(source_path)):
            if isinstance(node, ast.ImportFrom):
                imported = {alias.name for alias in node.names}
                if imported & {"ApprovedRefreshSession", "OwnerRefreshSession"}:
                    violations.append((relative, node.lineno, "concrete_owner_import"))
            if isinstance(node, ast.Attribute):
                chain = []
                current = node
                while isinstance(current, ast.Attribute):
                    chain.append(current.attr)
                    current = current.value
                if isinstance(current, ast.Name) and current.id in {
                    "owner",
                    "session",
                    "approved_session",
                    "owner_session",
                }:
                    if set(chain) & {"native", "service"}:
                        violations.append((relative, node.lineno, ast.unparse(node)))
    assert not violations, violations


def test_runner_does_not_query_work_store_sql_schema_directly():
    source_path = SRC_ROOT / "runners/llm_runner.py"
    violations = []
    for node in ast.walk(_tree(source_path)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "execute" or not node.args:
            continue
        query = node.args[0]
        if isinstance(query, ast.Constant) and isinstance(query.value, str):
            normalized = query.value.lower()
            if any(
                table in normalized
                for table in (
                    " from attempt ",
                    " from quick_scan_question_pack",
                    " from quick_scan_budget_attempt",
                    " from work_item ",
                    " from answer_checkpoint ",
                    " from work_event ",
                )
            ):
                violations.append((node.lineno, query.value))
    assert not violations, violations


def test_raw_store_connections_are_confined_to_declared_storage_adapters():
    """Only declared adapters may drive an owner store transaction seam."""
    violations = []
    for source_path in _source_paths():
        relative = _relative(source_path)
        if relative in STORE_SQL_ADAPTERS:
            continue
        for node in ast.walk(_tree(source_path)):
            if isinstance(node, ast.Attribute) and node.attr in {"connect", "transaction"}:
                owner = ast.unparse(node.value)
                # A direct sqlite3 open and the read-only view are public seams.
                if owner not in {"sqlite3", "view", "read_view"}:
                    violations.append((relative, node.lineno, f"{owner}.{node.attr}"))
    assert not violations, violations


def test_question_pack_raw_transaction_seam_has_one_declared_consumer():
    """The raw question-pack transaction seam stays with its single owner."""
    violations = []
    for source_path in _source_paths():
        relative = _relative(source_path)
        if relative == QUESTION_PACK_SEAM_OWNER:
            continue
        for node in ast.walk(_tree(source_path)):
            if isinstance(node, ast.Attribute) and node.attr in QUESTION_PACK_SEAM_NAMES:
                violations.append((relative, node.lineno, node.attr))
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in QUESTION_PACK_SEAM_NAMES:
                        violations.append((relative, node.lineno, alias.name))
    assert not violations, violations


def test_production_store_consumers_use_only_owner_public_members():
    """A consumer may call the declared store API, never a private member."""
    from src.utils.quick_scan_refresh_inspect import (
        READ_ONLY_VIEW_API_METHODS,
        ReadOnlyWorkView,
    )

    store_private = {
        name
        for name, value in store.QuickScanWorkStore.__dict__.items()
        if name.startswith("_") and not name.startswith("__") and callable(value)
    }
    store_private.add("_transaction_state")
    view_private = {
        name
        for name, value in ReadOnlyWorkView.__dict__.items()
        if name.startswith("_") and not name.startswith("__") and callable(value)
    }
    forbidden = store_private | view_private
    owner_modules = {"quick_scan_work_store.py", "quick_scan_refresh_inspect.py"}

    violations = []
    undeclared_calls = []
    for source_path in _source_paths():
        if source_path.name in owner_modules:
            continue
        relative = _relative(source_path)
        tree = _tree(source_path)
        # A module may own a private helper of its own; it may not reach a
        # private member that belongs to the store or the read-only view.
        own_private = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name.startswith("_")
            and not node.name.startswith("__")
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in forbidden
                and node.attr not in own_private
            ):
                violations.append((relative, node.lineno, node.attr))
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in store.WORK_STORE_API_METHODS
                and ast.unparse(node.func.value).split(".")[-1]
                in {"store", "work_store", "ledger", "view", "QuickScanWorkStore"}
            ):
                if node.func.attr not in store.WORK_STORE_API_METHODS:
                    undeclared_calls.append((relative, node.lineno, node.func.attr))
    assert not violations, violations
    assert not undeclared_calls, undeclared_calls
    assert set(store.WORK_STORE_API_METHODS) <= {
        name for name in dir(store.QuickScanWorkStore) if not name.startswith("_")
    }
    assert set(READ_ONLY_VIEW_API_METHODS) <= {
        name for name in dir(ReadOnlyWorkView) if not name.startswith("_")
    }


def test_research_and_context_adapter_consumers_import_only_owner_exports():
    """A consumer imports declared owner exports, never an undeclared name."""
    import importlib

    owner_exports = {
        name: set(importlib.import_module(name).MODULE_API_EXPORTS) for name in EXPORT_BOUND_OWNERS
    }
    violations = []
    for source_path in _source_paths():
        for node in ast.walk(_tree(source_path)):
            if not isinstance(node, ast.ImportFrom):
                continue
            module_name = _imported_module(source_path, node)
            if module_name not in owner_exports:
                continue
            for alias in node.names:
                if alias.name == "*":
                    violations.append((_relative(source_path), node.lineno, module_name, "*"))
                elif alias.name not in owner_exports[module_name]:
                    violations.append(
                        (_relative(source_path), node.lineno, module_name, alias.name)
                    )
    assert not violations, violations


def test_research_output_schema_composes_only_versioned_owner_apis():
    from src.utils import (
        quick_scan_research_intent,
        quick_scan_research_output,
        quick_scan_research_session,
    )

    assert quick_scan_research_session.MODULE_API_PROTOCOL == (
        "stockqa.quick-scan-research-session-module/1.0.0"
    )
    assert "api_research_session_ddl_v23" in quick_scan_research_session.MODULE_API_EXPORTS
    assert quick_scan_research_intent.MODULE_API_PROTOCOL == (
        "stockqa.utils-quick_scan_research_intent-module/1.1.0"
    )
    assert "api_research_intent_ddl_v23" in quick_scan_research_intent.MODULE_API_EXPORTS
    assert quick_scan_research_output.MODULE_API_PROTOCOL == (
        "stockqa.quick-scan-research-output-adapter/1.1.0"
    )
    assert {
        "api_research_output_ddl_v23",
        "api_research_output_ddl_v27",
        "api_research_output_stage",
    } <= quick_scan_research_output.MODULE_API_EXPORTS

    # The adapter composes the other owners through their declared exports.
    imported = set()
    for node in ast.walk(_tree(Path(quick_scan_research_output.__file__))):
        if isinstance(node, ast.ImportFrom):
            module_name = _imported_module(Path(quick_scan_research_output.__file__), node)
            if module_name in {
                "src.utils.quick_scan_research_intent",
                "src.utils.quick_scan_research_session",
            }:
                imported.update(alias.name for alias in node.names)
    assert {
        "api_research_session_ddl_v23",
        "api_research_intent_ddl_v23",
    } <= imported
    assert not {"DDL_RESEARCH_SESSION", "DDL_RESEARCH_INTENT"} & imported


def test_transport_codec_facade_preserves_store_validation_semantics():
    """The transport facade keeps the owner's validation semantics."""
    assert store.validate_transport_text("  route-a  ", "route", maximum=40) == "route-a"
    assert store.cost_to_micros(Decimal("0.000001"), "cost", allow_zero=True) == 1
    assert store.canonical_native_events([]) == []
    assert store.sanitize_transport_receipt({"attempt_id": "a", "api_key": "secret"}) == {
        "attempt_id": "a"
    }
