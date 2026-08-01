import json
from typing import List, Dict, Any, Optional

from core.message_builder import StandardMessageBuilder


class DesktopAgent:
    def __init__(self, static_system_prompt: str, tools_schema: list):
        # ✅ 静态资源初始化后不再修改
        self.static_system_prompt = static_system_prompt
        self.tools_schema = tools_schema

        # ✅ 动态状态：标准消息历史
        self.conversation_history: List[Dict[str, Any]] = []
        self.builder = StandardMessageBuilder()

    async def run(self, user_input: str):
        # 1. 获取记忆（独立于 system prompt）
        memory_text = self._get_memory_preamble()

        # 1.5 历史过长时压缩
        await self._compress_if_needed()

        # 2. 构建本轮 messages（system 已嵌入 messages[0]）
        messages = self.builder.build_messages(
            history=self.conversation_history,
            current_user_input=user_input,
            memory_preamble=memory_text,
            system_prompt=self.static_system_prompt  # ✅ 嵌入到 messages 开头
        )

        # 3. ReAct 循环
        for step in range(5):
            # ✅ 调用 LLM：system 已嵌入 messages，tools 作为独立参数
            response = await self.llm_client.chat(
                messages=messages,                # 动态消息流（含 system）
                tools=self.tools_schema            # ✅ 纯 Schema，不参与拼接
            )

            # --- 处理响应 ---

            # 情况A: LLM 输出最终文本
            if response.content and not response.tool_calls:
                self.conversation_history.append({
                    "role": "assistant",
                    "content": response.content
                })
                yield response.content
                break

            # 情况B: LLM 决定调用工具
            if response.tool_calls:
                # ✅ 记录完整的 assistant 消息（包含所有 tool_calls）
                assistant_msg = {
                    "role": "assistant",
                    "content": response.content or "",  # ✅ 空字符串而非 None
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments
                            }
                        }
                        for tc in response.tool_calls  # ✅ 所有 tool_calls
                    ]
                }
                self.conversation_history.append(assistant_msg)

                # ✅ 逐个执行并记录每个 tool 的结果
                for tc in response.tool_calls:
                    result = await self.execute_tool(
                        tc.function.name,
                        json.loads(tc.function.arguments)
                    )
                    self.conversation_history.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": tc.function.name,
                        "content": json.dumps(result, ensure_ascii=False)
                                    if isinstance(result, dict) else str(result)
                    })

                # ✅ 下一轮请求时，messages 会自动包含上述 tool 消息
                # 无需再传 current_user_input，因为历史已经更新
                messages = self.builder.build_messages(
                    history=self.conversation_history,
                    memory_preamble=memory_text,
                    system_prompt=self.static_system_prompt  # ✅ 保持 system 始终在 messages[0]
                )
                continue

    async def _compress_if_needed(self, max_tokens: int = 4000):
        """当历史过长时，压缩最早的几轮对话"""
        total_chars = sum(len(str(m.get("content", ""))) for m in self.conversation_history)

        if total_chars <= max_tokens:
            return

        recent = self.conversation_history[-10:]
        old = self.conversation_history[:-10]

        summary = await self.llm_client.summarize(old)

        # ✅ 修复：摘要作为 system 消息，不破坏 user/assistant 交替
        self.conversation_history = [
            {"role": "system", "content": f"[历史摘要] {summary}"}
        ] + recent

        # ✅ 确保第一条非 system 消息是 user 角色
        if recent and recent[0].get("role") != "user":
            self.conversation_history.insert(1, {
                "role": "user",
                "content": "[继续之前的任务]"
            })
