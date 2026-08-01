import datetime
import copy
import json
import sqlite3
import os
import time
import threading
from dataclasses import dataclass, field
from typing import Dict, Set, Any, Optional, List

# 轻量 logger，避免循环导入
import logging
_logger = logging.getLogger("agent_state")
def get_logger_migrations():
    return _logger


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
    
    plan: Optional[Any] = None  # TaskPlan 对象，或 dict（兼容旧数据）
    available_tools: List[str] = field(default_factory=list)  # BUG-5: 保存时可用工具列表
    matched_domains: List[str] = field(default_factory=list)  # BUG-5: 保存时匹配的域

    def to_dict(self) -> Dict[str, Any]:
        base = {
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
        # BUG-L3-1: plan 可能是 TaskPlan 对象或 dict，统一序列化
        if self.plan is None:
            base["plan"] = "null"
        elif hasattr(self.plan, 'to_dict'):
            base["plan"] = json.dumps(self.plan.to_dict(), ensure_ascii=False)
        else:
            base["plan"] = json.dumps(self.plan, ensure_ascii=False)
        base["available_tools"] = json.dumps(self.available_tools, ensure_ascii=False)
        base["matched_domains"] = json.dumps(self.matched_domains, ensure_ascii=False)
        return base

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TaskCheckpoint':
        plan_raw = json.loads(data.get("plan", "null"))
        # BUG-L3-1: 反序列化时尝试还原为 TaskPlan 对象
        plan_obj = None
        if isinstance(plan_raw, dict) and plan_raw:
            try:
                from core.plan import TaskPlan
                plan_obj = TaskPlan.from_dict(plan_raw)
            except Exception:
                plan_obj = plan_raw  # 兼容旧格式，保留原始 dict
        elif isinstance(plan_raw, dict):
            plan_obj = plan_raw
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
            plan=plan_obj,
            available_tools=json.loads(data.get("available_tools", "[]")),
            matched_domains=json.loads(data.get("matched_domains", "[]")),
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
                            current_step INTEGER DEFAULT 0,
                            messages TEXT NOT NULL,
                            completed_steps TEXT NOT NULL,
                            remaining_steps TEXT NOT NULL,
                            current_tool_name TEXT,
                            current_tool_args TEXT,
                            progress_summary TEXT,
                            created_at TEXT NOT NULL,
                            is_resumed INTEGER DEFAULT 0,
                            plan TEXT,
                            available_tools TEXT,
                            matched_domains TEXT
                        )
                    """)
                    # 旧数据库迁移：如果表已存在但缺少 plan 列，则添加
                    cursor = conn.execute("PRAGMA table_info(checkpoints)")
                    existing_columns = {row[1] for row in cursor.fetchall()}
                    if "plan" not in existing_columns:
                        conn.execute("ALTER TABLE checkpoints ADD COLUMN plan TEXT")
                        get_logger_migrations().info("已添加 plan 列到 checkpoints 表")
                    # 迁移：补充 current_step / available_tools / matched_domains
                    if "current_step" not in existing_columns:
                        conn.execute("ALTER TABLE checkpoints ADD COLUMN current_step INTEGER DEFAULT 0")
                    if "available_tools" not in existing_columns:
                        conn.execute("ALTER TABLE checkpoints ADD COLUMN available_tools TEXT")
                    if "matched_domains" not in existing_columns:
                        conn.execute("ALTER TABLE checkpoints ADD COLUMN matched_domains TEXT")
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

    def save_checkpoint(self, **kwargs):
        try:
            self._init_db()
            if not self._db_initialized:
                return
            cleanup_age_hours = kwargs.pop("cleanup_age_hours", 24)
            checkpoint = kwargs.pop("checkpoint", None)
            if checkpoint is None:
                checkpoint = TaskCheckpoint(**kwargs)
            data = checkpoint.to_dict()
            with sqlite3.connect(CHECKPOINT_DB_PATH, timeout=10) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("""
                    INSERT OR REPLACE INTO checkpoints (
                        checkpoint_id, session_id, trace_id, user_task, turn,
                        current_step, messages, completed_steps, remaining_steps,
                        current_tool_name, current_tool_args,
                        progress_summary, created_at, is_resumed, plan,
                        available_tools, matched_domains
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """, (
                    data["checkpoint_id"], data["session_id"], data["trace_id"],
                    data["user_task"], data["turn"], data["current_step"],
                    data["messages"], data["completed_steps"], data["remaining_steps"],
                    data["current_tool_name"], data["current_tool_args"],
                    data["progress_summary"], data["created_at"], data["plan"],
                    data["available_tools"], data["matched_domains"],
                ))
                
                cutoff_time = (datetime.datetime.now() - datetime.timedelta(hours=cleanup_age_hours)).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute("DELETE FROM checkpoints WHERE created_at < ?", (cutoff_time,))
                
                conn.commit()
        except Exception as e:
            _logger.error(f"保存检查点失败: {e}")

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
    final_emitted: bool = False
    parse_fail: int = 0


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
    _history_messages: list = field(default_factory=list)  # BUG-X-1: 从引擎同步的消息历史
    
    def reset(self):
        self.collected_steps = []
        self.retry_count = 0
        self.consecutive_progress = 0
        self.skills_injected = set()
        self.final_saved = False
        self.pending_images = []
        self.checkpoint = None
    
    def set_history(self, messages: list):
        """BUG-X-1: 接收引擎传来的完整消息历史，供多轮对话上下文恢复。"""
        self._history_messages = messages
    
    def get_history(self) -> list:
        """BUG-X-1: Return synced message history with deepcopy protection."""
        return copy.deepcopy(self._history_messages)


# ======================================================
# State Machine (Bug 1/2/3/5 fixes)
# ======================================================

# Bug 5: Complete missing transition paths
_VALID_TRANSITIONS = {
    "IDLE": {"RUNNING", "CANCELLED"},
    "RUNNING": {"PAUSED", "CANCELLED", "TIMEOUT", "COMPLETED", "WAITING_CONFIRMATION"},
    "PAUSED": {"RUNNING", "CANCELLED"},
    "WAITING_CONFIRMATION": {"RUNNING", "CANCELLED", "TIMEOUT"},
    "TIMEOUT": {"IDLE", "RUNNING", "CANCELLED"},
    "COMPLETED": set(),
    "CANCELLED": {"IDLE"},
}

_STATE_TTL_SECONDS = 3600


@dataclass
class AgentState:
    """Thread-safe state container (Bug 1: deepcopy isolation)."""
    status: str = "IDLE"
    request: Optional[dict] = None
    created_at: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def get_state(self) -> dict:
        """Bug 1: Return deepcopy to protect internal state."""
        with self._lock:
            return copy.deepcopy({"status": self.status, "created_at": self.created_at})

    def get_request(self) -> Optional[dict]:
        """Bug 1: Return deepcopy to protect internal request."""
        with self._lock:
            if self.request is None:
                return None
            return copy.deepcopy(self.request)

    def transition_to(self, new_status: str) -> None:
        """Bug 2/5: State transition must pass validation table."""
        with self._lock:
            valid_targets = _VALID_TRANSITIONS.get(self.status, set())
            if new_status not in valid_targets:
                raise ValueError(
                    f"Invalid transition: {self.status} -> {new_status}, "
                    f"valid targets: {valid_targets}"
                )
            self.status = new_status

    def update_state(self, **kwargs) -> None:
        """Bug 2: Block direct status mutation; use transition_to() instead."""
        with self._lock:
            if "status" in kwargs:
                raise ValueError("Status change must use transition_to() method")
            for key, value in kwargs.items():
                if key == "status":
                    continue
                if hasattr(self, key):
                    setattr(self, key, value)


    def _check_timeout(self) -> bool:
        """Bug 6: Read-only timeout check -- no side effects, no state mutation."""
        with self._lock:
            return (time.time() - self.created_at) > _STATE_TTL_SECONDS

    def _enforce_timeouts(self) -> int:
        """Bug 6: Check and enforce timeout. Only called in write-path methods."""
        with self._lock:
            if (time.time() - self.created_at) > _STATE_TTL_SECONDS:
                self.status = "TIMEOUT"
                return 1
            return 0




class GlobalStateManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.domain_state = SessionDomainState()
            cls._instance._agent_state = AgentState()
        return cls._instance

    def create_turn_state(self, user_task: str) -> AgentTurnState:
        return AgentTurnState(
            turn=-1,
            child_turn=0,
            user_task=user_task,
            user_task_time=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )


    # -- State machine delegation (Bug 1/2/3/5/6) --

    def get_state(self) -> dict:
        """Bug 1: Return deepcopy state snapshot."""
        return self._agent_state.get_state()

    def get_request(self) -> Optional[dict]:
        """Bug 1: Return deepcopy request snapshot."""
        return self._agent_state.get_request()

    def get_active_sessions(self) -> list:
        """Bug 6: Read-only query -- uses _check_timeout(), no state mutation."""
        if not self._agent_state._check_timeout():
            return [self._agent_state.get_state()]
        return []

    def transition_to(self, new_status: str) -> None:
        """Bug 3/5/6: Auto-enforce timeout before transition."""
        self._agent_state._enforce_timeouts()
        self._agent_state.transition_to(new_status)

    def update_state(self, **kwargs) -> None:
        """Bug 2: Block direct status mutation."""
        self._agent_state.update_state(**kwargs)

    def create_state(self, status: str = "IDLE", request: Optional[dict] = None) -> None:
        """Bug 3/6: Auto-enforce timeout before creating new state."""
        self._agent_state._enforce_timeouts()
        with self._agent_state._lock:
            self._agent_state.status = status
            self._agent_state.request = copy.deepcopy(request) if request is not None else None
            self._agent_state.created_at = time.time()



global_state_manager = GlobalStateManager()

# Bug 8: Singleton alias for agent state management
agent_state_manager = global_state_manager