import datetime
import copy
import json
import sqlite3
import os
import time
import threading
from dataclasses import dataclass, field
from typing import Dict, Set, Any, Optional, List, Generator

# 轻量 logger，避免循环导入
import logging
_logger = logging.getLogger("agent_state")
def get_logger_migrations():
    return _logger

# 记忆/持久化函数依赖
from memory import append_session_message, get_full_session_messages, get_full_session_messages_rich
from memory.long_term import persist_task_memory
from logger import get_logger
from tools import (build_dynamic_system_prompt, build_tools_param, match_domains,
                   match_persona, add_domain_to_session, get_session_domains,
                   clear_session_domains)
from memory import (log_session_turn, log_trace, build_context_messages,
                    get_layered_memory, clear_session_messages, update_last_turn)
from utils.error_mapper import translate_error
from config.settings import CONTEXT_ROUTER_ENABLED
from core.progress_controller import ProgressEvaluator
from core.message_builder import build_system_prompt
from core.streaming import inline_compress


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


# ═══════════════════════════════════════════════
# 记忆/持久化函数（从 ReActEngine 迁移）
# ═══════════════════════════════════════════════

def inject_memory_if_needed(messages, layered_cache):
    """将记忆作为额外的 system 消息注入，不污染 user/assistant 序列。

    幂等保护：主循环每轮 step 都会调用本方法，若已存在记忆 system 消息
    （含 [长期记忆]/[近期交互] 标记）则直接返回，避免上下文膨胀。

    近期交互改为轻量级记忆注入：将 episodic_history 压缩为
    recent_history + current_task_focus，大幅缩减 tool_result 等大字段。
    """
    for _m in messages:
        if _m.get("role") == "system":
            _c = _m.get("content", "") or ""
            if "[长期记忆]" in _c or "[近期交互]" in _c:
                return messages

    result = list(messages)

    memory_parts = []
    if layered_cache.get("long_term"):
        memory_parts.append(f"[长期记忆]\n{layered_cache['long_term']}")

    # 近期交互：将 episodic_history 轻量化后注入（缩减 60%+ 无效 Token）
    recent_structured = layered_cache.get("recent_structured")
    if recent_structured:
        try:
            if isinstance(recent_structured, str):
                episodic_history = json.loads(recent_structured)
            else:
                episodic_history = recent_structured
            if not isinstance(episodic_history, list):
                episodic_history = []
        except Exception:
            episodic_history = []

        try:
            lightweight = _build_lightweight_memory(episodic_history)
            lightweight_json = json.dumps(lightweight, ensure_ascii=False, indent=2)
        except Exception:
            lightweight_json = json.dumps({"error": "memory serialization failed"})
        memory_parts.append(f"[近期交互]\n{lightweight_json}")

    if memory_parts:
        result.insert(1, {
            "role": "system",
            "content": "\n\n".join(memory_parts)
        })
    return result


def _truncate_str(s: str, max_len: int) -> str:
    """安全截断字符串；非字符串输入转为字符串；超长保留头部。"""
    try:
        text = str(s)
    except Exception:
        text = ""
    return text[:max_len] if len(text) > max_len else text


def _extract_key_findings(entry: dict) -> list:
    """从条目中提取 web_search 的关键发现（title/snippet 前 3 条，各截断 50 字符）。

    仅处理 tool_name == "web_search" 的调用；tool_result 为非法 JSON 或
    无结果时返回 []，不抛异常、不影响主流程。
    """
    findings = []
    try:
        tool_calls = entry.get("tool_calls") or []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            if tc.get("tool_name") != "web_search":
                continue
            raw = tc.get("tool_result") or ""
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            # 兼容常见结果容器字段
            results = None
            for key in ("results", "data", "items", "search_results"):
                candidate = data.get(key)
                if isinstance(candidate, list):
                    results = candidate
                    break
            if results is None:
                continue
            for item in results[:3]:
                if not isinstance(item, dict):
                    continue
                title = item.get("title") or item.get("name") or ""
                snippet = item.get("snippet") or item.get("summary") or item.get("description") or ""
                text = (title or snippet or "").strip()
                if text:
                    findings.append(_truncate_str(text, 50))
    except Exception:
        return []
    return findings


def _infer_current_focus(lightweight_rounds: list) -> str:
    """根据最近一轮状态推断当前任务焦点。

    - waiting_confirmation：提取 artifacts.pending_action（无则默认提示）
    - in_progress：提取当前子任务
    - 无明确状态：默认"继续执行当前任务"
    """
    try:
        if not lightweight_rounds:
            return "继续执行当前任务"
        latest = lightweight_rounds[-1]
        status = str(latest.get("status") or "").lower()

        if "waiting_confirmation" in status or "waiting" in status:
            artifacts = latest.get("artifacts") or {}
            pending = ""
            if isinstance(artifacts, dict):
                pending = str(artifacts.get("pending_action") or "")
            return pending if pending else "等待用户确认后继续执行"

        if "in_progress" in status:
            subtask = str(latest.get("subtask_status") or "").strip()
            return subtask if subtask else "正在执行当前任务"

        user_task = str(latest.get("user_task") or "").strip()
        return user_task if user_task else "继续执行当前任务"
    except Exception:
        return "继续执行当前任务"


def _build_lightweight_memory(episodic_history: list, max_rounds: int = 3) -> dict:
    """将完整 episodic_history 压缩为轻量级记忆对象。

    仅取末尾 max_rounds 轮；每轮保留：
    round / user_task(200) / ai_reply(200) / status / key_findings / artifacts / timestamp。
    返回 {recent_history: [...], current_task_focus: str}。
    输入为空列表时返回空结构，不抛异常。
    """
    try:
        if not isinstance(episodic_history, list):
            episodic_history = []
    except Exception:
        episodic_history = []

    recent_rounds = episodic_history[-max_rounds:] if len(episodic_history) > max_rounds else episodic_history

    recent_history = []
    for entry in recent_rounds:
        if not isinstance(entry, dict):
            continue
        artifacts = entry.get("artifacts")
        if artifacts is None:
            artifacts = {}
        recent_history.append({
            "round": entry.get("round_index", entry.get("round", 0)),
            "user_task": _truncate_str(entry.get("user_task", ""), 200),
            "ai_reply": _truncate_str(entry.get("ai_reply", ""), 200),
            "status": entry.get("status", entry.get("task_status", entry.get("main_task_status", ""))),
            "key_findings": _extract_key_findings(entry),
            "artifacts": artifacts,
            "timestamp": entry.get("timestamp", ""),
        })

    current_task_focus = _infer_current_focus(recent_history)
    return {
        "recent_history": recent_history,
        "current_task_focus": current_task_focus,
    }


def safe_final(engine, state, text, outcome="success", **kw) -> Generator[str, None, None]:
    """全任务仅 emit 一次 final。重复调用直接吞掉，防止前端收到双 final。

    从 ReActEngine._safe_final 迁移，通过 engine 参数访问 session_id/trace_id/streamer。
    """
    if getattr(state, "final_emitted", False):
        return
    state.final_emitted = True
    try:
        append_session_message(engine.session_id, "assistant", text, msg_type="final_answer")
    except Exception as e:
        import traceback
        get_logger().error(f"[ReactEngine] 保存AI回复失败: {e}", traceback=traceback.format_exc(),
                           trace_id=engine.trace_id, session_id=engine.session_id)
    # persist_task_memory 内部已调用 add_task_memory 写入长期记忆（含关键工具信息），
    # 此处不再重复调用，避免每次任务结束写入两条记录。
    persist_task_memory(engine, state, text, outcome=outcome)
    yield engine.streamer.emit_final(text, **kw)


def _parse_stored_tool_calls(m: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """解析落库的 tool_calls JSON 列为 API tool_calls 结构；解析失败返回 None。"""
    raw = m.get("tool_calls") or ""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    if isinstance(raw, list) and raw:
        return raw
    return None


def _truncate_keep_tail(content: str, limit: int) -> str:
    """超长截断：保留头部与尾部（中间省略），不得丢弃尾部。"""
    if len(content) <= limit:
        return content
    half = max(limit // 2, 1)
    head = content[:half]
    tail = content[-half:]
    return f"{head}\n\n...(内容过长已截断，原长度 {len(content)})...\n\n{tail}"


def load_cross_turn_history(engine, user_message: str, max_entries: int = 30,
                            max_chars: int = 6000,
                            max_tool_content_chars: int = 20000) -> List[Dict[str, Any]]:
    """加载跨 Turn 历史对话（完整轮次注入），修复多轮对话模型失忆。

    数据源：SQLite chat_history（get_full_session_messages_rich，已过滤 is_temporary）。
    注入过滤规则（与用户设计目标一致：工具结果全量以 role="tool" 拼入上下文）：
    - role=user 且 msg_type ∈ {chat, text, ""/None} → 注入（原文）
    - role=assistant 且 msg_type == "tool_call" → 注入（content 原文 + tool_calls JSON 解析）
    - role=tool   且 msg_type == "tool_result" → 注入（content 全量，不摘要；
      仅对单条加安全上限 max_tool_content_chars，超长时保留头部+尾部，不得丢弃尾部）
    - role=assistant 且 msg_type == "final_answer" → 注入（原文，单条 max_chars 截断保留）
    - 过滤 think / system 等内部消息
    序列合法性：跨轮注入按完整轮次边界裁剪（user 开头 → final_answer 结尾），
    保证 assistant(tool_calls) 与其后续 tool 消息成对出现，无孤儿 tool 消息。
    防重复：若最后一轮 user 与 user_message 相同则排除整轮（当前轮由主流程重建）。
    """
    try:
        history = get_full_session_messages_rich(engine.session_id, limit=500)
    except Exception as e:
        get_logger().warning(f"[跨Turn历史] 读取会话历史失败: {e}",
                             trace_id=engine.trace_id, session_id=engine.session_id)
        return []

    # ── 第一步：过滤出可注入消息（保序） ──
    def _eligible(m: Dict[str, Any]) -> bool:
        role = m.get("role")
        msg_type = m.get("msg_type") or "chat"
        if role == "user":
            return msg_type in ("chat", "text")
        if role == "assistant":
            return msg_type in ("tool_call", "final_answer")
        if role == "tool":
            return msg_type == "tool_result"
        return False

    eligible_msgs = [m for m in history if _eligible(m)]

    # ── 第二步：按完整轮次分组（每轮 = user 开头 → 下一个 user 之前） ──
    turns: List[List[Dict[str, Any]]] = []
    cur: List[Dict[str, Any]] = []
    for m in eligible_msgs:
        if m.get("role") == "user" and cur:
            turns.append(cur)
            cur = []
        cur.append(m)
    if cur:
        turns.append(cur)

    # 排除最后一轮为当前输入（防重复；当前轮由主流程重建）
    if user_message and turns:
        last = turns[-1]
        if last and last[0].get("role") == "user" and (last[0].get("content") or "") == user_message:
            turns.pop()

    # ── 第三步：只保留最近 max_entries 个完整轮次 ──
    if len(turns) > max_entries:
        turns = turns[-max_entries:]

    # ── 第四步：序列合法性校验 + 组装 API 消息 ──
    injected: List[Dict[str, Any]] = []
    for turn in turns:
        # 4.0 【修复 S5/M3】跳过未完成轮次：无 final_answer 的轮次表示任务中断
        # （ai_reply 为空），注入这种轮次会污染上下文、浪费 token。
        has_final = any(
            m.get("role") == "assistant" and (m.get("msg_type") or "") == "final_answer"
            for m in turn
        )
        if not has_final:
            continue

        # 4.1 清理孤儿 tool / 无 tool_calls 的 tool_call 行（安全兜底）
        cleaned: List[Dict[str, Any]] = []
        for m in turn:
            role = m.get("role")
            if role == "tool":
                # 仅当已挂起合法 assistant(tool_call) 才保留，否则丢弃孤儿 tool
                if cleaned and cleaned[-1].get("role") == "assistant" \
                        and cleaned[-1].get("_tc_parsed"):
                    cleaned.append(m)
                continue
            if role == "assistant" and (m.get("msg_type") or "") == "tool_call":
                tcs = _parse_stored_tool_calls(m)
                if not tcs:
                    continue  # 无有效 tool_calls 的 tool_call 行不可用
                m2 = dict(m)
                m2["_tc_parsed"] = tcs
                cleaned.append(m2)
                continue
            cleaned.append(m)

        # 4.2 尾部孤悬 assistant(tool_call)（其后无 tool 消息）→ 丢弃悬空段
        while cleaned and cleaned[-1].get("role") == "assistant" and cleaned[-1].get("_tc_parsed"):
            cleaned.pop()

        # 4.2.1 【修复 DeepSeek 400】中间孤悬的 assistant(tool_call)：
        # 如果后面紧跟的不是 tool 响应（如中间被 user_response 或其他角色隔开），
        # 必须把 _tc_parsed 标记摘掉，转为普通 assistant 消息，
        # 否则 DeepSeek 会报 "An assistant message with 'tool_calls' must be followed by tool messages..."
        # 导致新一轮 LLM 调用永远 400。
        for i in range(len(cleaned) - 1):
            if cleaned[i].get("role") == "assistant" and cleaned[i].get("_tc_parsed"):
                nxt = cleaned[i + 1]
                is_followed_by_tool = (nxt.get("role") == "tool")
                if not is_followed_by_tool:
                    # 孤立 tool_call → 转为普通 assistant（保留 content，删除 _tc_parsed）
                    cleaned[i].pop("_tc_parsed", None)

        # 4.2.2 【修复 H2】合并连续同 content 的 assistant(tool_call)：
        # _persist_tool_pair 按每个 tool_call 单独落库（每条都带相同的 full_content），
        # 跨轮恢复时须还原为单条 assistant + 合并的 tool_calls，避免 content 重复 N 份。
        merged: List[Dict[str, Any]] = []
        i = 0
        while i < len(cleaned):
            m = cleaned[i]
            if m.get("role") == "assistant" and m.get("_tc_parsed"):
                # 收集连续、同 content、同 role="assistant" 的 tool_call 消息
                combined_tool_calls = list(m["_tc_parsed"])
                content = m.get("content", "") or ""
                j = i + 1
                while j < len(cleaned):
                    nxt = cleaned[j]
                    if nxt.get("role") == "assistant" and nxt.get("_tc_parsed") \
                            and (nxt.get("content") or "") == content:
                        combined_tool_calls.extend(nxt["_tc_parsed"])
                        j += 1
                    else:
                        break
                merged_m = dict(m)
                merged_m["_tc_parsed"] = combined_tool_calls
                merged.append(merged_m)
                i = j
            else:
                merged.append(m)
                i += 1
        cleaned = merged

        # 4.3 组装
        for m in cleaned:
            role = m.get("role")
            content = m.get("content", "") or ""
            if role == "tool":
                content = _truncate_keep_tail(content, max_tool_content_chars)
                injected.append({
                    "role": "tool",
                    "tool_call_id": m.get("tool_call_id") or "",
                    "content": content,
                })
            elif role == "assistant" and m.get("_tc_parsed"):
                if len(content) > max_chars:
                    content = content[:max_chars] + f"\n\n...(内容过长已截断，原长度 {len(content)})..."
                injected.append({
                    "role": "assistant",
                    "content": content,
                    "tool_calls": m["_tc_parsed"],
                })
            else:
                if len(content) > max_chars:
                    content = content[:max_chars] + f"\n\n...(内容过长已截断，原长度 {len(content)})..."
                injected.append({"role": role, "content": content})

    return injected


# ═══════════════════════════════════════════════
# 从 ReActEngine 迁移：会话级 turn 游标
# ═══════════════════════════════════════════════

# ── 会话级 turn 游标（进程内缓存 + session DB 持久化） ──
# 跨任务/跨引擎实例存活，避免每任务新建引擎后 turn 归零。
_SESSION_TURN: dict[str, int] = {}
# P0-4/RE3: 进程内兜底计数器——写盘失败时仍可分配唯一 turn，避免同进程内撞 turn。
_TURN_FALLBACK: dict[str, int] = {}


# ═══════════════════════════════════════════════
# 从 ReActEngine 迁移：RunState / PrepareResult / BreakerState
# ═══════════════════════════════════════════════

@dataclass
class RunState:
    """run() 主循环的可变状态容器，便于在阶段方法间传递。

    将原 run() 内的局部可变状态收敛为数据类属性，
    使阶段方法（prepare_run_state / _call_llm_and_parse / ...）能通过
    同一对象引用读写状态，避免 nonlocal 变量。
    """
    # ── 消息与历史 ──
    conversation_messages: List[Dict[str, Any]] = field(default_factory=list)
    tool_call_history: List[Dict[str, Any]] = field(default_factory=list)
    thinking_summaries: List[str] = field(default_factory=list)

    # ── 循环状态 ──
    step: int = 0
    child_turn: int = 0
    consecutive_empty_turns: int = 0
    final_reply_saved: bool = False
    start_step: int = 0

    # ── 上下文缓存 ──
    layered_cache: Dict[str, Any] = field(default_factory=dict)

    # ── 每轮临时状态（循环内重置） ──
    full_content: str = ""
    full_reasoning: str = ""
    llm_reasoning_trace: Any = None


@dataclass
class PrepareResult:
    """prepare_run_state 的返回结果。

    承载 run() 后续（for step 循环 + _do_final_cleanup）需要的初始化数据。
    可变循环状态（conversation_messages / thinking_summaries / tool_call_history /
    layered_cache / consecutive_empty_turns / final_reply_saved / start_step）
    通过 RunState 承载，本类仅承载不可变配置。
    """
    flow_control: str  # "return" / "proceed"
    state: Any  # AgentTurnState
    model_id: Any
    base_url: str
    default_model: str
    default_provider: str
    temperature: float
    call_seq: list
    tools_param: Any
    tank_logger: Any  # TankLogger (_tl)
    session_logger: Any  # SessionLogger (_sl)
    remaining_steps: list


@dataclass
class BreakerState:
    """收敛 8 个断路器属性，避免散落在 ReActEngine 实例上。"""
    breaker_hits: int = 0
    last_error_category: str = ""
    last_exception_type: str = ""
    last_tool_call_signature: str = ""
    consecutive_fail_count: int = 0
    global_consecutive_failures: int = 0
    consecutive_empty_search: int = 0
    failure_breaker_msg_injected: bool = False


# ═══════════════════════════════════════════════
# 从 ReActEngine 迁移的方法（self → engine）
# ═══════════════════════════════════════════════

def _allocate_turn(engine, session_id: str) -> int:
    """会话内用户问题序号，0-indexed：第1个问题→0，第10个→9。
    持久化为真相源，进程内 _TURN_FALLBACK 兜底：无论写盘成功与否都更新内存，
    保证写盘失败时同进程内不撞 turn。"""
    from memory.session import load_turn_count, save_turn_count

    try:
        cur = load_turn_count(session_id)
    except Exception as e:
        get_logger().error(f"load_turn_count failed: {e}", exc_info=True)
        cur = _TURN_FALLBACK.get(session_id, -1)   # 从内存兜底读
    nxt = cur + 1
    try:
        save_turn_count(session_id, nxt)
    except Exception as e:
        get_logger().error(f"save_turn_count failed: {e}", exc_info=True)
    _TURN_FALLBACK[session_id] = nxt   # 无论写盘成功与否，都更新内存
    return nxt


def save_ai_thinking(engine, summary: str):
    """保存 AI 思考过程摘要（msg_type='think'，不参与上下文构建）"""
    if engine.session_id and summary:
        try:
            append_session_message(engine.session_id, "assistant", summary,
                                   trace_id=engine.trace_id, msg_type="think",
                                   skip_compress=True)
        except Exception as e:
            get_logger().warning(f"append think msg failed: {e}", trace_id=engine.trace_id, session_id=engine.session_id)


def reset_breaker_state(engine):
    engine._breaker_hits = 0
    engine._last_error_category = ""
    engine._last_exception_type = ""
    engine._last_tool_call_signature = ""
    engine._consecutive_fail_count = 0
    engine._global_consecutive_failures = 0
    engine._consecutive_empty_search = 0
    engine._failure_breaker_msg_injected = False
    engine._loop_detector.reset()


def on_emit_final(engine, text: str, timestamp: Optional[str], state, _tl, _sl):
    try:
        ts = timestamp or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        get_logger().log_turn_complete(
            session_id=engine.session_id,
            session_name=engine.session_name,
            turn=state.turn,
            ai_reply=(text or "")[:2000],
            ai_reply_time=ts,
            reasoning_trace="",
        )
        # TankLogger / SessionLogger 结束 Turn
        _tl.log_turn_end(
            ai_reply=(text or "")[:2000],
            reasoning_trace="",
        )
        _tl.end_trace(
            success=True,
            summary=(text or "")[:200],
        )
        _sl.end_turn(
            ai_reply=(text or "")[:2000],
            status="completed",
            error="",
            reasoning_trace="",
            route_level="",
        )
        update_last_turn(engine.session_id, state.turn)
    except Exception:
        pass


def save_checkpoint(engine, user_message, state, remaining_steps, _step: int,
                    tool_name: str = "", tool_args: Dict = None,
                    progress: str = "", messages_param=None):
    if not engine.session_id:
        return
    try:
        checkpoint = TaskCheckpoint(
            session_id=engine.session_id,
            trace_id=engine.trace_id,
            user_task=user_message,
            turn=state.turn,
            current_step=_step,
            messages=messages_param or [],
            completed_steps=engine.completed_steps,
            remaining_steps=remaining_steps,
            current_tool_name=tool_name,
            current_tool_args=tool_args or {},
            progress_summary=progress,
        )
        checkpoint_manager.save_checkpoint(checkpoint=checkpoint)
        get_logger().info(f"检查点已保存: {checkpoint.checkpoint_id}",
                          trace_id=engine.trace_id, session_id=engine.session_id)
    except Exception as e:
        get_logger().error(f"保存检查点失败: {e}", trace_id=engine.trace_id, session_id=engine.session_id)


def _is_user_confirmation(text: str) -> bool:
    """判断用户消息是否为对上一轮确认请求的肯定回复。

    精确匹配简短确认词，或"确认/可以/同意"开头的消息（如"确认，采用蓝色科技风"），
    避免"请确认一下..."这类非确认回复被误判。
    """
    t = (text or "").strip()
    if not t:
        return False
    if t in ("确认", "可以", "好的", "好", "同意", "没问题", "OK", "Ok", "ok", "是的", "对", "就这么办", "就按这个来", "就按这个", "好的，就按这个", "好的，就按这个来"):
        return True
    if "就按" in t:
        return True
    for prefix in ("确认，", "确认,", "确认 ", "确认生成", "确认按", "可以，", "可以,", "可以 ", "同意", "OK，", "ok，"):
        if t.startswith(prefix):
            return True
    return False


def _detect_confirmation_signal(layered_cache: dict, user_message: str) -> str:
    """检测"用户确认了上一轮确认请求"场景，返回注入 LLM 上下文的信号文本。

    条件：用户消息为肯定确认 + recent_structured 最近一轮处于 waiting_confirmation。
    命中时返回明确的系统指令，让 LLM 感知"已确认、勿再请求确认"，从根因消除重复确认循环。
    """
    if not _is_user_confirmation(user_message):
        return ""
    rs = layered_cache.get("recent_structured", "") if isinstance(layered_cache, dict) else ""
    if not rs:
        return ""
    try:
        data = json.loads(rs)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(data, list) or not data:
        return ""
    # data[-1] 是包含当前 user_message 的条目（状态尚未确定），
    # 检查 data[-2]（真正的上一轮）是否处于 waiting_confirmation
    if len(data) < 2:
        return ""
    prev = data[-2]
    status = str(prev.get("task_status") or prev.get("status") or "").lower()
    if "waiting_confirmation" in status or "waiting" in status:
        return ("[系统] 检测到您对上一轮确认请求的肯定回复，上一轮请求已确认。"
                "请直接继续执行当前任务，不要再次请求确认。")
    return ""


def build_user_content(engine, user_message, _thinking_summaries, confirmation_signal: str = ""):
    """构建用户消息各部分，统一正常路径和压缩路径"""
    # 各段预算占比（总和 = 1.0）
    _BUDGET = {
        "user_message":       0.50,   # ① 用户任务（核心）
        "plan_and_tools":     0.40,   # ② 规划 + 工具 + 思考轨迹
    }

    MAX_USER_CONTENT_CHARS = 12000
    segments = {}  # key → raw text with header

    # ① 用户任务（每次请求）
    base_user = f"【任务】\n{user_message}"
    if confirmation_signal:
        base_user = f"{confirmation_signal}\n\n{base_user}"
    segments["user_message"] = base_user

    # ⑥ AI思考摘要（近两轮）
    plan_tool_parts = []
    if _thinking_summaries:
        trace_lines = []
        for i, rt in enumerate(_thinking_summaries[-2:]):
            items = [f"#{i+1}"]
            if rt.get("current_intent"):
                items.append(rt["current_intent"])
            if rt.get("decision_rationale"):
                items.append(f"理由:{rt['decision_rationale'][:60]}")
            trace_lines.append(" | ".join(items))
        plan_tool_parts.append("【思考轨迹】\n" + "\n".join(trace_lines))

    if plan_tool_parts:
        segments["plan_and_tools"] = "\n\n".join(plan_tool_parts)

    # ─── 按预算截断 ───
    parts = []
    for key, budget_ratio in _BUDGET.items():
        raw = segments.get(key, "")
        if not raw:
            continue
        budget = int(MAX_USER_CONTENT_CHARS * budget_ratio)
        if len(raw) <= budget:
            parts.append(raw)
        else:
            parts.append(raw[:budget] + "\n[…该段已截断]")

    return "\n\n".join(parts)


def prepare_run_state(engine, rs: RunState, user_message: str,
                      model_choice: Dict[str, Any],
                      resumed_checkpoint: Optional[TaskCheckpoint] = None
                      ) -> Generator[str, None, PrepareResult]:
    """初始化 run() 状态：模型配置、记忆加载、上下文构建。

    从 run() 开头提取。包含：模型 fallback、logger 初始化、checkpoint 恢复/域匹配、
    BM25 上下文路由、话题切换清理、conversation_messages 初始化。

    可变循环状态通过 rs(RunState) 承载，不可变配置通过返回值 PrepareResult 承载。

    Returns:
        PrepareResult，flow_control 为 "return" 时终止 run()。
    """
    # 初始化局部变量（便于 finally 同步到 rs）
    conversation_messages: List[Dict[str, Any]] = []
    _thinking_summaries = []
    _tool_call_history = []
    layered_cache = {}
    consecutive_empty_turns = 0
    final_reply_saved = False
    start_step = 0

    try:
        from llm_client import get_available_models
        model_id = model_choice.get("id")
        provider = model_choice["provider"]
        model_name = model_choice["model"]
        temperature = model_choice.get("temperature", 0.75)
        base_url = model_choice.get("base_url", "https://api.deepseek.com")

        known_models = [m["model"] for m in get_available_models()]
        known_providers = {m["model"]: m["provider"] for m in get_available_models()}
        need_fallback = False

        if model_name not in known_models:
            need_fallback = True
        elif known_providers.get(model_name) != provider:
            need_fallback = True

        if need_fallback:
            available = get_available_models()
            # 禁止静默切云端：优先回退到同 provider（本地 Ollama）模型；
            # 若本地无可用模型则显式抛错，绝不静默切换到 DeepSeek 云端
            # （T-52~T-55 的 402 根因之一：fallback 静默切到无密钥的 deepseek-chat）。
            if available:
                same_provider = [m for m in available if m.get("provider") == provider]
                local_ollama = [m for m in available if m.get("provider") == "ollama"]
                fallback_candidates = same_provider or local_ollama
                fallback_model = fallback_candidates[0] if fallback_candidates else None
                if fallback_model is None:
                    raise RuntimeError(
                        f"模型 {model_name}（provider={provider}）不可用，且本地 Ollama 无可用模型可回退。"
                        f"已拒绝静默切换到云端模型，请检查 models.json 中 ollama 模型配置。"
                    )
            else:
                fallback_model = {"provider": "ollama", "model": "deepseek-r1:7b", "display": "本地 Ollama"}
            model_choice = dict(fallback_model)
            provider = model_choice["provider"]
            model_name = model_choice["model"]
            model_id = model_choice.get("id")
            base_url = model_choice.get("base_url", "http://localhost:11434")

        default_model = model_name
        default_provider = provider

        # 辅助调用（记忆压缩/提取、进度摘要等）provider 路由跟随会话：
        # 会话使用 ollama 时，辅助调用同样走 ollama，避免静默打到 DeepSeek 云端
        # （T-52~T-55 的 402 根因之二：辅助调用回落 deepseek 无密钥）。
        try:
            from memory.llm import set_session_provider
            set_session_provider(default_provider, default_model)
        except Exception:
            pass
        call_seq = [0]
        engine.completed_steps = []
        remaining_steps = []

        # 🆕 TankLogger trace 初始化
        from logger import get_tank_logger
        _tl = get_tank_logger()
        _tl.start_trace(engine.session_id, user_message[:80])

        # 🆕 SessionLogger 初始化
        from logger import get_session_logger
        _sl = get_session_logger()

        # 🆕 Per-Session 标准文本日志初始化
        from logger import get_per_session_logger
        _session_std_log = get_per_session_logger(engine.session_id)
        _session_std_log.info(f"═══ Session {engine.session_id} Run 开始 ═══")
        _session_std_log.info(f"用户输入: {user_message[:200]}")

        yield engine.streamer.emit_step("开始分析问题...")
        get_logger().info("Agent启动", trace_id=engine.trace_id, session_id=engine.session_id,
                          user_message=user_message[:100])

        layered_cache = {}
        _matched_domains = []
        _switch_result = None  # 由正常路径填充，BM25 段复用

        try:
            if resumed_checkpoint:
                yield engine.streamer.emit_step(f"正在恢复任务，上次执行到第 {resumed_checkpoint.turn} 轮")
                engine.completed_steps = resumed_checkpoint.completed_steps
                remaining_steps = resumed_checkpoint.remaining_steps

                # 保留 build_context_messages 调用以触发增量压缩，但结果不再用于子轮次拼接
                build_context_messages(engine.session_id, is_continuation=True)

                layered_cache = get_layered_memory(engine.session_id, user_message)

                loaded_domains = get_session_domains(engine.session_id)
                _matched_domains = list(loaded_domains)

                full_system = build_dynamic_system_prompt(domains=_matched_domains, persona_mode="")

                resume_prompt = f"""【任务恢复】以下是之前未完成任务的状态，请从第{resumed_checkpoint.turn+1}步继续执行：

已完成步骤：
{json.dumps(engine.completed_steps, ensure_ascii=False, indent=2)}

剩余步骤：
{json.dumps(remaining_steps, ensure_ascii=False)}

当前进度：{resumed_checkpoint.progress_summary}

请继续执行后续步骤，不要重复已完成的工作。"""

                full_system = full_system + "\n\n" + resume_prompt
                engine._full_system = full_system

                checkpoint_manager.mark_as_resumed(resumed_checkpoint.checkpoint_id)

            else:
                t0 = time.perf_counter()
                domains = match_domains(user_message)
                persona_mode = match_persona(user_message)
                _matched_domains = domains
                for _d in _matched_domains:
                    add_domain_to_session(engine.session_id, _d)
                elapsed_i = int((time.perf_counter() - t0) * 1000)
                log_trace(engine.trace_id, "intent.match_domains", "intent", elapsed_i,
                          session_id=engine.session_id, status="success",
                          input_summary=user_message[:200], output_summary=str(domains))
                get_logger().info("域匹配完成", trace_id=engine.trace_id, session_id=engine.session_id,
                                  domains=domains, persona=persona_mode, elapsed_ms=elapsed_i)

                # BM25 上下文路由：提前到 build_context_messages 之前执行，
                # 将引擎的判定结果传入 context builder，避免两套检测器结论矛盾
                if CONTEXT_ROUTER_ENABLED:
                    _switch_result = engine.context_detector.detect(user_message)
                    get_logger().debug(f"[上下文路由] BM25 检测结果: is_switch={_switch_result['is_switch']}, "
                                      f"sim={_switch_result['norm_similarity']}, reason={_switch_result['reason']}, "
                                      f"new_terms={_switch_result.get('new_unique_terms', [])}",
                                      trace_id=engine.trace_id, session_id=engine.session_id)
                else:
                    _switch_result = {"is_switch": False, "norm_similarity": 1.0, "reason": "feature_disabled"}

                # 话题切换清理已统一到主循环前的完整版（内存状态/域/记忆/消息/start_step 一次性处理）。
                # 此处不再重复清理，避免 clear_session_messages 二次执行清掉中间新写入消息、
                # 以及 completed_steps.clear 等冗余调用。build_context_messages 在旧消息清理前
                # 执行以触发增量压缩（结果不再用于子轮次拼接），符合原注释意图。
                build_context_messages(
                    engine.session_id,
                    is_continuation=not _switch_result["is_switch"]
                )

                layered_cache = get_layered_memory(engine.session_id, user_message)

                full_system = build_dynamic_system_prompt(
                    domains=domains, persona_mode=persona_mode
                )
                engine._full_system = full_system

                log_session_turn("user", user_message, trace_id=engine.trace_id,
                                 session_id=engine.session_id)

        except Exception as e:
            import traceback
            traceback.print_exc()
            # 兜底：translate_error 可能在循环导入 / 早期失败时未定义
            try:
                err_text = translate_error(str(e)[:500])
            except (NameError, Exception):
                err_text = f"处理异常: {str(e)[:500]}"
            yield engine.streamer.emit_error(err_text)
            return PrepareResult(
                flow_control="return",
                state=None, model_id=None, base_url="", default_model="",
                default_provider="", temperature=0.0, call_seq=[],
                tools_param=None, tank_logger=None, session_logger=None,
                remaining_steps=[],
            )

        state = global_state_manager.create_turn_state(user_message)
        # ✅ 会话级 turn 游标：跨任务累计，持久化到 session DB（进程重启不归零）
        #    恢复场景用检查点的 turn 保持一致性
        state.turn = resumed_checkpoint.turn if resumed_checkpoint else _allocate_turn(engine, engine.session_id)
        state.child_turn = 0
        state.final_emitted = False
        engine.progress = ProgressEvaluator()
        engine._turn_state = state
        # 挂钩 emit_final 回调：每次 emit_final 时自动记录轮次完成 + 更新 turn 号
        engine.streamer.set_on_final(lambda text, ts: on_emit_final(engine, text, ts, state, _tl, _sl))
        get_logger().info(f"初始轮次: {state.turn}, resumed_checkpoint: {resumed_checkpoint}",
                         trace_id=engine.trace_id, session_id=engine.session_id)

        if resumed_checkpoint:
            start_step = resumed_checkpoint.current_step + 1
            get_logger().info(f"从检查点恢复，轮次设为: {state.turn}，从第 {start_step} 步继续",
                             trace_id=engine.trace_id, session_id=engine.session_id)
        else:
            start_step = state.turn

        # save_ai_reply 已移除 —— AI 消息持久化收敛为前端 saveSessionToBackend 单一入口
        _cache = {}
        engine._last_tool_call_signature = ""      # 上次工具调用签名（失败熔断用）
        engine._consecutive_fail_count = 0          # 同名同参数连续真失败计数
        engine._global_consecutive_failures = 0
        engine._consecutive_empty_search = 0         # 连续空搜索结果计数（用于自适应降级）
        engine._failure_breaker_msg_injected = False
        engine._breaker_hits = 0
        engine._last_error_category = ""
        engine._last_exception_type = ""
        _thinking_summaries = []
        _tool_call_history = []

        final_reply_saved = False
        consecutive_empty_turns = 0
        state.child_turn = 0
        # 每次 run 重置循环检测器，保证多轮对话间调用历史不累积（原为 run 内局部变量，天然隔离）
        engine._loop_detector.reset()

        # ── BM25 上下文路由：检测任务切换 ──
        # detect() 已在正常路径中提前执行（确保 build_context_messages 拿到一致判断），
        # 此处复用结果；checkpoint 恢复路径走 else 分支不填充 _switch_result，跳过此段
        switch_result = _switch_result
        if switch_result is None:
            switch_result = {"is_switch": False, "norm_similarity": 1.0, "reason": "checkpoint_resume"}

        if switch_result["is_switch"]:
            get_logger().info(
                f"[话题切换] 旧任务 → 新任务: '{user_message[:50]}'，"
                f"清空所有旧上下文"
            )

            # 1. 清空引擎内存状态
            reset_breaker_state(engine)
            engine.completed_steps.clear()
            _thinking_summaries.clear()
            _tool_call_history.clear()
            engine._last_tool_call_signature = ""
            engine._consecutive_fail_count = 0
            engine._global_consecutive_failures = 0

            # 2. 清空会话域（关键！移除旧域工具）
            clear_session_domains(engine.session_id)
            for _d in _matched_domains:
                add_domain_to_session(engine.session_id, _d)

            # 3. 重新获取记忆并清理所有层级
            layered_cache = get_layered_memory(engine.session_id, user_message)

            # 3a. 清理 recent_structured（移除 child_turns 详情）
            if layered_cache.get("recent_structured"):
                try:
                    rc = json.loads(layered_cache["recent_structured"])
                    for turn in rc:
                        turn["child_turns"] = []
                        # 只保留一句话摘要
                        turn["context_summary"] = f"[已完成] {turn.get('user_task', '')[:60]}"
                    layered_cache["recent_structured"] = json.dumps(rc, ensure_ascii=False)
                except (json.JSONDecodeError, TypeError):
                    layered_cache["recent_structured"] = ""

            # 3b. 清理 long_term（移除任务相关记忆，只保留用户偏好）
            if layered_cache.get("long_term"):
                layered_cache["long_term"] = ""

            # 3c. 清理 older_summary
            if layered_cache.get("older_summary"):
                layered_cache["older_summary"] = ""

            # 4. 用新域重建 system prompt
            full_system = build_dynamic_system_prompt(
                domains=_matched_domains,
                persona_mode=persona_mode
            )
            engine._full_system = full_system

            # 5. 清空会话历史消息（可选：保留最近 1 条系统消息）
            clear_session_messages(engine.session_id, keep_system=True)

            start_step = 0

            get_logger().info(f"[话题切换] 完成。新域: {_matched_domains}")
            yield engine.streamer.emit_step(f"检测到新任务，已切换上下文")

        # 将当前任务记入历史（不影响后续检测，仅当 detect() 内部管理）
        engine.context_detector.commit_task(user_message)

        # ── Turn 级日志：主循环外创建 Turn ──
        _tl.log_turn_start(
            turn_index=state.turn,
            user_input=user_message,
            system_prompt=engine._full_system,
            route_level="",
        )
        _sl.start_turn(
            user_input=user_message,
            user_intent="",
            route_level="",
            system_prompt=engine._full_system,
            system_prompt_version="",
            reasoning_trace="",
            metadata={},
        )

        # 初始化 Multi-Turn 消息数组（优先从检查点恢复完整对话历史）
        # P0 修复：显式调用 build_system_prompt 组装 system prompt（角色/模式 + 工具列表 + 安全规则），
        # 以 build_dynamic_system_prompt 动态结果（core.md/safety.md/output.md/技能注入）作为安全规则基础，
        # 保证任何路径下 conversation_messages[0] 都是 system。
        _loaded_domains = get_session_domains(engine.session_id) if engine.session_id else []
        _tools_param = build_tools_param(loaded_domains=list(_loaded_domains)) or build_tools_param(loaded_domains=_matched_domains)
        _system_content = build_system_prompt(
            mode=persona_mode or "default",
            tools=_tools_param,
            safety_rules=engine._full_system,
        )
        if resumed_checkpoint and resumed_checkpoint.messages:
            conversation_messages = [dict(m) for m in resumed_checkpoint.messages]
            # 更新 system prompt（可能因会话上下文变化）；若恢复历史首条不是 system 则强制前置
            if conversation_messages and conversation_messages[0].get("role") == "system":
                conversation_messages[0]["content"] = _system_content
            else:
                conversation_messages.insert(0, {"role": "system", "content": _system_content})
        else:
            conversation_messages: List[Dict[str, Any]] = [
                {"role": "system", "content": _system_content}
            ]
            # ── 注入跨 Turn 历史对话（修复多轮失忆：此前仅传当前 user 输入）──
            # 按完整轮次注入 user/assistant(tool_call)/tool(tool_result)/assistant(final_answer)，
            # 工具结果全量拼入上下文；首轮无历史时为空，行为与原来一致；条数截断由 load_cross_turn_history 负责。
            try:
                _cross_turn_history = load_cross_turn_history(engine, user_message)
                if _cross_turn_history:
                    conversation_messages.extend(_cross_turn_history)
                    get_logger().info(
                        f"[跨Turn历史] 注入 {len(_cross_turn_history)} 条历史对话到上下文",
                        trace_id=engine.trace_id, session_id=engine.session_id,
                    )
            except Exception as _h_err:
                get_logger().warning(f"[跨Turn历史] 注入失败: {_h_err}",
                                     trace_id=engine.trace_id, session_id=engine.session_id)
            # 构建初始用户消息（含记忆注入 + 用户确认信号）
            confirmation_signal = _detect_confirmation_signal(layered_cache, user_message)
            if confirmation_signal:
                get_logger().info(
                    f"[确认信号] 检测到用户确认上一轮请求，注入信号到用户消息",
                    trace_id=engine.trace_id, session_id=engine.session_id,
                )
            initial_user_content = build_user_content(engine, user_message, _thinking_summaries, confirmation_signal)
            conversation_messages.append({
                "role": "user",
                "content": initial_user_content
        })

        return PrepareResult(
            flow_control="proceed",
            state=state,
            model_id=model_id,
            base_url=base_url,
            default_model=default_model,
            default_provider=default_provider,
            temperature=temperature,
            call_seq=call_seq,
            tools_param=_tools_param,
            tank_logger=_tl,
            session_logger=_sl,
            remaining_steps=remaining_steps,
        )
    finally:
        # 同步可变状态回 rs（无论从哪个 return 点退出都执行）
        rs.conversation_messages = conversation_messages
        rs.thinking_summaries = _thinking_summaries
        rs.tool_call_history = _tool_call_history
        rs.layered_cache = layered_cache
        rs.consecutive_empty_turns = consecutive_empty_turns
        rs.final_reply_saved = final_reply_saved
        rs.start_step = start_step