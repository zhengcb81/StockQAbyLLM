"""Explicit private saved-native proof; no owner fabrication or network."""

import copy
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("IQS_BATCH_ORIGINAL_ROOT") is None,
    reason="explicit private saved native proof only",
)


def test_same_registered_native_scope_projects_repeatably_without_caller_intent():
    from iqs_agent_runtime_lab.receiptmap import canonical_json, project_batch_response
    from iqs_company_agent_lab.answers import parse_answers_payload
    from iqs_company_agent_lab.task import task_from_bundle

    from src.utils.quick_scan_owner_budget_continuation_v15 import _read_view
    from src.utils.quick_scan_owner_research import (
        OwnerResearchRejected,
        _native_batch_scope,
    )
    from src.utils.quick_scan_refresh_admission_journal import read_record

    root = Path(os.environ["IQS_BATCH_ORIGINAL_ROOT"])
    path = root / "priced-safe-recovery-candidate/evidence/actual102-backup.sqlite"
    original = path.read_bytes()
    base = root / "actual-native-catl-profile-01"
    task = task_from_bundle(json.loads((base / "cold-session/task-bundle.json").read_bytes()))
    config = json.loads((base / "run-config.json").read_bytes())
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        view = _read_view(db, path)
        admission = read_record(db, config["confirmation_id"])
        entries = {
            row["question_id"]: dict(kind="work", work_item_id=row["work_item_id"])
            for row in admission["receipt"]["items"]
        }
        sdk = "4d46a825-728f-4372-8947-26ec3737ff3f"
        first = _native_batch_scope(view, task, entries, sdk)
        second = _native_batch_scope(view, task, entries, sdk)
        assert first == second == ["IQS_12", "IQS_16"]
        saved = view.get_research_visible_output(sdk)
        before = copy.deepcopy(saved)
        output = saved["visible_output"]
        a = project_batch_response(
            task,
            first,
            content=output["content"],
            tool_calls=output["tool_calls"],
            finish_reason=output["finish_reason"],
        )
        b = project_batch_response(
            task,
            second,
            content=output["content"],
            tool_calls=output["tool_calls"],
            finish_reason=output["finish_reason"],
        )
        assert a == b and a["tool_calls"] is None and a["finish_reason"] == "stop"
        parsed = parse_answers_payload(a["content"])
        assert parsed["answers"] == output["tool_calls"][0]["arguments"]["answers"]
        assert (
            parsed["declared_question_ids"] == output["tool_calls"][0]["arguments"]["question_ids"]
        )
        assert saved == before == view.get_research_visible_output(sdk)
        with pytest.raises(OwnerResearchRejected):
            _native_batch_scope(view, task, entries, "wrong-native-SDK")
        with pytest.raises(OwnerResearchRejected):
            _native_batch_scope(view, task, {first[0]: entries[first[0]]}, sdk)
    assert path.read_bytes() == original
