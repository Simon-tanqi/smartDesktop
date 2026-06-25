"""
MyAgent 记忆系统 — 对外统一 API

v4：APP_DIR/DATA_DIR 分离，per-session 短期记忆，Ollama 本地模型压缩/提取，
    自定义技能目录支持。

架构：
  config.py         — 路径常量 / YAML 策略加载 / APP_DIR vs DATA_DIR
  llm.py            — LLM 适配（Ollama / ai_concat 双通道）
  long_term.py      — Markdown 长期记忆（追加式）+ 按需关键词检索 + custom/ 扫描
  extractor.py      — 异步事实提取（本地模型 + 自定义技能类别）
  compressor.py     — Token 预算制异步压缩（per-session summaries.db）
  session.py        — 会话管理 + 消息写入入口
  stores/
    ├── json_store.py     — 会话元数据 JSON
    └── sqlite_store.py   — per-session messages.db + summaries.db + 全局日志
"""

# ── 配置 ──
from memory.config import generate_trace_id, reload_policy

# ── LLM 抽象 ──
from memory.llm import set_llm_caller

# ── JSON 存储（仅用于会话元数据）─
from memory.stores.json_store import (
    load_long_term,
    save_long_term,
    load_compressed,
    save_compressed,
    get_user_id,
)

# ── SQLite 存储 ──
from memory.stores.sqlite_store import (
    log_trace,
    log_operation,
    add_chat_message,
    get_chat_messages,
    get_session_messages,
    count_chat_messages,
    delete_chat_session,
    migrate_old_chat_db,
    add_summary,
    get_latest_summary,
    get_summaries,
)

# ── 长期记忆（Markdown）─
from memory.long_term import (
    get_relevant_memory,
    append_skill,
    append_project,
    append_fact,
    append_summary,
    append_custom_skill,
    add_compressed_summary,
    read_skills,
    read_projects,
    read_facts,
    read_summaries,
    read_user_info,
    list_custom_skill_files,
)

# ── 事实提取 ──
from memory.extractor import (
    extract_facts,
    extract_facts_async,
    register_custom_categories,
)

# ── 压缩 ──
from memory.compressor import (
    add_compressed_summary,
    get_recent_compressed,
    compress_conversation,
    compress_conversation_async,
    get_latest_summary_for_session,
    estimate_tokens,
    estimate_messages_tokens,
)

# ── 会话管理 ──
from memory.session import (
    create_session,
    rename_session,
    get_active_session_id,
    set_active_session,
    list_sessions,
    delete_session,
    append_session_message,
    log_session_turn,
    update_last_turn,
    get_last_turn,
    generate_session_id,
    get_log_dir,
    save_ai_log,
    build_context_messages,
)
