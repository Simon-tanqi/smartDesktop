import os
import json
import inspect
from pathlib import Path

TOOL_REGISTRY = {}
_PROMPT_CACHE: dict = {}

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

def _load_prompt(filename: str) -> str:
    """读取并缓存单个 prompt 模块"""
    cache_key = f"_file_{filename}"
    if cache_key in _PROMPT_CACHE:
        return _PROMPT_CACHE[cache_key]
    path = PROMPTS_DIR / filename
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    _PROMPT_CACHE[cache_key] = text
    return text


# ── 工具 → 意图映射 ──
# 每个工具名对应一个意图列表，表示该工具在哪些意图下暴露
TOOL_INTENT_MAP = {
    "scan_directory":       ["file_manage", "general_chat"],
    "move_file":            ["file_manage"],
    "copy_file":            ["file_manage"],
    "resolve_folder":       ["file_manage"],
    "execute_command":      ["file_manage"],
    "search_files":         ["file_manage", "code_generate"],
    "search_content":       ["code_generate"],
    "get_workspace_path":   ["code_generate"],
    "write_file":           ["file_manage", "code_generate", "general_chat"],
    "read_file":            ["file_manage", "code_generate", "general_chat"],
    "replace_in_file":      ["code_generate"],
    "get_project_summary":  ["code_generate"],
    "remember_user_info":   ["general_chat"],
    "append_to_log":        ["general_chat"],
}

# ── 技能包摘要（先告诉大模型有哪些技能包可用） ──
SKILL_PACK_SUMMARIES = {
    "file_manage":   "文件扫描、复制、移动、删除、终端命令执行等本地文件操作",
    "code_generate": "代码读写、内容搜索、文件替换、项目结构分析",
    "db_query":      "数据库查询（待扩展）",
    "general_chat":  "记忆管理、日志追加、通用问答",
}


# ── 装饰器 ──

def path_params(*params):
    """装饰器：标记函数的哪些参数是路径"""
    def decorator(func):
        func._path_params = list(params)
        return func
    return decorator


def register_tool(func):
    """将函数注册为可用工具"""
    sig = inspect.signature(func)
    parameters = {}
    for name, param in sig.parameters.items():
        parameters[name] = {
            "type": str(param.annotation.__name__ if param.annotation != inspect.Parameter.empty else "any"),
            "default": param.default if param.default != inspect.Parameter.empty else "必填"
        }
    path_list = getattr(func, '_path_params', [])
    TOOL_REGISTRY[func.__name__] = {
        "function": func,
        "description": func.__doc__.strip() if func.__doc__ else "无描述",
        "parameters": parameters,
        "path_params": path_list
    }
    return func


# ── 动态 System Prompt ──

def _get_tool_descriptions(intent_type: str) -> str:
    """
    按意图筛选激活的工具，返回 Claude Code 风格的详细工具描述。
    优先读 prompts/tools/<tool>.md，找不到则退化为 docstring。
    """
    lines = ["# 当前可用工具\n"]
    for name, info in TOOL_REGISTRY.items():
        allowed = TOOL_INTENT_MAP.get(name, ["general_chat"])
        if intent_type not in allowed and "general_chat" not in allowed:
            continue
        # 优先加载详细的工具描述文件
        tool_md = _load_prompt(f"tools/{name}.md")
        if tool_md:
            lines.append(tool_md)
        else:
            # 退化：使用 docstring
            lines.append(f"## {name}\n{info['description']}\n")
    return "\n".join(lines)


def build_dynamic_system_prompt(
    intent_type: str = "general_chat",
    plan_mode: bool = False,
    persona_mode: str = None,
) -> str:
    """
    五层动态组装 System Prompt：
    1. core.md / persona — 身份 + 输出风格（persona 模式替换 core）
    2. safety.md          — 安全约束（始终加载）
    3. tools/*.md         — 工具详细描述（按意图筛选）
    4. skills/*.md        — 技能包提示词（按意图加载，persona 模式跳过）
    5. modes/plan.md      — 计划模式（条件加载）
    """
    cache_key = f"{intent_type}_{plan_mode}_{persona_mode}"
    if cache_key in _PROMPT_CACHE:
        return _PROMPT_CACHE[cache_key]

    parts = []

    # 第 1 层：核心身份 + 输出风格（persona 模式替换）
    if persona_mode:
        persona = _load_prompt(f"modes/{persona_mode}.md")
        parts.append(persona if persona else _load_prompt("core.md"))
    else:
        parts.append(_load_prompt("core.md"))

    # 第 2 层：安全约束
    parts.append(_load_prompt("safety.md"))

    # 第 3 层：工具详细描述（按意图筛选）
    tools_desc = _get_tool_descriptions(intent_type)
    parts.append(tools_desc)

    # 第 4 层：技能包专用规则（persona 模式跳过，避免规则冲突）
    if not persona_mode:
        skill_md = _load_prompt(f"skills/{intent_type}.md")
        if skill_md:
            parts.append(skill_md)
        else:
            from intent_router import SKILL_PROMPTS
            fallback = SKILL_PROMPTS.get(intent_type, "")
            if fallback:
                parts.append(fallback)

    # 第 5 层：计划模式（与 persona 互斥，persona 自带行为约束）
    if plan_mode and not persona_mode:
        parts.append(_load_prompt("modes/plan.md"))

    prompt = "\n\n".join(parts)
    _PROMPT_CACHE[cache_key] = prompt
    return prompt


# ── 延迟导入子模块（不变） ──
from . import file_tools
from . import code_tools
from . import search_tools
from . import chat_tools
from . import db_tools