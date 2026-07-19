import datetime
import threading
import yaml
from pathlib import Path

TOOL_REGISTRY = {}
_PROMPT_CACHE: dict = {}

_SESSION_DOMAINS: dict[str, set] = {}
_SESSION_DOMAINS_LOCK = threading.Lock()

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


def _ensure_tool_registration():
    from core.tool_data_provider import tool_data_provider
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
def load_domain_skills(domain: str, reason: str, session_id: str = None) -> str:
    import json
    
    _ensure_tool_registration()
    
    # 从 tool_data_provider 动态获取所有注册域（避免 tools.yaml 与硬编码不同步）
    from core.tool_data_provider import tool_data_provider as _tdp_inner
    supported_domains = _tdp_inner.get_all_domains()
    
    if domain not in supported_domains:
        return json.dumps({
            "success": False,
            "message": f"不支持的域 '{domain}'。支持的域: {', '.join(sorted(supported_domains))}"
        }, ensure_ascii=False)
    
    try:
        if session_id:
            add_domain_to_session(session_id, domain)
        
        domain_tools = _tdp_inner.get_domain_tools(domain)
        
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
def ask_user_clarification(question: str, options: list = None, **kwargs):
    import json
    import secrets
    token = secrets.token_hex(16)
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
    from core.tool_data_provider import tool_data_provider
    return tool_data_provider.match_domains(user_input)


def match_persona(user_input: str) -> str | None:
    from core.tool_data_provider import tool_data_provider
    return tool_data_provider.match_persona(user_input)


def get_skill_prompt(domain: str) -> str:
    path = PROMPTS_DIR / f"skills/{domain}.md"
    if not path.exists():
        path = AGENT_SKILLS_DIR / f"skills/{domain}.md"
    
    if not path.exists():
        return ""
    
    mtime = path.stat().st_mtime
    cache_key = f"_file_skills_{domain}_{mtime}"
    if cache_key in _PROMPT_CACHE:
        return _PROMPT_CACHE[cache_key]
    
    text = path.read_text(encoding="utf-8").strip()
    _PROMPT_CACHE[cache_key] = text
    return text


def build_dynamic_system_prompt(domains: list[str] = None, persona_mode: str = None) -> str:
    from core.tool_data_provider import tool_data_provider
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
    
    parts = [core_prompt]
    if safety_prompt:
        parts.append(safety_prompt)
    if skill_prompts:
        parts.extend(skill_prompts)
    if tool_list:
        parts.append(tool_list)
    if persona_prompt:
        parts.append(f"## 角色模式：{persona_mode}\n{persona_prompt}")
    parts.append(output_prompt)
    
    return "\n\n---\n\n".join(parts)


def build_tools_param(loaded_domains: list[str] = None) -> list[dict] | None:
    from core.tool_data_provider import tool_data_provider
    return tool_data_provider.build_api_tools_param(loaded_domains)


def _load_prompt(filename: str) -> str:
    path = PROMPTS_DIR / filename
    if not path.exists():
        path = AGENT_SKILLS_DIR / filename
    
    if not path.exists():
        return ""
    
    mtime = path.stat().st_mtime
    cache_key = f"_file_{filename}_{mtime}"
    if cache_key in _PROMPT_CACHE:
        return _PROMPT_CACHE[cache_key]
    
    text = path.read_text(encoding="utf-8").strip()
    _PROMPT_CACHE[cache_key] = text
    return text


_ensure_tool_registration()