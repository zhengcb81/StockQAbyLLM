# LLM API 配置说明

## 概述

系统使用 `llm_apis.json` 配置文件来管理多个 LLM API 密钥。这样更加安全，且支持多个 LLM 提供商作为备用。

## 配置文件结构

`llm_apis.json` 文件包含以下配置：

```json
{
  "default_provider": "deepseek",
  "providers": {
    "deepseek": {
      "name": "DeepSeek",
      "enabled": true,
      "api_key": "YOUR_API_KEY",
      "base_url": "https://api.deepseek.com/v1/chat/completions",
      "model": "deepseek-chat",
      "timeout": 60,
      "max_retries": 3,
      "description": "描述"
    }
  }
}
```

## 支持的 LLM 提供商

### 1. DeepSeek（已配置）
- **base_url**: `https://api.deepseek.com/v1/chat/completions`
- **model**: `deepseek-chat`
- **状态**: ✅ 已启用，API密钥已配置

### 2. MiniMax（备用）
- **base_url**: `https://api.minimax.chat/v1/text/chatcompletion`
- **model**: `abab6.5s-chat`
- **状态**: ⚠️ 未启用，需要配置 API 密钥

### 3. 智谱GLM（备用）
- **base_url**: `https://open.bigmodel.cn/api/paas/v4/chat/completions`
- **model**: `glm-4`
- **状态**: ⚠️ 未启用，需要配置 API 密钥

### 4. 小米MiMo（备用）
- **base_url**: 需要填写
- **model**: 需要填写
- **状态**: ⚠️ 未启用，需要完整配置

### 5. OpenAI（可选）
- **base_url**: `https://api.openai.com/v1/chat/completions`
- **model**: `gpt-4`
- **状态**: ⚠️ 未启用，需要配置 API 密钥

## 如何添加新的 API 密钥

### 步骤 1: 编辑配置文件
编辑 `llm_apis.json` 文件：

```json
{
  "default_provider": "deepseek",
  "providers": {
    "minimax": {
      "enabled": true,
      "api_key": "你的MiniMax API密钥"
    }
  }
}
```

### 步骤 2: 启用提供商
将 `"enabled"` 设置为 `true`

### 步骤 3: 设置为默认（可选）
如果想更改默认提供商，修改 `"default_provider"` 字段

## 使用方法

### 使用默认提供商（DeepSeek）
```bash
python main_with_llm.py --company "海康威视" --config config.txt
```

### 指定使用其他提供商
```bash
# 使用 MiniMax
python main_with_llm.py --company "腾讯控股" --provider minimax --config config.txt

# 使用智谱GLM
python main_with_llm.py --company "阿里巴巴" --provider glm --config config.txt
```

## 安全注意事项

1. **不要提交 API 密钥到 Git**
   - `llm_apis.json` 已添加到 `.gitignore`
   - 只提交 `llm_apis.json.example`

2. **保护配置文件**
   - 配置文件权限设置为只读
   - 不要分享包含 API 密钥的配置文件

3. **定期轮换密钥**
   - 建议定期更换 API 密钥
   - 如果密钥泄露，立即撤销并重新生成

## 故障转移

如果当前提供商失败，系统会：
1. 自动重试 3 次（指数退避）
2. 如果所有重试都失败，抛出异常
3. 未来版本会支持自动切换到备用提供商

## 获取 API 密钥

### DeepSeek
- 官网：https://platform.deepseek.com/
- 定价：https://platform.deepseek.com/pricing

### MiniMax
- 官网：https://api.minimax.chat/
- 文档：https://api.minimax.chat/document/Text-chat/2

### 智谱GLM
- 官网：https://open.bigmodel.cn/
- 文档：https://open.bigmodel.cn/dev/api

### 小米MiMo
- 联系小米获取 API 访问权限

### OpenAI
- 官网：https://platform.openai.com/
- 文档：https://platform.openai.com/docs/api-reference
