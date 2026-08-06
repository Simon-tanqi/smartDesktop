import threading
import yaml
from pathlib import Path
import json

TOOL_REGISTRY = {}
_PROMPT_CACHE: dict = {}

_SESSION_DOMAINS: dict[str, set] = {}
# RLock：支持同一线程在持有锁期间的安全重入，避免 future 因嵌套调用 get_session_domains
# 等场景而触发死锁。所有 with _SESSION_DOMAINS_LOCK: 临界区已极短，RLock 开销可忽略。
_SESSION_DOMAINS_LOCK = threading.RLock()

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
TOOLS_CONFIG_PATH = PROMPTS_DIR / "tools.yaml"

AGENT_SKILLS_DIR = Path(__file__).parent.parent / "agent_skills"
AGENT_TOOLS_YAML = AGENT_SKILLS_DIR / "agent_tools.yaml"


def path_params(*params):
    def decorator(func):
        func._path_params = list(params)
        return func
    return decorator


def register_tool(func=None, *, timeout=60):
    if func is not None:
        TOOL_REGISTRY[func.__name__] = {
            "function": func,
            "description": func.__doc__.strip() if func.__doc__ else "无描述",
            "parameters": {},
            "timeout": timeout,
            "_needs_registration": True
        }
        return func
    
    def decorator(fn):
        TOOL_REGISTRY[fn.__name__] = {
            "function": fn,
            "description": fn.__doc__.strip() if fn.__doc__ else "无描述",
            "parameters": {},
            "timeout": timeout,
            "_needs_registration": True
        }
        return fn
    
    return decorator


from . import file_tools
from . import word_tools
from . import ppt_tools
from . import system_tools
from . import code_tools
from . import search_tools
from . import gui_tools
from . import web_download
try:
    from core.tool_data_provider import tool_data_provider
except ImportError as e:
    raise ImportError(
        f"无法导入 tool_data_provider 模块，请确保 core.tool_data_provider 已正确安装。"
        f"原始错误: {e}"
    ) from e

# Prompt 缓存锁（RLock 防重入，_load_prompt 可能在 _try_inject_skill 内部被嵌套调用）
_PROMPT_CACHE_LOCK = threading.RLock()

def _ensure_tool_registration(): 
    """惰性初始化：确保所有注册工具已通过 tool_data_provider 注册。
    
    TOOL_REGISTRY 仅作临时存储，最终以 tool_data_provider 为准。
    调用此函数后，TOOL_REGISTRY 中的 _needs_registration 标记被清除，
    后续工具查找应优先从 tool_data_provider 获取完整 schema。
    """
    registered_count = 0
    for name, tool_info in TOOL_REGISTRY.items():
        if tool_info.get("_needs_registration"):
            func = tool_info["function"]
            result = tool_data_provider.register_tool(func, timeout=tool_info["timeout"])
            tool_info.update(result)
            tool_info["_needs_registration"] = False
            registered_count += 1


def add_domain_to_session(session_id: str, domain: str):
    with _SESSION_DOMAINS_LOCK:
        if session_id not in _SESSION_DOMAINS:
            _SESSION_DOMAINS[session_id] = set()
        _SESSION_DOMAINS[session_id].add(domain)


def get_session_domains(session_id: str) -> set:
    with _SESSION_DOMAINS_LOCK:
        return _SESSION_DOMAINS.get(session_id, set()).copy()


def clear_session_domains(session_id: str):
    with _SESSION_DOMAINS_LOCK:
        if session_id in _SESSION_DOMAINS:
            del _SESSION_DOMAINS[session_id]


@register_tool(timeout=30)
def load_domain_skills(domain: str, reason: str, session_id: str) -> str:
    """按需加载某个技能域的全部工具。当当前可用工具无法完成用户任务时调用此工具。

    Args:
        domain: 要加载的技能域名，如 "excel_analyze", "file_tools", "system_tools"
        reason: 为什么需要加载此域（简述用户需求）
        session_id: 当前会话ID（必填）

    Returns:
        加载结果，成功后下一轮将获得该域的全部工具
    """
    import json
    
    if not session_id:
        return json.dumps({
            "success": False,
            "message": "session_id 为必填参数，请提供当前会话ID"
        }, ensure_ascii=False)
    
    _ensure_tool_registration()
    
    # 从 tool_data_provider 动态获取所有注册域（避免 tools.yaml 与硬编码不同步）
    supported_domains = tool_data_provider.get_all_domains()
    
    if domain not in supported_domains:
        return json.dumps({
            "success": False,
            "message": f"不支持的域 '{domain}'。支持的域: {', '.join(sorted(supported_domains))}"
        }, ensure_ascii=False)
    
    # 检查是否已加载该域（避免重复加载）
    existing_domains = get_session_domains(session_id)
    if domain in existing_domains:
        return json.dumps({
            "success": True,
            "message": f"ℹ️ '{domain}' 域已在当前会话中加载，无需重复加载。",
            "domain": domain,
            "already_loaded": True
        }, ensure_ascii=False)
    
    try:
        add_domain_to_session(session_id, domain)
        
        domain_tools = tool_data_provider.get_domain_tools(domain)
        
        tools_list = ", ".join(domain_tools[:3])
        if len(domain_tools) > 3:
            tools_list += f" 等{len(domain_tools)}个工具"
        
        return json.dumps({
            "success": True,
            "message": f"✅ 已成功加载 '{domain}' 域技能。\n\n"
                      f"📋 加载原因: {reason}\n"
                      f"🛠️  新增工具: {tools_list if domain_tools else '无特殊工具'}\n\n"
                      f"💡 下一轮对话你将获得该域的全部工具，请继续完成任务。",
            "domain": domain,
            "tools": domain_tools
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({
            "success": False,
            "message": f"加载域失败: {str(e)}"
        }, ensure_ascii=False)


@register_tool(timeout=30)
def tool_load_schema(session_id: str, tool_name: str) -> str:
    """按需加载某个工具的完整参数 schema。"""
    # 从注册表中找到对应工具，返回完整 JSON schema
    tool = _find_tool_in_registry(tool_name)
    if not tool:
        return f"未找到工具: {tool_name}"
    # 统一注册中心视图字段为 params（与 build_api_tools_param 数据源一致），
    # 降级路径（TOOL_REGISTRY）使用 parameters，两者兼容
    params = tool.get("params") or tool.get("parameters") or {}
    return json.dumps(params, ensure_ascii=False, indent=2)
    
def _find_tool_in_registry(tool_name: str) -> dict | None:
    """从 TOOL_REGISTRY 中查找工具，优先从 tool_data_provider 获取完整 schema。"""
    # 确保所有工具已注册后再查找，避免返回不完整的 schema
    _ensure_tool_registration()
    # 优先从 tool_data_provider 获取（与 build_tools_param 数据源一致）
    tool = tool_data_provider.get_tool(tool_name)
    if tool:
        return tool
    # 降级到 TOOL_REGISTRY（仅当 tool_data_provider 中不存在时使用）
    return TOOL_REGISTRY.get(tool_name)

@register_tool(timeout=30)
def ask_user_clarification(question: str, options: list = None, **kwargs):
    """
    向用户发起澄清式提问。仅当用户指令存在多种合理理解、需要用户选择时使用。

    Args:
        question: 向用户展示的问题
        options: 候选选项列表（可选）

    确认 token 由 react_engine Path B 流程管理：首次调用返回 confirmation_required，
    用户确认后引擎带 confirmation_token 重调，本工具校验通过后返回 success。
    """
    import json
    from core.confirmation_manager import confirmation_manager
    confirmation_token = kwargs.get("confirmation_token") or ""

    if confirmation_token:
        # 确认通过后的重调：校验一次性 token（存于统一 confirmation_manager）
        pending = confirmation_manager.pop_confirmation(confirmation_token)
        if not pending:
            return json.dumps({
                "success": False,
                "message": "确认令牌无效或已过期，请重新发起澄清"
            }, ensure_ascii=False)
        return json.dumps({
            "success": True,
            "message": "用户已确认",
            "clarification": pending.get("command", question),
        }, ensure_ascii=False)

    token = confirmation_manager.create_confirmation(question)
    return json.dumps({
        "success": False,
        "message": "需要用户确认",
        "confirmation_required": True,
        "confirmation_token": token,
        "title": "操作确认",
        "command": question,
        "details": {"问题": question, "选项": options if options else []}
    }, ensure_ascii=False)


def match_domains(user_input: str) -> list[str]:
    return tool_data_provider.match_domains(user_input)


def match_persona(user_input: str) -> str | None:
    return tool_data_provider.match_persona(user_input)


def get_skill_prompt(domain: str) -> str:
    path = PROMPTS_DIR / f"skills/{domain}.md"
    if not path.exists():
        path = AGENT_SKILLS_DIR / f"skills/{domain}.md"
    
    if not path.exists():
        return ""
    
    mtime = path.stat().st_mtime
    # 缓存键包含完整路径，避免同名文件冲突
    cache_key = f"_file_skills_{str(path)}_{mtime}"
    with _PROMPT_CACHE_LOCK:
        if cache_key in _PROMPT_CACHE:
            return _PROMPT_CACHE[cache_key]
    
    text = path.read_text(encoding="utf-8").strip()
    with _PROMPT_CACHE_LOCK:
        _PROMPT_CACHE[cache_key] = text
    return text

def remove_domain_from_session(session_id: str, domain: str):
    with _SESSION_DOMAINS_LOCK:
        if session_id in _SESSION_DOMAINS:
            _SESSION_DOMAINS[session_id].discard(domain)

def build_dynamic_system_prompt(domains: list[str] = None, persona_mode: str = None) -> str:
    from logger import get_logger
    core_prompt = _load_prompt("core.md")
    output_prompt = _load_prompt("output.md")
    safety_prompt = _load_prompt("safety.md")
    
    if not core_prompt:
        print("[WARN] core.md 为空或不存在")
    
    tool_list = tool_data_provider.build_system_prompt_tool_list(domains)
    
    persona_prompt = ""
    if persona_mode:
        persona_prompt = _load_prompt(f"modes/{persona_mode}.md")
    
    skill_prompts = []
    if domains:
        for d in domains:
            sp = get_skill_prompt(d)
            if sp:
                skill_prompts.append(sp)
    
    # 静态部分（core / safety / output）放在前缀，动态部分（技能、工具清单、角色模式）
    # 固定在尾部：不同轮次之间前缀保持稳定，利于 LLM API 的前缀缓存命中
    parts = []
    if core_prompt:
        parts.append(core_prompt)
    if safety_prompt:
        parts.append(safety_prompt)
    if output_prompt:
        parts.append(output_prompt)
    if skill_prompts:
        parts.extend(skill_prompts)
    if tool_list:
        parts.append(tool_list)
    if persona_prompt:
        parts.append(f"## 角色模式：{persona_mode}\n{persona_prompt}")
    
    return "\n\n---\n\n".join(parts)


def build_tools_param(loaded_domains: list[str] = None) -> list[dict] | None:
    return tool_data_provider.build_api_tools_param(loaded_domains)


def _load_prompt(filename: str) -> str:
    path = PROMPTS_DIR / filename
    if not path.exists():
        path = AGENT_SKILLS_DIR / filename
    
    if not path.exists():
        return ""
    
    mtime = path.stat().st_mtime
    # 缓存键包含完整路径，避免同名文件冲突
    cache_key = f"_file_{str(path)}_{mtime}"
    with _PROMPT_CACHE_LOCK:
        if cache_key in _PROMPT_CACHE:
            return _PROMPT_CACHE[cache_key]
    
    text = path.read_text(encoding="utf-8").strip()
    with _PROMPT_CACHE_LOCK:
        _PROMPT_CACHE[cache_key] = text
    return text


# ── 应用启动时一次性注册所有工具，消除惰性注册的不确定性 ──
_initialize_all_tools = _ensure_tool_registration
_initialize_all_tools()