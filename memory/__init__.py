"""
MyAgent 记忆系统 — 对外统一 API

架构（v2 — Markdown 追加式 + 异步蒸馏 + 按需注入）：
  config.py      — 路径常量、默认值
  llm.py         — LLM 调用抽象（可替换 provider）
  long_term.py   — Markdown 追加存储 + 按需关键词检索
  extractor.py   — 异步事实提取
  compressor.py  — 异步对话压缩
  session.py     — 会话生命周期 + 消息写入入口
  stores/
    ├── json_store.py      — 会话元数据 JSON（轻量索引，非记忆主存储）
    └── sqlite_store.py    — 聊天记录 / 追踪 / 操作日志

外部调用方式：
  app.py 的 system prompt 构建改为 get_relevant_memory(user_message)
  append_session_message 内部已自动切换到异步提取和压缩
"""

# ── 配置 ──
from memory.config import generate_trace_id

# ── LLM 抽象 ──
from memory.llm import set_llm_caller

# ── JSON 存储（仅用于会话元数据）─
from memory.stores.json_store import (
    load_long_term,      # 迁移后仅读不写
    save_long_term,      # 迁移后禁止调用
    load_compressed,     # 迁移后仅读不写
    save_compressed,     # 迁移后禁止调用
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
)

# ── 长期记忆（Markdown）─
from memory.long_term import (
    get_relevant_memory,       # ★ 核心：按需注入，替代 load_long_term + get_recent_compressed
    append_skill,
    append_project,
    append_fact,
    append_summary,
    add_compressed_summary,    # 兼容旧接口
    read_skills,
    read_projects,
    read_facts,
    read_summaries,
    read_user_info,
)

# ── 事实提取 ──
from memory.extractor import extract_facts, extract_facts_async

# ── 压缩 ──
from memory.compressor import (
    add_compressed_summary,
    get_recent_compressed,
    compress_conversation,
    compress_conversation_async,
    get_latest_summary,
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
