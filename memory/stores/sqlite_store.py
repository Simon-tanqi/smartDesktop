"""
SQLite 存储后端：Agent 追踪日志、操作日志、聊天记录、压缩摘要

v4: 短期记忆改为 per-session 目录隔离
    summary 改为 per-session summaries.db（替代 summaries.md）
    全局日志和追踪仍在 DATA_DIR 下
"""
import os
import sqlite3
import threading
import datetime
from contextlib import contextmanager
from memory.config import (
    TRACE_DB, OPERATION_LOG_DB,
    get_session_messages_db, get_session_summaries_db,
)


# ═══════════════════════════════════════════════
# 连接管理（线程本地 + LRU，防止 per-session DB 过多）
# ═══════════════════════════════════════════════

_MAX_CONNS_PER_THREAD = 8

class _ThreadLocalPool(threading.local):
    def __init__(self):
        self.conn_pool = {}    # db_path -> conn
        self.access = []       # LRU 追踪

    def __del__(self):
        """线程终止时关闭所有连接并从全局注册表移除。"""
        try:
            for path, conn in list(self.conn_pool.items()):
                try:
                    conn.close()
                except Exception:
                    pass
                try:
                    with _global_conn_lock:
                        if path in _global_conns and conn in _global_conns[path]:
                            _global_conns[path].remove(conn)
                except Exception:
                    pass  # 解释器关闭时锁可能已失效
            self.conn_pool.clear()
            self.access.clear()
        except Exception:
            pass  # 防止线程终止时对象已被销毁导致的异常

_tl_pool = _ThreadLocalPool()

# 全局连接注册表：跟踪所有线程持有的连接，用于跨线程关闭
_global_conn_lock = threading.Lock()
_global_conns = {}  # db_path -> list of sqlite3.Connection


@contextmanager
def _get_conn(db_path, row_factory=False):
    """获取 SQLite 连接。线程本地 LRU 池，避免跨线程共享。
    
    增强版本：包含连接健康检查和重试机制
    """
    conn = None
    orig_row_factory = None
    pool = _tl_pool.conn_pool
    access = _tl_pool.access
    
    # 连接重试机制
    for attempt in range(3):
        try:
            # 检查现有连接的健康状态
            if db_path in pool:
                conn = pool[db_path]
                try:
                    conn.execute("SELECT 1").fetchone()
                    access.remove(db_path)
                except (sqlite3.ProgrammingError, sqlite3.OperationalError):
                    try:
                        conn.close()
                    except Exception:
                        pass
                    del pool[db_path]
                    if db_path in access:
                        access.remove(db_path)
                    with _global_conn_lock:
                        if db_path in _global_conns and conn in _global_conns[db_path]:
                            _global_conns[db_path].remove(conn)
                    conn = None
            
            if conn is None:
                if len(pool) >= _MAX_CONNS_PER_THREAD:
                    oldest = access.pop(0)
                    oldest_conn = pool[oldest]
                    try:
                        oldest_conn.close()
                    except Exception:
                        pass
                    del pool[oldest]
                    with _global_conn_lock:
                        if oldest in _global_conns and oldest_conn in _global_conns[oldest]:
                            _global_conns[oldest].remove(oldest_conn)
                
                print(f"[DEBUG] Opening SQLite: {db_path}")
                print(f"[DEBUG] Exists: {os.path.exists(db_path)}")
                print(f"[DEBUG] Parent dir: {os.path.exists(os.path.dirname(db_path))}")
                conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                pool[db_path] = conn
                with _global_conn_lock:
                    _global_conns.setdefault(db_path, []).append(conn)
            
            access.append(db_path)
            orig_row_factory = conn.row_factory
            if row_factory:
                conn.row_factory = sqlite3.Row
            
            break
            
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
            print(f"[DEBUG] SQLite error on attempt {attempt+1}: {e}")
            if attempt < 2:
                import time
                time.sleep(0.1 * (attempt + 1))
                continue
            else:
                # 所有重试都失败，重新抛出异常
                raise
    
    try:
        yield conn
    finally:
        if conn:
            try:
                if row_factory and orig_row_factory is not None:
                    conn.row_factory = orig_row_factory
                conn.commit()
            except Exception:
                # 如果commit失败，尝试rollback
                try:
                    conn.rollback()
                except Exception:
                    pass


@contextmanager
def _get_conn_simple(db_path, row_factory=False):
    """简单连接：每次创建关闭，用于低频操作（trace / oplog）。"""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    if row_factory:
        conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ═══════════════════════════════════════════════
# 内部工具
# ═══════════════════════════════════════════════

def _sanitize(text):
    if not text:
        return ""
    return " ".join(text.replace("\r", " ").replace("\n", " ").replace("\t", " ").split())


def _truncate(s, max_len=500):
    s = str(s) if s else ""
    return s[:max_len] + "...(截断)" if len(s) > max_len else s


# ═══════════════════════════════════════════════
# Agent 追踪日志（全局）
# ═══════════════════════════════════════════════

def _init_trace_tables(conn):
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


def log_trace(trace_id, span_name, node_type, duration_ms,
              session_id=None, parent_span_id=None,
              provider=None, model_name=None, model_params=None,
              input_summary=None, output_summary=None,
              error=None, status="success", extra=None):
    with _get_conn_simple(TRACE_DB) as conn:
        _init_trace_tables(conn)
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


# ═══════════════════════════════════════════════
# 操作日志（全局）
# ═══════════════════════════════════════════════

def _init_oplog_tables(conn):
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


def log_operation(trace_id, user_id, session_id, operation, detail=""):
    detail = _sanitize(detail)
    with _get_conn_simple(OPERATION_LOG_DB) as conn:
        _init_oplog_tables(conn)
        conn.execute(
            "INSERT INTO operation_log (trace_id, user_id, session_id, operation, detail) VALUES (?, ?, ?, ?, ?)",
            (trace_id, user_id, session_id, operation, detail),
        )


# ═══════════════════════════════════════════════
# 聊天记录（per-session messages.db）
# ═══════════════════════════════════════════════

def _init_chat_tables(conn):
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
            msg_id TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(trace_id, role, content)
        )
    """)
    for col, col_type in [
        ("msg_type", "TEXT DEFAULT 'chat'"),
        ("trace_id", "TEXT NOT NULL DEFAULT ''"),
        ("user_id", "TEXT NOT NULL DEFAULT ''"),
        ("msg_id", "TEXT DEFAULT ''"),
        ("is_temporary", "INTEGER DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE chat_history ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_history(session_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_trace ON chat_history(trace_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_msg_id ON chat_history(session_id, msg_id) WHERE msg_id != ''")


def add_chat_message(session_id, role, content, msg_type="chat",
                     trace_id="", user_id="", timestamp="", msg_id="",
                     is_temporary=False):
    if role not in ("user", "assistant", "system"):
        return
    db_path = get_session_messages_db(session_id)
    with _get_conn(db_path) as conn:
        _init_chat_tables(conn)
        if timestamp:
            conn.execute(
                "INSERT OR IGNORE INTO chat_history (trace_id, user_id, session_id, role, content, msg_type, msg_id, is_temporary, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (trace_id, user_id, session_id, role, content, msg_type, msg_id, int(is_temporary), timestamp),
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO chat_history (trace_id, user_id, session_id, role, content, msg_type, msg_id, is_temporary) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (trace_id, user_id, session_id, role, content, msg_type, msg_id, int(is_temporary)),
            )


def get_chat_messages(session_id, limit=200, offset=0):
    db_path = get_session_messages_db(session_id)
    if not os.path.exists(db_path):
        return []
    conn = None
    try:
        conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
        conn.execute("PRAGMA busy_timeout=5000")
        _init_chat_tables(conn)
        rows = conn.execute(
            "SELECT role, content, msg_type, msg_id, is_temporary, created_at FROM chat_history "
            "WHERE session_id = ? AND (msg_type = 'chat' OR msg_type = '' OR msg_type IS NULL OR msg_type = 'think') "
            "ORDER BY id ASC LIMIT ? OFFSET ?",
            (session_id, limit, offset),
        ).fetchall()
        result = []
        for r in rows:
            result.append({"role": r[0], "content": r[1], "msg_type": r[2] or "chat",
                     "msg_id": r[3], "is_temporary": bool(r[4]), "created_at": r[5]})
        return result
    finally:
        if conn:
            conn.close()


def get_session_messages(session_id, limit=500):
    """获取用于 LLM 上下文的消息（仅 chat 类型）。"""
    db_path = get_session_messages_db(session_id)
    if not os.path.exists(db_path):
        return []
    with _get_conn(db_path, row_factory=True) as conn:
        _init_chat_tables(conn)
        rows = conn.execute(
            "SELECT role, content FROM chat_history "
            "WHERE session_id = ? AND (msg_type = 'chat' OR msg_type IS NULL OR msg_type = '') AND is_temporary = 0 "
            "ORDER BY id ASC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    return [{"role": r[0], "content": r[1]} for r in rows]


def get_full_session_messages(session_id, limit=500):
    """获取完整的会话消息（包含工具调用和结果）。"""
    db_path = get_session_messages_db(session_id)
    if not os.path.exists(db_path):
        return []
    with _get_conn(db_path, row_factory=True) as conn:
        _init_chat_tables(conn)
        rows = conn.execute(
            "SELECT role, content, msg_type FROM chat_history "
            "WHERE session_id = ? AND is_temporary = 0 "
            "ORDER BY id ASC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    return [{"role": r[0], "content": r[1], "msg_type": r[2] or "chat"} for r in rows]


def _get_chat_messages_with_ids(session_id, msg_type_filter="chat"):
    """内部：获取带 ID 的消息（压缩用）。"""
    db_path = get_session_messages_db(session_id)
    if not os.path.exists(db_path):
        return []
    with _get_conn(db_path, row_factory=True) as conn:
        _init_chat_tables(conn)
        rows = conn.execute(
            "SELECT id, role, content FROM chat_history "
            "WHERE session_id = ? AND (msg_type = ? OR msg_type IS NULL OR msg_type = '') "
            "ORDER BY id ASC",
            (session_id, msg_type_filter),
        ).fetchall()
    return [{"id": r[0], "role": r[1], "content": r[2]} for r in rows]


def delete_chat_messages_before(session_id, before_id):
    db_path = get_session_messages_db(session_id)
    if not os.path.exists(db_path):
        return
    with _get_conn(db_path) as conn:
        _init_chat_tables(conn)
        conn.execute(
            "DELETE FROM chat_history WHERE session_id = ? AND id < ?",
            (session_id, before_id),
        )


def count_chat_messages(session_id):
    db_path = get_session_messages_db(session_id)
    if not os.path.exists(db_path):
        return 0
    with _get_conn(db_path) as conn:
        _init_chat_tables(conn)
        count = conn.execute(
            "SELECT COUNT(*) FROM chat_history WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
    return count


def delete_chat_session(session_id):
    """删除会话的全部聊天记录 + summaries + 目录。"""
    import shutil
    from memory.config import get_session_dir
    sess_dir = get_session_dir(session_id)
    db_path = get_session_messages_db(session_id)
    sum_db_path = get_session_summaries_db(session_id)

    # 第一步：通过全局注册表关闭所有线程持有的相关连接
    for path in [db_path, sum_db_path]:
        with _global_conn_lock:
            conns_to_close = _global_conns.pop(path, [])[:]
        for conn in conns_to_close:
            try:
                conn.close()
            except Exception:
                pass

    # 第二步：清理当前线程本地池中的残留
    for path in [db_path, sum_db_path]:
        if path in _tl_pool.conn_pool:
            try:
                _tl_pool.conn_pool[path].close()
            except Exception:
                pass
            del _tl_pool.conn_pool[path]
            if path in _tl_pool.access:
                _tl_pool.access.remove(path)

    # 第三步：删除目录
    if os.path.exists(sess_dir):
        shutil.rmtree(sess_dir, ignore_errors=True)


# ═══════════════════════════════════════════════
# 压缩摘要（per-session summaries.db）
# ═══════════════════════════════════════════════

def _init_summary_tables(conn):
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            cover_turn_start INTEGER,
            cover_turn_end INTEGER,
            summary_text TEXT NOT NULL,
            token_est INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_summaries_session ON summaries(session_id, created_at)")


def add_summary(session_id, summary_text, cover_turn_start=None,
                cover_turn_end=None, token_est=0):
    """写入一条压缩摘要到 per-session summaries.db。"""
    db_path = get_session_summaries_db(session_id)
    with _get_conn(db_path) as conn:
        _init_summary_tables(conn)
        conn.execute(
            "INSERT INTO summaries (session_id, cover_turn_start, cover_turn_end, summary_text, token_est) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, cover_turn_start, cover_turn_end, summary_text, token_est),
        )


def get_latest_summary(session_id):
    """获取最近一条摘要。"""
    db_path = get_session_summaries_db(session_id)
    if not os.path.exists(db_path):
        return ""
    with _get_conn(db_path, row_factory=True) as conn:
        _init_summary_tables(conn)
        row = conn.execute(
            "SELECT summary_text FROM summaries WHERE session_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    return row[0] if row else ""


def get_summaries(session_id, limit=5):
    """获取最近 N 条摘要。"""
    db_path = get_session_summaries_db(session_id)
    if not os.path.exists(db_path):
        return []
    with _get_conn(db_path, row_factory=True) as conn:
        _init_summary_tables(conn)
        rows = conn.execute(
            "SELECT summary_text, cover_turn_start, cover_turn_end, token_est "
            "FROM summaries WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    return [{"text": r[0], "cover_start": r[1], "cover_end": r[2],
             "token_est": r[3]} for r in rows]


# ═══════════════════════════════════════════════
# 旧数据迁移：chat_history.db → per-session
# ═══════════════════════════════════════════════

def migrate_old_chat_db():
    """一次性：将旧 chat_history.db 的数据按 session_id 分拆到 per-session 目录。"""
    from memory.config import OLD_CHAT_DB

    if not os.path.exists(OLD_CHAT_DB):
        return

    conn = sqlite3.connect(OLD_CHAT_DB)
    conn.row_factory = sqlite3.Row
    try:
        # 获取所有不同的 session_id
        rows = conn.execute(
            "SELECT DISTINCT session_id FROM chat_history WHERE session_id IS NOT NULL"
        ).fetchall()
        session_ids = [r[0] for r in rows]

        migrated = 0
        for sid in session_ids:
            msgs = conn.execute(
                "SELECT trace_id, user_id, session_id, role, content, "
                "msg_type, msg_id, created_at "
                "FROM chat_history WHERE session_id = ? ORDER BY id",
                (sid,),
            ).fetchall()
            if not msgs:
                continue

            db_path = get_session_messages_db(sid)
            with _get_conn(db_path) as new_conn:
                _init_chat_tables(new_conn)
                for m in msgs:
                    new_conn.execute(
                        "INSERT INTO chat_history "
                        "(trace_id, user_id, session_id, role, content, msg_type, msg_id, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (m["trace_id"], m["user_id"], m["session_id"],
                         m["role"], m["content"],
                         m["msg_type"] or "chat", m["msg_id"] or "",
                         m["created_at"]),
                    )
            migrated += len(msgs)

        conn.close()
        if migrated > 0:
            # 备份旧文件
            backup = OLD_CHAT_DB + ".bak"
            if not os.path.exists(backup):
                os.rename(OLD_CHAT_DB, backup)
            print(f"[Memory] 迁移完成：{len(session_ids)} 个会话，{migrated} 条消息 → per-session 目录")
    except Exception as e:
        print(f"[Memory] 旧数据迁移失败: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass
