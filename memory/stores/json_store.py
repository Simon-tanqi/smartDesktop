"""
JSON 存储后端：长期记忆、压缩记忆、会话元数据、用户标识
"""
import json
import os
import uuid
from memory.config import (
    MEMORY_FILE, COMPRESSED_FILE, SHORT_TERM_FILE,
    USER_ID_FILE,
)

# 长期记忆默认结构（仅用于 load_long_term 的向后兼容）
DEFAULT_LONG_TERM = {
    "user_info": {"name": "", "job_title": "", "company": ""},
    "core_knowledge": [],
    "projects": [],
    "preferences": [],
    "facts": [],
}


# ═══════════════════════════════════════════════
# 长期记忆
# ═══════════════════════════════════════════════

def load_long_term():
    if not os.path.exists(MEMORY_FILE):
        return DEFAULT_LONG_TERM.copy()
    with open(MEMORY_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    merged = DEFAULT_LONG_TERM.copy()
    if "user_info" in data:
        merged["user_info"].update(data["user_info"])
    for key in ["core_knowledge", "projects", "preferences", "facts"]:
        if key in data and isinstance(data[key], list):
            merged[key].extend(data[key])
        elif key in data:
            merged[key] = data[key]
    return merged


def save_long_term(memory):
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(memory, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════
# 压缩记忆
# ═══════════════════════════════════════════════

def load_compressed():
    if not os.path.exists(COMPRESSED_FILE):
        return []
    with open(COMPRESSED_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_compressed(compressed_list):
    with open(COMPRESSED_FILE, "w", encoding="utf-8") as f:
        json.dump(compressed_list, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════
# 会话元数据（不含消息内容，消息内容在 SQLite）
# ═══════════════════════════════════════════════

def load_sessions_db():
    """加载会话元数据（id / title / created / last_turn）。"""
    if not os.path.exists(SHORT_TERM_FILE):
        return {}
    with open(SHORT_TERM_FILE, "r", encoding="utf-8") as f:
        db = json.load(f)
    # 兼容旧数据：如果 messages 字段存在，删除它（消息已迁移到 SQLite）
    for s in db.values():
        s.pop("messages", None)
    return db


def save_sessions_db(db):
    with open(SHORT_TERM_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════
# 用户标识
# ═══════════════════════════════════════════════

def get_user_id():
    """获取或生成持久用户 ID。"""
    if os.path.exists(USER_ID_FILE):
        with open(USER_ID_FILE, "r") as f:
            uid = f.read().strip()
            if uid:
                return uid
    uid = f"user_{uuid.uuid4().hex[:8]}"
    with open(USER_ID_FILE, "w") as f:
        f.write(uid)
    return uid
