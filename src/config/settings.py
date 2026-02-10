"""配置常量和默认设置。

该模块定义了系统的默认配置和常量。
"""

from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent.parent

# 默认配置文件路径
DEFAULT_CONFIG_FILE = "config.txt"

# 日志目录
LOG_DIR = PROJECT_ROOT / "logs"

# 输出目录
OUTPUT_DIR = PROJECT_ROOT / "outputs"

# 默认编码
DEFAULT_ENCODING = "utf-8"

# 配置文件相关
MAX_QUESTION_LENGTH = 1000  # 最大问题长度（字符）
MIN_QUESTION_LENGTH = 1  # 最小问题长度（字符）

# 输出格式
JSON_INDENT = 4  # JSON 缩进空格数
ENSURE_ASCII = False  # JSON 输出是否确保 ASCII（False 以支持中文）

# 日志设置
LOG_LEVEL = "INFO"  # 默认日志级别
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# LLM 配置默认值
DEFAULT_TIMEOUT = 60  # 默认超时时间（秒）
DEFAULT_MAX_RETRIES = 3  # 默认最大重试次数
DEFAULT_RETRY_DELAY = 2  # 默认重试延迟基数（秒）
DEFAULT_SCORE = 5  # 默认答案评分

# 批量处理配置
DEFAULT_BATCH_SIZE = 10  # 默认批量大小

# LLM 模型参数
MAX_TOKENS = 2000  # LLM 最大token数
TEMPERATURE = 0.7  # LLM 温度参数

# 评分范围
SCORE_MIN = 1  # 最小评分
SCORE_MAX = 10  # 最大评分

# 显示截断长度
DISPLAY_QUERY_TRUNCATE = 50  # 查询文本截断长度
DISPLAY_TITLE_TRUNCATE = 30  # 标题截断长度
DISPLAY_QUESTION_TRUNCATE = 50  # 问题截断长度
DISPLAY_ANSWER_TRUNCATE = 200  # 答案截断长度
DISPLAY_LINE_WIDTH = 70  # 显示分隔线宽度
