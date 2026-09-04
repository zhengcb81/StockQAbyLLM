"""LLM provider performance benchmarks."""

from unittest.mock import Mock

import pytest


@pytest.mark.benchmark(group="llm_cache")
def test_cache_performance_hit(benchmark):
    """Benchmark cache hit performance."""
    from src.utils.llm_integration import RequestCache

    cache = RequestCache(max_size=1000, ttl_seconds=3600)

    # Pre-populate cache
    cache.set("provider", "test_question", "system_prompt", {"answer": "cached answer"})

    def cache_get():
        return cache.get("provider", "test_question", "system_prompt")

    result = benchmark(cache_get)
    assert result is not None


@pytest.mark.benchmark(group="llm_cache")
def test_cache_performance_set(benchmark):
    """Benchmark cache set performance."""
    from src.utils.llm_integration import RequestCache

    cache = RequestCache(max_size=1000, ttl_seconds=3600)

    def cache_set():
        cache.set("provider", "test_question", "system_prompt", {"answer": "test answer"})

    benchmark(cache_set)


@pytest.mark.benchmark(group="llm_response")
def test_response_parsing(benchmark):
    """Benchmark LLM response parsing performance."""
    from src.providers.llm_response_parser import LLMResponseParser

    parser = LLMResponseParser()
    mock_response = '{"score": 8, "description": "This is a test answer"}'

    def parse_response():
        return parser.parse_response(mock_response)

    result = benchmark(parse_response)
    # Result may be None if parsing fails, that's OK for benchmarking


@pytest.mark.benchmark(group="llm_config")
def test_config_loading(benchmark, tmp_path):
    """Benchmark config loading performance."""
    import json

    from src.config.json_config_manager import JSONConfigManager

    config_data = {
        "llm": {"api_key": "test_key", "base_url": "https://api.test.com", "model": "test_model"}
    }
    config_path = tmp_path / "test_config.json"
    config_path.write_text(json.dumps(config_data))

    def load_config():
        return JSONConfigManager(str(config_path))

    result = benchmark(load_config)
    assert result is not None


@pytest.mark.benchmark(group="llm_cost")
def test_cost_calculation(benchmark):
    """Benchmark cost calculation performance."""
    from src.utils.llm_integration import RequestCost

    def calculate_cost():
        cost = RequestCost()
        cost.calculate_from_tokens(1000, 500)
        return cost.to_dict()

    result = benchmark(calculate_cost)
    assert "total_cost" in result


@pytest.mark.benchmark(group="llm_token")
def test_token_usage(benchmark):
    """Benchmark token usage tracking performance."""
    from src.utils.llm_integration import TokenUsage

    def track_usage():
        usage = TokenUsage(prompt_tokens=1000, completion_tokens=500)
        return usage.to_dict()

    result = benchmark(track_usage)
    assert "total_tokens" in result


@pytest.mark.benchmark(group="llm_context")
def test_request_context(benchmark):
    """Benchmark request context creation performance."""
    from src.utils.llm_integration import RequestContext

    def create_context():
        context = RequestContext()
        return context.to_dict()

    result = benchmark(create_context)
    assert "request_id" in result


@pytest.mark.benchmark(group="llm_cascade")
def test_provider_cascade(benchmark):
    """Benchmark provider cascade operations performance."""
    from src.utils.llm_integration import ProviderCascade

    cascade = ProviderCascade(["provider1", "provider2", "provider3"])

    def get_primary():
        return cascade.get_primary()

    result = benchmark(get_primary)
    assert result == "provider1"


@pytest.mark.benchmark(group="llm_string")
def test_string_operations(benchmark):
    """Benchmark string operations common in LLM processing."""
    test_text = "This is a test prompt for LLM processing benchmarking. " * 10

    def process_text():
        # Common operations: strip, lower, split, join
        processed = test_text.strip().lower()
        words = processed.split()
        return " ".join(words)

    result = benchmark(process_text)
    assert isinstance(result, str)


@pytest.mark.benchmark(group="llm_json")
def test_json_operations(benchmark):
    """Benchmark JSON operations common in LLM responses."""
    import json

    test_data = {
        "choices": [{"message": {"content": "Test response content"}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }

    def process_json():
        return json.dumps(test_data, ensure_ascii=False)

    result = benchmark(process_json)
    assert isinstance(result, str)
