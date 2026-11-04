"""
Unified confirmation manager.

Single global singleton ConfirmationManager with:
- Unified TTL (300s), thread lock, expiry cleanup
- Two storage formats: challenge and confirm
- Risk level engine (Bug 7)
- Timeout callback support (Bug 10)
"""
import copy
import secrets
import time
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable


# ======================================================
# Bug 7: Risk level with precise action matching
# ======================================================

class RiskLevel(Enum):
    """操作风险等级：低 / 中 / 高，用于决定确认策略。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


HIGH_RISK_ACTIONS = {
    "delete_file", "execute_command", "run_script",
    "drop_table", "format_disk", "run_python_script",
}

MEDIUM_RISK_ACTIONS = {
    "write_file", "send_email", "install_package", "update_record",
}


# ======================================================
# Bug 9: ConfirmationRequest dataclass with safe default
# ======================================================

@dataclass
class ConfirmationRequest:
    """Confirmation request with safe expires_at default (Bug 9 fix)."""
    token: str = ""
    command: str = ""
    level: str = "medium"
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + 300)
    status: str = "PENDING"



class ConfirmationManager:
    """Unified confirmation manager, global singleton."""

    _instance: Optional["ConfirmationManager"] = None
    _instance_lock = threading.Lock()

    def __init__(self, on_timeout_callback: Optional[Callable] = None):
        """Bug 10: Accept optional timeout callback for external notification."""
        self._pending: dict = {}
        self._lock = threading.Lock()
        self._ttl = 300
        self._on_timeout_callback = on_timeout_callback

    @classmethod
    def instance(cls) -> "ConfirmationManager":
        """Get global singleton (thread-safe)."""
        if cls._instance is not None:
            return cls._instance
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # --- General methods ---

    def store(self, token: str, data: dict) -> None:
        """Store pending item. data must contain 'type' field ('challenge' or 'confirm')."""
        data["_expiry"] = time.time() + self._ttl
        with self._lock:
            self._pending[token] = data

    def pop(self, token: str) -> Optional[dict]:
        """Pop and validate non-expired entry. Returns deepcopy (Bug 1 fix)."""
        with self._lock:
            entry = self._pending.pop(token, None)
            if entry and time.time() <= entry.get("_expiry", 0):
                return copy.deepcopy(entry)
            return None

    # --- Bug 6: Read-only check vs write enforcement ---

    def _is_expired(self, entry: dict) -> bool:
        """Bug 6: Read-only expiry check -- no side effects, no mutation."""
        return time.time() > entry.get("_expiry", 0)

    def _enforce_timeouts(self) -> int:
        """Bug 6/10: Enforce timeouts with optional callback notification."""
        now = time.time()
        timed_out = []
        with self._lock:
            for token, entry in list(self._pending.items()):
                if now > entry.get("_expiry", 0):
                    timed_out.append((token, copy.deepcopy(entry)))
                    del self._pending[token]
        # Bug 10: Call timeout callback outside lock
        if timed_out and self._on_timeout_callback:
            for _token, _entry in timed_out:
                try:
                    self._on_timeout_callback(_entry)
                except Exception:
                    pass
        return len(timed_out)

    def get_pending_requests(self) -> list:
        """Bug 6: Read-only query -- uses _is_expired(), no state mutation."""
        now = time.time()
        with self._lock:
            return [
                copy.deepcopy(e)
                for e in self._pending.values()
                if now <= e.get("_expiry", 0)
            ]

    # --- Legacy cleanup (delegates to _enforce_timeouts) ---

    def cleanup_expired(self) -> int:
        """Clean up all expired entries, return count."""
        return self._enforce_timeouts()

    def _cleanup_expired(self) -> int:
        """Bug 3: Internal alias called by create_* methods."""
        return self._enforce_timeouts()

    # --- Bug 7: Risk level determination ---

    def _determine_risk_level(self, action: str, action_input=None) -> RiskLevel:
        """Bug 7: Precise set-matching instead of fragile substring matching."""
        if action in HIGH_RISK_ACTIONS:
            return RiskLevel.HIGH
        if action in MEDIUM_RISK_ACTIONS:
            return RiskLevel.MEDIUM
        # Safety-first: unrecognized actions default to MEDIUM
        return RiskLevel.MEDIUM

    # --- Challenge-specific (safe_exec.py) ---

    def create_challenge(self, command, desc, source="", level="medium", **extra):
        """Create challenge token. Bug 3/6: auto-enforce timeouts before creation."""
        self._cleanup_expired()
        token = secrets.token_hex(16)
        data = {
            "type": "challenge",
            "command": command,
            "desc": desc,
            "source": source,
            "level": level,
            "timestamp": time.time(),
        }
        data.update(extra)
        self.store(token, data)
        return token

    def verify_challenge(self, token: str, response: str, expected_phrase: str) -> Optional[dict]:
        """校验挑战短语：用户输入必须与配置短语逐字一致（仅忽略首尾空白与大小写）。

        安全约束：不接受 "ok" / "确认" / "同意" 等泛化词，否则一句话即可放行高危操作，
        P1 挑战确认形同虚设。令牌一次性使用：pop 后无论校验成败均已失效。
        """
        entry = self.pop(token)
        if not entry or entry.get("type") != "challenge":
            return None
        if (response or "").strip().lower() != (expected_phrase or "").strip().lower():
            return None
        return entry

    # --- Confirm-specific (system_tools.py) ---

    def create_confirmation(self, command: str) -> str:
        """Create confirmation token. Bug 3/6: auto-enforce timeouts before creation."""
        self._cleanup_expired()
        token = secrets.token_hex(16)
        self.store(token, {
            "type": "confirm",
            "command": command,
        })
        return token

    def pop_confirmation(self, token: str) -> Optional[dict]:
        """Pop confirmation entry. Returns entry on success, None if expired."""
        entry = self.pop(token)
        if not entry or entry.get("type") != "confirm":
            return None
        return entry


# 便捷别名
confirmation_manager = ConfirmationManager.instance()
