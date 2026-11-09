"""Native shared scan consumers depend on the public lifecycle contract."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "consumer",
    [
        "quick_scan_company_context",
        "quick_scan_owner_research",
        "quick_scan_shared_company",
        "quick_scan_shared_routes",
    ],
)
def test_shared_native_consumer_uses_only_public_lifecycle_surface(consumer):
    source = (ROOT / "src" / "utils" / f"{consumer}.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    violations = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr.startswith("_")
            and ast.unparse(node.value) in {"lifecycle", "self.lifecycle", "clone"}
        ):
            violations.append(f"{node.lineno}: {ast.unparse(node)}")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"getattr", "setattr", "hasattr"}
            and len(node.args) >= 2
            and ast.unparse(node.args[0]) in {"lifecycle", "self.lifecycle", "clone"}
            and isinstance(node.args[1], ast.Constant)
            and str(node.args[1].value).startswith("_")
        ):
            violations.append(f"{node.lineno}: {ast.unparse(node)}")
    assert not violations, "\n".join(violations)


def test_native_lifecycle_reads_original_attempt_without_opening_or_extending_work(tmp_path):
    from src.runners.llm_runner import QuickScanWorkLifecycle
    from src.utils.quick_scan_owner_refresh import OwnerRefreshSession
    from src.utils.quick_scan_refresh_execution import ApprovedRefreshSession
    from tests.unit.test_quick_scan_work_store import (
        _created,
        _store,
        _successful_search_attempt,
    )

    store = _store(tmp_path, [1800000000.0])
    item = _created(store)
    qid, work_id = item["question_id"], item["work_item_id"]
    owner = ApprovedRefreshSession.__new__(ApprovedRefreshSession)
    owner.native = OwnerRefreshSession.__new__(OwnerRefreshSession)
    owner.native.store = store
    owner.service = SimpleNamespace(store=store)
    owner.entries = {qid: {"kind": "work", "work_item_id": work_id}}
    lifecycle = QuickScanWorkLifecycle(
        store,
        entity_id=item["entity_id"],
        run_id="RUN_1",
        scan_id="SCAN_1",
        identity={},
        route_id="route-1",
        transport_managed=True,
        owner_refresh_session=owner,
    )
    assert lifecycle.company_scan_uses_native_search() is True
    assert lifecycle.company_scan_uses_native_search(recovery_only=True) is False
    _successful_search_attempt(store, work_id)
    before = store.path.read_bytes()
    assert lifecycle.company_scan_uses_native_search(recovery_only=True) is True
    result = lifecycle.company_scan_progress([qid])
    assert result["rows"][0]["latest_attempt"]["phase"] == "response_available"
    assert result["rows"][0]["question_id"] == qid
    with pytest.raises(ValueError, match="original_registered_work_required"):
        lifecycle.seal_saved_delivery("WORK_FOREIGN")
    assert store.path.read_bytes() == before


def test_ordered_route_binding_is_detached_and_only_accepts_original_routes(tmp_path):
    from src.runners.llm_runner import QuickScanWorkLifecycle
    from src.utils.quick_scan_owner_refresh import OwnerRefreshSession
    from src.utils.quick_scan_refresh_execution import ApprovedRefreshSession
    from src.utils.quick_scan_work_store import QuickScanWorkStore

    store = QuickScanWorkStore(tmp_path / "work.sqlite")
    routes = [
        {"id": key, "model": f"model-{key}", "provider_config_ref": key, "quota_group": key}
        for key in ("primary", "fallback")
    ]
    owner = ApprovedRefreshSession.__new__(ApprovedRefreshSession)
    owner.native = OwnerRefreshSession.__new__(OwnerRefreshSession)
    owner.native.store = store
    owner.service = SimpleNamespace(store=store)
    owner.proof = {
        "request": {"protocol": "iqs.refresh_request/1.6.0"},
        "preview": {
            "ordered_route_targets": [
                {
                    "route_id": route["id"],
                    "provider_config_ref": route["provider_config_ref"],
                    "model": route["model"],
                    "provider": "synthetic",
                }
                for route in routes
            ],
        },
    }
    owner.execution_routes = lambda policy: policy["routes"]
    owner.assert_provider_model = lambda *_: None
    lifecycle = QuickScanWorkLifecycle(
        store,
        entity_id="ENT_A",
        run_id="run",
        scan_id="scan",
        identity={},
        owner_refresh_session=owner,
        transport_managed=True,
        route_id="primary",
        provider_name="primary",
        model_requested="model-primary",
        budget_policy={"routes": routes},
        budget_route=routes[0],
    )
    client = SimpleNamespace(provider_name="synthetic", model="model-fallback")
    fallback = lifecycle.for_approved_company_route(routes[1], client)
    binding = fallback.registered_research_work()
    assert (binding.route_id, binding.provider_actual(), binding.model_requested) == (
        "fallback",
        "synthetic",
        "model-fallback",
    )
    binding.budget_route["quota_group"] = "foreign"
    assert fallback.registered_research_work().budget_route["quota_group"] == "fallback"
    assert lifecycle.registered_research_work().route_id == "primary"
    with pytest.raises(ValueError, match="shared_company_unapproved_route"):
        lifecycle.for_approved_company_route({**routes[1], "quota_group": "foreign"}, client)
