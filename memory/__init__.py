"""
smartDesktop 记忆系统 — 对外统一 API

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
    clear_session_messages,
    migrate_old_chat_db,
    add_summary,
    get_latest_summary,
    get_summaries,
    get_full_session_messages_rich,
    archive_session_topic,
    list_archived_topics,
    get_archived_topic_messages,
)

# ── 长期记忆（Markdown）─
from memory.long_term import (
    get_relevant_memory,
    append_skill,
    append_project,
    append_fact,
    append_custom_skill,
    add_compressed_summary,
    add_task_memory,
    read_skills,
    read_projects,
    read_facts,
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
    increment_merge_summary,
)

# ── 会话管理 ──
from memory.session import (
    create_session,
    rename_session,
    get_active_session_id,
    set_active_session,
    list_sessions,
    get_session,
    delete_session,
    append_session_message,
    log_session_turn,
    update_last_turn,
    get_last_turn,
    generate_session_id,
    get_log_dir,
    save_ai_log,
    build_context_messages,
    build_context_suffix,
    get_full_session_messages,
    build_step_summary,
    append_think_step,
    get_layered_memory,
    # 新增：上下文构建辅助函数
    build_dynamic_system_prompt,
    get_long_term_memory,
    get_compressed_summary,
    get_current_plan,
    get_recent_thinking_trace,
    load_recent_rounds,
)


# ═══════════════════════════════════════════════
# 依赖检查（模块加载时自动执行）
# ═══════════════════════════════════════════════
import os
import sys as _sys

_checked_deps = False


def _check_dependencies():
    """检查必要依赖库是否已安装，缺失时记录 ERROR/WARNING。"""
    global _checked_deps
    if _checked_deps:
        return
    _checked_deps = True

    try:
        from core.logger import get_context_logger
        ctx_log = get_context_logger()
    except ImportError:
        ctx_log = None

    # sqlite3 内置，无需安装
    try:
        import sqlite3 as _sqlite3
        _ = _sqlite3.sqlite_version
    except Exception:
        msg = "sqlite3 不可用，上下文构建将无法正常工作"
        if ctx_log:
            ctx_log.error(msg)
        print(f"[ERROR] {msg}")

    # requests
    try:
        import requests  # noqa: F401
    except ImportError:
        msg = "缺少依赖: requests。请运行 pip install requests 安装"
        if ctx_log:
            ctx_log.error(msg)
        print(f"[WARNING] {msg}")

    # python-dotenv
    try:
        import dotenv  # noqa: F401
    except ImportError:
        msg = "缺少依赖: python-dotenv。请运行 pip install python-dotenv 安装"
        if ctx_log:
            ctx_log.error(msg)
        print(f"[WARNING] {msg}")

    # 检查 prompts/*.md 是否存在
    _app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for _md in ("core.md", "output.md", "safety.md"):
        _path = os.path.join(_app_dir, "prompts", _md)
        if not os.path.exists(_path):
            msg = f"提示词文件缺失: prompts/{_md}"
            if ctx_log:
                ctx_log.warning(msg)
            print(f"[WARNING] {msg}")


_check_dependencies()
