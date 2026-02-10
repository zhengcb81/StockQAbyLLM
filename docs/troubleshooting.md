# StockQAbyLLM 故障排查

本文档帮助诊断和解决常见问题。

## 目录

- [诊断步骤](#诊断步骤)
- [常见错误及解决方案](#常见错误及解决方案)
- [日志分析](#日志分析)
- [调试方法](#调试方法)
- [问题报告](#问题报告)

## 诊断步骤

### 1. 确认问题类型

首先确定问题类型：

- **安装问题**: 无法安装或设置
- **配置问题**: 配置文件或环境变量
- **运行时错误**: 程序运行时崩溃
- **性能问题**: 运行缓慢或占用资源过多
- **输出问题**: 结果不正确或格式错误

### 2. 收集信息

收集以下信息帮助诊断：

```bash
# 系统信息
python --version
pip --version

# 项目信息
git log -1
git status

# 环境信息
pip list

# 日志文件
ls -la logs/
```

### 3. 查看日志

日志文件位置：`logs/`

```bash
# 查看最新日志
tail -f logs/stockqa.log

# 查看错误日志
grep ERROR logs/stockqa.log
```

## 常见错误及解决方案

### 安装问题

#### 错误: ModuleNotFoundError: No module named 'src'

**症状**: 导入模块时找不到

**解决方案**:

1. 确认在项目根目录
2. 使用 `-e` 安装：

```bash
pip install -e .
```

3. 检查 PYTHONPATH：

```bash
export PYTHONPATH="${PYTHONPATH}:$(pwd)"
```

#### 错误: Microsoft Visual C++ 14.0 is required

**症状**: Windows 上安装包失败

**解决方案**:

1. 安装 Microsoft C++ Build Tools
2. 或使用预编译的 wheel 包

### 配置问题

#### 错误: 配置文件不存在

**症状**: `FileNotFoundError: config.txt`

**解决方案**:

1. 创建配置文件：

```bash
cp config.txt.example config.txt
```

2. 或使用绝对路径：

```bash
python main.py --config /absolute/path/to/config.txt
```

#### 错误: API 密钥无效

**症状**: `APIError: Invalid API key`

**解决方案**:

1. 检查 `.env` 文件
2. 检查 `llm_apis.json` 配置
3. 确认 API 密钥没有过期

```bash
# 测试 API 密钥
curl -H "Authorization: Bearer YOUR_KEY" https://api.example.com/test
```

### 运行时错误

#### 错误: ConnectionError

**症状**: 无法连接到 API

**诊断**:

```bash
# 测试网络连接
ping api.example.com

# 测试 DNS
nslookup api.example.com

# 测试端口
telnet api.example.com 443
```

**解决方案**:

1. 检查网络连接
2. 检查防火墙设置
3. 确认 API 服务可用
4. 尝试使用代理

#### 错误: TimeoutError

**症状**: API 请求超时

**解决方案**:

1. 增加超时时间（`llm_apis.json`）：

```json
{
  "providers": {
    "deepseek": {
      "timeout": 120
    }
  }
}
```

2. 检查网络速度
3. 减少批量大小

#### 错误: JSONDecodeError

**症状**: 无法解析 API 响应

**诊断**:

查看实际响应内容：

```python
import requests
response = requests.get(url)
print(response.text)
```

**解决方案**:

1. 检查 API 文档确认响应格式
2. 检查 API 是否返回错误页面
3. 查看日志中的原始响应

#### 错误: KeyError

**症状**: 访问不存在的字典键

**解决方案**:

1. 检查配置文件格式
2. 使用 `.get()` 方法避免错误：

```python
value = config.get("key", default_value)
```

### 性能问题

#### 问题: 处理速度慢

**诊断**:

```bash
# 使用性能分析
python -m cProfile -o profile.stats main.py --mode basic
python -m pstats profile.stats
```

**解决方案**:

1. 调整批量大小
2. 使用异步 API
3. 启用缓存
4. 升级硬件

#### 问题: 内存占用高

**诊断**:

```bash
# 使用内存分析器
pip install memory_profiler
python -m memory_profiler main.py --mode basic
```

**解决方案**:

1. 减少批量大小
2. 分批处理
3. 清理缓存

## 日志分析

### 日志级别

- **DEBUG**: 详细调试信息
- **INFO**: 一般信息
- **WARNING**: 警告信息
- **ERROR**: 错误信息
- **CRITICAL**: 严重错误

### 查看特定级别日志

```bash
# 只看错误
grep ERROR logs/stockqa.log

# 只看警告
grep WARNING logs/stockqa.log

# 查看最近的错误
grep ERROR logs/stockqa.log | tail -20
```

### 日志格式示例

```
2026-02-09 10:30:45,123 [INFO] 正在处理问题: 什么是股票？
2026-02-09 10:30:46,456 [ERROR] API 调用失败: Connection timeout
2026-02-09 10:30:46,457 [DEBUG] 重试第 1 次...
```

### 常见日志模式

| 日志内容 | 可能原因 | 解决方案 |
|---------|---------|---------|
| `Connection timeout` | 网络问题 | 检查网络连接 |
| `API key invalid` | 密钥错误 | 更新 API 密钥 |
| `Rate limit exceeded` | 调用过多 | 减少请求频率 |
| `JSON decode error` | 响应格式错误 | 检查 API 配置 |

## 调试方法

### 启用调试模式

```bash
python main.py --mode basic --verbose
```

### 使用 Python 调试器

```python
# 在代码中添加断点
import pdb; pdb.set_trace()

# 或使用 breakpoint() (Python 3.7+)
breakpoint()
```

### 使用 IPython

```python
from IPython import embed
embed()
```

### 查看变量

```python
# 在调试时
print(locals())
print(globals())
```

### 追踪执行

```bash
# 追踪代码执行
python -m trace --trace main.py --mode basic
```

## 问题报告

### 报告前检查清单

在报告问题前，请确认：

- [ ] 已查看 FAQ 文档
- [ ] 已查看故障排查文档
- [ ] 已搜索类似 Issues
- [ ] 已尝试常见解决方案
- [ ] 收集了必要的诊断信息

### 问题报告模板

```markdown
## 问题描述
清晰简洁地描述问题

## 复现步骤
1. 执行命令 '...'
2. 点击 '....'
3. 滚动到 '....'
4. 看到错误

## 预期行为
描述你期望发生什么

## 实际行为
描述实际发生了什么

## 环境信息
- 操作系统: [如 Windows 11]
- Python 版本: [如 3.11.0]
- 项目版本: [如 v1.0.0]

## 日志输出
粘贴相关日志输出

## 额外信息
其他可能有助于解决问题的信息
```

### 提供诊断信息

```bash
# 系统信息
python --version
pip --version

# Git 信息
git log -1
git diff

# 依赖信息
pip freeze > requirements.txt

# 日志文件
tar -czf logs.tar.gz logs/
```

## 预防措施

### 定期备份

```bash
# 备份配置文件
cp config.txt config.txt.backup
cp llm_apis.json llm_apis.json.backup
```

### 使用版本控制

```bash
# 提交前检查
git status
git diff

# 创建分支
git checkout -b experiment
```

### 监控资源

```bash
# 监控 CPU
top -p $(pgrep -f main.py)

# 监控内存
watch -n 1 'ps aux | grep main.py'

# 监控磁盘
df -h
```

## 获取帮助

如果以上方法都无法解决问题：

1. **查看文档**:
   - [架构文档](architecture.md)
   - [开发者指南](development.md)
   - [常见问题](faq.md)

2. **搜索 Issues**:
   - [GitHub Issues](https://github.com/yourusername/StockQAbyLLM/issues)

3. **创建新 Issue**:
   - 使用问题报告模板
   - 提供完整的诊断信息

4. **联系维护者**:
   - 通过 GitHub 联系
   - 发送邮件（如果有）

---

*文档版本: 1.0*
*最后更新: 2026-02-09*
