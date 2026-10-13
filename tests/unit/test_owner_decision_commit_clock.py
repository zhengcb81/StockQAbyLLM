"""Synthetic owner control; no financial labels, native approval or HTTP."""

from datetime import datetime

import pytest

from tests.unit.test_quick_scan_known_outcome import (
    arguments,
    authority,
    fixture,
    immutable,
    resolve,
)


@pytest.mark.parametrize("kind", ["resolution", "retry"])
@pytest.mark.parametrize("clock_delta", [60, -30])
def test_commit_clock_after_authority_recheck_controls_durable_time_and_cooldown(
    tmp_path, kind, clock_delta
):
    store, clock, members, pack, receipt, _, _ = fixture(tmp_path)
    clock[0] += 121
    work_id = members[0]["work_item_id"]
    if kind == "retry":
        resolve(store, members, pack, receipt)
    started = clock[0]

    class DelayedAuthority(type(authority())):
        def authorize(self, request):
            grant = super().authorize(request)
            if self.calls == 2:
                clock[0] += clock_delta
            return grant

    permission = DelayedAuthority()

    def execute():
        if kind == "resolution":
            return store.resolve_known_response(
                work_id, **arguments(store, members, pack, receipt, owner_authority=permission)
            )
        return store.record_owner_retry_decision(
            work_id,
            purpose="retry_unusable_answer",
            retry_after_seconds=90,
            owner_authority=permission,
            idempotency_key="delayed-commit",
        )

    before = store.path.read_bytes()
    physical = immutable(store)
    if clock_delta < 0:
        with pytest.raises(ValueError, match="commit_clock"):
            execute()
        assert store.path.read_bytes() == before
    else:
        result = execute()
        stamp = lambda x: datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp()
        assert stamp(result["decided_at"]) == started + clock_delta
        if kind == "retry":
            assert stamp(result["next_retry_at"]) == started + clock_delta + 90
        else:
            assert store.list_events(work_id)[-1]["occurred_at"] == started + clock_delta
    assert immutable(store) == physical


def test_owner_decision_projection_is_opt_in_and_does_not_change_original_refresh_shape(tmp_path):
    store, clock, members, pack, receipt, _, _ = fixture(tmp_path)
    clock[0] += 121
    resolve(store, members, pack, receipt)
    work_id = members[0]["work_item_id"]
    store.record_owner_retry_decision(
        work_id,
        purpose="comparison",
        retry_after_seconds=0,
        owner_authority=authority(),
        idempotency_key="independent-cell",
        comparison_controls=dict(pack_size=3, thinking_enabled=True, max_output_tokens=32768),
    )
    item = store.get_item(work_id)
    kwargs = dict(
        entity_id=item["entity_id"],
        question_ids=[item["question_id"]],
        scope_bindings={item["question_id"]: dict(scope=item["scope"], scope_id=item["scope_id"])},
    )
    before = store.path.read_bytes()
    original = store.owner_refresh_projection(**kwargs)[item["question_id"]]
    assert set(original) == {"work_item_id", "generation", "status"}
    extended = store.owner_refresh_projection(**kwargs, include_owner_decisions=True)[
        item["question_id"]
    ]
    assert extended["retry_purpose"] == "comparison" and extended["delivery_required"] is False
    assert extended["comparison_controls"] == dict(
        pack_size=3, thinking_enabled=True, max_output_tokens=32768
    )
    with pytest.raises(ValueError):
        store.owner_refresh_projection(**kwargs, include_owner_decisions=1)
    assert store.path.read_bytes() == before
