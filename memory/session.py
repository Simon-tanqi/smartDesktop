"""
会话生命周期管理：创建、切换、删除会话，以及消息写入主入口。

核心改动：
  - extract_facts → extract_facts_async（不阻塞主流程）
  - compress_conversation → compress_conversation_async（不阻塞主流程）
"""
import os
import datetime

from memory.config import (
    ACTIVE_SESSION_FILE, LOGS_DIR, NOISE_PHRASES,
    DEFAULT_KEEP_ROUNDS, DEFAULT_SUMMARY_MODEL,
)
from memory.stores.json_store import load_sessions_db, save_sessions_db
from memory.stores.sqlite_store import (
    add_chat_message, get_chat_messages, log_operation,
)
from memory.extractor import extract_facts_async
from memory.compressor import compress_conversation_async


# ═══════════════════════════════════════════════
# 会话 ID 生成
# ═══════════════════════════════════════════════

def generate_session_id():
    """生成会话 ID：年月日小时_序号，如 2026061415_1"""
    now = datetime.datetime.now()
    base = now.strftime("%Y%m%d%H")
    db = load_sessions_db()
    max_num = 0
    for sid in db:
        if sid.startswith(base):
            try:
                num = int(sid.split("_")[1])
                if num > max_num:
                    max_num = num
            except (ValueError, IndexError):
                continue
    return f"{base}_{max_num + 1}"


# ═══════════════════════════════════════════════
# 会话 CRUD
# ═══════════════════════════════════════════════

def create_session(title="新会话"):
    session_id = generate_session_id()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db = load_sessions_db()
    db[session_id] = {
        "id": session_id,
        "title": title,
        "created": now,
    }
    save_sessions_db(db)
    set_active_session(session_id)
    return session_id


def get_active_session_id():
    if os.path.exists(ACTIVE_SESSION_FILE):
        with open(ACTIVE_SESSION_FILE, "r") as f:
            sid = f.read().strip()
        db = load_sessions_db()
        if sid and sid in db:
            return sid
    return create_session("新会话")


def set_active_session(session_id):
    with open(ACTIVE_SESSION_FILE, "w") as f:
        f.write(session_id)


def list_sessions():
    db = load_sessions_db()
    sessions = []
    for sid, data in db.items():
        sessions.append({
            "id": sid,
            "title": data.get("title", sid),
            "created_at": data.get("created", ""),
        })
    sessions.sort(key=lambda x: x["created_at"], reverse=True)
    return sessions


def delete_session(session_id):
    from memory.stores.sqlite_store import delete_chat_session
    db = load_sessions_db()
    if session_id in db:
        del db[session_id]
        save_sessions_db(db)
        delete_chat_session(session_id)
        if os.path.exists(ACTIVE_SESSION_FILE):
            with open(ACTIVE_SESSION_FILE, "r") as f:
                if f.read().strip() == session_id:
                    os.remove(ACTIVE_SESSION_FILE)


# ═══════════════════════════════════════════════
# 轮次追踪
# ═══════════════════════════════════════════════

def update_last_turn(session_id, turn):
    db = load_sessions_db()
    if session_id in db:
        db[session_id]["last_turn"] = turn
        save_sessions_db(db)


def get_last_turn(session_id):
    db = load_sessions_db()
    return db.get(session_id, {}).get("last_turn", 0)


# ═══════════════════════════════════════════════
# 核心：消息写入入口
# ═══════════════════════════════════════════════

def append_session_message(session_id, role, content,
                           trace_id="", user_id="",
                           max_turns=DEFAULT_KEEP_ROUNDS,
                           summary_model=DEFAULT_SUMMARY_MODEL,
                           skip_compress=False):
    """
    智能记忆写入：

    1. 闲聊过滤
    2. 确保会话元数据存在
    3. 写入 SQLite 聊天记录（追加式）
    4. 用户消息：异步事实提取 + 异步压缩检查
    5. 操作日志
    """
    content = content.strip()
    if not content:
        return session_id

    # ── 1. 闲聊过滤 ──
    if content in NOISE_PHRASES:
        log_session_turn(role, content, trace_id=trace_id, user_id=user_id, session_id=session_id)
        return session_id
    if len(content) < 3 and any(p in content for p in ["好的", "没问题", "知道了"]):
        log_session_turn(role, content, trace_id=trace_id, user_id=user_id, session_id=session_id)
        return session_id

    # ── 2. 确保会话元数据存在 ──
    db = load_sessions_db()
    if session_id not in db:
        db[session_id] = {
            "id": session_id,
            "title": content[:25] if content else "新会话",
            "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        save_sessions_db(db)

    # ── 3. 写入 SQLite ──
    add_chat_message(session_id, role, content, msg_type="chat",
                     trace_id=trace_id, user_id=user_id)

    # ── 4. 用户消息：异步事实提取 + 异步压缩 ──
    if role == "user":
        # 异步事实提取（不阻塞主流程）
        extract_facts_async(content, trace_id=trace_id, user_id=user_id,
                            session_id=session_id, model=summary_model)

        # 更新标题（如果仍是默认值）
        if db[session_id]["title"] in ("新会话", "") or db[session_id]["title"].startswith("Session_"):
            db[session_id]["title"] = content[:25]
            save_sessions_db(db)

        # 异步压缩检查
        if not skip_compress:
            # 统计用户轮数
            messages = get_chat_messages(session_id)
            user_turns = sum(1 for m in messages if m["role"] == "user")
            if user_turns > max_turns:
                log_operation(trace_id, user_id, session_id,
                              "memory.compress.start",
                              f"turns {user_turns} > {max_turns}")
                compress_conversation_async(session_id, keep_rounds=max_turns,
                                            summary_model=summary_model,
                                            trace_id=trace_id, user_id=user_id)

    # ── 5. 操作日志 ──
    log_session_turn(role, content, trace_id=trace_id, user_id=user_id, session_id=session_id)
    return session_id


# ═══════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════

def log_session_turn(role, content, trace_id="", user_id="", session_id=""):
    """记录会话轮次到操作日志。"""
    log_operation(trace_id, user_id, session_id,
                  operation=f"chat.{role}", detail=content)


def get_log_dir():
    return LOGS_DIR


def save_ai_log(content: str, filename: str = None):
    """追加 AI 调用日志到 Markdown 文件。"""
    if not content.strip():
        return
    if filename is None:
        filename = f"ai_log{datetime.datetime.now().strftime('%Y%m%d')}.md"
    log_path = os.path.join(get_log_dir(), filename)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n---\n**{timestamp}**\n\n{content}\n")
