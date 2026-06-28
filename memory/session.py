"""
会话生命周期管理：创建、切换、删除会话，以及消息写入主入口。

v4：
  - 适配 per-session 消息存储
  - 压缩摘要从 per-session summaries.db 读取
  - Token 预算制触发压缩
"""
import os
import re
import datetime
import threading

from memory.config import (
    ACTIVE_SESSION_FILE, LOGS_DIR, NOISE_PHRASES,
    RECENT_TOKEN_MIN, RECENT_TOKEN_MAX, SUMMARY_TOKEN_BUDGET,
)
from memory.stores.json_store import load_sessions_db, save_sessions_db
from memory.stores.sqlite_store import (
    add_chat_message, get_chat_messages, log_operation,
    get_session_messages, get_latest_summary,
)
from memory.extractor import extract_facts_async
from memory.compressor import (
    compress_conversation_async,
    estimate_messages_tokens,
)

# ── 标题生成：剥离代码块与多余空白后再截断 ──
def _make_title(content: str) -> str:
    """从用户消息生成会话标题：剥离 Markdown 代码块后取前 25 字符。"""
    if not content:
        return "新会话"
    # 剥离 ```...``` 代码块
    cleaned = re.sub(r"```[\s\S]*?```", "", content)
    # 压缩空白
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return "新会话"
    return cleaned[:25]



# Token优化：这些纯确认/招呼短消息跳过事实提取（"我叫XX"等有信息量短输入不在此列）
_FACT_EXTRACT_SKIP = set(NOISE_PHRASES) | {
    "好的", "行", "可以", "继续", "收到", "明白",
    "ok", "OK", "yes", "no", "对", "没错", "知道", "了解",
    "来了", "在", "有", "没", "啥", "？", "?", "。。", "...",
    "下一步", "然后呢", "还有呢", "开始", "开始吧",
}
_FACT_EXTRACT_MIN_LEN = 3

# 会话创建锁：防止多线程并发创建会话时产生重复 ID（TOCTOU 竞态）
_session_lock = threading.Lock()


# ═══════════════════════════════════════════════
# 会话 ID 生成
# ═══════════════════════════════════════════════

def generate_session_id():
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

def _get_next_session_name():
    db = load_sessions_db()
    used_numbers = set()
    for data in db.values():
        title = data.get("title", "")
        m = re.match(r"^新会话(\d+)$", title)
        if m:
            used_numbers.add(int(m.group(1)))
    n = 1
    while n in used_numbers:
        n += 1
    return f"新会话{n}"


def create_session(title=None):
    with _session_lock:
        if title is None:
            title = _get_next_session_name()
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


def rename_session(session_id, new_title):
    new_title = new_title.strip()
    if not new_title:
        return False
    db = load_sessions_db()
    if session_id not in db:
        return False
    db[session_id]["title"] = new_title
    save_sessions_db(db)
    return True


def ensure_session_titles():
    db = load_sessions_db()
    used_numbers = set()
    for data in db.values():
        title = data.get("title", "")
        m = re.match(r"^新会话(\d+)$", title)
        if m:
            used_numbers.add(int(m.group(1)))
    changed = False
    for sid, data in db.items():
        title = data.get("title", "")
        if not title or title.strip() == "":
            n = 1
            while n in used_numbers:
                n += 1
            data["title"] = f"新会话{n}"
            used_numbers.add(n)
            changed = True
    if changed:
        save_sessions_db(db)


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
        # 过滤测试/调试 session，防止污染前端会话列表
        if sid.startswith(("test", "diag", "debug")):
            continue
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
# 上下文构建：SQLite 消息 + per-session 摘要
# ═══════════════════════════════════════════════

def _detect_repeated_query(messages, current_user_msg, similarity_threshold=0.35):
    if not current_user_msg or not messages:
        return []

    def _bigrams(text):
        chinese = re.sub(r'[^\u4e00-\u9fff]', '', text)
        if len(chinese) < 2:
            return set()
        return {chinese[i:i+2] for i in range(len(chinese)-1)}

    def _is_tool_call(content):
        content = content.strip()
        return ('"tool_name"' in content[:200] or
                content.startswith('{"tool_call"') or
                content.startswith('{"type":"tool"'))

    current_bigrams = _bigrams(current_user_msg)
    if not current_bigrams:
        return []

    exclude_indices = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "user":
            continue
        hist_content = msg.get("content", "")
        hist_bigrams = _bigrams(hist_content)
        if not hist_bigrams:
            continue
        intersection = len(current_bigrams & hist_bigrams)
        union = len(current_bigrams | hist_bigrams)
        if union == 0:
            continue
        if intersection / union >= similarity_threshold:
            j = i + 1
            turn_assistants = []
            while j < len(messages) and messages[j].get("role") != "user":
                if messages[j].get("role") == "assistant":
                    turn_assistants.append(j)
                j += 1
            for idx in turn_assistants:
                if not _is_tool_call(messages[idx].get("content", "")):
                    exclude_indices.append(idx)
    return sorted(set(exclude_indices), reverse=True)


def build_context_messages(session_id, limit=500, summary_max_tokens=SUMMARY_TOKEN_BUDGET):
    """构建 LLM 上下文消息列表。"""
    messages = get_session_messages(session_id, limit=limit)

    last_user_msg = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user_msg = m.get("content", "")
            break

    if last_user_msg:
        exclude_indices = _detect_repeated_query(messages, last_user_msg)
        if exclude_indices:
            for idx in exclude_indices:
                if idx < len(messages):
                    del messages[idx]
            messages.append({
                "role": "system",
                "content": "【注意】用户重复了之前提问。历史工具调用结果已保留，已完成部分可直接引用，但失败/遗漏部分需重新执行。"
            })

    # 清理连续同角色消息：保留每个 user 段的最后一条，丢弃中间的纯 user 段
    # 防止失败请求累积导致全是 user 消息
    if messages:
        cleaned = [messages[0]]
        for m in messages[1:]:
            prev = cleaned[-1]
            if prev["role"] == m["role"] == "user":
                # 保留最后一条 user，替换前一条
                cleaned[-1] = m
            elif prev["role"] == m["role"] == "assistant":
                cleaned[-1]["content"] = prev["content"] + "\n" + m["content"]
            else:
                cleaned.append(m)
        messages = cleaned

    # 注入 per-session 压缩摘要
    latest = get_latest_summary(session_id)
    if latest:
        truncated = latest
        max_iterations = 20
        iterations = 0
        while estimate_messages_tokens([{"role": "system", "content": truncated}]) > summary_max_tokens:
            new_len = int(len(truncated) * 0.8)
            if new_len >= len(truncated) or new_len == 0:
                truncated = ""  # 无法再缩减，放弃摘要注入
                break
            truncated = truncated[:new_len]
            iterations += 1
            if iterations >= max_iterations:
                truncated = ""
                break
        if truncated:
            messages.append({
                "role": "system",
                "content": f"【历史对话摘要】{truncated}"
            })

    return messages


# ═══════════════════════════════════════════════
# 核心：消息写入入口
# ═══════════════════════════════════════════════

def append_session_message(session_id, role, content,
                           trace_id="", user_id="",
                           skip_compress=False):
    content = content.strip()
    if not content:
        return session_id

    # 闲聊过滤 — 统一精确匹配，消除子串误杀风险
    if content in NOISE_PHRASES:
        log_session_turn(role, content, trace_id=trace_id, user_id=user_id, session_id=session_id)
        return session_id

    # 确保会话元数据存在
    db = load_sessions_db()
    if session_id not in db:
        db[session_id] = {
            "id": session_id,
            "title": _make_title(content),
            "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        save_sessions_db(db)

    # 写入 per-session messages.db
    now_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    add_chat_message(session_id, role, content, msg_type="chat",
                     trace_id=trace_id, user_id=user_id, timestamp=now_ts)

    if role == "user":
        # Token优化：纯确认/招呼类短消息不触发事实提取，但放过"我叫XX"等有信息量的短输入
        stripped = content.strip()
        if len(stripped) >= _FACT_EXTRACT_MIN_LEN and stripped not in _FACT_EXTRACT_SKIP:
            extract_facts_async(content, trace_id=trace_id, user_id=user_id,
                                session_id=session_id)

        if db[session_id].get("title", "") in ("新会话", "") or \
           (db[session_id].get("title", "") or "").startswith("Session_"):
            db[session_id]["title"] = _make_title(content)
            save_sessions_db(db)

        if not skip_compress:
            messages = get_chat_messages(session_id)
            total_tokens = estimate_messages_tokens(messages)
            if total_tokens > RECENT_TOKEN_MIN:
                threshold_label = "soft" if total_tokens <= RECENT_TOKEN_MAX else "hard"
                log_operation(trace_id, user_id, session_id,
                              "memory.compress.start",
                              f"tokens {total_tokens} > {RECENT_TOKEN_MIN} ({threshold_label})")
                compress_conversation_async(
                    session_id,
                    recent_token_min=RECENT_TOKEN_MIN,
                    recent_token_max=RECENT_TOKEN_MAX,
                    trace_id=trace_id,
                    user_id=user_id,
                )

    log_session_turn(role, content, trace_id=trace_id, user_id=user_id, session_id=session_id)
    return session_id


# ═══════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════

def log_session_turn(role, content, trace_id="", user_id="", session_id=""):
    log_operation(trace_id, user_id, session_id,
                  operation=f"chat.{role}", detail=content)


def get_log_dir():
    return LOGS_DIR


def save_ai_log(content: str, filename: str = None):
    if not content.strip():
        return
    if filename is None:
        filename = f"ai_log{datetime.datetime.now().strftime('%Y%m%d')}.md"
    log_path = os.path.join(get_log_dir(), filename)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n---\n**{timestamp}**\n\n{content}\n")
