"""
全局配置常量 — 唯一入口

所有魔法数字、URL、默认值集中于此，业务代码统一从此文件导入。
修改常量只需改一处，全局生效。

导入方式：
    from config.settings import DEFAULT_TEMPERATURE, LLM_TIMEOUT
"""
import os

# ═══════════════════════════════════════════════
#  路径配置
# ═══════════════════════════════════════════════
WORKSPACE_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 用户数据目录（放在 APPDATA 下，便于项目打包分发）
# 与 memory/config.py 保持一致，确保记忆文件路径统一
DATA_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'MyAgent')
os.makedirs(DATA_DIR, exist_ok=True)

SCREENSHOTS_DIR = os.path.join(DATA_DIR, "screenshots")
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

MODELS_CONFIG_PATH = os.path.join(DATA_DIR, "models.json")
USER_PROFILE_PATH = os.path.join(DATA_DIR, 'user_profile.json')
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
COMMAND_TIMEOUT = 120           # 秒，shell 命令执行超时（统一入口，safe_exec/file_tools 共用）
PYTHON_TIMEOUT = 300            # 秒，Python 代码执行超时（全盘扫描等耗时操作可临时设更大值）
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

# ═══════════════════════════════════════════════
#  熔断参数（回路护板）
# ═══════════════════════════════════════════════
# 连续调用同一工具 ≥N 次 → 拒绝执行本轮
MAX_SAME_TOOL_STREAK = 3
# 域内任意工具连续调用 ≥N 次（不限工具名） → 强制终止
MAX_DOMAIN_TOOL_STREAK = 6
# 同一工具连续失败 ≥N 次（success:false） → 熔断注入
MAX_CONSECUTIVE_FAILURES = 3
# 同一工具同一参数连续调用 ≥N 次 → 拒绝执行
MAX_EXACT_REPEAT = 3

# ═══════════════════════════════════════════════
#  模型预设配置
# ═══════════════════════════════════════════════
MODEL_PRESETS = {
    "deepseek":     {"name": "DeepSeek", "base_url": "https://api.deepseek.com",         "temperature": 0.7, "models": ["deepseek-chat", "deepseek-reasoner"]},
    "hunyuan":      {"name": "混元",      "base_url": "https://api.hunyuan.cloud.tencent.com/v1", "temperature": 0.7, "models": ["hunyuan-turbos-latest", "hunyuan-lite"]},
    "qwen":         {"name": "通义千问",   "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "temperature": 0.7, "models": ["qwen-turbo", "qwen-plus", "qwen-max"]},
    "zhipu":        {"name": "智谱GLM",   "base_url": "https://open.bigmodel.cn/api/paas/v4", "temperature": 0.7, "models": ["glm-4-flash", "glm-4-plus"]},
    "doubao":       {"name": "豆包",      "base_url": "https://ark.cn-beijing.volces.com/api/v3", "temperature": 0.7, "models": ["doubao-lite-128k", "doubao-pro-256k"]},
    "openai":       {"name": "OpenAI",    "base_url": "https://api.openai.com/v1",          "temperature": 0.7, "models": ["gpt-4o", "gpt-4o-mini"]},
    "ollama":       {"name": "Ollama 本地","base_url": "http://localhost:11434/v1",          "temperature": 0.7, "models": [], "warning": "本地模型功能可能受模型能力影响，效果远不如云端大模型"},
    "custom":       {"name": "自定义",     "base_url": "",                                   "temperature": 0.7, "models": []},
}
