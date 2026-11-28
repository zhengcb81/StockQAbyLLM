"""Real Company Agent package behind the StockQA public facade.

The unit suite consumes the Company Agent as a public fake. This file is the
real-material counterpart: when the actual ``iqs_company_agent_lab`` package
is importable it proves the same facade operations work against the owner's
published contract. Without the package it skips — the fake-based unit suite
already covers the consumer side.
"""

import pytest

from src.utils.quick_scan_company_api import company_agent_public_api

pytest.importorskip(
    "iqs_company_agent_lab",
    reason="real Company Agent package not importable; unit fakes cover the consumer side",
)


def test_real_facade_passes_the_versioned_operation_projection():
    facade = company_agent_public_api(
        "create_owner_task", "task_snapshot", "task_from_snapshot", "LIMIT_CAPS"
    )

    task = facade.create_owner_task(
        {
            "protocol": "iqs.company-scan-task/1.0.0",
            "run_id": "REAL_FACADE_MATERIAL",
            "company_ref": {"path": "company.json", "bytes": 1, "sha256": "a" * 64},
            "information_cutoff": "2026-10-15",
            "limits": {"max_model_requests_per_session": 2},
            "strategy_id": "free_agent",
            "question_refs": [],
        },
        [{"question_id": "Q1", "question": {"id": "Q1"}}],
        {"origin": "synthetic-material", "subject": {"subject_ref": {"entity_id": "ENT_A"}}},
        {"records": []},
        {"provider": "synthetic", "model": "synthetic-model"},
        {},
    )
    contract = facade.task_snapshot(task)
    assert contract["protocol"] == "iqs.company-agent-task-contract/1.1.0"
    assert contract["question_ids"] == ["Q1"]
    restored = facade.task_from_snapshot(contract)
    assert facade.task_snapshot(restored) == contract
    assert isinstance(facade.LIMIT_CAPS, dict)

    # The owner module object itself never crosses the facade.
    assert not hasattr(facade, "__all__")
    with __import__("pytest").raises(AttributeError):
        facade.events  # noqa: B018 - an implementation module must stay hidden
