import inspect
import datetime
from pathlib import Path

import yaml

TOOL_REGISTRY = {}
_PROMPT_CACHE: dict = {}

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
TOOLS_CONFIG_PATH = PROMPTS_DIR / "tools.yaml"

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


def _load_prompt(filename: str) -> str:
    """读取并缓存单个 prompt 模块（mtime 自动失效）"""
    path = PROMPTS_DIR / filename
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
    本地关键词匹配域（从 tools.yaml 加载），零延迟。
    返回命中域列表 + 始终包含 'general_chat' 兜底。
    """
    config = _load_tools_config()
    domains = set()
    for domain_name, definition in config.get("domains", {}).items():
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
    """从 tools.yaml 程序化生成紧凑工具描述"""
    config = _load_tools_config()
    active = set(config.get("always_inject", []))
    for domain in domains:
        active.update(config.get("domains", {}).get(domain, {}).get("tools", []))

    tools_meta = config.get("tools", {})
    lines = ["# 工具"]
    for name in sorted(active):
        meta = tools_meta.get(name)
        if not meta:
            # 降级：尝试从 TOOL_REGISTRY 取描述
            if name in TOOL_REGISTRY:
                lines.append(f"## {name}: {TOOL_REGISTRY[name]['description']}")
            continue
        desc = meta.get("desc", "")
        note = f" [{meta['note']}]" if meta.get("note") else ""
        lines.append(f"## {name}: {desc}{note}")
        params_str = _format_params(meta.get("params", {}))
        if params_str:
            lines.append(f"  参数：{params_str}")
    return "\n".join(lines)


def get_skill_prompt(domain: str) -> str:
    """按需加载指定域的技能 prompt（从 skills/*.md 加载）"""
    return _load_prompt(f"skills/{domain}.md")


def _get_skill_prompts(domains: list[str]) -> str:
    """合并所有命中域的技能规则"""
    parts = []
    for domain in domains:
        skill = _load_prompt(f"skills/{domain}.md")
        if skill:
            parts.append(skill)
    return "\n\n".join(parts)


def build_dynamic_system_prompt(
    domains: list[str] = None,
    plan_mode: bool = False,
    persona_mode: str = None,
) -> str:
    """
    动态组装 System Prompt：
    0. 日期头（动态，不缓存）
    1. core.md / persona — 身份 + 行为准则
    2. safety.md — 安全约束
    3. output.md — 输出格式
    4. 工具列表 — 从 tools.yaml 程序化生成
    5. 域技能 — 从 skills/*.md 加载
    6. modes/plan.md — 计划模式
    """
    if domains is None:
        domains = ["general_chat"]

    parts = []

    # 第 0 层：当前日期时间
    date_header = _build_date_header()
    parts.append(date_header)

    # 第 1 层：核心身份
    if persona_mode:
        persona = _load_prompt(f"modes/{persona_mode}.md")
        parts.append(persona if persona else _load_prompt("core.md"))
    else:
        parts.append(_load_prompt("core.md"))

    # 第 2 层：输出格式
    parts.append(_load_prompt("output.md"))

    # 第 3 层：工具列表（YAML → 程序化生成）
    parts.append(_generate_tool_list(domains))

    # 第 4 层：域技能
    skill_parts = _get_skill_prompts(domains)
    if skill_parts:
        parts.append(skill_parts)

    # 第 5 层：计划模式
    if plan_mode and not persona_mode:
        parts.append(_load_prompt("modes/plan.md"))

    prompt_core = "\n\n".join(parts[1:])
    return date_header + "\n\n" + prompt_core


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
