"""任务计划数据结构：定义计划步骤与整体计划的模型及序列化。

PlanStep 描述单个执行步骤（工具、参数、依赖与执行状态），TaskPlan 将步骤
组合为一份可持久化、可恢复的完整计划，支持与 JSON 互相转换。
"""
import datetime
from typing import List, Dict, Any, Optional, Literal
from dataclasses import dataclass, field

@dataclass
class PlanStep:
    """单个计划步骤：记录待执行工具、参数、依赖关系与执行结果。"""

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
    """完整任务计划：由多个 PlanStep 组成，支持进度跟踪与 JSON 序列化。"""

    plan_id: str
    user_task: str
    steps: List[PlanStep]
    current_step_index: int = 0          # 指向下一个要执行的步骤
    created_at: str = field(default_factory=lambda: datetime.datetime.now().isoformat())
    version: int = 1
    is_complete: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """将计划导出为可 JSON 序列化的字典。"""
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
        """从字典还原计划对象，缺失字段使用默认值兜底。"""
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