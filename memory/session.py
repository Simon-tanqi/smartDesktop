"""
会话生命周期管理：创建、切换、删除会话，以及消息写入主入口。

核心改动（v3 — Token 预算制 + 摘要注入）：
  - extract_facts → extract_facts_async（不阻塞主流程）
  - 压缩触发从"轮次计数"改为"Token 预算"
  - build_context_messages 将摘要注入 LLM 上下文
"""
import os
import re
import datetime

from memory.config import (
    ACTIVE_SESSION_FILE, LOGS_DIR, NOISE_PHRASES,
    DEFAULT_KEEP_ROUNDS, DEFAULT_SUMMARY_MODEL,
    RECENT_TOKEN_MIN, RECENT_TOKEN_MAX, SUMMARY_TOKEN_BUDGET,
)
from memory.stores.json_store import load_sessions_db, save_sessions_db
from memory.stores.sqlite_store import (
    add_chat_message, get_chat_messages, log_operation,
    get_session_messages,
)
from memory.extractor import extract_facts_async
from memory.compressor import (
    compress_conversation_async,
    estimate_messages_tokens,
    get_latest_summary,
)


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

def _get_next_session_name():
    """获取下一个「新会话N」的可用编号。如果之前的被改名释放了，优先补空位。"""
    import re
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
    """修改会话名称。如果原名称是「新会话N」，释放该编号供后续复用。"""
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
    """启动时迁移：给所有 title 为 None 或空的旧会话分配「新会话N」名称。"""
    import re
    db = load_sessions_db()
    # 先计算已占用的编号
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
# 轮次追踪（保留兼容）
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
# 上下文构建：SQLite 消息 + 压缩摘要
# ═══════════════════════════════════════════════

def _detect_repeated_query(messages, current_user_msg, similarity_threshold=0.35):
    """
    检测当前用户消息是否与历史中的某条用户消息高度相似。
    使用中文 bigram Jaccard 相似度。
    返回需要排除的旧 assistant 摘要消息索引列表（保留工具调用 + 工具结果对）。
    """
    if not current_user_msg or not messages:
        return []

    def _bigrams(text):
        """提取中文 bigram 特征"""
        chinese = re.sub(r'[^\u4e00-\u9fff]', '', text)
        if len(chinese) < 2:
            return set()
        return {chinese[i:i+2] for i in range(len(chinese)-1)}

    def _is_tool_call(content):
        """判断 assistant 消息是否为工具调用（JSON 格式），而非自然语言摘要"""
        content = content.strip()
        return content.startswith('{"tool_call"') or content.startswith('{"type":"tool"')

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
        jaccard = intersection / union

        if jaccard >= similarity_threshold:
            # 找到该轮次的 assistant 消息，只排除摘要（非工具调用），保留工具调用结果供复用
            j = i + 1
            turn_assistants = []
            while j < len(messages) and messages[j].get("role") != "user":
                if messages[j].get("role") == "assistant":
                    turn_assistants.append(j)
                j += 1
            for idx in turn_assistants:
                content = messages[idx].get("content", "")
                if not _is_tool_call(content):
                    exclude_indices.append(idx)

    return sorted(set(exclude_indices), reverse=True)


def build_context_messages(session_id, limit=500, summary_max_tokens=SUMMARY_TOKEN_BUDGET):
    """
    构建 LLM 上下文消息列表（SQLite 近期消息 + 压缩摘要）。

    返回：与 get_session_messages 相同格式的 dict 列表。
    摘要以 role="system" 形式追加在消息列表末尾，由上层决定如何使用。

    Args:
        session_id: 会话 ID
        limit: SQLite 消息上限
        summary_max_tokens: 摘要最大 token 数

    Returns:
        [{"role": ..., "content": ...}, ...]
    """
    messages = get_session_messages(session_id, limit=limit)

    # ── 重复提问检测：排除旧的工具调用结果 ──
    # 获取当前会话最后一条用户消息
    last_user_msg = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user_msg = m.get("content", "")
            break

    if last_user_msg:
        exclude_indices = _detect_repeated_query(messages, last_user_msg)
        if exclude_indices:
            # 从后往前删除，避免索引偏移
            for idx in exclude_indices:
                if idx < len(messages):
                    del messages[idx]
            # 插入一条标记，告知 LLM 可复用已完成结果，但失败/遗漏部分需重新执行
            messages.append({
                "role": "system",
                "content": "【注意】用户重复了之前的提问。历史中的工具调用结果已保留，已完成的部分可直接引用其输出内容，但执行失败或遗漏的部分请重新执行对应工具补充，不要假设所有步骤都已完成。"
            })

    # ── 注入压缩摘要 ──
    latest = get_latest_summary(days=30)
    if latest:
        # 截断到预算
        truncated = latest
        while estimate_messages_tokens([{"role": "system", "content": truncated}]) > summary_max_tokens:
            truncated = truncated[:int(len(truncated) * 0.8)]
        summary_msg = {
            "role": "system",
            "content": f"【历史对话摘要】{truncated}"
        }
        messages.append(summary_msg)

    return messages


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
    4. 用户消息：异步事实提取 + Token 预算制压缩检查
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
    now_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    add_chat_message(session_id, role, content, msg_type="chat",
                     trace_id=trace_id, user_id=user_id, timestamp=now_ts)

    # ── 4. 用户消息：异步事实提取 + Token 预算制压缩 ──
    if role == "user":
        # 异步事实提取（不阻塞主流程）
        extract_facts_async(content, trace_id=trace_id, user_id=user_id,
                            session_id=session_id, model=summary_model)

        # 更新标题（如果仍是默认值）
        if db[session_id]["title"] in ("新会话", "") or db[session_id]["title"].startswith("Session_"):
            db[session_id]["title"] = content[:25]
            save_sessions_db(db)

        # ── Token 预算制压缩检查 ──
        if not skip_compress:
            messages = get_chat_messages(session_id)
            total_tokens = estimate_messages_tokens(messages)

            # 软阈值触发：超过最小值且用户在发消息（任务间隙），启动异步压缩
            if total_tokens > RECENT_TOKEN_MIN:
                threshold_label = "soft" if total_tokens <= RECENT_TOKEN_MAX else "hard"
                log_operation(trace_id, user_id, session_id,
                              "memory.compress.start",
                              f"tokens {total_tokens} > {RECENT_TOKEN_MIN} ({threshold_label})")
                compress_conversation_async(
                    session_id,
                    recent_token_min=RECENT_TOKEN_MIN,
                    recent_token_max=RECENT_TOKEN_MAX,
                    summary_model=summary_model,
                    trace_id=trace_id,
                    user_id=user_id,
                )

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
