"""
全局配置常量 — 唯一入口

所有魔法数字、URL、默认值集中于此，业务代码统一从此文件导入。
修改常量只需改一处，全局生效。

导入方式：
    from config.settings import DEFAULT_TEMPERATURE, LLM_TIMEOUT
"""
# ═══════════════════════════════════════════════
#  LLM 调用
# ═══════════════════════════════════════════════
DEFAULT_TEMPERATURE = 0.3
LLM_TIMEOUT = 30               # 秒，LLM API 请求超时
DEFAULT_MODEL = "deepseek-chat"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# ═══════════════════════════════════════════════
#  Ollama 本地模型
# ═══════════════════════════════════════════════
DEFAULT_OLLAMA_MODEL = "deepseek-r1:7b"
DEFAULT_OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434/v1"

# ═══════════════════════════════════════════════
#  搜索 / 网页请求
# ═══════════════════════════════════════════════
SEARCH_TIMEOUT = 30             # 秒，搜索请求超时
HTML_PARSE_MAX_CHARS = 15000    # HTML 解析截断长度
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
MOBILE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"

# ═══════════════════════════════════════════════
#  命令执行
# ═══════════════════════════════════════════════
MAX_OUTPUT_CHARS = 2000         # 命令输出截断长度
CONFIRM_TIMEOUT = 300           # 秒，确认等待超时

# ═══════════════════════════════════════════════
#  日志
# ═══════════════════════════════════════════════
LOG_FLUSH_INTERVAL = 5          # 秒，缓冲刷盘间隔
LOG_MAX_BUFFER_SIZE = 50        # 条，缓冲条数达此值立即刷盘
LOG_MIN_LEVEL = "INFO"          # 最低记录级别
LOG_SIZE_LIMIT_MB = 500         # 日志总大小阈值（MB），超限时弹窗提醒
LOG_SCAN_DEPTH = 3              # 文件搜索目录递归深度上限
