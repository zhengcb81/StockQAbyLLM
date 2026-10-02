"""LLM 配置管理单元测试。

该模块测试 LLMConfig 类的配置管理功能。
"""

import json
from pathlib import Path

import pytest

from src.config.llm_config import LLMConfig


def _quick_scan_policy(order=("primary", "backup"), *, policy_id="test-policy"):
    models = {
        "primary": {
            "id": "primary-route",
            "enabled": True,
            "provider_config_ref": "primary",
            "model": "model-a",
            "quota_group": "account-a",
            "max_in_flight": 1,
        },
        "backup": {
            "id": "backup-route",
            "enabled": True,
            "provider_config_ref": "backup",
            "model": "model-b",
            "quota_group": "account-b",
            "max_in_flight": 1,
        },
    }
    return {
        "schema_version": "2.0.0",
        "policy_id": policy_id,
        "execution_owner": "StockQAbyLLM",
        "configured": True,
        "dispatch": {
            "max_in_flight_total": 4,
            "max_questions_per_pack": 32,
            "one_entity_per_pack": True,
            "separate_score_and_fact_packs": True,
            "required_capabilities": ["web_search", "structured_output"],
            "on_preferred_capacity_full": "wait",
            "speculative_racing": False,
        },
        "budget": {
            "currency": "USD",
            "max_cost": 100.0,
            "max_requests": 100,
            "max_cost_per_attempt": 10.0,
            "reset_on_restart": False,
        },
        "cost_policy": {
            "pricing_basis": "user_cap",
            "pricing_ref": "fixture-user-cap",
            "include_search_charges": True,
            "include_failed_attempts": True,
            "reserve_before_dispatch": True,
            "unknown_actual_cost_action": "retain_reservation_and_pause",
        },
        "quota_groups": [
            {
                "id": "account-a",
                "max_in_flight": 1,
                "window_seconds_hint": None,
                "unknown_reset_cooldown_seconds": 18000,
                "half_open_probe_limit": 1,
            },
            {
                "id": "account-b",
                "max_in_flight": 1,
                "window_seconds_hint": None,
                "unknown_reset_cooldown_seconds": 18000,
                "half_open_probe_limit": 1,
            },
        ],
        "models": [models[name] for name in order],
        "fallback": {
            "on_quota_exhausted": "cooldown_group_then_next",
            "on_rate_limit": "respect_retry_after_then_next",
            "on_server_error": "bounded_retry_then_next",
            "on_auth_error": "disable_route_then_next",
            "on_invalid_request": "stop_without_fallback",
            "on_malformed_response": "bounded_retry_then_next",
            "on_missing_capability": "skip_before_dispatch",
            "on_timeout": "reconcile_receipt_before_retry",
            "on_low_score": "accept",
            "on_unknown_answer": "accept_with_gap",
            "on_fallback_success": "stop_dispatch_keep_primary_health",
            "max_attempts_per_dispatch_round": 2,
        },
        "comparison": {
            "enabled": False,
            "max_models_per_question": 2,
            "max_cost": 0,
            "max_requests": 0,
            "same_input_and_cutoff_required": True,
            "separate_from_primary_fallback": True,
        },
        "resume": {
            "success_checkpoint": "per_question",
            "replay_outbox_before_dispatch": True,
            "preserve_uncertain_requests": True,
            "persist_cooldowns_and_budget": True,
        },
    }


def _llm_config_content(policy=None):
    content = {
        "default_provider": "primary",
        "providers": {
            "primary": {
                "enabled": True,
                "api_key": "fixture-primary-key",
                "model": "legacy-a",
            },
            "backup": {
                "enabled": True,
                "api_key": "fixture-backup-key",
                "model": "legacy-b",
            },
        },
    }
    if policy is not None:
        content["quick_scan_model_policy"] = policy
    return content


class TestLLMConfigInit:
    """测试 LLMConfig 初始化。"""

    def test_init_with_default_path(self):
        """测试使用默认路径初始化。"""
        config = LLMConfig()
        assert config.config_file is not None
        assert isinstance(config.config_file, Path)

    def test_init_with_custom_path(self, tmp_path):
        """测试使用自定义路径初始化。"""
        config_file = tmp_path / "custom_llm_config.json"
        config_content = {
            "default_provider": "test_provider",
            "providers": {"test_provider": {"api_key": "test_key", "model": "test_model"}},
        }
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        assert str(config.config_file) == str(config_file)
        assert config.config_file.exists()

    def test_init_with_nonexistent_file(self, tmp_path):
        """测试使用不存在的文件初始化。"""
        nonexistent = tmp_path / "nonexistent.json"
        config = LLMConfig(str(nonexistent))
        # 配置管理器应该仍然被创建，但配置为默认值
        assert config is not None
        assert config.config == {"default_provider": "deepseek", "providers": {}}


class TestLLMConfigLoad:
    """测试配置加载功能。"""

    def test_load_valid_config(self, tmp_path):
        """测试加载有效配置。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {
            "default_provider": "deepseek",
            "providers": {
                "deepseek": {
                    "api_key": "sk_test",
                    "model": "deepseek-chat",
                    "base_url": "https://api.deepseek.com",
                    "timeout": 60,
                    "max_retries": 3,
                }
            },
        }
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        # 配置在初始化时自动加载
        loaded = config.config

        assert loaded["default_provider"] == "deepseek"
        assert "deepseek" in loaded["providers"]

    def test_load_invalid_json(self, tmp_path):
        """测试加载无效 JSON。"""
        config_file = tmp_path / "invalid.json"
        config_file.write_text("{invalid json}", encoding="utf-8")

        config = LLMConfig(str(config_file))
        # 无效 JSON 应该返回默认配置
        assert isinstance(config.config, dict)
        assert "default_provider" in config.config

    def test_load_empty_file(self, tmp_path):
        """测试加载空文件。"""
        config_file = tmp_path / "empty.json"
        config_file.write_text("", encoding="utf-8")

        config = LLMConfig(str(config_file))
        # 空文件应该返回默认配置
        assert isinstance(config.config, dict)
        assert "default_provider" in config.config


class TestLLMConfigGetProvider:
    """测试获取提供者配置。"""

    def test_get_existing_provider(self, tmp_path):
        """测试获取已存在的提供者。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {
            "default_provider": "test",
            "providers": {"test": {"api_key": "test_key", "model": "test_model"}},
        }
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        provider = config.get_provider_config("test")

        assert provider is not None
        assert provider["api_key"] == "test_key"
        assert provider["model"] == "test_model"

    def test_get_nonexistent_provider(self, tmp_path):
        """测试获取不存在的提供者。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {"default_provider": "test", "providers": {}}
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        provider = config.get_provider_config("nonexistent")

        assert provider is None

    def test_get_provider_with_all_fields(self, tmp_path):
        """测试获取包含所有字段的提供者。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {
            "default_provider": "complete",
            "providers": {
                "complete": {
                    "api_key": "key",
                    "model": "model",
                    "base_url": "https://api.example.com",
                    "timeout": 120,
                    "max_retries": 5,
                }
            },
        }
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        provider = config.get_provider_config("complete")

        assert provider["api_key"] == "key"
        assert provider["model"] == "model"
        assert provider["base_url"] == "https://api.example.com"
        assert provider["timeout"] == 120
        assert provider["max_retries"] == 5


class TestLLMConfigDefaultProvider:
    """测试默认提供者功能。"""

    def test_get_default_provider(self, tmp_path):
        """测试获取默认提供者。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {
            "default_provider": "deepseek",
            "providers": {
                "deepseek": {"api_key": "key1", "model": "model1"},
                "openai": {"api_key": "key2", "model": "model2"},
            },
        }
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        default = config.get_default_provider()

        assert default == "deepseek"

    def test_get_default_provider_when_missing(self, tmp_path):
        """测试 default_provider 缺失时的行为。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {"providers": {}}
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        default = config.get_default_provider()

        # 应该返回默认值（可能是 "deepseek" 或 None）
        assert isinstance(default, str) or default is None

    def test_get_default_provider_from_empty_config(self, tmp_path):
        """测试从空配置获取默认提供者。"""
        config_file = tmp_path / "empty.json"
        config_file.write_text("{}", encoding="utf-8")

        config = LLMConfig(str(config_file))
        default = config.get_default_provider()

        # 应该有合理的默认行为
        assert isinstance(default, str) or default is None


class TestLLMConfigValidation:
    """测试配置验证。"""

    def test_validate_provider_with_required_fields(self, tmp_path):
        """测试验证包含必需字段的提供者。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {"providers": {"valid": {"api_key": "key", "model": "model"}}}
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        provider = config.get_provider_config("valid")

        assert provider is not None
        assert "api_key" in provider
        assert "model" in provider

    def test_validate_provider_with_missing_api_key(self, tmp_path):
        """测试验证缺少 api_key 的提供者。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {"providers": {"incomplete": {"model": "model"}}}
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        provider = config.get_provider_config("incomplete")

        # 应该返回配置，即使缺少 api_key
        assert provider is not None
        # get() 返回 None 如果键不存在
        assert provider.get("api_key") is None or provider.get("api_key") == ""

    def test_validate_provider_with_missing_model(self, tmp_path):
        """测试验证缺少 model 的提供者。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {"providers": {"incomplete": {"api_key": "key"}}}
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        provider = config.get_provider_config("incomplete")

        # 应该返回配置，即使缺少 model
        assert provider is not None
        # get() 返回 None 如果键不存在
        assert provider.get("model") is None or provider.get("model") == ""


class TestLLMConfigProvidersList:
    """测试提供者列表功能。"""

    def test_get_all_providers(self, tmp_path):
        """测试获取所有提供者。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {
            "providers": {
                "provider1": {"api_key": "key1"},
                "provider2": {"api_key": "key2"},
                "provider3": {"api_key": "key3"},
            }
        }
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        config_data = config.config

        assert "providers" in config_data
        assert len(config_data["providers"]) == 3

    def test_get_providers_from_empty_config(self, tmp_path):
        """测试从空配置获取提供者列表。"""
        config_file = tmp_path / "empty.json"
        config_file.write_text("{}", encoding="utf-8")

        config = LLMConfig(str(config_file))
        config_data = config.config

        # 空 JSON 文件会被正确加载，但不会自动添加 providers 键
        # 因为文件存在且是有效的 JSON
        assert isinstance(config_data, dict)
        # providers 可能不存在，如果不存在我们使用 get() 方法
        providers = config_data.get("providers", {})
        assert isinstance(providers, dict)


class TestLLMConfigSave:
    """测试配置保存功能。"""

    def test_save_provider_config(self, tmp_path):
        """测试保存提供者配置。"""
        config_file = tmp_path / "llm_config.json"

        config = LLMConfig(str(config_file))
        config_data = {
            "default_provider": "new_provider",
            "providers": {"new_provider": {"api_key": "new_key", "model": "new_model"}},
        }

        # 如果有 save 方法
        if hasattr(config, "save"):
            config.save(config_data)

            # 验证保存成功
            config2 = LLMConfig(str(config_file))
            loaded = config2.load()
            assert loaded["default_provider"] == "new_provider"


class TestLLMConfigEdgeCases:
    """测试边界情况。"""

    def test_config_with_unicode_characters(self, tmp_path):
        """测试包含 Unicode 字符的配置。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {
            "default_provider": "测试提供者",
            "providers": {"测试提供者": {"api_key": "密钥🔑", "model": "模型😀"}},
        }
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        loaded = config.config

        assert "测试提供者" in loaded["providers"]
        assert loaded["providers"]["测试提供者"]["api_key"] == "密钥🔑"
        assert loaded["default_provider"] == "测试提供者"

    def test_config_with_extra_fields(self, tmp_path):
        """测试包含额外字段的配置。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {
            "default_provider": "test",
            "providers": {
                "test": {
                    "api_key": "key",
                    "model": "model",
                    "extra_field": "extra_value",
                    "another_field": 123,
                }
            },
        }
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        provider = config.get_provider_config("test")

        # 额外字段应该被保留
        assert provider is not None
        assert "extra_field" in provider

    def test_config_with_special_characters_in_api_key(self, tmp_path):
        """测试 API 密钥中的特殊字符。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {
            "providers": {
                "test": {"api_key": "sk-abc123!@#$%^&*()_+-=[]{}|;':\",./<>?", "model": "model"}
            }
        }
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        provider = config.get_provider_config("test")

        assert provider is not None
        assert "sk-abc123!@#$%^&*()_+-=[]{}|;':\",./<>?" in provider["api_key"]

    def test_config_with_very_long_values(self, tmp_path):
        """测试包含非常长值的配置。"""
        config_file = tmp_path / "llm_config.json"
        long_api_key = "a" * 10000
        config_content = {"providers": {"test": {"api_key": long_api_key, "model": "model"}}}
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        provider = config.get_provider_config("test")

        assert provider is not None
        assert len(provider["api_key"]) == 10000


class TestLLMConfigGetEnabledProviders:
    """测试获取已启用的提供者功能。"""

    def test_get_enabled_providers(self, tmp_path):
        """测试获取所有已启用的提供者。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {
            "providers": {
                "enabled1": {"api_key": "key1", "enabled": True},
                "enabled2": {"api_key": "key2", "enabled": True},
                "disabled": {"api_key": "key3", "enabled": False},
                "no_enabled_field": {"api_key": "key4"},
            }
        }
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        enabled = config.get_enabled_providers()

        assert len(enabled) == 2
        assert "enabled1" in enabled
        assert "enabled2" in enabled
        assert "disabled" not in enabled
        assert "no_enabled_field" not in enabled

    def test_get_enabled_providers_empty(self, tmp_path):
        """测试从空配置获取已启用的提供者。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {"providers": {}}
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        enabled = config.get_enabled_providers()

        assert len(enabled) == 0


class TestQuickScanModelPolicy:
    def test_unconfigured_policy_uses_legacy_default_provider(self, tmp_path):
        path = tmp_path / "llm_apis.json"
        path.write_text(json.dumps(_llm_config_content()), encoding="utf-8")

        policy = LLMConfig(str(path)).get_quick_scan_model_policy()

        assert policy["configured"] is False
        assert policy["routes"][0]["provider_config_ref"] == "primary"
        assert policy["routes"][0]["model"] == "legacy-a"
        assert policy["policy_version"].startswith("legacy-single-provider@")

    def test_configured_policy_preserves_user_order_and_fingerprints_changes(self, tmp_path):
        path = tmp_path / "llm_apis.json"
        first_policy = _quick_scan_policy()
        path.write_text(json.dumps(_llm_config_content(first_policy)), encoding="utf-8")
        first = LLMConfig(str(path)).get_quick_scan_model_policy()

        reordered = _quick_scan_policy(("backup", "primary"))
        path.write_text(json.dumps(_llm_config_content(reordered)), encoding="utf-8")
        second = LLMConfig(str(path)).get_quick_scan_model_policy()

        assert [route["provider_config_ref"] for route in first["routes"]] == [
            "primary",
            "backup",
        ]
        assert [route["provider_config_ref"] for route in second["routes"]] == [
            "backup",
            "primary",
        ]
        assert first["policy_id"] == second["policy_id"]
        assert first["policy_version"] != second["policy_version"]
        assert first["budget"]["currency"] == "USD"
        assert first["dispatch"]["max_in_flight_total"] == 4
        assert first["quota_groups"][0]["max_in_flight"] == 1
        assert first["routes"][0]["max_in_flight"] == 1

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda policy: policy.pop("budget"),
            lambda policy: policy["fallback"].pop("on_auth_error"),
            lambda policy: policy["resume"].update(persist_cooldowns_and_budget=False),
        ],
    )
    def test_loading_incomplete_v2_policy_fails_closed(self, tmp_path, mutate):
        policy = _quick_scan_policy()
        mutate(policy)
        path = tmp_path / "llm_apis.json"
        path.write_text(json.dumps(_llm_config_content(policy)), encoding="utf-8")

        with pytest.raises(ValueError):
            LLMConfig(str(path)).get_quick_scan_model_policy()

    def test_saved_order_change_applies_to_next_snapshot_only(self, tmp_path):
        path = tmp_path / "llm_apis.json"
        path.write_text(json.dumps(_llm_config_content(_quick_scan_policy())), encoding="utf-8")
        manager = LLMConfig(str(path))
        active_run_snapshot = manager.get_quick_scan_model_policy()

        manager.save_quick_scan_model_policy(_quick_scan_policy(("backup", "primary")))

        assert [route["provider_config_ref"] for route in active_run_snapshot["routes"]] == [
            "primary",
            "backup",
        ]
        assert [
            route["provider_config_ref"]
            for route in manager.get_quick_scan_model_policy()["routes"]
        ] == [
            "backup",
            "primary",
        ]

    def test_disabled_policy_routes_are_not_dispatched(self, tmp_path):
        policy = _quick_scan_policy()
        policy["models"][0]["enabled"] = False
        path = tmp_path / "llm_apis.json"
        path.write_text(json.dumps(_llm_config_content(policy)), encoding="utf-8")

        snapshot = LLMConfig(str(path)).get_quick_scan_model_policy()

        assert [route["provider_config_ref"] for route in snapshot["routes"]] == ["backup"]

    def test_save_policy_is_atomic_and_preserves_provider_credentials(self, tmp_path):
        path = tmp_path / "config" / "llm_apis.json"
        path.parent.mkdir()
        path.write_text(json.dumps(_llm_config_content()), encoding="utf-8")
        manager = LLMConfig(str(path))

        saved = manager.save_quick_scan_model_policy(_quick_scan_policy())
        reopened = LLMConfig(str(path))

        assert saved["configured"] is True
        assert reopened.get_api_key("primary") == "fixture-primary-key"
        assert [
            route["provider_config_ref"]
            for route in reopened.get_quick_scan_model_policy()["routes"]
        ] == [
            "primary",
            "backup",
        ]
        assert list(path.parent.glob("*.tmp")) == []

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda policy: policy.pop("budget"),
            lambda policy: policy["dispatch"].pop("max_in_flight_total"),
            lambda policy: policy["quota_groups"][0].pop("max_in_flight"),
            lambda policy: policy["fallback"].pop("on_rate_limit"),
            lambda policy: policy.pop("comparison"),
            lambda policy: policy.pop("resume"),
            lambda policy: policy.update(unrecognized="ignored-policy-field"),
        ],
    )
    def test_incomplete_v2_policy_is_rejected_without_touching_saved_config(self, tmp_path, mutate):
        path = tmp_path / "llm_apis.json"
        original = _llm_config_content(_quick_scan_policy())
        path.write_text(json.dumps(original), encoding="utf-8")
        original_bytes = path.read_bytes()
        manager = LLMConfig(str(path))
        invalid = _quick_scan_policy()
        mutate(invalid)

        with pytest.raises(ValueError):
            manager.save_quick_scan_model_policy(invalid)

        assert path.read_bytes() == original_bytes
        assert list(path.parent.glob(".*.tmp")) == []

    @pytest.mark.parametrize(
        "mutate, message",
        [
            (lambda p: p.update(schema_version="9.0.0"), "schema_version"),
            (
                lambda p: p["dispatch"].update(required_capabilities=["structured_output"]),
                "web_search",
            ),
            (lambda p: p["models"].clear(), "models"),
            (lambda p: p["models"][0].update(provider_config_ref="missing"), "不存在"),
            (lambda p: p["models"][0].update(api_key="must-not-persist"), "凭据"),
        ],
    )
    def test_save_rejects_invalid_or_secret_bearing_policy_without_touching_file(
        self, tmp_path, mutate, message
    ):
        path = tmp_path / "llm_apis.json"
        original = _llm_config_content()
        path.write_text(json.dumps(original), encoding="utf-8")
        policy = _quick_scan_policy()
        mutate(policy)
        before = path.read_bytes()

        with pytest.raises(ValueError, match=message):
            LLMConfig(str(path)).save_quick_scan_model_policy(policy)

        assert path.read_bytes() == before

    @pytest.mark.parametrize(
        "mutate, message",
        [
            (lambda p: p.update(policy_id="p" * 201), "200"),
            (lambda p: p["quota_groups"][0].update(id="g" * 201), "200"),
            (lambda p: p["models"][0].update(id="r" * 201), "200"),
            (lambda p: p["models"][0].update(provider_config_ref="x" * 201), "200"),
            (lambda p: p["models"][0].update(model="m" * 201), "200"),
            (lambda p: p["models"][0].update(quota_group="q" * 201), "200"),
            (
                lambda p: p["cost_policy"].update(
                    pricing_basis="verified_rate_card", pricing_ref=None
                ),
                "pricing_ref",
            ),
            (
                lambda p: p["cost_policy"].update(
                    pricing_basis="verified_rate_card", pricing_ref=""
                ),
                "pricing_ref",
            ),
            (
                lambda p: p["comparison"].update(enabled=True, max_cost=0, max_requests=1),
                "comparison.max_cost",
            ),
            (
                lambda p: p["comparison"].update(enabled=True, max_cost=1, max_requests=0),
                "comparison.max_requests",
            ),
        ],
    )
    @pytest.mark.parametrize("operation", ["load", "save"])
    def test_policy_schema_constraints_fail_closed(self, tmp_path, mutate, message, operation):
        policy = _quick_scan_policy()
        mutate(policy)
        initial = _llm_config_content(policy if operation == "load" else None)
        path = tmp_path / "llm_apis.json"
        path.write_text(json.dumps(initial), encoding="utf-8")
        before = path.read_bytes()
        manager = LLMConfig(str(path))

        with pytest.raises(ValueError, match=message):
            if operation == "load":
                manager.get_quick_scan_model_policy()
            else:
                manager.save_quick_scan_model_policy(policy)

        assert path.read_bytes() == before


class TestLLMConfigGetAPIKey:
    """测试获取 API 密钥功能。"""

    def test_get_api_key_enabled_provider(self, tmp_path):
        """测试获取已启用提供者的 API 密钥。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {
            "providers": {
                "test": {"api_key": "test_key", "enabled": True},
            }
        }
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        api_key = config.get_api_key("test")

        assert api_key == "test_key"

    def test_get_api_key_disabled_provider(self, tmp_path):
        """测试获取已禁用提供者的 API 密钥。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {
            "providers": {
                "test": {"api_key": "test_key", "enabled": False},
            }
        }
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        api_key = config.get_api_key("test")

        assert api_key is None

    def test_get_api_key_nonexistent_provider(self, tmp_path):
        """测试获取不存在提供者的 API 密钥。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {"providers": {}}
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        api_key = config.get_api_key("nonexistent")

        assert api_key is None

    def test_get_api_key_empty_key(self, tmp_path):
        """测试获取空的 API 密钥。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {
            "providers": {
                "test": {"api_key": "", "enabled": True},
            }
        }
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        api_key = config.get_api_key("test")

        assert api_key is None


class TestLLMConfigGetBaseURL:
    """测试获取 base URL 功能。"""

    def test_get_base_url_existing_provider(self, tmp_path):
        """测试获取已存在提供者的 base URL。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {
            "providers": {
                "test": {"base_url": "https://api.example.com"},
            }
        }
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        base_url = config.get_base_url("test")

        assert base_url == "https://api.example.com"

    def test_get_base_url_nonexistent_provider(self, tmp_path):
        """测试获取不存在提供者的 base URL。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {"providers": {}}
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        base_url = config.get_base_url("nonexistent")

        assert base_url == ""

    def test_get_base_url_provider_without_url(self, tmp_path):
        """测试获取没有 base URL 的提供者。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {
            "providers": {
                "test": {"api_key": "key"},
            }
        }
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        base_url = config.get_base_url("test")

        assert base_url == ""


class TestLLMConfigGetModel:
    """测试获取模型名称功能。"""

    def test_get_model_existing_provider(self, tmp_path):
        """测试获取已存在提供者的模型名称。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {
            "providers": {
                "test": {"model": "gpt-4"},
            }
        }
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        model = config.get_model("test")

        assert model == "gpt-4"

    def test_get_model_nonexistent_provider(self, tmp_path):
        """测试获取不存在提供者的模型名称。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {"providers": {}}
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        model = config.get_model("nonexistent")

        assert model == ""

    def test_get_model_provider_without_model(self, tmp_path):
        """测试获取没有模型名称的提供者。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {
            "providers": {
                "test": {"api_key": "key"},
            }
        }
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        model = config.get_model("test")

        assert model == ""


class TestLLMConfigGetTimeout:
    """测试获取超时时间功能。"""

    def test_get_timeout_custom_value(self, tmp_path):
        """测试获取自定义超时时间。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {
            "providers": {
                "test": {"timeout": 120},
            }
        }
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        timeout = config.get_timeout("test")

        assert timeout == 120

    def test_get_timeout_default_value(self, tmp_path):
        """测试获取默认超时时间。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {
            "providers": {
                "test": {},
            }
        }
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        timeout = config.get_timeout("test")

        # 默认值是 60（从 settings.py 导入的 DEFAULT_TIMEOUT）
        assert timeout > 0

    def test_get_timeout_nonexistent_provider(self, tmp_path):
        """测试获取不存在提供者的超时时间。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {"providers": {}}
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        timeout = config.get_timeout("nonexistent")

        # 应该返回默认值
        assert timeout > 0


class TestLLMConfigGetMaxRetries:
    """测试获取最大重试次数功能。"""

    def test_get_max_retries_custom_value(self, tmp_path):
        """测试获取自定义最大重试次数。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {
            "providers": {
                "test": {"max_retries": 5},
            }
        }
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        max_retries = config.get_max_retries("test")

        assert max_retries == 5

    def test_get_max_retries_default_value(self, tmp_path):
        """测试获取默认最大重试次数。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {
            "providers": {
                "test": {},
            }
        }
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        max_retries = config.get_max_retries("test")

        # 默认值是 3（从 settings.py 导入的 DEFAULT_MAX_RETRIES）
        assert max_retries >= 0

    def test_get_max_retries_nonexistent_provider(self, tmp_path):
        """测试获取不存在提供者的最大重试次数。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {"providers": {}}
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        max_retries = config.get_max_retries("nonexistent")

        # 应该返回默认值
        assert max_retries >= 0


class TestLLMConfigIsProviderEnabled:
    """测试检查提供者是否启用功能。"""

    def test_is_provider_enabled_true(self, tmp_path):
        """测试检查已启用的提供者。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {
            "providers": {
                "test": {"enabled": True},
            }
        }
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        is_enabled = config.is_provider_enabled("test")

        assert is_enabled is True

    def test_is_provider_enabled_false(self, tmp_path):
        """测试检查已禁用的提供者。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {
            "providers": {
                "test": {"enabled": False},
            }
        }
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        is_enabled = config.is_provider_enabled("test")

        assert is_enabled is False

    def test_is_provider_enabled_no_field(self, tmp_path):
        """测试检查没有 enabled 字段的提供者。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {
            "providers": {
                "test": {},
            }
        }
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        is_enabled = config.is_provider_enabled("test")

        assert is_enabled is False

    def test_is_provider_enabled_nonexistent(self, tmp_path):
        """测试检查不存在的提供者。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {"providers": {}}
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        is_enabled = config.is_provider_enabled("nonexistent")

        assert is_enabled is False


class TestLLMConfigReload:
    """测试重新加载配置功能。"""

    def test_reload_updates_config(self, tmp_path):
        """测试重新加载更新配置。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {
            "default_provider": "provider1",
            "providers": {"provider1": {"api_key": "key1"}},
        }
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        assert config.config["default_provider"] == "provider1"

        # 修改配置文件
        new_config_content = {
            "default_provider": "provider2",
            "providers": {"provider2": {"api_key": "key2"}},
        }
        config_file.write_text(json.dumps(new_config_content, ensure_ascii=False), encoding="utf-8")

        # 重新加载
        config.reload()

        assert config.config["default_provider"] == "provider2"

    def test_reload_with_invalid_json(self, tmp_path):
        """测试重新加载无效 JSON 后的配置。"""
        config_file = tmp_path / "llm_config.json"
        config_content = {
            "default_provider": "provider1",
            "providers": {"provider1": {"api_key": "key1"}},
        }
        config_file.write_text(json.dumps(config_content, ensure_ascii=False), encoding="utf-8")

        config = LLMConfig(str(config_file))
        assert config.config["default_provider"] == "provider1"

        # 写入无效 JSON
        config_file.write_text("{invalid json}", encoding="utf-8")

        # 重新加载（应该使用默认配置）
        config.reload()

        # 应该使用默认配置
        assert "default_provider" in config.config
