"""Native approved pack3 branch, one-member tail, actual main and fake HTTP.

Only vendor HTTP is replaced. Every identity/PRE/consent/action/pack/checkpoint
is created by its original implementation in owned synthetic stores.
This is not three-question accuracy, human consent or financial qualification.
"""

import copy
import json
import sys
from unittest.mock import Mock

import pytest

from src.config.llm_config import LLMConfig
from tests.integration.test_c06_subject_public_e2e import actual, utc
from tests.integration.test_controlled_refresh_public import controlled_public_fixture
from tests.unit.test_llm_config import _quick_scan_policy
from tests.unit.test_quick_scan_observation_context import content
from tests.unit.test_quick_scan_work_transport import _http_response


@pytest.mark.parametrize("actual", ["1.2.0"], indirect=True)
def test_public_main_pack3_tail_preserves_native_store_identity_controls_and_keyless_recovery(
    actual, tmp_path, monkeypatch, capsys
):
    run_public_main_pack3(actual, tmp_path, monkeypatch, capsys)


def run_public_main_pack3(actual, tmp_path, monkeypatch, capsys, *, target_model=None):
    import main_with_llm
    from src.providers import llm_client

    model = target_model or "mimo-v2.6-flash"
    selected = "backup" if target_model else "primary"
    policy = _quick_scan_policy(order=("primary", "backup") if target_model else ("primary",))
    policy["models"][0]["model"] = "mimo-v2.6-flash"
    providers = dict(
        primary=dict(
            enabled=True,
            api_key="synthetic-only",
            model="mimo-v2.6-flash",
            base_url="https://api.xiaomimimo.com/v1/chat/completions",
            max_retries=1,
            format_repair_budget=0,
        )
    )
    if target_model:
        policy["models"][1]["model"] = model
        providers["backup"] = dict(providers["primary"], model=model)
    policy["budget"].update(currency="CNY", max_cost=2, max_requests=4, max_cost_per_attempt=1)
    llm_file = tmp_path / "llm_apis.json"
    llm_file.write_text(
        json.dumps(
            dict(
                default_provider=selected,
                providers=providers,
                quick_scan_model_policy=policy,
            )
        ),
        encoding="utf-8",
    )
    normalized = LLMConfig(str(llm_file)).get_quick_scan_model_policy(default_provider="primary")
    target = (
        dict(
            provider="mimo",
            model=model,
            route_id="backup-route",
            provider_config_ref="backup",
        )
        if target_model
        else None
    )
    ready = controlled_public_fixture(
        actual,
        tmp_path,
        monkeypatch,
        capsys,
        policy=normalized,
        ledger_path=tmp_path / "quick_scan_work.sqlite",
        pack_size=3,
        comparison_target=target,
    )
    prepared = ready["prepared"]
    store = ready["material"]["store"]
    bundle = prepared["bundle"]
    qid = prepared["q"]["id"]
    http = Mock()
    sent = []

    def post(*args, **kwargs):
        data = kwargs["json"]
        sent.append(copy.deepcopy(data))
        assert len(sent) == 1 and data["model"] == model
        assert data["thinking"] == {"type": "enabled"} and data["max_completion_tokens"] == 32768
        body = copy.deepcopy(content())
        body.update(question_id=qid, information_as_of=utc()[:10])
        body["evidence"][0]["url"] = "https://example.org/source"
        answer = dict(
            entity_id=prepared["ref"]["entity_id"],
            company_name="Synthetic E2E issuer",
            question_id=qid,
            status="scored",
            score=8,
            description=json.dumps(body),
            information_as_of=utc()[:10],
        )
        response = _http_response(200)
        response.json.return_value = dict(
            id="synthetic-control-pack-response",
            model=model,
            choices=[
                dict(
                    finish_reason="stop",
                    message=dict(
                        content=json.dumps({"answers": [answer]}),
                        annotations=[dict(type="url_citation", url="https://example.org/source")],
                    ),
                )
            ],
            usage=dict(
                prompt_tokens=100,
                completion_tokens=5000,
                total_tokens=5100,
                completion_tokens_details=dict(reasoning_tokens=4500),
                web_search_usage=dict(tool_usage=1),
            ),
        )
        return response

    http.post.side_effect = post
    monkeypatch.setattr(llm_client.http_client_manager, "get_sync_session", lambda: http)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir(exist_ok=True)
    spend = tmp_path / "spend.json"
    spend.write_text(
        json.dumps(
            dict(
                schema_version="1.0.0",
                currency="CNY",
                hard_cap=2,
                pricing_snapshot_ref="synthetic-user-cap",
                authorized_at=utc(),
            )
        ),
        encoding="utf-8",
    )
    output = tmp_path / "pack-result.json"
    argv = [
        "main_with_llm.py",
        "--company",
        "Synthetic E2E issuer",
        "--entity-id",
        prepared["ref"]["entity_id"],
        "--provider",
        selected,
        "--require-search",
        "--config",
        str(bundle / "questions.json"),
        "--output",
        str(output),
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
        ready["confirmation"]["confirmation_id"],
        "--question-pack-size",
        "3",
        "--thinking-enabled",
        "true",
        "--max-output-tokens",
        "32768",
        "--spend-authorization",
        str(spend),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    assert main_with_llm.main() == 0, capsys.readouterr().out
    assert len(sent) == 1 and store.get_item(ready["work_id"])["status"] == "result_ready"
    pack_id = store.list_attempts(ready["work_id"])[0]["attempt_id"]
    with store._connect() as connection:
        member = connection.execute(
            "SELECT pack_id FROM quick_scan_question_pack_member WHERE attempt_id=?",
            (pack_id,),
        ).fetchone()
    assert member is not None and store.get_question_pack(member["pack_id"])["member_count"] == 1
    # A one-member tail still used the original pack3 execution branch. Recovery
    # has no new send and must not require model configuration or generation flags.
    llm_file.unlink()
    spend.unlink()
    cutoff = argv.index("--question-pack-size")
    monkeypatch.setattr(sys, "argv", argv[:cutoff])
    assert main_with_llm.main() == 0, capsys.readouterr().out
    assert len(sent) == 1 and json.loads(output.read_bytes())["API_requests"] == 0
    if target_model:
        assert (
            store.get_owner_refresh_binding(ready["work_id"])["binding"]["owner_control"][
                "comparison_target"
            ]["model"]
            == model
        )
        assert store.list_attempts(ready["work_id"])[0]["model_requested"] == model
        assert (
            ready["material"]["store"].get_item(ready["material"]["item"]["work_item_id"])["status"]
            == "failed"
        )
