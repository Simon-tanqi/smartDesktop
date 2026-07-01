import inspect
import datetime
from pathlib import Path

import yaml

TOOL_REGISTRY = {}
_PROMPT_CACHE: dict = {}

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
TOOLS_CONFIG_PATH = PROMPTS_DIR / "tools.yaml"

# Agent 学习技能目录（与内置技能分离）
AGENT_SKILLS_DIR = Path(__file__).parent.parent / "agent_skills"
AGENT_TOOLS_YAML = AGENT_SKILLS_DIR / "agent_tools.yaml"

# ── YAML 配置加载（带缓存 + mtime 热加载） ──
_tools_config_cache = None
_tools_config_mtime = 0.0

def _load_tools_config():
    """加载 tools.yaml 配置（缓存 + 文件修改时自动刷新）"""
    global _tools_config_cache, _tools_config_mtime
    if not TOOLS_CONFIG_PATH.exists():
        _tools_config_cache = {
            "always_inject": [],
            "tools": {},
            "domains": {},
            "personas": {},
        }
        return _tools_config_cache
    try:
        current_mtime = TOOLS_CONFIG_PATH.stat().st_mtime
    except OSError:
        current_mtime = 0.0
    if _tools_config_cache is not None and abs(current_mtime - _tools_config_mtime) < 0.001:
        return _tools_config_cache
    try:
        with open(TOOLS_CONFIG_PATH, "r", encoding="utf-8") as f:
            _tools_config_cache = yaml.safe_load(f)
        if not _tools_config_cache or "domains" not in _tools_config_cache:
            print(f"[WARN] tools.yaml 加载异常（domains 缺失），使用空配置")
            _tools_config_cache = {"always_inject": [], "tools": {}, "domains": {}, "personas": {}}
    except Exception as e:
        print(f"[ERROR] tools.yaml 加载失败: {e}")
        if _tools_config_cache is None:
            _tools_config_cache = {"always_inject": [], "tools": {}, "domains": {}, "personas": {}}
    _tools_config_mtime = current_mtime
    return _tools_config_cache


# ── Agent 学习技能配置加载（带缓存 + mtime 热加载） ──
_agent_tools_config_cache = None
_agent_tools_config_mtime = 0.0


def _load_agent_tools_config():
    """加载 agent_tools.yaml 配置（缓存 + 文件修改时自动刷新）"""
    global _agent_tools_config_cache, _agent_tools_config_mtime
    
    if not AGENT_TOOLS_YAML.exists():
        _agent_tools_config_cache = {"domains": {}}
        return _agent_tools_config_cache
    
    try:
        current_mtime = AGENT_TOOLS_YAML.stat().st_mtime
    except OSError:
        current_mtime = 0.0
    
    if _agent_tools_config_cache is not None and abs(current_mtime - _agent_tools_config_mtime) < 0.001:
        return _agent_tools_config_cache
    
    try:
        with open(AGENT_TOOLS_YAML, "r", encoding="utf-8") as f:
            _agent_tools_config_cache = yaml.safe_load(f)
        if not _agent_tools_config_cache:
            _agent_tools_config_cache = {"domains": {}}
    except Exception as e:
        print(f"[ERROR] agent_tools.yaml 加载失败: {e}")
        if _agent_tools_config_cache is None:
            _agent_tools_config_cache = {"domains": {}}
    
    _agent_tools_config_mtime = current_mtime
    return _agent_tools_config_cache


def _load_prompt(filename: str) -> str:
    """读取并缓存单个 prompt 模块（mtime 自动失效）"""
    # 优先从 prompts/ 目录加载（内置技能）
    path = PROMPTS_DIR / filename
    if not path.exists():
        # 如果不存在，尝试从 agent_skills/ 目录加载（学习技能）
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


# ── 装饰器 ──

def path_params(*params):
    """装饰器：标记函数的哪些参数是路径"""
    def decorator(func):
        func._path_params = list(params)
        return func
    return decorator


def register_tool(func=None, *, timeout=60):
    """将函数注册为可用工具。"""
    def decorator(fn):
        sig = inspect.signature(fn)
        parameters = {}
        for name, param in sig.parameters.items():
            parameters[name] = {
                "type": str(param.annotation.__name__ if param.annotation != inspect.Parameter.empty else "any"),
                "default": param.default if param.default != inspect.Parameter.empty else "必填"
            }
        path_list = getattr(fn, '_path_params', [])
        TOOL_REGISTRY[fn.__name__] = {
            "function": fn,
            "description": fn.__doc__.strip() if fn.__doc__ else "无描述",
            "parameters": parameters,
            "path_params": path_list,
            "timeout": timeout
        }
        return fn

    if func is not None:
        return decorator(func)
    return decorator


# ── 关键词匹配 ──

def _keyword_hit(text: str, keywords: list[str]) -> bool:
    """任一关键词命中即返回 True"""
    text_lower = text.lower()
    for kw in keywords:
        if kw.lower() in text_lower:
            return True
    return False


def match_domains(user_input: str) -> list[str]:
    """
    本地关键词匹配域（从 tools.yaml 和 agent_tools.yaml 加载），零延迟。
    返回命中域列表 + 始终包含 'general_chat' 兜底。
    """
    domains = set()
    
    # 1. 从内置 skills 匹配
    config = _load_tools_config()
    for domain_name, definition in config.get("domains", {}).items():
        if _keyword_hit(user_input, definition.get("keywords", [])):
            domains.add(domain_name)
    
    # 2. 从学习 skills 匹配
    agent_config = _load_agent_tools_config()
    for domain_name, definition in agent_config.get("domains", {}).items():
        if _keyword_hit(user_input, definition.get("keywords", [])):
            domains.add(domain_name)
    
    domains.add("general_chat")
    return sorted(domains)


def match_persona(user_input: str) -> str | None:
    """检测是否显式触发角色模式（从 tools.yaml 加载）"""
    config = _load_tools_config()
    for persona, keywords in config.get("personas", {}).items():
        if _keyword_hit(user_input, keywords):
            return persona
    return None


# ── Prompt 生成 ──

def _format_params(params: dict) -> str:
    """将 YAML params 格式化为紧凑参数字符串"""
    if not params:
        return ""
    parts = []
    for pname, pinfo in params.items():
        ptype = pinfo.get("type", "str")
        opt = "可选" if pinfo.get("optional") else "必填"
        default = ""
        if "default" in pinfo:
            default = f"={pinfo['default']}"
        parts.append(f"{pname}({ptype},{opt}{default})")
    return "，".join(parts)


def _generate_tool_list(domains: list[str]) -> str:
    """从 tools.yaml 程序化生成紧凑工具描述（单行格式：## name: desc [note]）"""
    config = _load_tools_config()
    always_inject = set(config.get("always_inject", []))
    active = set(always_inject)
    for domain in domains:
        active.update(config.get("domains", {}).get(domain, {}).get("tools", []))

    tools_meta = config.get("tools", {})
    lines = ["# 工具"]
    for name in sorted(active):
        meta = tools_meta.get(name)
        if not meta:
            if name in TOOL_REGISTRY:
                lines.append(f"## {name}: {TOOL_REGISTRY[name]['description']}")
            continue
        desc = meta.get("desc", "")
        note = meta.get("note", "")
        # domain-only 工具：参数合并到 note
        if name not in always_inject:
            params_str = _format_params(meta.get("params", {}))
            if params_str:
                note = f"{note}；参数：{params_str}" if note else f"参数：{params_str}"
        bracket = f" [{note}]" if note else ""
        lines.append(f"## {name}: {desc}{bracket}")
    return "\n".join(lines)


def get_skill_prompt(domain: str) -> str:
    """按需加载指定域的技能 prompt（从 skills/*.md 加载）"""
    return _load_prompt(f"skills/{domain}.md")


def _get_skill_prompts(domains: list[str]) -> str:
    """合并所有命中域的技能规则（从内置和学习技能加载）"""
    parts = []
    for domain in domains:
        # 1. 尝试从内置 skills 加载
        skill = _load_prompt(f"skills/{domain}.md")
        if skill:
            parts.append(skill)
        else:
            # 2. 尝试从学习 skills 加载
            agent_skill = _load_prompt(f"{domain}.md")
            if agent_skill:
                parts.append(agent_skill)
    return "\n\n".join(parts)


def build_dynamic_system_prompt(
    domains: list[str] = None,
    plan_mode: bool = False,
    persona_mode: str = None,
    extra_memory: str = "",
) -> str:
    """
    动态组装 System Prompt（按缓存友好顺序排列）：
    0. core.md / persona — 身份 + 行为准则（静态）
    1. output.md — 输出格式（静态）
    2. 工具列表 — 从 tools.yaml 程序化生成（按域静态）
    3. 域技能 — 从 skills/*.md 加载（按域静态）
    4. extra_memory — 长期记忆/压缩摘要等外部注入内容（动态）
    5. modes/plan.md — 计划模式（按模式静态）
    6. 日期头 — 当前日期时间（动态，末尾）
    """
    if domains is None:
        domains = ["general_chat"]

    parts = []

    # 第 0 层：核心身份（静态，命中缓存）
    if persona_mode:
        persona = _load_prompt(f"modes/{persona_mode}.md")
        parts.append(persona if persona else _load_prompt("core.md"))
    else:
        parts.append(_load_prompt("core.md"))

    # 第 1 层：输出格式（静态，命中缓存）
    parts.append(_load_prompt("output.md"))

    # 第 2 层：工具列表（按域静态，同域命中缓存）
    parts.append(_generate_tool_list(domains))

    # 第 3 层：域技能（按域静态，同域命中缓存）
    skill_parts = _get_skill_prompts(domains)
    if skill_parts:
        parts.append(skill_parts)

    # 第 4 层：长期记忆 + 压缩摘要等外部注入（动态内容，在技能之后、计划模式之前）
    if extra_memory and extra_memory.strip():
        parts.append(extra_memory.strip())

    # 第 5 层：计划模式（按模式静态）
    if plan_mode and not persona_mode:
        parts.append(_load_prompt("modes/plan.md"))

    # 拼接所有静态内容
    static_prompt = "\n\n".join(parts)

    # 最后一层：当前日期时间（唯一每天变动的字段，放在末尾）
    date_header = _build_date_header()

    return static_prompt + "\n\n" + date_header


def _build_date_header() -> str:
    """生成当前日期时间头，不参与缓存。"""
    now = datetime.datetime.now()
    return (
        f"## 当前时间\n"
        f"- 日期: {now.strftime('%Y-%m-%d')}（{['星期一','星期二','星期三','星期四','星期五','星期六','星期日'][now.weekday()]}）\n"
        f"- 时间: {now.strftime('%H:%M:%S')}\n"
    )


# ── 延迟导入子模块 ──
from . import file_tools
from . import code_tools
from . import search_tools
from . import db_tools
from . import registry_tools
from . import web_download
from . import screenshot_tools
