# StockQAbyLLM

[![Tests](https://github.com/yourusername/StockQAbyLLM/actions/workflows/ci.yml/badge.svg)](https://github.com/yourusername/StockQAbyLLM/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/yourusername/StockQAbyLLM/branch/main/graph/badge.svg)](https://codecov.io/gh/yourusername/StockQAbyLLM)
[![Security](https://github.com/yourusername/StockQAbyLLM/actions/workflows/security.yml/badge.svg)](https://github.com/yourusername/StockQAbyLLM/actions/workflows/security.yml)
[![Documentation](https://github.com/yourusername/StockQAbyLLM/actions/workflows/docs.yml/badge.svg)](https://github.com/yourusername/StockQAbyLLM/actions/workflows/docs.yml)

[中文 README](README.md) | English

A stock question-answering system powered by Large Language Models.

## Project Overview

StockQAbyLLM is a QA system capable of batch processing questions from configuration files and generating answers. The current implementation is an architectural refactor version, focusing on code quality, testability, and maintainability.

**Note**: Core features (web search and LLM integration) are still placeholder implementations and will be completed in future versions.

## Features

- ✅ Batch load questions from configuration files
- ✅ Structured data models (Question, Answer, QAResult)
- ✅ Comprehensive error handling and logging
- ✅ Type safety (Type Hints + Dataclasses)
- ✅ Full unit test and integration test coverage
- ✅ Backward compatible with original interface
- ✅ Command-line argument support
- ✅ UTF-8 encoding support (Chinese-friendly)

## Project Structure

```
StockQAbyLLM/
├── main.py                      # Program entry point
├── config.txt                   # Configuration file (question list)
├── pyproject.toml               # Project configuration
├── README.md                    # Project documentation
│
├── src/                         # Source code
│   ├── core/                    # Core business logic
│   │   ├── models.py           # Data models
│   │   └── exceptions.py       # Custom exceptions
│   ├── config/                  # Configuration management
│   │   ├── config_manager.py   # Configuration manager
│   │   └── settings.py         # Configuration constants
│   └── utils/                   # Utility functions
│       └── logger.py           # Logging system
│
├── tests/                       # Test suite
│   ├── unit/                   # Unit tests
│   ├── integration/            # Integration tests
│   └── conftest.py             # pytest configuration
│
└── logs/                        # Log files
```

## Quick Start

### Installation

```bash
# Clone repository
git clone <repository-url>
cd StockQAbyLLM

# Create virtual environment (recommended)
python -m venv venv
source venv/bin/activate  # Linux/Mac
# or
venv\Scripts\activate  # Windows

# Install dependencies (optional, for development)
pip install -e ".[dev]"
```

### Usage

1. **Create configuration file** (`config.txt`):

```
How to learn Python programming?
What are the development trends in AI?
How do quantum computers work?
What are the latest methods for stock market prediction?
```

2. **Run the program**:

```bash
# Basic usage (default configuration)
python main.py

# Specify configuration file
python main.py --config my_questions.txt

# Output to file
python main.py --output results.json

# Enable verbose logging
python main.py --verbose
```

3. **View results**:

The program outputs JSON format results:

```json
{
    "How to learn Python programming?": "This is the answer to 'How to learn Python programming?' (obtained via web search).",
    "What are the development trends in AI?": "This is the answer to 'What are the development trends in AI?' (obtained via web search)."
}
```

## Command-Line Arguments

| Argument | Short | Description | Default |
|----------|-------|-------------|---------|
| `--config` | - | Configuration file path | `config.txt` |
| `--output` | - | Output file path | Console output |
| `--verbose` | `-v` | Enable verbose logging | Off |
| `--help` | `-h` | Show help information | - |

## Development

### Running Tests

```bash
# Run all tests
pytest

# Run unit tests
pytest tests/unit/

# Run integration tests
pytest tests/integration/

# Generate coverage report
pytest --cov=src --cov-report=html
```

### Code Formatting

```bash
# Format code with black
black src/ tests/

# Check code quality with pylint
pylint src/

# Type checking with mypy
mypy src/
```

## Architecture Design

### Layered Architecture

The project adopts a layered architecture design with separation of concerns:

- **Presentation Layer** (`main.py`): Command-line interface and user interaction
- **Business Logic Layer** (`core/`): Core business logic and data models
- **Data Access Layer** (`config/`): Configuration management and data access
- **Utility Layer** (`utils/`): Common utility functions (logging, etc.)

### Design Patterns

- **Dataclass Pattern**: Use `@dataclass` to define data models
- **Repository Pattern**: `ConfigManager` encapsulates configuration access
- **Exception Hierarchy**: Custom exception hierarchy
- **Factory Pattern**: Object creation through factory methods

### Testing Strategy

- **Unit Tests**: Test individual component functionality
- **Integration Tests**: Test complete business workflows
- **Backward Compatibility Tests**: Ensure new version is compatible with old version

## Configuration File Format

The configuration file `config.txt` uses a simple text format:

- One question per line
- Automatically trim leading/trailing whitespace
- Automatically ignore empty lines
- UTF-8 encoding, supports Chinese and other Unicode characters

Example:

```
Question 1
Question 2
Question 3
```

## Logging

Log files are saved in the `logs/` directory, named by date:

- Filename: `stock_qa_YYYYMMDD.log`
- Format: `Timestamp - ModuleName - Level - Message`
- Levels: DEBUG, INFO, WARNING, ERROR

Example:

```
2026-01-04 10:30:45 - src.config.config_manager - INFO - Loading configuration file: config.txt
2026-01-04 10:30:45 - src.config.config_manager - INFO - Successfully loaded 4 questions
```

## Error Handling

The system includes comprehensive error handling mechanisms:

- **FileNotFoundError**: Configuration file does not exist
- **EmptyConfigError**: Configuration file is empty
- **ValidationError**: Data validation failed
- **ProcessingError**: Question processing failed

All errors include detailed error messages and context information.

## Tech Stack

- **Python**: 3.8+
- **Testing Framework**: pytest
- **Code Formatting**: black
- **Type Checking**: mypy
- **Logging**: Standard library `logging` module

## Contributing

Contributions are welcome! Please follow these steps:

1. Fork this repository
2. Create a feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

### Code Standards

- Follow PEP 8 guidelines
- Use black to format code
- Add type hints
- Write unit tests
- Add docstrings

## Version History

### v0.1.0 (2026-01-04)

**Refactor Phase 1 Completed**:
- ✅ Established layered architecture
- ✅ Implemented data models (Question, Answer, QAResult)
- ✅ Implemented configuration management system
- ✅ Implemented logging system
- ✅ Rewrote main entry point
- ✅ Added comprehensive tests (37 tests, 100% pass rate)
- ✅ Backward compatibility verified
- ✅ Error handling and type safety

**Planned Features**:
- [ ] Phase 2: Core business logic separation (QAEngine)
- [ ] Phase 3: Complete type hints and error handling
- [ ] Phase 4: Documentation and test coverage improvement
- [ ] Phase 5: CI/CD and quality gates
- [ ] Future: Implement real web search
- [ ] Future: Integrate LLM API
- [ ] Future: Add stock-specific features

## License

This project is licensed under the MIT License - see LICENSE file for details

## Author

Zheng Zengbo

## Acknowledgments

- Thanks to all contributors
- Thanks to the Python community for excellent tools and libraries
