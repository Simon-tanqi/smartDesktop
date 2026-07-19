import datetime
import json
import sqlite3
import os
import threading
from dataclasses import dataclass, field
from typing import Dict, Set, Any, Optional, List


# ═══════════════════════════════════════════════
# 检查点数据库路径（优先使用 DATA_DIR）
# ═══════════════════════════════════════════════
try:
    from memory.config import DATA_DIR
    CHECKPOINT_DB_PATH = os.path.join(DATA_DIR, 'checkpoints.db')
except ImportError:
    DATA_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'MyAgent')
    CHECKPOINT_DB_PATH = os.path.join(DATA_DIR, 'checkpoints.db')

LOGS_DIR = os.path.dirname(CHECKPOINT_DB_PATH)


# ═══════════════════════════════════════════════
# 任务检查点数据类
# ═══════════════════════════════════════════════

@dataclass
class TaskCheckpoint:
    session_id: str = ""
    trace_id: str = ""
    user_task: str = ""
    turn: int = 0
    current_step: int = 0  # 2026-07-12 修复：补充子步骤字段，react_engine.py:382 调用处传了 current_step 但类中未定义
    messages: List[Dict[str, Any]] = field(default_factory=list)
    completed_steps: List[Dict[str, Any]] = field(default_factory=list)
    remaining_steps: List[str] = field(default_factory=list)
    current_tool_name: str = ""
    current_tool_args: Dict[str, Any] = field(default_factory=dict)
    progress_summary: str = ""
    created_at: str = field(default_factory=lambda: datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    checkpoint_id: str = field(default_factory=lambda: f"chk_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}_{os.urandom(4).hex()}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "user_task": self.user_task,
            "turn": self.turn,
            "current_step": self.current_step,
            "messages": json.dumps(self.messages, ensure_ascii=False),
            "completed_steps": json.dumps(self.completed_steps, ensure_ascii=False),
            "remaining_steps": json.dumps(self.remaining_steps, ensure_ascii=False),
            "current_tool_name": self.current_tool_name,
            "current_tool_args": json.dumps(self.current_tool_args, ensure_ascii=False),
            "progress_summary": self.progress_summary,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TaskCheckpoint':
        return cls(
            checkpoint_id=data.get("checkpoint_id", ""),
            session_id=data.get("session_id", ""),
            trace_id=data.get("trace_id", ""),
            user_task=data.get("user_task", ""),
            turn=data.get("turn", 0),
            current_step=data.get("current_step", 0),  # 2026-07-12 修复：旧数据无此字段时默认 0
            messages=json.loads(data.get("messages", "[]")),
            completed_steps=json.loads(data.get("completed_steps", "[]")),
            remaining_steps=json.loads(data.get("remaining_steps", "[]")),
            current_tool_name=data.get("current_tool_name", ""),
            current_tool_args=json.loads(data.get("current_tool_args", "{}")),
            progress_summary=data.get("progress_summary", ""),
            created_at=data.get("created_at", ""),
        )


# ═══════════════════════════════════════════════
# 检查点存储管理器
# ═══════════════════════════════════════════════

class CheckpointManager:
    _instance = None
    _db_initialized = False
    _instance_lock = threading.Lock()
    _init_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def _init_db(self):
        with self._init_lock:
            if self._db_initialized:
                return
            try:
                os.makedirs(LOGS_DIR, exist_ok=True)
                with sqlite3.connect(CHECKPOINT_DB_PATH, timeout=10) as conn:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS checkpoints (
                            checkpoint_id TEXT PRIMARY KEY,
                            session_id TEXT NOT NULL,
                            trace_id TEXT,
                            user_task TEXT NOT NULL,
                            turn INTEGER NOT NULL DEFAULT 0,
                            messages TEXT NOT NULL,
                            completed_steps TEXT NOT NULL,
                            remaining_steps TEXT NOT NULL,
                            current_tool_name TEXT,
                            current_tool_args TEXT,
                            progress_summary TEXT,
                            created_at TEXT NOT NULL,
                            is_resumed INTEGER DEFAULT 0
                        )
                    """)
                    conn.execute("""
                        CREATE INDEX IF NOT EXISTS idx_checkpoints_session ON checkpoints(session_id)
                    """)
                    conn.execute("""
                        CREATE INDEX IF NOT EXISTS idx_checkpoints_created ON checkpoints(created_at)
                    """)
                    conn.commit()
                self._db_initialized = True
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"警告：检查点数据库初始化失败，任务恢复功能将不可用: {e}")

    def save_checkpoint(self, checkpoint: TaskCheckpoint, cleanup_age_hours: int = 24):
        self._init_db()
        if not self._db_initialized:
            return
        data = checkpoint.to_dict()
        with sqlite3.connect(CHECKPOINT_DB_PATH, timeout=10) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                INSERT OR REPLACE INTO checkpoints (
                    checkpoint_id, session_id, trace_id, user_task, turn,
                    messages, completed_steps, remaining_steps,
                    current_tool_name, current_tool_args,
                    progress_summary, created_at, is_resumed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """, (
                data["checkpoint_id"], data["session_id"], data["trace_id"],
                data["user_task"], data["turn"], data["messages"],
                data["completed_steps"], data["remaining_steps"],
                data["current_tool_name"], data["current_tool_args"],
                data["progress_summary"], data["created_at"]
            ))
            
            cutoff_time = (datetime.datetime.now() - datetime.timedelta(hours=cleanup_age_hours)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("DELETE FROM checkpoints WHERE created_at < ?", (cutoff_time,))
            
            conn.commit()

    def get_latest_checkpoint(self, session_id: str) -> Optional[TaskCheckpoint]:
        self._init_db()
        if not self._db_initialized:
            return None
        with sqlite3.connect(CHECKPOINT_DB_PATH, timeout=10) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            cursor = conn.execute("""
                SELECT * FROM checkpoints 
                WHERE session_id = ? AND is_resumed = 0
                ORDER BY created_at DESC LIMIT 1
            """, (session_id,))
            row = cursor.fetchone()
            if row:
                columns = [desc[0] for desc in cursor.description]
                data = dict(zip(columns, row))
                return TaskCheckpoint.from_dict(data)
        return None

    def mark_as_resumed(self, checkpoint_id: str):
        self._init_db()
        if not self._db_initialized:
            return
        with sqlite3.connect(CHECKPOINT_DB_PATH, timeout=10) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                UPDATE checkpoints SET is_resumed = 1 WHERE checkpoint_id = ?
            """, (checkpoint_id,))
            conn.commit()

    def delete_checkpoints(self, session_id: str):
        self._init_db()
        if not self._db_initialized:
            return
        with sqlite3.connect(CHECKPOINT_DB_PATH, timeout=10) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("DELETE FROM checkpoints WHERE session_id = ?", (session_id,))
            conn.commit()

    def cleanup_old_checkpoints(self, max_age_hours: int = 24):
        self._init_db()
        if not self._db_initialized:
            return
        cutoff_time = (datetime.datetime.now() - datetime.timedelta(hours=max_age_hours)).strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(CHECKPOINT_DB_PATH, timeout=10) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("DELETE FROM checkpoints WHERE created_at < ?", (cutoff_time,))
            conn.commit()


checkpoint_manager = CheckpointManager()


# ═══════════════════════════════════════════════
# 原有状态类保持不变
# ═══════════════════════════════════════════════

@dataclass
class AgentTurnState:
    turn: int = 0
    child_turn: int = 0
    user_task: str = ""
    user_task_time: str = ""


@dataclass
class SessionDomainState:
    _domains: Dict[str, Set[str]] = field(default_factory=dict)
    
    def add_domain(self, session_id: str, domain: str):
        if session_id not in self._domains:
            self._domains[session_id] = set()
        self._domains[session_id].add(domain)
    
    def get_domains(self, session_id: str) -> Set[str]:
        return self._domains.get(session_id, set()).copy()
    
    def clear_domains(self, session_id: str):
        if session_id in self._domains:
            del self._domains[session_id]


@dataclass
class AgentExecutionState:
    collected_steps: list = field(default_factory=list)
    retry_count: int = 0
    consecutive_progress: int = 0
    skills_injected: set = field(default_factory=set)
    final_saved: bool = False
    pending_images: list = field(default_factory=list)
    checkpoint: Optional[TaskCheckpoint] = None
    
    def reset(self):
        self.collected_steps = []
        self.retry_count = 0
        self.consecutive_progress = 0
        self.skills_injected = set()
        self.final_saved = False
        self.pending_images = []
        self.checkpoint = None


class GlobalStateManager:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.domain_state = SessionDomainState()
        return cls._instance
    
    def create_turn_state(self, user_task: str) -> AgentTurnState:
        return AgentTurnState(
            turn=-1,  # 由调用方从 get_last_turn 覆盖；-1 是哨兵值
            child_turn=0,
            user_task=user_task,
            user_task_time=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )


global_state_manager = GlobalStateManager()