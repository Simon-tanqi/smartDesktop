"""
SQLite 存储后端：Agent 追踪日志、操作日志、聊天记录
"""
import sqlite3
from memory.config import TRACE_DB, OPERATION_LOG_DB, CHAT_DB


# ═══════════════════════════════════════════════
# 内部工具
# ═══════════════════════════════════════════════

def _sanitize(text):
    """清洗日志内容：移除回车/换行/制表符，压缩多余空白。"""
    if not text:
        return ""
    return " ".join(text.replace("\r", " ").replace("\n", " ").replace("\t", " ").split())


def _truncate(s, max_len=500):
    """截断字符串，超长时附加标记。"""
    s = str(s) if s else ""
    return s[:max_len] + "...(截断)" if len(s) > max_len else s


# ═══════════════════════════════════════════════
# Agent 追踪日志
# ═══════════════════════════════════════════════

def _get_trace_db():
    conn = sqlite3.connect(TRACE_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_trace (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT NOT NULL,
            session_id TEXT,
            parent_span_id INTEGER,
            span_name TEXT NOT NULL,
            node_type TEXT NOT NULL,
            provider TEXT,
            model_name TEXT,
            model_params TEXT,
            input_summary TEXT,
            output_summary TEXT,
            error TEXT,
            duration_ms INTEGER,
            status TEXT DEFAULT 'success',
            extra TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trace_trace_id ON agent_trace(trace_id)")
    conn.commit()
    return conn


def log_trace(trace_id, span_name, node_type, duration_ms,
              session_id=None, parent_span_id=None,
              provider=None, model_name=None, model_params=None,
              input_summary=None, output_summary=None,
              error=None, status="success", extra=None):
    conn = _get_trace_db()
    conn.execute("""
        INSERT INTO agent_trace
            (trace_id, session_id, parent_span_id, span_name, node_type,
             provider, model_name, model_params, input_summary, output_summary,
             error, duration_ms, status, extra)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (trace_id, session_id, parent_span_id, span_name, node_type,
          provider, model_name, model_params,
          _truncate(input_summary), _truncate(output_summary),
          error, duration_ms, status, extra))
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════
# 操作日志
# ═══════════════════════════════════════════════

def _get_op_log_db():
    conn = sqlite3.connect(OPERATION_LOG_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS operation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            session_id TEXT,
            operation TEXT NOT NULL,
            detail TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_op_trace ON operation_log(trace_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_op_user ON operation_log(user_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_op_session ON operation_log(session_id)")
    conn.commit()
    return conn


def log_operation(trace_id, user_id, session_id, operation, detail=""):
    """写入结构化操作日志。"""
    detail = _sanitize(detail)
    conn = _get_op_log_db()
    conn.execute(
        "INSERT INTO operation_log (trace_id, user_id, session_id, operation, detail) VALUES (?, ?, ?, ?, ?)",
        (trace_id, user_id, session_id, operation, detail),
    )
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════
# 聊天记录（SQLite）
# ═══════════════════════════════════════════════

def _get_chat_db():
    conn = sqlite3.connect(CHAT_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT NOT NULL DEFAULT '',
            user_id TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            msg_type TEXT DEFAULT 'chat',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # 兼容旧表
    for col, col_type in [
        ("msg_type", "TEXT DEFAULT 'chat'"),
        ("trace_id", "TEXT NOT NULL DEFAULT ''"),
        ("user_id", "TEXT NOT NULL DEFAULT ''"),
    ]:
        try:
            conn.execute(f"ALTER TABLE chat_history ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_history(session_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_trace ON chat_history(trace_id)")
    conn.commit()
    return conn


def add_chat_message(session_id, role, content, msg_type="chat", trace_id="", user_id=""):
    """写入聊天记录。"""
    if role not in ("user", "assistant", "system"):
        return
    conn = _get_chat_db()
    conn.execute(
        "INSERT INTO chat_history (trace_id, user_id, session_id, role, content, msg_type) VALUES (?, ?, ?, ?, ?, ?)",
        (trace_id, user_id, session_id, role, content, msg_type),
    )
    conn.commit()
    conn.close()


def get_chat_messages(session_id, limit=200, offset=0):
    """读取聊天记录，按时间正序，支持分页。"""
    conn = _get_chat_db()
    rows = conn.execute(
        "SELECT role, content, msg_type, created_at FROM chat_history "
        "WHERE session_id = ? ORDER BY id ASC LIMIT ? OFFSET ?",
        (session_id, limit, offset),
    ).fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1], "msg_type": r[2], "created_at": r[3]} for r in rows]


def get_session_messages(session_id, limit=500):
    """
    获取用于构建 LLM 上下文的会话消息。
    仅返回 msg_type='chat' 或 msg_type 为空的消息（排除思考追踪消息）。
    """
    conn = _get_chat_db()
    rows = conn.execute(
        "SELECT role, content FROM chat_history "
        "WHERE session_id = ? AND (msg_type = 'chat' OR msg_type IS NULL OR msg_type = '') "
        "ORDER BY id ASC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in rows]


def _get_chat_messages_with_ids(session_id, msg_type_filter="chat"):
    """内部使用：获取带 ID 的消息列表（用于压缩时定位删除边界）。"""
    conn = _get_chat_db()
    rows = conn.execute(
        "SELECT id, role, content FROM chat_history "
        "WHERE session_id = ? AND (msg_type = ? OR msg_type IS NULL OR msg_type = '') "
        "ORDER BY id ASC",
        (session_id, msg_type_filter),
    ).fetchall()
    conn.close()
    return [{"id": r[0], "role": r[1], "content": r[2]} for r in rows]


def delete_chat_messages_before(session_id, before_id):
    """删除指定 ID 之前的聊天记录（压缩时使用）。"""
    conn = _get_chat_db()
    conn.execute(
        "DELETE FROM chat_history WHERE session_id = ? AND id < ?",
        (session_id, before_id),
    )
    conn.commit()
    conn.close()


def count_chat_messages(session_id):
    """统计会话聊天记录总数。"""
    conn = _get_chat_db()
    count = conn.execute(
        "SELECT COUNT(*) FROM chat_history WHERE session_id = ?", (session_id,)
    ).fetchone()[0]
    conn.close()
    return count


def delete_chat_session(session_id):
    """删除指定会话的全部聊天记录。"""
    conn = _get_chat_db()
    conn.execute("DELETE FROM chat_history WHERE session_id = ?", (session_id,))
    conn.commit()
    conn.close()
