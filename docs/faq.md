# StockQAbyLLM 常见问题 (FAQ)

本文档回答了用户和开发者常见的问题。

## 目录

- [安装问题](#安装问题)
- [配置问题](#配置问题)
- [使用问题](#使用问题)
- [API 问题](#api-问题)
- [性能问题](#性能问题)
- [错误信息](#错误信息)

## 安装问题

### Q: 支持哪些 Python 版本？

**A:** 支持 Python 3.9 及更高版本。推荐使用 Python 3.10 或 3.11。

### Q: 如何安装依赖？

**A:** 使用以下命令：

```bash
pip install -e ".[dev]"
```

这将安装所有依赖，包括开发工具。

### Q: 安装时出现编码错误怎么办？

**A:** 确保使用 UTF-8 编码：

```bash
# Windows
set PYTHONUTF8=1
pip install -e ".[dev]"

# Linux/Mac
PYTHONUTF8=1 pip install -e ".[dev]"
```

### Q: 依赖冲突怎么办？

**A:** 建议使用虚拟环境：

```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows
pip install -e ".[dev]"
```

## 配置问题

### Q: 如何配置 LLM API？

**A:** 编辑 `llm_apis.json` 文件：

```json
{
  "default_provider": "deepseek",
  "providers": {
    "deepseek": {
      "api_key": "your-api-key",
      "base_url": "https://api.deepseek.com",
      "model": "deepseek-chat",
      "enabled": true
    }
  }
}
```

### Q: API 密钥应该放在哪里？

**A:** 推荐使用环境变量：

1. 创建 `.env` 文件
2. 添加 `LLM_API_KEY=your-key`
3. 在代码中使用 `os.getenv("LLM_API_KEY")`

### Q: 如何批量添加问题？

**A:** 编辑 `config.txt` 文件，每行一个问题：

```
什么是股票？
如何购买股票？
股票的风险有哪些？
```

### Q: 支持 JSON 格式的问题配置吗？

**A:** 支持。创建 JSON 文件：

```json
{
  "questions": [
    "什么是股票？",
    "如何购买股票？"
  ]
}
```

## 使用问题

### Q: 如何运行基础模式？

**A:** 使用以下命令：

```bash
python main.py --mode basic --config config.txt
```

### Q: 如何运行 LLM 模式？

**A:** 使用以下命令：

```bash
python main.py --mode llm --config config.txt
```

### Q: 如何指定输出文件？

**A:** 使用 `--output` 参数：

```bash
python main.py --mode basic --config config.txt --output results.json
```

### Q: 如何启用详细日志？

**A:** 使用 `--verbose` 参数：

```bash
python main.py --mode basic --verbose
```

### Q: 输出结果在哪里？

**A:** 默认输出到控制台。使用 `--output` 参数指定文件：

```bash
python main.py --mode basic --output outputs/results.json
```

### Q: 如何处理大量问题？

**A:** 使用批量处理功能，创建包含所有问题的配置文件，系统会自动批量处理。

## API 问题

### Q: 支持哪些 LLM 提供者？

**A:** 目前支持：
- DeepSeek
- MiniMax
- GLM (智谱)
- OpenAI (兼容接口)

### Q: 如何添加新的 LLM 提供者？

**A:** 在 `llm_apis.json` 中添加配置：

```json
{
  "providers": {
    "new_provider": {
      "api_key": "your-key",
      "base_url": "https://api.example.com",
      "model": "model-name",
      "enabled": true
    }
  }
}
```

### Q: API 调用失败怎么办？

**A:** 检查以下几点：
1. API 密钥是否正确
2. 网络连接是否正常
3. API 配额是否用完
4. 查看日志文件 `logs/` 获取详细错误信息

### Q: 如何设置 API 超时？

**A:** 在 `llm_apis.json` 中配置：

```json
{
  "providers": {
    "deepseek": {
      "timeout": 120,
      "max_retries": 5
    }
  }
}
```

### Q: API 调用太慢怎么办？

**A:** 可以：
1. 调整超时时间
2. 使用异步模式（如果支持）
3. 减少批量大小

## 性能问题

### Q: 处理大量问题时速度慢怎么办？

**A:** 可以：
1. 使用批量处理模式
2. 调整批量大小
3. 使用异步 API（如果支持）

### Q: 内存占用过高怎么办？

**A:** 可以：
1. 减少批量大小
2. 分批处理问题
3. 定期清理缓存

### Q: 如何监控性能？

**A:** 查看日志文件中的处理时间统计，或使用性能分析工具：

```bash
python -m cProfile -o profile.stats main.py --mode basic
```

## 错误信息

### Q: 出现 "配置文件不存在" 错误

**A:** 确保配置文件路径正确：

```bash
# 使用绝对路径
python main.py --config /path/to/config.txt

# 或相对路径
python main.py --config ./config.txt
```

### Q: 出现 "API 密钥无效" 错误

**A:** 检查：
1. API 密钥是否正确
2. 是否使用了正确的环境变量
3. `llm_apis.json` 配置是否正确

### Q: 出现 "网络连接错误"

**A:** 检查：
1. 网络连接是否正常
2. API 服务是否可用
3. 防火墙是否阻止了连接

### Q: 出现 "JSON 解析错误"

**A:** 这可能是因为：
1. API 返回了非 JSON 格式的响应
2. 响应被截断或损坏
3. 查看日志获取详细信息

### Q: 出现 "问题格式无效" 错误

**A:** 确保问题：
1. 不是空字符串
2. 不是仅包含空白字符
3. 长度在限制范围内（默认 1-1000 字符）

## 其他问题

### Q: 如何卸载？

**A:** 使用 pip：

```bash
pip uninstall StockQAbyLLM
```

### Q: 如何更新到最新版本？

**A:** 使用 git：

```bash
git pull origin main
pip install -e ".[dev]"
```

### Q: 如何报告 bug？

**A:** 在 GitHub Issues 中提交：
1. 清晰描述问题
2. 提供复现步骤
3. 附上错误日志
4. 说明环境信息（操作系统、Python 版本等）

### Q: 如何贡献代码？

**A:** 查看 [贡献指南](../CONTRIBUTING.md) 了解详情。

### Q: 是否支持 Docker？

**A:** Docker 支持正在开发中，敬请期待。

### Q: 是否支持其他语言？

**A:** 目前仅支持中文问题。多语言支持在计划中。

## 仍未解决问题？

如果以上 FAQ 没有解决您的问题：

1. 查看 [故障排查文档](troubleshooting.md)
2. 查看 [开发者指南](development.md)
3. 在 GitHub Issues 中提问
4. 联系项目维护者

---

*文档版本: 1.0*
*最后更新: 2026-02-09*