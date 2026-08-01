import json
from typing import List, Dict, Any, Optional


def build_system_prompt(mode: Optional[str] = None, tools: Any = None,
                        safety_rules: Optional[str] = None) -> str:
    """组装 system prompt 文本（角色/模式 + 工具列表 + 安全规则），供 react_engine 调用。

    Args:
        mode: 角色/模式描述文本（如 persona_mode 或 "default"），None 时省略该段。
        tools: 工具列表，支持三种形态：
            - str：已渲染好的工具列表文本（原样拼接）
            - list[dict]：OpenAI 兼容 tools 参数（{"type":"function","function":{...}}），自动渲染为文本
            - list[str]：工具名列表，自动渲染为文本
            None 时省略该段。
        safety_rules: 安全规则/基础 prompt 文本。若传入的文本中已包含角色/工具等完整
            内容（如 build_dynamic_system_prompt 的动态结果），直接拼接作为安全与约束段，
            避免丢失动态域/技能注入。None 时省略该段。

    Returns:
        组装完成的 system prompt 文本；各段以 "\n\n---\n\n" 分隔。
    """
    parts: List[str] = []

    if mode:
        parts.append(f"## 角色与模式\n{mode}")

    if tools:
        if isinstance(tools, str):
            parts.append(tools)
        elif isinstance(tools, list):
            lines = ["## 可用工具"]
            for t in tools:
                if isinstance(t, str):
                    lines.append(f"- {t}")
                elif isinstance(t, dict):
                    fn = t.get("function", t)
                    name = fn.get("name", "") if isinstance(fn, dict) else ""
                    desc = fn.get("description", "") if isinstance(fn, dict) else ""
                    if name:
                        lines.append(f"- {name}: {desc}" if desc else f"- {name}")
                    elif t.get("name"):
                        lines.append(f"- {t['name']}: {t.get('desc', '')}" if t.get("desc") else f"- {t['name']}")
            parts.append("\n".join(lines))
        else:
            parts.append(str(tools))

    if safety_rules:
        parts.append(safety_rules)

    return "\n\n---\n\n".join(parts)


class StandardMessageBuilder:
    """
    标准 Multi-Turn 消息构建器。
    严格遵循：System 静态不变，Tools 独立传递，本类只处理 messages 数组。
    """
    @staticmethod
    def build_messages(
        *,
        history: List[Dict[str, Any]],          # 完整的多轮对话历史
        current_user_input: Optional[str] = None, # 当前轮次的新用户输入
        memory_preamble: Optional[str] = None,    # 可选：长期记忆/会话摘要注入
        system_prompt: Optional[str] = None       # 可选：静态 System Prompt，嵌入 messages[0]
    ) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []

        # 0. System Prompt 注入（静态，放在最前，由 AI 做多段合并）
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        # 1. 记忆注入（作为 system 消息，不污染 user/assistant 序列）
        if memory_preamble:
            messages.append({
                "role": "system",
                "content": f"[背景信息 - 仅供上下文参考]\n{memory_preamble}"
            })

        # 2. 追加历史对话（真正的 Multi-Turn 核心）
        # history 结构: [{"role":"user","content":"..."}, {"role":"assistant","tool_calls":[...]}, {"role":"tool",...}]
        messages.extend(history)

        # 3. 追加当前用户输入（如果有）
        if current_user_input is not None:
            messages.append({"role": "user", "content": current_user_input})

        return messages
