# Performance Benchmarks

This directory contains performance benchmarks for the StockQAbyLLM project.

## Running Benchmarks

To run all benchmarks:

```bash
pytest tests/benchmarks/ --benchmark-only
```

To run a specific benchmark file:

```bash
pytest tests/benchmarks/test_batch_benchmark.py --benchmark-only
pytest tests/benchmarks/test_llm_benchmark.py --benchmark-only
```

## Benchmark Groups

### Batch Processing (`test_batch_benchmark.py`)

Tests the performance of batch processing operations:

- `batch_serial`: Serial strategy performance (5, 25 items)
- `batch_chunked`: Chunked strategy performance (5, 25, 100 items)
- `batch_aggregation`: Answer aggregation performance (100 items)
- `batch_validation`: Output validation performance (50 items)
- `batch_progress`: Progress reporting overhead (100 iterations)
- `batch_formatter`: JSON formatting performance (100 items)
- `batch_string`: String operations performance (1000 items)
- `batch_strategy_factory`: Strategy factory creation (3 strategies)
- `batch_progress_calculation`: Progress percentage calculation

### LLM Operations (`test_llm_benchmark.py`)

Tests the performance of LLM-related operations:

- `llm_cache`: Cache hit/set performance
- `llm_response`: Response parsing performance
- `llm_cost`: Cost calculation performance
- `llm_token`: Token usage tracking performance
- `llm_context`: Request context creation performance
- `llm_cascade`: Provider cascade operations
- `llm_string`: String operations in LLM processing
- `llm_json`: JSON operations for LLM responses
- `llm_config`: Config loading performance

## Understanding Results

Benchmarks are grouped by functionality. Key metrics:

- **Min**: Best execution time
- **Median**: Typical execution time (50th percentile)
- **Mean**: Average execution time
- **StdDev**: Standard deviation (consistency)
- **OPS**: Operations per second

## Performance Regression Detection

To detect performance regressions, save baseline results:

```bash
pytest tests/benchmarks/ --benchmark-only --benchmark-autosave
```

Then compare future runs against the baseline:

```bash
pytest tests/benchmarks/ --benchmark-only --benchmark-compare
```

## Adding New Benchmarks

1. Create a new test file in `tests/benchmarks/`
2. Use the `@pytest.mark.benchmark(group="name")` decorator
3. Use the `benchmark` fixture to time the operation
4. Keep benchmarks focused on measuring single operations

Example:

```python
@pytest.mark.benchmark(group="my_group")
def test_my_operation(benchmark):
    def operation():
        return my_function()
    result = benchmark(operation)
    assert result is not None
```
