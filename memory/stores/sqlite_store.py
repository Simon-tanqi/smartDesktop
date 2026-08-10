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
import time
from contextlib import contextmanager
from memory.config import (
    TRACE_DB, OPERATION_LOG_DB,
    get_session_messages_db, get_session_summaries_db,
)
from logger import get_logger


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
                        _conn_locks.pop(conn, None)
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

# 连接级互斥锁：conn -> RLock
# 串行化"连接使用（execute/commit）"与"跨线程关闭（delete_chat_session）"，
# 避免在其它线程仍持有连接时并发 close 导致 sqlite3 C 层访问冲突
_conn_locks = {}


def _get_conn_lock(conn):
    """获取连接级互斥锁（懒创建）。"""
    with _global_conn_lock:
        lock = _conn_locks.get(conn)
        if lock is None:
            lock = threading.RLock()
            _conn_locks[conn] = lock
        return lock


def _drop_conn_lock(conn):
    """连接关闭后移除其锁条目，避免泄漏引用。"""
    with _global_conn_lock:
        _conn_locks.pop(conn, None)

# 删除中路径标记：delete_chat_session 删除目录期间置位，
# 防止其它线程在 rmtree 窗口内重新打开连接导致 WinError 32
_deleting_paths = set()


def _wait_deleting_done(db_path, timeout=2.0):
    """等待 db_path 的删除中标记解除。

    删除会话目录期间，其它线程可能通过 _get_conn 重新打开连接；
    此函数让它们短暂等待删除完成，避免在 rmtree 窗口内重新持有句柄。
    返回 True 表示可以继续；False 表示超时仍处于删除中。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _global_conn_lock:
            if db_path not in _deleting_paths:
                return True
        time.sleep(0.05)
    return False


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
                conn_lock = _get_conn_lock(conn)
                with conn_lock:
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
                        _drop_conn_lock(conn)
                        conn = None
            
            if conn is None:
                if not _wait_deleting_done(db_path):
                    raise sqlite3.OperationalError(
                        f"database is being deleted: {db_path}")
                if len(pool) >= _MAX_CONNS_PER_THREAD:
                    oldest = access.pop(0)
                    oldest_conn = pool[oldest]
                    oldest_conn_lock = _get_conn_lock(oldest_conn)
                    with oldest_conn_lock:
                        try:
                            oldest_conn.close()
                        except Exception:
                            pass
                    del pool[oldest]
                    with _global_conn_lock:
                        if oldest in _global_conns and oldest_conn in _global_conns[oldest]:
                            _global_conns[oldest].remove(oldest_conn)
                    _drop_conn_lock(oldest_conn)
                
                print(f"[DEBUG] Opening SQLite: {db_path}")
                print(f"[DEBUG] Exists: {os.path.exists(db_path)}")
                print(f"[DEBUG] Parent dir: {os.path.exists(os.path.dirname(db_path))}")
                conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute("PRAGMA synchronous=NORMAL")
                pool[db_path] = conn
                with _global_conn_lock:
                    _global_conns.setdefault(db_path, []).append(conn)
            
            access.append(db_path)
            orig_row_factory = conn.row_factory
            if row_factory:
                conn.row_factory = sqlite3.Row
            
            break
            
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
            err_str = str(e)
            print(f"[DEBUG] SQLite error on attempt {attempt+1}: {e}")
            is_locked = "database is locked" in err_str or "BUSY" in err_str.upper()
            max_retry = 5 if is_locked else 2
            if attempt < max_retry:
                import time
                if is_locked:
                    delay = 0.5 * (2 ** attempt)  # 指数退避: 0.5s, 1s, 2s, 4s, 8s
                else:
                    delay = 0.1 * (attempt + 1)
                time.sleep(delay)
                continue
            else:
                # 所有重试都失败，重新抛出异常
                raise
    
    try:
        conn_lock = _get_conn_lock(conn)
        with conn_lock:
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
    """简单连接：每次创建关闭，用于低频操作（trace / oplog / per-session DB）。

    关闭前执行 wal_checkpoint(PASSIVE)，促进 WAL 合并到主库，
    避免连接常驻导致 WAL 无限增长和 -shm 损坏。
    """
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    if row_factory:
        conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        try:
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        try:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass
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

_CHAT_HISTORY_FULL_COLS = [
    "id", "trace_id", "user_id", "session_id", "role", "content",
    "msg_type", "msg_id", "created_at", "is_temporary",
    "tool_calls", "thinking_summary", "tool_call_id",
]


def _migrate_chat_history_drop_unique(conn):
    """重建 chat_history 表以移除旧版 UNIQUE(trace_id, role, content) 约束。

    该表级约束 + INSERT OR IGNORE 会导致 role+content 相同的消息被静默丢弃
    （真实会话中用户连续相同输入 / 助手重复回复时会丢历史）。
    仅当检测到旧表定义含该约束时才执行迁移；新库 / 已迁移库直接跳过。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='chat_history'"
    ).fetchone()
    if not row or not row[0] or "UNIQUE(trace_id, role, content)" not in row[0]:
        return
    old_cols = [r[1] for r in conn.execute("PRAGMA table_info(chat_history)").fetchall()]
    conn.execute("ALTER TABLE chat_history RENAME TO chat_history_old")
    conn.execute("""
        CREATE TABLE chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT NOT NULL DEFAULT '',
            user_id TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            msg_type TEXT DEFAULT 'chat',
            msg_id TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_temporary INTEGER DEFAULT 0,
            tool_calls TEXT DEFAULT '',
            thinking_summary TEXT DEFAULT '',
            tool_call_id TEXT DEFAULT ''
        )
    """)
    shared = [c for c in _CHAT_HISTORY_FULL_COLS if c in old_cols]
    if shared:
        cols_str = ", ".join(shared)
        conn.execute(
            f"INSERT INTO chat_history ({cols_str}) SELECT {cols_str} FROM chat_history_old"
        )
    conn.execute("DROP TABLE chat_history_old")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_history(session_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_trace ON chat_history(trace_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_msg_id ON chat_history(session_id, msg_id) WHERE msg_id != ''")
    # 显式提交迁移，避免在未走 _get_conn_simple 的只读调用路径中被回滚
    conn.commit()


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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # 旧版表结构含 UNIQUE(trace_id, role, content) 表级约束，导致相同内容消息被
    # INSERT OR IGNORE 静默丢弃。检测到旧表时重建表移除该约束。
    _migrate_chat_history_drop_unique(conn)
    for col, col_type in [
        ("msg_type", "TEXT DEFAULT 'chat'"),
        ("trace_id", "TEXT NOT NULL DEFAULT ''"),
        ("user_id", "TEXT NOT NULL DEFAULT ''"),
        ("msg_id", "TEXT DEFAULT ''"),
        ("is_temporary", "INTEGER DEFAULT 0"),
        ("tool_calls", "TEXT DEFAULT ''"),
        ("thinking_summary", "TEXT DEFAULT ''"),
        ("tool_call_id", "TEXT DEFAULT ''"),
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
                     is_temporary=False, tool_calls="", thinking_summary="",
                     tool_call_id=""):
    if role not in ("user", "assistant", "system", "tool"):
        return
    db_path = get_session_messages_db(session_id)
    # 使用 _get_conn（线程本地连接池）：生命周期内复用连接，避免每次开关连接
    # 引发 WAL 文件锁/并发读写问题（替代 _get_conn_simple）。
    with _get_conn(db_path) as conn:
        _init_chat_tables(conn)
        cols = "trace_id, user_id, session_id, role, content, msg_type, msg_id, is_temporary"
        placeholders = "?, ?, ?, ?, ?, ?, ?, ?"
        params = [trace_id, user_id, session_id, role, content, msg_type, msg_id, int(is_temporary)]
        if tool_calls:
            cols += ", tool_calls"
            placeholders += ", ?"
            params.append(tool_calls)
        if thinking_summary:
            cols += ", thinking_summary"
            placeholders += ", ?"
            params.append(thinking_summary)
        if tool_call_id:
            cols += ", tool_call_id"
            placeholders += ", ?"
            params.append(tool_call_id)
        if timestamp:
            cols += ", created_at"
            placeholders += ", ?"
            params.append(timestamp)
        try:
            conn.execute(
                f"INSERT INTO chat_history ({cols}) VALUES ({placeholders})",
                params,
            )
        except sqlite3.IntegrityError as e:
            # 约束冲突（如 idx_chat_msg_id 唯一索引命中）：记录具体冲突日志，
            # 不再静默忽略，便于定位丢消息问题。
            get_logger().warning(
                f"add_chat_message IntegrityError: {e}",
                session_id=session_id, role=role, msg_type=msg_type,
                msg_id=msg_id,
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
            "WHERE session_id = ? AND (msg_type = 'chat' OR msg_type = '' OR msg_type IS NULL OR msg_type = 'think' OR msg_type = 'text' OR msg_type = 'final_answer') "
            "ORDER BY created_at ASC, id ASC LIMIT ? OFFSET ?",
            (session_id, limit, offset),
        ).fetchall()
        result = []
        for r in rows:
            result.append({"role": r[0], "content": r[1], "msg_type": r[2] or "chat",
                     "msg_id": r[3], "is_temporary": bool(r[4]), "created_at": r[5]})
        return result
    except sqlite3.DatabaseError as e:
        print(f"[WARN] get_chat_messages 读取失败: {e}, session={session_id}")
        return []
    finally:
        if conn:
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except Exception:
                pass
            conn.close()


def get_session_messages(session_id, limit=500):
    """获取用于 LLM 上下文的消息（仅 chat/text 类型）。"""
    db_path = get_session_messages_db(session_id)
    if not os.path.exists(db_path):
        return []
    try:
        with _get_conn_simple(db_path, row_factory=True) as conn:
            _init_chat_tables(conn)
            rows = conn.execute(
                "SELECT role, content FROM chat_history "
                "WHERE session_id = ? AND (msg_type = 'chat' OR msg_type IS NULL OR msg_type = '' OR msg_type = 'text') AND is_temporary = 0 "
                "ORDER BY created_at ASC, id ASC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [{"role": r[0], "content": r[1]} for r in rows]
    except sqlite3.DatabaseError as e:
        print(f"[WARN] get_session_messages 读取失败: {e}, session={session_id}")
        return []


def get_full_session_messages(session_id, limit=500):
    """获取完整的会话消息（包含工具调用和结果）。"""
    db_path = get_session_messages_db(session_id)
    if not os.path.exists(db_path):
        return []
    try:
        with _get_conn_simple(db_path, row_factory=True) as conn:
            _init_chat_tables(conn)
            rows = conn.execute(
                "SELECT role, content, msg_type FROM chat_history "
                "WHERE session_id = ? AND is_temporary = 0 "
                "ORDER BY created_at ASC, id ASC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [{"role": r[0], "content": r[1], "msg_type": r[2] or "chat"} for r in rows]
    except sqlite3.DatabaseError as e:
        print(f"[WARN] get_full_session_messages 读取失败: {e}, session={session_id}")
        return []


def get_full_session_messages_rich(session_id, limit=500):
    """获取完整会话消息（含 tool_calls / thinking_summary / tool_call_id）。"""
    db_path = get_session_messages_db(session_id)
    if not os.path.exists(db_path):
        return []
    try:
        with _get_conn_simple(db_path, row_factory=True) as conn:
            _init_chat_tables(conn)
            rows = conn.execute(
                "SELECT id, role, content, msg_type, msg_id, tool_calls, thinking_summary, tool_call_id, created_at "
                "FROM chat_history "
                "WHERE session_id = ? AND is_temporary = 0 "
                "ORDER BY created_at ASC, id ASC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [
            {
                "id": r[0],
                "role": r[1],
                "content": r[2],
                "msg_type": r[3] or "chat",
                "msg_id": r[4],
                "tool_calls": r[5],
                "thinking_summary": r[6],
                "tool_call_id": r[7],
                "created_at": r[8],
            }
            for r in rows
        ]
    except sqlite3.DatabaseError as e:
        print(f"[WARN] get_full_session_messages_rich 读取失败: {e}, session={session_id}")
        return []


def _get_chat_messages_with_ids(session_id, msg_type_filter="chat"):
    """内部：获取带 ID 的消息（压缩用）。"""
    db_path = get_session_messages_db(session_id)
    if not os.path.exists(db_path):
        return []
    try:
        with _get_conn_simple(db_path, row_factory=True) as conn:
            _init_chat_tables(conn)
            rows = conn.execute(
                "SELECT id, role, content FROM chat_history "
                "WHERE session_id = ? AND (msg_type = ? OR msg_type IS NULL OR msg_type = '') "
                "ORDER BY created_at ASC, id ASC",
                (session_id, msg_type_filter),
            ).fetchall()
        return [{"id": r[0], "role": r[1], "content": r[2]} for r in rows]
    except sqlite3.DatabaseError as e:
        print(f"[WARN] _get_chat_messages_with_ids 读取失败: {e}, session={session_id}")
        return []


def delete_chat_messages_before(session_id, before_id):
    db_path = get_session_messages_db(session_id)
    if not os.path.exists(db_path):
        return
    try:
        with _get_conn_simple(db_path) as conn:
            _init_chat_tables(conn)
            conn.execute(
                "DELETE FROM chat_history WHERE session_id = ? AND id < ?",
                (session_id, before_id),
            )
    except sqlite3.DatabaseError as e:
        print(f"[WARN] delete_chat_messages_before 失败: {e}, session={session_id}")


def count_chat_messages(session_id):
    db_path = get_session_messages_db(session_id)
    if not os.path.exists(db_path):
        return 0
    try:
        with _get_conn_simple(db_path) as conn:
            _init_chat_tables(conn)
            count = conn.execute(
                "SELECT COUNT(*) FROM chat_history WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
        return count
    except sqlite3.DatabaseError:
        return 0


def delete_chat_session(session_id):
    """删除会话的全部聊天记录 + summaries + 目录。"""
    import shutil
    from memory.config import get_session_dir
    sess_dir = get_session_dir(session_id)
    db_path = get_session_messages_db(session_id)
    sum_db_path = get_session_summaries_db(session_id)
    target_paths = {db_path, sum_db_path}

    # 第一步：标记删除中，阻止其它线程在删除窗口内重新打开连接
    with _global_conn_lock:
        _deleting_paths.update(target_paths)

    try:
        # 第二步：通过全局注册表关闭所有线程持有的相关连接
        # （-wal/-shm 文件句柄随 conn.close() 一并释放）
        # 每个连接先取连接级锁等待使用者退出，再关闭，避免并发 close 崩溃
        for path in target_paths:
            with _global_conn_lock:
                conns_to_close = _global_conns.pop(path, [])[:]
            for conn in conns_to_close:
                conn_lock = _get_conn_lock(conn)
                with conn_lock:
                    try:
                        conn.close()
                    except Exception:
                        pass
                _drop_conn_lock(conn)

        # 第三步：清理当前线程本地池中的残留，保持注册表一致性
        for path in target_paths:
            if path in _tl_pool.conn_pool:
                conn = _tl_pool.conn_pool[path]
                conn_lock = _get_conn_lock(conn)
                with conn_lock:
                    try:
                        conn.close()
                    except Exception:
                        pass
                _drop_conn_lock(conn)
                del _tl_pool.conn_pool[path]
                while path in _tl_pool.access:
                    _tl_pool.access.remove(path)

        # 第四步：删除目录，带重试窗口兜底瞬态占用（0.5s x 3）
        if os.path.exists(sess_dir):
            for attempt in range(3):
                shutil.rmtree(sess_dir, ignore_errors=True)
                if not os.path.exists(sess_dir):
                    break
                if attempt < 2:
                    time.sleep(0.5)
    finally:
        # 第五步：解除删除标记
        with _global_conn_lock:
            _deleting_paths.difference_update(target_paths)


def clear_session_messages(session_id: str, keep_system: bool = True):
    """清空会话历史消息，可选保留 system 角色消息。

    Args:
        session_id: 会话 ID
        keep_system: True 时只清除 user/assistant/tool 消息，保留 system 消息
    """
    db_path = get_session_messages_db(session_id)
    if not os.path.exists(db_path):
        return
    try:
        with _get_conn_simple(db_path) as conn:
            _init_chat_tables(conn)
            if keep_system:
                conn.execute(
                    "DELETE FROM chat_history WHERE session_id = ? AND role != 'system'",
                    (session_id,)
                )
            else:
                conn.execute(
                    "DELETE FROM chat_history WHERE session_id = ?",
                    (session_id,)
                )
            conn.commit()
    except sqlite3.DatabaseError as e:
        print(f"[WARN] clear_session_messages 失败: {e}, session={session_id}")


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
    try:
        with _get_conn_simple(db_path) as conn:
            _init_summary_tables(conn)
            conn.execute(
                "INSERT INTO summaries (session_id, cover_turn_start, cover_turn_end, summary_text, token_est) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, cover_turn_start, cover_turn_end, summary_text, token_est),
            )
    except sqlite3.DatabaseError as e:
        print(f"[WARN] add_summary 失败: {e}, session={session_id}")


def get_latest_summary(session_id):
    """获取最近一条摘要。"""
    db_path = get_session_summaries_db(session_id)
    if not os.path.exists(db_path):
        return ""
    try:
        with _get_conn_simple(db_path, row_factory=True) as conn:
            _init_summary_tables(conn)
            row = conn.execute(
                "SELECT summary_text FROM summaries WHERE session_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return row[0] if row else ""
    except sqlite3.DatabaseError:
        return ""


def get_summaries(session_id, limit=5):
    """获取最近 N 条摘要。"""
    db_path = get_session_summaries_db(session_id)
    if not os.path.exists(db_path):
        return []
    try:
        with _get_conn_simple(db_path, row_factory=True) as conn:
            _init_summary_tables(conn)
            rows = conn.execute(
                "SELECT summary_text, cover_turn_start, cover_turn_end, token_est "
                "FROM summaries WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [{"text": r[0], "cover_start": r[1], "cover_end": r[2],
                 "token_est": r[3]} for r in rows]
    except sqlite3.DatabaseError:
        return []


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
            with _get_conn_simple(db_path) as new_conn:
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
