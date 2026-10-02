"""Cross-process durable budget and concurrency integration coverage."""

from __future__ import annotations

import multiprocessing
import time
from concurrent.futures import ThreadPoolExecutor


def _parallel_budget_worker(database_path, state, start_event, worker_index):
    """Issue ten real client dispatches through a blocking in-memory HTTP stub."""
    from src.providers.llm_client import LLMClient, http_client_manager
    from src.utils.quick_scan_work_store import QuickScanWorkStore
    from src.utils.quick_scan_work_transport import (
        bind_quick_scan_budget,
        bind_quick_scan_route,
    )

    models = {
        "fixture-a1": ("route-a1", "A"),
        "fixture-a2": ("route-a2", "A"),
        "fixture-b1": ("route-b1", "B"),
        "fixture-b2": ("route-b2", "B"),
    }
    policy = {
        "configured": True,
        "policy_id": "par-01-shared-policy",
        "policy_version": "par-01-shared-policy@v1",
        "budget": {
            "currency": "USD",
            "max_cost": 2,
            "max_requests": 20,
            "max_cost_per_attempt": 0.1,
        },
        "cost_policy": {
            "pricing_basis": "verified_rate_card",
            "pricing_ref": "par-01-fixture-rate-v1",
            "reserve_before_dispatch": True,
            "unknown_actual_cost_action": "retain_reservation_and_pause",
        },
        "dispatch": {"max_in_flight_total": 4},
        "quota_groups": [
            {"id": "A", "max_in_flight": 2},
            {"id": "B", "max_in_flight": 2},
        ],
        "routes": [
            {
                "id": route_id,
                "provider_config_ref": "fixture-openai-account",
                "model": model,
                "quota_group": group,
                "max_in_flight": 1,
                "eligible": True,
                "unavailable_reason": None,
            }
            for model, (route_id, group) in models.items()
        ],
    }

    class _Response:
        status_code = 200

        def __init__(self, request_number, model):
            self.headers = {"x-request-id": f"par-01-{request_number}"}
            self._payload = {
                "id": f"response-{request_number}",
                "status": "completed",
                "model": model,
                "output": [
                    {
                        "type": "web_search_call",
                        "id": f"search-{request_number}",
                        "status": "completed",
                        "action": {
                            "type": "search",
                            "sources": [
                                {
                                    "type": "url",
                                    "url": "https://example.test/fixture-source",
                                }
                            ],
                        },
                    },
                    {
                        "type": "message",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "fixture result"}],
                    },
                ],
            }

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class _BlockingSession:
        def post(self, _url, *, json, **_kwargs):
            model = json["model"]
            route_id, group = models[model]
            with state["lock"]:
                active_total = state["active_total"] + 1
                state["active_total"] = active_total
                state["max_total"] = max(state["max_total"], active_total)
                if active_total == 4:
                    state["first_wave_release"].set()
                group_active = state["group_active"][group] + 1
                state["group_active"][group] = group_active
                state["max_group"][group] = max(state["max_group"][group], group_active)
                route_active = state["route_active"][route_id] + 1
                state["route_active"][route_id] = route_active
                state["max_route"][route_id] = max(state["max_route"][route_id], route_active)
                request_number = state["starts"] + 1
                state["starts"] = request_number

            if request_number <= 4 and not state["first_wave_release"].wait(timeout=15):
                raise RuntimeError("first four admitted HTTP requests did not overlap")
            # Hold the HTTP boundary long enough for queued work in both
            # processes to contend for the same durable capacity slots.
            time.sleep(0.08)

            with state["lock"]:
                state["active_total"] -= 1
                state["group_active"][group] -= 1
                state["route_active"][route_id] -= 1
            return _Response(request_number, model)

    http_client_manager.get_sync_session = lambda: _BlockingSession()
    store = QuickScanWorkStore(database_path)

    def dispatch(index):
        model = list(models)[(worker_index * 10 + index) % len(models)]
        route_id, group = models[model]
        client = LLMClient(
            api_key="fixture-only",
            model=model,
            base_url="https://api.openai.com/v1/responses",
            provider_name="openai",
        )
        start_event.wait(20)
        with bind_quick_scan_budget(
            store,
            policy,
            cost_resolver=lambda _receipt: {
                "actual_cost": 0.01,
                "pricing_ref": "par-01-fixture-rate-v1",
                "source_ref": "par-01-local-usage-stub",
            },
        ):
            with bind_quick_scan_route(
                route_id=route_id,
                provider="fixture-openai-account",
                model_requested=model,
                quota_group=group,
            ):
                return client.send_search_request(f"fixture question {worker_index}-{index}")

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(dispatch, range(10)))
    assert len(results) == 10
    with state["lock"]:
        state["completed_workers"] += 1


def _parallel_state(manager):
    return manager.dict(
        {
            "lock": manager.RLock(),
            "active_total": 0,
            "max_total": 0,
            "starts": 0,
            "completed_workers": 0,
            "first_wave_release": manager.Event(),
            "group_active": manager.dict({"A": 0, "B": 0}),
            "max_group": manager.dict({"A": 0, "B": 0}),
            "route_active": manager.dict(
                {"route-a1": 0, "route-a2": 0, "route-b1": 0, "route-b2": 0}
            ),
            "max_route": manager.dict({"route-a1": 0, "route-a2": 0, "route-b1": 0, "route-b2": 0}),
        }
    )


def test_two_processes_share_budget_and_real_dispatch_concurrency_slots(tmp_path):
    """Twenty actual POSTs obey the shared budget and all three slot limits."""
    from src.utils.quick_scan_work_store import QuickScanWorkStore

    context = multiprocessing.get_context("spawn")
    database_path = str(tmp_path / "shared-budget.sqlite")
    manager = context.Manager()
    state = _parallel_state(manager)
    start_event = manager.Event()
    processes = [
        context.Process(
            target=_parallel_budget_worker,
            args=(database_path, state, start_event, worker_index),
        )
        for worker_index in range(2)
    ]
    try:
        # Initialize schema before either worker opens the shared SQLite file.
        QuickScanWorkStore(database_path)
        for process in processes:
            process.start()
        start_event.set()
        for process in processes:
            process.join(timeout=45)
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        assert [process.exitcode for process in processes] == [0, 0]

        store = QuickScanWorkStore(database_path)
        status = store.get_quick_scan_budget_status("par-01-shared-policy")
        assert state["completed_workers"] == 2
        assert state["starts"] == 20
        assert state["max_total"] == 4
        assert state["max_group"]["A"] <= 2
        assert state["max_group"]["B"] <= 2
        assert all(
            state["max_route"][route] <= 1
            for route in ("route-a1", "route-a2", "route-b1", "route-b2")
        )
        assert status["requests"] == 20
        assert status["spent_micros"] == 200_000
        assert status["reserved_micros"] == 0
        assert status["in_flight"] == 0
        assert status["unreconciled_attempts"] == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        manager.shutdown()
