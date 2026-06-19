"""
记忆系统配置：路径、常量、默认值
"""
import os
import uuid

# ── 项目根目录 ──
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── 记忆数据目录 ──
MEMORY_DIR = os.path.join(BASE_DIR, "memory")
os.makedirs(MEMORY_DIR, exist_ok=True)

# ── Markdown 长期记忆（主存储：人可读、追加式）─
MEMORY_DATA_DIR = os.path.join(MEMORY_DIR, "data")
os.makedirs(MEMORY_DATA_DIR, exist_ok=True)

USER_MD = os.path.join(MEMORY_DATA_DIR, "user.md")
PROJECTS_MD = os.path.join(MEMORY_DATA_DIR, "projects.md")
SKILLS_MD = os.path.join(MEMORY_DATA_DIR, "skills.md")
FACTS_MD = os.path.join(MEMORY_DATA_DIR, "facts.md")
SUMMARIES_MD = os.path.join(MEMORY_DATA_DIR, "summaries.md")

# ── 旧 JSON 存储（仅用于一次性的数据迁移）─
MEMORY_FILE = os.path.join(MEMORY_DIR, "user_memory.json")
COMPRESSED_FILE = os.path.join(MEMORY_DIR, "compressed_memory.json")
SHORT_TERM_FILE = os.path.join(MEMORY_DIR, "short_term_memory.json")

# ── 会话数据目录 ──
SESSION_DIR = os.path.join(BASE_DIR, "session")
os.makedirs(SESSION_DIR, exist_ok=True)

# ── 日志目录 ──
LOGS_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

# ── 数据库文件路径 ──
SESSION_LOG_FILE = os.path.join(SESSION_DIR, "session_log.json")
CHAT_DB = os.path.join(SESSION_DIR, "chat_history.db")
ACTIVE_SESSION_FILE = os.path.join(SESSION_DIR, "active_session.txt")
USER_ID_FILE = os.path.join(SESSION_DIR, "user_id.txt")
OPERATION_LOG_DB = os.path.join(LOGS_DIR, "operation_log.db")
TRACE_DB = os.path.join(LOGS_DIR, "agent_trace.db")

# ── 压缩配置 ──
MAX_COMPRESSED_ENTRIES = 100
DEFAULT_KEEP_ROUNDS = 8
DEFAULT_SUMMARY_MODEL = "deepseek-chat"
MAX_SUMMARY_LENGTH = 50   # 摘要最大字数

# ── 按需注入配置 ──
MAX_RELEVANT_MEMORY_CHARS = 800   # 注入 system prompt 的最大字符数

# ── 闲聊过滤词表 ──
NOISE_PHRASES = ["你好", "在吗", "谢谢", "再见", "哦", "嗯", "哈哈", "哈喽", "hey", "hi", "你是谁"]


def generate_trace_id():
    """生成每次请求的追踪 ID（12 位 hex）。"""
    return uuid.uuid4().hex[:12]
