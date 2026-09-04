# StockQAbyLLM Documentation

Welcome to the StockQAbyLLM documentation!

## Overview

StockQAbyLLM is a question-answering system powered by Large Language Models, designed for processing stock-related and general questions.

## Quick Links

- [Quick Start Guide](QUICKSTART.md) - Get started in 5 minutes
- [API Reference](api/core.md) - Detailed API documentation
- [Examples](../examples/) - Usage examples

## Features

- Batch question processing from configuration files
- Structured data models with type safety
- Comprehensive error handling and logging
- Full test coverage
- Command-line interface
- UTF-8 encoding support

## Installation

```bash
pip install -e .
```

For development:

```bash
pip install -e ".[dev]"
```

## Basic Usage

```bash
python main.py --config my_questions.txt --output answers.json
```

## Documentation Structure

- **[Quick Start](QUICKSTART.md)**: Get started in 5 minutes
- **[API Reference](api/)**: Detailed API documentation
  - [Core Module](api/core.md) - Data models and business logic
  - [Config Module](api/config.md) - Configuration management
  - [Utils Module](api/utils.md) - Utility functions
  - [Interfaces](api/interfaces.md) - Interface definitions
  - [Providers](api/providers.md) - LLM provider implementations

## Version

Current version: v0.1.0

## License

MIT License - see [LICENSE](../LICENSE) for details.
