"""
记忆系统配置：路径、常量、默认值

v4: APP_DIR / DATA_DIR 分离，支持 PyInstaller 打包；
    YAML 策略配置；长期 / 短期记忆子目录隔离。
"""
import os
import sys
import uuid
import yaml


# ═══════════════════════════════════════════════
# 路径分离：应用路径（只读） vs 用户数据路径（可读写）
# ═══════════════════════════════════════════════

if getattr(sys, 'frozen', False):
    # PyInstaller 打包后：exe 内部临时解压目录（只读）
    APP_DIR = sys._MEIPASS
else:
    APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 用户数据目录（可读写，持久化）
if sys.platform == 'win32':
    DATA_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'MyAgent')
elif sys.platform == 'darwin':
    DATA_DIR = os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', 'MyAgent')
else:
    DATA_DIR = os.path.join(os.path.expanduser('~'), '.config', 'MyAgent')

os.makedirs(DATA_DIR, exist_ok=True)

# ═══════════════════════════════════════════════
# 策略配置（YAML）
# ═══════════════════════════════════════════════

CONFIG_DIR = os.path.join(DATA_DIR, 'config')
os.makedirs(CONFIG_DIR, exist_ok=True)

CONFIG_YAML = os.path.join(CONFIG_DIR, 'memory_policy.yaml')
DEFAULT_CONFIG_YAML = os.path.join(APP_DIR, 'memory', 'config', 'memory_policy.yaml')


def _generate_default_policy():
    """从代码默认值生成 memory_policy.yaml 写入磁盘，确保始终有可编辑的配置文件。"""
    default_policy = {
        # 可选：专用摘要/提取模型（不配置则复用主模型 deepseek-chat）
        # summary_model:
        #   provider: ollama
        #   model_name: qwen2.5:7b
        #   base_url: http://localhost:11434/v1
        #   api_key: ollama
        'compression': {
            'model': {'provider': 'deepseek', 'model_name': 'deepseek-chat'},
            'token_budget': {'soft_threshold': 30000, 'hard_threshold': 50000,
                             'target_tokens': 15000},
            'recent_turns': 3,
            'max_summary_chars': 500,
        },
        'extraction': {
            'model': {'provider': 'deepseek', 'model_name': 'deepseek-chat'},
        },
        'retrieval': {
            'long_term_top_k': 3,
            'max_chars': 800,
        },
        'skills': {
            'custom': {},
        },
    }
    try:
        with open(CONFIG_YAML, 'w', encoding='utf-8') as f:
            f.write("# MyAgent 记忆策略配置\n")
            f.write("# 修改后调用 reload_policy() 或重启生效\n\n")
            yaml.dump(default_policy, f, allow_unicode=True, default_flow_style=False)
    except Exception:
        pass  # 写入失败不阻断启动，使用内存中的 defaults


def _load_policy():
    """加载 memory_policy.yaml，不存在时从代码默认值生成一份写入磁盘。"""
    if not os.path.exists(CONFIG_YAML):
        _generate_default_policy()

    defaults = {
        'compression': {
            'model': {'provider': 'deepseek', 'model_name': 'deepseek-chat'},
            'token_budget': {'soft_threshold': 30000, 'hard_threshold': 50000,
                             'target_tokens': 15000},
            'recent_turns': 3,
            'max_summary_chars': 500,
        },
        'extraction': {
            'model': {'provider': 'deepseek', 'model_name': 'deepseek-chat'},
        },
        'retrieval': {
            'long_term_top_k': 3,
            'max_chars': 800,
        },
        'skills': {
            'custom': {},
        },
    }
    try:
        if os.path.exists(CONFIG_YAML):
            with open(CONFIG_YAML, 'r', encoding='utf-8') as f:
                loaded = yaml.safe_load(f)
            if loaded:
                # 深度合并
                for section in defaults:
                    if section in loaded and isinstance(loaded[section], dict):
                        defaults[section].update(loaded[section])
    except Exception as e:
        print(f"[WARN] Failed to load memory_policy.yaml: {e}")
    return defaults


_policy = _load_policy()

# ── 压缩配置（从 YAML 读取）──
COMPRESS_MODEL_CONFIG   = _policy['compression']['model']
TOKEN_SOFT_THRESHOLD    = _policy['compression']['token_budget']['soft_threshold']
TOKEN_HARD_THRESHOLD    = _policy['compression']['token_budget']['hard_threshold']
TOKEN_TARGET            = _policy['compression']['token_budget']['target_tokens']
RECENT_TURNS_KEEP       = _policy['compression']['recent_turns']
MAX_SUMMARY_CHARS       = _policy['compression']['max_summary_chars']

# ── 提取配置 ──
EXTRACT_MODEL_CONFIG    = _policy['extraction']['model']

# ── 可选摘要模型（覆盖压缩和提取的模型配置；不配则复用主模型）──
SUMMARY_MODEL_CONFIG    = _policy.get('summary_model')

# ── 检索配置 ──
LONG_TERM_TOP_K         = _policy['retrieval']['long_term_top_k']
MAX_RELEVANT_MEMORY_CHARS = _policy['retrieval']['max_chars']

# ── 自定义技能配置 ──
SKILLS_CUSTOM           = _policy.get('skills', {}).get('custom', {})

def get_custom_skill_config():
    """返回自定义技能配置字典。extractor.py 调用此函数以动态注入技能类别。"""
    return dict(SKILLS_CUSTOM)

# ── 兼容旧常量名 ──
RECENT_TOKEN_MIN        = TOKEN_SOFT_THRESHOLD
RECENT_TOKEN_MAX        = TOKEN_HARD_THRESHOLD
SUMMARY_TOKEN_BUDGET    = 800   # 摘要最大注入 token 数（与 MAX_SUMMARY_CHARS=500 对齐，~275 token）
DEFAULT_SUMMARY_MODEL   = COMPRESS_MODEL_CONFIG.get('model_name', 'deepseek-r1:7b')
SUMMARY_MODEL           = COMPRESS_MODEL_CONFIG.get('model_name', 'deepseek-r1:7b')
MAX_SUMMARY_LENGTH      = MAX_SUMMARY_CHARS
DEFAULT_KEEP_ROUNDS     = RECENT_TURNS_KEEP  # 已废弃，保留兼容

# 召回过滤：当前用户消息与会话历史的 bigram Jaccard 加权相似度阈值
# 低于此阈值时，判定话题已切换，丢弃全部历史上下文（仅保留压缩摘要）
RECALL_RELEVANCE_THRESHOLD = 0.1

# ═══════════════════════════════════════════════
# 长期记忆目录（DATA_DIR 下）
# ═══════════════════════════════════════════════

LONG_TERM_DIR = os.path.join(DATA_DIR, 'memory', 'long_term')
os.makedirs(LONG_TERM_DIR, exist_ok=True)

USER_MD        = os.path.join(LONG_TERM_DIR, 'user.md')
PROJECTS_MD    = os.path.join(LONG_TERM_DIR, 'projects.md')
SKILLS_MD      = os.path.join(LONG_TERM_DIR, 'skills.md')
FACTS_MD       = os.path.join(LONG_TERM_DIR, 'facts.md')
SUMMARIES_MD   = os.path.join(LONG_TERM_DIR, 'summaries.md')

# ── 自定义技能目录：用户可以直接在此放 .md 文件 ──
CUSTOM_SKILLS_DIR = os.path.join(LONG_TERM_DIR, 'custom')
os.makedirs(CUSTOM_SKILLS_DIR, exist_ok=True)

# ═══════════════════════════════════════════════
# 短期记忆目录（DATA_DIR 下，按会话隔离）
# ═══════════════════════════════════════════════

SHORT_TERM_DIR = os.path.join(DATA_DIR, 'memory', 'short_term')
os.makedirs(SHORT_TERM_DIR, exist_ok=True)

def get_session_dir(session_id):
    """获取会话专属目录，不存在则创建。"""
    d = os.path.join(SHORT_TERM_DIR, session_id)
    os.makedirs(d, exist_ok=True)
    return d

def get_session_messages_db(session_id):
    """会话原始消息数据库路径。"""
    return os.path.join(get_session_dir(session_id), 'messages.db')

def get_session_summaries_db(session_id):
    """会话压缩摘要数据库路径。"""
    return os.path.join(get_session_dir(session_id), 'summaries.db')

def get_session_summary_md(session_id):
    """会话摘要纯文本文件路径。"""
    return os.path.join(get_session_dir(session_id), 'session_summary.md')

# ═══════════════════════════════════════════════
# 旧路径兼容（迁移用）
# ═══════════════════════════════════════════════

OLD_MEMORY_DIR      = os.path.join(APP_DIR, 'memory')
OLD_MEMORY_DATA_DIR = os.path.join(OLD_MEMORY_DIR, 'data')
OLD_SESSION_DIR     = os.path.join(APP_DIR, 'session')
OLD_CHAT_DB         = os.path.join(OLD_SESSION_DIR, 'chat_history.db')
OLD_SHORT_TERM_FILE = os.path.join(OLD_MEMORY_DIR, 'short_term_memory.json')
OLD_MEMORY_FILE     = os.path.join(OLD_MEMORY_DIR, 'user_memory.json')
OLD_COMPRESSED_FILE = os.path.join(OLD_MEMORY_DIR, 'compressed_memory.json')

# ═══════════════════════════════════════════════
# 会话元数据 & 日志目录
# ═══════════════════════════════════════════════

SESSION_DIR         = os.path.join(DATA_DIR, 'session')
os.makedirs(SESSION_DIR, exist_ok=True)

LOGS_DIR            = os.path.join(DATA_DIR, 'logs')
os.makedirs(LOGS_DIR, exist_ok=True)

# ── 元数据（保留 JSON，轻量索引）──
SESSION_LOG_FILE    = os.path.join(SESSION_DIR, 'session_log.json')
SHORT_TERM_FILE     = os.path.join(SESSION_DIR, 'sessions_meta.json')
ACTIVE_SESSION_FILE = os.path.join(SESSION_DIR, 'active_session.txt')
USER_ID_FILE        = os.path.join(SESSION_DIR, 'user_id.txt')

# ── 全局日志 DB ──
OPERATION_LOG_DB    = os.path.join(LOGS_DIR, 'operation_log.db')
TRACE_DB            = os.path.join(LOGS_DIR, 'agent_trace.db')

# ── 旧 JSON 存储兼容 ──
MEMORY_FILE     = os.path.join(SESSION_DIR, 'user_memory.json')
COMPRESSED_FILE = os.path.join(SESSION_DIR, 'compressed_memory.json')

# ═══════════════════════════════════════════════
# 其他常量
# ═══════════════════════════════════════════════

MAX_COMPRESSED_ENTRIES = 100

NOISE_PHRASES = [
    "你好", "在吗", "谢谢", "再见", "哦", "嗯", "哈哈", "哈喽",
    "hey", "hi", "你是谁",
    # 短确认/应答词 — 从 session.py _FACT_EXTRACT_SKIP 合并至此
    "好的", "没问题", "知道了", "OK", "ok", "收到", "懂了", "行", "好",
    "可以", "明白", "yes", "no", "对", "没错", "知道", "了解",
    "继续",
    # 极简无信息量短词
    "来了", "在", "有", "没", "啥", "？", "?", "。。", "...",
    "下一步", "然后呢", "还有呢", "开始", "开始吧",
]

# ═══════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════

def generate_trace_id():
    """生成每次请求的追踪 ID。"""
    from datetime import datetime
    return f"{datetime.now().strftime('%Y%m%d')}_{uuid.uuid4().hex[:12]}"


def reload_policy():
    """热重载 YAML 配置（开发调试用）。"""
    global _policy, COMPRESS_MODEL_CONFIG, TOKEN_SOFT_THRESHOLD, TOKEN_HARD_THRESHOLD
    global TOKEN_TARGET, RECENT_TURNS_KEEP, MAX_SUMMARY_CHARS
    global EXTRACT_MODEL_CONFIG, SUMMARY_MODEL_CONFIG, LONG_TERM_TOP_K, MAX_RELEVANT_MEMORY_CHARS
    global SKILLS_CUSTOM
    global RECENT_TOKEN_MIN, RECENT_TOKEN_MAX, SUMMARY_TOKEN_BUDGET
    global DEFAULT_SUMMARY_MODEL, SUMMARY_MODEL, MAX_SUMMARY_LENGTH, DEFAULT_KEEP_ROUNDS

    _policy = _load_policy()
    COMPRESS_MODEL_CONFIG   = _policy['compression']['model']
    TOKEN_SOFT_THRESHOLD    = _policy['compression']['token_budget']['soft_threshold']
    TOKEN_HARD_THRESHOLD    = _policy['compression']['token_budget']['hard_threshold']
    TOKEN_TARGET            = _policy['compression']['token_budget']['target_tokens']
    RECENT_TURNS_KEEP       = _policy['compression']['recent_turns']
    MAX_SUMMARY_CHARS       = _policy['compression']['max_summary_chars']
    EXTRACT_MODEL_CONFIG    = _policy['extraction']['model']
    SUMMARY_MODEL_CONFIG    = _policy.get('summary_model')
    LONG_TERM_TOP_K         = _policy['retrieval']['long_term_top_k']
    MAX_RELEVANT_MEMORY_CHARS = _policy['retrieval']['max_chars']
    SKILLS_CUSTOM           = _policy.get('skills', {}).get('custom', {})

    RECENT_TOKEN_MIN        = TOKEN_SOFT_THRESHOLD
    RECENT_TOKEN_MAX        = TOKEN_HARD_THRESHOLD
    SUMMARY_TOKEN_BUDGET    = TOKEN_TARGET
    DEFAULT_SUMMARY_MODEL   = COMPRESS_MODEL_CONFIG.get('model_name', 'deepseek-r1:7b')
    SUMMARY_MODEL           = COMPRESS_MODEL_CONFIG.get('model_name', 'deepseek-r1:7b')
    MAX_SUMMARY_LENGTH      = MAX_SUMMARY_CHARS
    DEFAULT_KEEP_ROUNDS     = RECENT_TURNS_KEEP
