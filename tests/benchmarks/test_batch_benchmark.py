"""Batch processing performance benchmarks."""

from unittest.mock import Mock

import pytest

from src.config.config_manager import ConfigManager
from src.interfaces.batch_strategy import ChunkedStrategy, SerialStrategy


@pytest.mark.benchmark(group="batch_serial")
def test_serial_strategy_small_batch(benchmark):
    """Benchmark serial strategy with 5 questions."""
    strategy = SerialStrategy()
    questions = [f"question_{i}" for i in range(5)]

    def mock_processor(q):
        return {"question": q, "answer": f"answer_{q}"}

    def process_serial():
        return strategy.process(questions, mock_processor)

    results = benchmark(process_serial)
    assert len(results) == 5


@pytest.mark.benchmark(group="batch_serial")
def test_serial_strategy_medium_batch(benchmark):
    """Benchmark serial strategy with 25 questions."""
    strategy = SerialStrategy()
    questions = [f"question_{i}" for i in range(25)]

    def mock_processor(q):
        return {"question": q, "answer": f"answer_{q}"}

    def process_serial():
        return strategy.process(questions, mock_processor)

    results = benchmark(process_serial)
    assert len(results) == 25


@pytest.mark.benchmark(group="batch_chunked")
def test_chunked_strategy_small_batch(benchmark):
    """Benchmark chunked strategy with 5 questions."""
    strategy = ChunkedStrategy(chunk_size=2)
    questions = [f"question_{i}" for i in range(5)]

    def mock_processor(q):
        return {"question": q, "answer": f"answer_{q}"}

    def process_chunked():
        return strategy.process(questions, mock_processor)

    results = benchmark(process_chunked)
    assert len(results) == 5


@pytest.mark.benchmark(group="batch_chunked")
def test_chunked_strategy_medium_batch(benchmark):
    """Benchmark chunked strategy with 25 questions."""
    strategy = ChunkedStrategy(chunk_size=5)
    questions = [f"question_{i}" for i in range(25)]

    def mock_processor(q):
        return {"question": q, "answer": f"answer_{q}"}

    def process_chunked():
        return strategy.process(questions, mock_processor)

    results = benchmark(process_chunked)
    assert len(results) == 25


@pytest.mark.benchmark(group="batch_chunked")
def test_chunked_strategy_large_batch(benchmark):
    """Benchmark chunked strategy with 100 questions."""
    strategy = ChunkedStrategy(chunk_size=10)
    questions = [f"question_{i}" for i in range(100)]

    def mock_processor(q):
        return {"question": q, "answer": f"answer_{q}"}

    def process_chunked():
        return strategy.process(questions, mock_processor)

    results = benchmark(process_chunked)
    assert len(results) == 100


@pytest.mark.benchmark(group="batch_aggregation")
def test_answer_aggregation(benchmark):
    """Benchmark answer aggregation performance."""
    answers = [
        {
            "question": f"question_{i}",
            "answer": f"answer_{i}",
            "confidence": 0.85 + (i % 10) * 0.01,
            "sources": [f"source_{i}.pdf"],
        }
        for i in range(100)
    ]

    def aggregate():
        return {
            "total_questions": len(answers),
            "results": answers,
            "avg_confidence": sum(a["confidence"] for a in answers) / len(answers),
        }

    result = benchmark(aggregate)
    assert result["total_questions"] == 100


@pytest.mark.benchmark(group="batch_validation")
def test_output_validation(benchmark):
    """Benchmark output validation performance."""
    test_outputs = [
        {
            "question": f"question_{i}",
            "answer": f"answer_{i}",
            "confidence": 0.85,
            "sources": [f"source_{i}.pdf"],
        }
        for i in range(50)
    ]

    def validate_outputs():
        results = []
        for output in test_outputs:
            if all(k in output for k in ["question", "answer", "confidence"]):
                results.append(True)
            else:
                results.append(False)
        return results

    results = benchmark(validate_outputs)
    assert len(results) == 50


@pytest.mark.benchmark(group="batch_progress")
def test_progress_reporting(benchmark):
    """Benchmark progress reporting overhead."""
    from src.interfaces.batch_strategy import BatchProgress

    def report_progress():
        results = []
        for i in range(100):
            progress = BatchProgress(current=i, total=100, success_count=i, error_count=0)
            results.append(progress.percentage)
        return results

    results = benchmark(report_progress)
    assert len(results) == 100


@pytest.mark.benchmark(group="batch_formatter")
def test_json_formatting(benchmark):
    """Benchmark JSON output formatting performance."""
    import json

    test_data = {
        "results": [
            {"question": f"question_{i}", "answer": f"answer_{i}", "confidence": 0.85}
            for i in range(100)
        ],
        "metadata": {"total": 100, "timestamp": "2026-02-12"},
    }

    def format_json():
        return json.dumps(test_data, ensure_ascii=False, indent=2)

    result = benchmark(format_json)
    assert isinstance(result, str)


@pytest.mark.benchmark(group="batch_string")
def test_string_operations(benchmark):
    """Benchmark common string operations in batch processing."""
    test_strings = [
        f"Test string number {i} for batch processing performance." for i in range(1000)
    ]

    def process_strings():
        results = []
        for s in test_strings:
            processed = s.strip().lower().replace("test", "check")
            results.append(processed)
        return results

    results = benchmark(process_strings)
    assert len(results) == 1000


@pytest.mark.benchmark(group="batch_strategy_factory")
def test_strategy_factory(benchmark):
    """Benchmark strategy factory creation."""
    from src.interfaces.batch_strategy import BatchStrategyFactory

    def create_strategies():
        return [
            BatchStrategyFactory.create("serial"),
            BatchStrategyFactory.create("chunked", chunk_size=10),
            BatchStrategyFactory.create("async", max_concurrency=5),
        ]

    results = benchmark(create_strategies)
    assert len(results) == 3


@pytest.mark.benchmark(group="batch_progress_calculation")
def test_progress_percentage(benchmark):
    """Benchmark progress percentage calculation."""
    from src.interfaces.batch_strategy import BatchProgress

    progress = BatchProgress(current=50, total=100, success_count=45, error_count=5)

    def calculate():
        return progress.percentage

    result = benchmark(calculate)
    assert 0 <= result <= 100
