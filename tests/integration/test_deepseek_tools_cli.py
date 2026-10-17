"""Public main CLI cold/warm test, only synthetic identities and HTTP doubles."""

import json

from tests.integration.test_external_context_cli_e2e import _inputs
from tests.integration.test_quick_scan_cli import _invoke
from tests.unit.test_deepseek_tools import _search_result, document
from tests.unit.test_external_search_provider import _response


def test_public_cli_deepseek_search_cold_then_warm_no_second_http(tmp_path, monkeypatch):
    kwargs, session, path = _inputs(tmp_path, monkeypatch)
    old = json.loads(path.read_text("utf-8"))
    new = document()
    new["execution_plan"] = old["execution_plan"]
    for query in new["execution_plan"]["queries"]:
        query.update(operation="search", source_operation_id=None, source_receipt_sha256=None)
    path.write_text(json.dumps(new), encoding="utf-8")
    kwargs["model_policy"]["budget"]["currency"] = "CNY"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-only")
    session.request.return_value = _response(json.dumps(_search_result()).encode())
    code, output, models = _invoke(monkeypatch, tmp_path, **kwargs)
    assert code == 0
    result = json.loads(output.read_text("utf-8"))
    assert result["answers"]["IQS_05"]["score"] == 6
    receipt = result["execution_receipts"]["IQS_05"]
    assert receipt["search_status"] == "executed"
    assert (
        receipt["search_binding"]["external_context_use"]["retrievals"][0]["route_kind"]
        == "deepseek_search"
    )
    assert session.request.call_count == 1 and models.post.call_count == 1
    code, output, models = _invoke(monkeypatch, tmp_path, **kwargs)
    assert code == 0 and models.post.call_count == 0 and session.request.call_count == 1
    assert json.loads(output.read_text("utf-8"))["execution_receipts"]["IQS_05"] == receipt
