# StockQAbyLLM - Usage Examples

This directory contains example code demonstrating how to use StockQAbyLLM.

## Examples

| Example | Description |
|---------|-------------|
| `basic_usage.py` | Simple example - process a single question |
| `batch_processing.py` | Batch processing - handle multiple questions from file |
| `async_usage.py` | Async processing - efficient concurrent processing |

## Running the Examples

### Basic Usage

Process a single question:

```bash
python basic_usage.py
```

### Batch Processing

Process multiple questions from a file:

```bash
python batch_processing.py
```

Results will be saved to `batch_results.json`.

### Async Processing

Process questions asynchronously:

```bash
# Single question
python async_usage.py

# Batch processing
python async_usage.py batch
```

## Configuration

See the [config_examples/](config_examples/) directory for configuration examples.

## Before You Run

1. Install dependencies:
   ```bash
   pip install -e .
   ```

2. Configure your API credentials in the example files or use a config file from `config_examples/`

## Note

The current version (v0.1.0) uses placeholder implementations. LLM integration and web search features will be completed in future versions.
