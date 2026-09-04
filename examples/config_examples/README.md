# Configuration Examples

This directory contains configuration examples for different LLM providers.

## Files

| File | Description |
|------|-------------|
| `batch_questions.txt` | Sample questions file for batch processing |
| `config_openai.yaml` | OpenAI API configuration example |
| `config_azure.yaml` | Azure OpenAI configuration example |

## Usage

1. Copy the appropriate config file:
   ```bash
   cp config_openai.yaml ~/my_config.yaml
   ```

2. Edit the file and add your API credentials:
   ```yaml
   llm:
     api_key: "your-actual-api-key-here"
   ```

3. Use the config file:
   ```python
   from src.config.config_provider import ConfigProvider

   config = ConfigProvider.load_from_yaml("~/my_config.yaml")
   ```

## Configuration Options

### LLM Provider
- `provider`: Provider type (openai, azure, etc.)
- `api_key`: Your API key
- `model`: Model name to use
- `base_url`: API endpoint URL
- `temperature`: Response randomness (0.0 - 2.0)
- `max_tokens`: Maximum response length
- `timeout`: Request timeout in seconds

### Retry Configuration
- `max_attempts`: Maximum retry attempts
- `initial_delay`: Initial retry delay
- `max_delay`: Maximum retry delay
- `backoff_factor`: Exponential backoff factor

### Cache Configuration
- `enabled`: Enable/disable caching
- `ttl`: Cache time-to-live in seconds
- `max_size`: Maximum cache entries

### Batch Processing
- `strategy`: Processing strategy (sync/async)
- `max_concurrent`: Maximum concurrent requests
- `chunk_size`: Questions per batch

### Output Configuration
- `format`: Output format (json/yaml/markdown)
- `save_intermediate`: Save intermediate results
- `output_dir`: Output directory path

## Security Notes

- **Never commit API keys to version control**
- Use environment variables for sensitive data when possible
- Add config files with secrets to `.gitignore`
