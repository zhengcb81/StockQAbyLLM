"""Exact, source-scoped model alias authorization; synthetic models only."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from src.providers.model_resolution import (
    model_resolution_allowed,
    model_resolution_sha256,
    normalize_model_resolution,
)


def _policy(provider="minimax", protocol="responses", requested="model-a", resolved="model-b"):
    return {
        "schema_version": "1.0.0",
        "aliases": [
            {
                "provider": provider,
                "protocol": protocol,
                "requested_model": requested,
                "resolved_model": resolved,
            }
        ],
    }


@pytest.mark.parametrize(
    "provider, protocol",
    [
        ("openai", "responses"),
        ("minimax", "responses"),
        ("minimax", "anthropic_messages"),
        ("mimo", "mimo_chat_completions"),
    ],
)
def test_exact_is_default_but_replacement_requires_source_scoped_registration(provider, protocol):
    for policy in (None, {"schema_version": "1.0.0", "aliases": []}):
        assert model_resolution_allowed(provider, protocol, "model-a", "model-a", policy)
        assert not model_resolution_allowed(provider, protocol, "model-a", "model-b", policy)

    policy = _policy(provider, protocol)
    assert model_resolution_allowed(provider, protocol, "model-a", "model-b", policy)
    assert not model_resolution_allowed(provider, protocol, "model-a", "model-c", policy)
    assert not model_resolution_allowed(provider, protocol, "MODEL-A", "model-b", policy)
    assert not model_resolution_allowed(provider, protocol, "model-a", "MODEL-B", policy)
    assert not model_resolution_allowed(provider, protocol, "model-b", "model-a", policy)


def test_alias_does_not_cross_provider_protocol_or_requested_model():
    policy = _policy()
    assert not model_resolution_allowed("openai", "responses", "model-a", "model-b", policy)
    assert not model_resolution_allowed(
        "minimax", "anthropic_messages", "model-a", "model-b", policy
    )
    assert not model_resolution_allowed("minimax", "responses", "other-a", "model-b", policy)
    assert not model_resolution_allowed("unknown", "responses", "model-a", "model-a", policy)
    assert not model_resolution_allowed(
        "openai", "anthropic_messages", "model-a", "model-a", policy
    )


@pytest.mark.parametrize(
    "actual", [None, "", " model-a", "model-a ", "model-a\n", "model*", "model?", "model[a]"]
)
def test_missing_or_ambiguous_http_model_never_becomes_requested(actual):
    assert not model_resolution_allowed("minimax", "responses", "model-a", actual, _policy())


def test_projection_and_digest_are_order_independent_and_detached_from_source():
    policy = _policy()
    policy["aliases"].append(_policy("mimo", "mimo_chat_completions")["aliases"][0])
    frozen = normalize_model_resolution(policy)
    original_digest = model_resolution_sha256(frozen)
    reversed_policy = copy.deepcopy(policy)
    reversed_policy["aliases"].reverse()
    assert normalize_model_resolution(reversed_policy) == frozen
    assert model_resolution_sha256(reversed_policy) == original_digest
    canonical = json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert original_digest == hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    policy["aliases"][0]["resolved_model"] = "unregistered-after-dispatch"
    assert model_resolution_allowed("minimax", "responses", "model-a", "model-b", frozen)
    assert not model_resolution_allowed(
        "minimax", "responses", "model-a", "unregistered-after-dispatch", frozen
    )
    assert model_resolution_sha256(frozen) == original_digest
    assert model_resolution_sha256(policy) != original_digest


def test_absent_and_explicit_empty_have_one_canonical_projection():
    empty = {"schema_version": "1.0.0", "aliases": []}
    assert normalize_model_resolution(None) == empty
    assert model_resolution_sha256(None) == model_resolution_sha256(empty)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(schema_version="2.0.0"),
        lambda p: p.update(unknown=True),
        lambda p: p.pop("aliases"),
        lambda p: p.update(aliases={}),
        lambda p: p["aliases"][0].update(unknown="ignored"),
        lambda p: p["aliases"][0].pop("resolved_model"),
        lambda p: p["aliases"][0].update(provider="MiniMax"),
        lambda p: p["aliases"][0].update(provider="deepseek"),
        lambda p: p["aliases"][0].update(protocol="chat_completions"),
        lambda p: p["aliases"][0].update(provider="openai", protocol="anthropic_messages"),
        lambda p: p["aliases"][0].update(provider="mimo", protocol="responses"),
        lambda p: p["aliases"][0].update(requested_model=""),
        lambda p: p["aliases"][0].update(resolved_model=" "),
        lambda p: p["aliases"][0].update(resolved_model="model\x00b"),
        lambda p: p["aliases"][0].update(resolved_model="model\u200bb"),
        lambda p: p["aliases"][0].update(resolved_model="model\ud800b"),
        lambda p: p["aliases"][0].update(resolved_model="model*"),
        lambda p: p["aliases"][0].update(resolved_model="model?"),
        lambda p: p["aliases"][0].update(resolved_model="model[a]"),
        lambda p: p["aliases"][0].update(resolved_model=" model-b"),
        lambda p: p["aliases"][0].update(resolved_model="model-b "),
        lambda p: p["aliases"][0].update(resolved_model="m" * 201),
        lambda p: p["aliases"][0].update(resolved_model=True),
        lambda p: p["aliases"].append(copy.deepcopy(p["aliases"][0])),
        lambda p: p.update(
            aliases=[dict(p["aliases"][0], resolved_model=f"model-{i}") for i in range(257)]
        ),
    ],
)
def test_invalid_policy_cannot_authorize_even_an_exact_response(mutate):
    policy = _policy()
    mutate(policy)
    with pytest.raises(ValueError, match="quick_scan_model_resolution"):
        normalize_model_resolution(policy)
    with pytest.raises(ValueError, match="quick_scan_model_resolution"):
        model_resolution_allowed("minimax", "responses", "model-a", "model-a", policy)


@pytest.mark.parametrize("value", [False, [], "policy", {}, {"schema_version": "1.0.0"}])
def test_non_policy_sources_are_rejected(value):
    with pytest.raises(ValueError, match="quick_scan_model_resolution"):
        normalize_model_resolution(value)


def test_published_schema_matches_native_validation_for_scoped_mapping():
    from jsonschema import Draft202012Validator

    schema_path = (
        Path(__file__).resolve().parents[2] / "src/config/quick_scan_model_resolution.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    assert not list(validator.iter_errors(_policy()))
    for invalid in (
        dict(_policy(), unknown=True),
        _policy("mimo", "responses"),
        _policy("openai", "anthropic_messages"),
        _policy(resolved="model*"),
        _policy(resolved="model\u200bb"),
        _policy(resolved=" model-b"),
        {"schema_version": "1.0.0", "aliases": [_policy()["aliases"][0]] * 2},
    ):
        assert list(validator.iter_errors(invalid))


@pytest.mark.parametrize("length", [160, 161])
def test_model_resolution_length_boundary_matches_schema_and_durable_limit(length):
    from jsonschema import Draft202012Validator

    policy = _policy(requested="a" * length, resolved="b" * length)
    schema_path = (
        Path(__file__).resolve().parents[2] / "src/config/quick_scan_model_resolution.schema.json"
    )
    validator = Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8")))
    if length == 160:
        assert not list(validator.iter_errors(policy))
        assert normalize_model_resolution(policy) == policy
        assert model_resolution_allowed("minimax", "responses", "a" * length, "b" * length, policy)
    else:
        assert list(validator.iter_errors(policy))
        with pytest.raises(ValueError, match="model identifiers"):
            normalize_model_resolution(policy)


def test_external_resolution_1_1_adds_scoped_deepseek_without_reinterpreting_old_aliases():
    from jsonschema import Draft202012Validator

    old_policy = _policy("deepseek", "responses", "requested-alias", "actual-model")
    with pytest.raises(ValueError, match="unsupported canonical"):
        normalize_model_resolution(old_policy)
    policy = {**old_policy, "schema_version": "1.1.0"}
    normalized = normalize_model_resolution(policy)
    assert normalized == policy
    assert model_resolution_allowed(
        "deepseek", "responses", "requested-alias", "actual-model", policy
    )
    assert not model_resolution_allowed(
        "deepseek", "responses", "requested-alias", "foreign-model", policy
    )
    assert not model_resolution_allowed(
        "openai", "responses", "requested-alias", "actual-model", policy
    )
    assert not model_resolution_allowed(
        "deepseek", "anthropic_messages", "requested-alias", "actual-model", policy
    )
    schema = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "src/config/quick_scan_model_resolution_v1_1.schema.json"
        ).read_text("utf-8")
    )
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    assert not list(validator.iter_errors(policy))
    for invalid in (
        {**policy, "schema_version": "1.0.0"},
        {**policy, "aliases": [_policy("deepseek", "anthropic_messages")["aliases"][0]]},
    ):
        assert list(validator.iter_errors(invalid))
    assert normalize_model_resolution(None) == {"schema_version": "1.0.0", "aliases": []}
    assert model_resolution_allowed("deepseek", "responses", "deepseek-flash", "deepseek-flash")
    assert not model_resolution_allowed("deepseek", "responses", "requested-alias", "actual-model")


@pytest.mark.parametrize(
    "url,provider",
    [
        ("http://api.deepseek.com/responses", "deepseek"),
        ("https://api.deepseek.com.evil.invalid/responses", "deepseek"),
        ("https://api.deepseek.com:444/responses", "deepseek"),
        ("https://api.deepseek.com/responses?token=synthetic", "deepseek"),
        ("https://synthetic@api.deepseek.com/responses", "deepseek"),
        ("https://api.deepseek.com/anthropic/v1/messages", "deepseek"),
        ("https://api.deepseek.com/responses", "openai"),
    ],
)
def test_external_text_endpoint_rejects_untrusted_host_path_and_provider(url, provider):
    from src.providers.llm_client import SearchCapabilityUnavailable, _search_endpoint

    with pytest.raises(SearchCapabilityUnavailable):
        _search_endpoint(url, "deepseek-flash", provider, external_context_only=True)


def test_deepseek_external_text_does_not_claim_native_search_capability():
    from src.providers.llm_client import (
        LLMClient,
        SearchCapabilityUnavailable,
        _search_endpoint,
    )

    client = LLMClient(
        "synthetic-only",
        "deepseek-flash",
        "https://api.deepseek.com/responses",
        provider_name="deepseek",
    )
    assert client.supports_web_search is False
    with pytest.raises(SearchCapabilityUnavailable):
        _search_endpoint(client.base_url, client.model, client.provider_name)
    assert _search_endpoint(
        client.base_url, client.model, client.provider_name, external_context_only=True
    ) == ("https://api.deepseek.com/responses", "deepseek", "responses")
