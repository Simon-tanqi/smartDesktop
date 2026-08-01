# core/plan.py
import datetime
from typing import List, Dict, Any, Optional, Literal
from dataclasses import dataclass, field

@dataclass
class PlanStep:
    step_id: int
    description: str                     # 人类可读描述
    tool_name: str                       # 必须存在于工具注册表
    tool_args: Dict[str, Any] = field(default_factory=dict)  # 参数占位符或固定值
    depends_on: List[int] = field(default_factory=list)      # 依赖的 step_id 列表
    expected_outcome: str = ""           # 预期结果摘要
    status: Literal["pending", "running", "success", "failed", "skipped"] = "pending"
    result: Optional[Any] = None
    retry_count: int = 0
    error: str = ""                      # 最近一次失败原因

@dataclass
class TaskPlan:
    plan_id: str
    user_task: str
    steps: List[PlanStep]
    current_step_index: int = 0          # 指向下一个要执行的步骤
    created_at: str = field(default_factory=lambda: datetime.datetime.now().isoformat())
    version: int = 1
    is_complete: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "user_task": self.user_task,
            "steps": [{"step_id": s.step_id, "description": s.description, "tool_name": s.tool_name,
                       "tool_args": s.tool_args, "depends_on": s.depends_on,
                       "expected_outcome": s.expected_outcome, "status": s.status,
                       "result": s.result, "retry_count": s.retry_count, "error": s.error} for s in self.steps],
            "current_step_index": self.current_step_index,
            "created_at": self.created_at,
            "version": self.version,
            "is_complete": self.is_complete,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskPlan":
        steps = [PlanStep(**s) for s in data["steps"]]
        return cls(
            plan_id=data["plan_id"],
            user_task=data["user_task"],
            steps=steps,
            current_step_index=data.get("current_step_index", 0),
            created_at=data.get("created_at", datetime.datetime.now().isoformat()),
            version=data.get("version", 1),
            is_complete=data.get("is_complete", False),
        )