# Quick Start Guide - StockQAbyLLM

Get started with StockQAbyLLM in 5 minutes!

## What is StockQAbyLLM?

StockQAbyLLM is a question-answering system that processes questions from configuration files and generates answers using Large Language Models. It's designed for stock-related queries but works with any general questions.

## Prerequisites

- Python 3.8 or higher
- pip (Python package installer)
- (Optional) Git for cloning the repository

## Installation (2 minutes)

### Step 1: Get the Code

```bash
# Clone the repository
git clone https://github.com/yourusername/StockQAbyLLM.git
cd StockQAbyLLM
```

Or download and extract the ZIP file.

### Step 2: Create Virtual Environment (Recommended)

```bash
# Windows
python -m venv venv
venv\Scripts\activate

# Linux/Mac
python3 -m venv venv
source venv/bin/activate
```

### Step 3: Install Dependencies

```bash
# For basic usage
pip install -e .

# For development (includes testing tools)
pip install -e ".[dev]"
```

## Basic Usage (3 minutes)

### Step 1: Create Your Questions File

Create a file named `my_questions.txt` with your questions (one per line):

```text
What is the current trend of AI stocks?
How to analyze financial statements?
What are the best practices for long-term investing?
```

### Step 2: Run the Program

```bash
python main.py --config my_questions.txt
```

### Step 3: View Results

The program will output results in JSON format:

```json
{
    "What is the current trend of AI stocks?": "Answer placeholder...",
    "How to analyze financial statements?": "Answer placeholder...",
    "What are the best practices for long-term investing?": "Answer placeholder..."
}
```

## Command-Line Options

| Option | Description | Example |
|--------|-------------|---------|
| `--config FILE` | Specify questions file | `--config my_questions.txt` |
| `--output FILE` | Save results to file | `--output results.json` |
| `--verbose` / `-v` | Show detailed logs | `--verbose` |
| `--help` / `-h` | Show help message | `--help` |

## Common Use Cases

### Process Questions and Save to File

```bash
python main.py --config my_questions.txt --output answers.json
```

### Enable Debug Logging

```bash
python main.py --config my_questions.txt --verbose
```

### Use Default Configuration

```bash
# Uses config.txt in the project root
python main.py
```

## FAQ

### Q: What file formats are supported for questions?

A: Use a plain text file (`.txt`) with one question per line. Empty lines are automatically ignored. UTF-8 encoding is supported for international characters.

### Q: Where can I find the logs?

A: Logs are saved in the `logs/` directory with filenames like `stock_qa_YYYYMMDD.log`.

### Q: How do I run tests?

A: Run `pytest` in the project root. For coverage report, use `pytest --cov=src --cov-report=html`.

### Q: I get a "ModuleNotFoundError" error.

A: Make sure you've installed the dependencies: `pip install -e .` (or `pip install -e ".[dev]"` for development).

### Q: Can I use this for non-stock questions?

A: Yes! While designed for stock-related Q&A, it works with any general questions.

## Next Steps

- Read the full [README](https://github.com/zhengcb81/StockQAbyLLM/blob/master/README.md) for detailed documentation
- Check the [examples/](../examples/) directory for more usage examples
- Explore the [API documentation](api/) for advanced usage

## Getting Help

- Open an issue on GitHub
- Check the [FAQ section](https://github.com/zhengcb81/StockQAbyLLM/blob/master/README.md#错误处理) in the main README
- Review the logs in the `logs/` directory for debugging

---

**Note**: The current version (v0.1.0) is in active development. LLM integration and web search features are placeholder implementations and will be completed in future versions.
