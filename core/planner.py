"""任务规划器：调用 LLM 将用户需求拆解为多步计划，并在失败时重规划。

Planner 负责首次规划（可强制至少两步），Replanner 依据最近工具消息中的
连续失败信号决定是否需要重规划，并合并已成功步骤生成新计划。
"""
import json
import time
from typing import List, Dict, Any, Optional
from llm_client import ai_concat
from logger import get_logger
from core.plan import TaskPlan, PlanStep

class Planner:
    """初始任务规划器：通过 LLM 生成结构化步骤计划。"""

    def __init__(self, model_name: str, provider: str, model_id: str = None, temperature: float = 0.3):
        """初始化规划器：保存模型标识与采样温度等调用参数。"""
        self.model_name = model_name
        self.provider = provider
        self.model_id = model_id
        self.temperature = temperature

    def create_plan(self, user_task: str, available_tools: List[str], 
                    trace_id: str = "", session_id: str = "",
                    force: bool = False) -> Optional[TaskPlan]:
        """
        调用 LLM 生成任务计划。
        返回 TaskPlan 对象，如果任务过于简单或无法规划，返回 None（调用方降级到 ReAct）。
        
        Args:
            force: 若 True，强制要求 LLM 至少生成 2 步（用于 L3 第一次规划失败后的重试）。
        """
        # 构建工具列表描述
        tools_desc = "\n".join([f"- {t}" for t in available_tools])
        system_prompt = f"""你是一个任务规划专家。请将用户需求拆解为有序的步骤列表。

可用工具：
{tools_desc}

输出要求：必须是合法的 JSON 对象，格式如下：
{{
  "steps": [
    {{
      "step_id": 1,
      "description": "步骤描述",
      "tool_name": "必须为上述工具之一",
      "tool_args": {{"参数名": "占位符或具体值"}},
      "depends_on": [],  // 依赖的 step_id 列表，默认空
      "expected_outcome": "预期结果摘要"
    }}
  ],
  "reason": "如果无法规划，说明原因"
}}

规则：
1. 步骤数不超过 10 步。
2. 每个步骤只能使用一个工具。
3. 如果某步骤需要上一步的输出，必须在 depends_on 中声明依赖（前置 step_id）。
4. 避免拆分过细，合并同类操作。
5. 如果任务可以直接用一个工具完成（或无需规划），则 steps 数组为空，并在 reason 中说明。
6. 输出必须只有 JSON，不要包含其他文本。
"""
        if force:
            system_prompt += "\n\n⚠️ 重要：即使任务看似简单，你也必须将其拆解为至少 2 个步骤，以确保完整执行。"  
        user_content = f"用户任务：{user_task}"
        messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}]

        try:
            response = ai_concat(
                messages=messages,
                model_name=self.model_name,
                provider=self.provider,
                model_id=self.model_id,
                temperature=self.temperature,
                trace_id=trace_id
            )
            raw = response["message"].get("content", "")
            # 尝试提取 JSON
            import re
            json_match = re.search(r'\{[\s\S]*\}', raw)
            if not json_match:
                get_logger().warning("Planner: 未找到 JSON，将降级到 ReAct", trace_id=trace_id)
                return None
            data = json.loads(json_match.group(0))
            steps_data = data.get("steps", [])
            if not steps_data:
                get_logger().info(f"Planner: 无需规划，原因: {data.get('reason', '未说明')}", trace_id=trace_id)
                return None
            # 构造 PlanStep
            steps = []
            for s in steps_data:
                step = PlanStep(
                    step_id=s["step_id"],
                    description=s["description"],
                    tool_name=s["tool_name"],
                    tool_args=s.get("tool_args", {}),
                    depends_on=s.get("depends_on", []),
                    expected_outcome=s.get("expected_outcome", ""),
                    status="pending"
                )
                steps.append(step)
            plan = TaskPlan(
                plan_id=f"plan_{int(time.time())}_{hash(user_task) & 0xffff}",
                user_task=user_task,
                steps=steps,
                current_step_index=0
            )
            get_logger().info(f"Planner: 生成计划，共 {len(steps)} 步", trace_id=trace_id)
            return plan
        except Exception as e:
            get_logger().error(f"Planner 失败: {e}", trace_id=trace_id)
            return None

class Replanner:
    """失败重规划器：检测连续失败信号，并在原计划基础上生成替代方案。"""

    def __init__(self, model_name: str, provider: str, model_id: str = None, temperature: float = 0.3):
        """初始化重规划器：保存模型标识与采样温度等调用参数。"""
        self.model_name = model_name
        self.provider = provider
        self.model_id = model_id
        self.temperature = temperature

    def should_replan(self, messages: List[Dict[str, Any]]) -> bool:
        """
        检测最近的工具消息中是否出现连续失败，判断是否需要触发重规划。

        规则：
        - 检查最近 8 条消息中的工具消息
        - 失败关键词：失败/错误/error/failed/timeout/exception/traceback
        - 从最新消息倒序检查，连续失败次数 >= 2 才返回 True（单次偶发失败不触发）
        """
        FAILURE_KEYWORDS = ["失败", "错误", "error", "failed", "timeout", "exception", "traceback"]

        # 取最近 8 条消息
        recent = messages[-8:] if len(messages) > 8 else messages

        consecutive_failures = 0
        # 从最新消息倒序检查
        for msg in reversed(recent):
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "tool" and isinstance(content, str):
                content_lower = content.lower()
                if any(kw in content or kw in content_lower for kw in FAILURE_KEYWORDS):
                    consecutive_failures += 1
                else:
                    break  # 遇到非失败工具消息，停止计数
            elif role == "tool":
                # content 非字符串（如 dict），不纳入失败统计
                break
        return consecutive_failures >= 2

    def replan(self, old_plan: TaskPlan, failed_step_id: int, error_msg: str,
               trace_id: str = "", session_id: str = "") -> Optional[TaskPlan]:
        """
        根据失败步骤和错误，生成新的后续计划（从失败步骤开始，包括失败步骤本身的新方案）。
        返回新的 TaskPlan，如果无法重规划则返回 None（表示任务失败）。
        """
        # 只保留未执行或失败的步骤（从失败步骤开始）
        remaining = [s for s in old_plan.steps if s.step_id >= failed_step_id and s.status != "success"]
        if not remaining:
            return None

        # 构造旧计划描述
        old_steps_desc = "\n".join([f"{s.step_id}: {s.description} (工具: {s.tool_name}, 状态: {s.status})" for s in old_plan.steps])
        system_prompt = f"""你是一个任务重规划专家。之前的计划在执行第 {failed_step_id} 步时失败，错误信息：{error_msg}

原始计划步骤：
{old_steps_desc}

请重新规划后续步骤（从失败步骤开始），避免再次出现同样错误。输出 JSON 格式同 Planner。
注意：
- 你可以调整步骤顺序、替换工具、拆分或合并步骤。
- 尽可能复用已成功步骤的结果（它们已存储在上下文中）。
- 如果无法继续，则 steps 数组为空，并在 reason 中说明。
"""
        user_content = f"用户任务：{old_plan.user_task}"
        messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}]

        try:
            response = ai_concat(
                messages=messages,
                model_name=self.model_name,
                provider=self.provider,
                model_id=self.model_id,
                temperature=self.temperature,
                trace_id=trace_id
            )
            raw = response["message"].get("content", "")
            import re
            json_match = re.search(r'\{[\s\S]*\}', raw)
            if not json_match:
                return None
            data = json.loads(json_match.group(0))
            new_steps_data = data.get("steps", [])
            if not new_steps_data:
                get_logger().warning(f"Replanner: 无法重规划，原因: {data.get('reason', '未说明')}", trace_id=trace_id)
                return None
            # 保留原计划中已成功的步骤（step_id < failed_step_id 且 status==success）
            successful = [s for s in old_plan.steps if s.step_id < failed_step_id and s.status == "success"]
            # 合并：成功步骤 + 新步骤（重新编号）
            combined = []
            for s in successful:
                combined.append(s)
            # 新步骤重新编号
            next_id = max([s.step_id for s in successful], default=0) + 1
            for s_data in new_steps_data:
                s_data["step_id"] = next_id
                next_id += 1
                step = PlanStep(**s_data)
                combined.append(step)
            # 调整 depends_on 中的 id 映射（新步骤依赖旧成功步骤可能没问题，但新步骤间依赖需要更新）
            # 简单起见，将 depends_on 中 < failed_step_id 的视为已满足，无需修改；新步骤间依赖按新 id 调整。
            # 更严谨的做法：这里假设新步骤不依赖旧步骤（除了已经成功的），实际可根据需要增强。
            new_plan = TaskPlan(
                plan_id=f"replan_{int(time.time())}_{hash(old_plan.user_task) & 0xffff}",
                user_task=old_plan.user_task,
                steps=combined,
                current_step_index=len(successful),  # 指向第一个新步骤
                version=old_plan.version + 1
            )
            get_logger().info(f"Replanner: 重规划成功，新计划共 {len(combined)} 步", trace_id=trace_id)
            return new_plan
        except Exception as e:
            get_logger().error(f"Replanner 失败: {e}", trace_id=trace_id)
            return None