import inspect
import datetime
import threading
from pathlib import Path

import yaml

# 导入专门的工具模块
from . import file_tools
from . import word_tools
from . import ppt_tools

TOOL_REGISTRY = {}
_PROMPT_CACHE: dict = {}

# 会话域状态管理 - 跟踪每个会话已加载的域
_SESSION_DOMAINS: dict[str, set] = {}
_SESSION_DOMAINS_LOCK = threading.Lock()

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
TOOLS_CONFIG_PATH = PROMPTS_DIR / "tools.yaml"

# Agent 学习技能目录（与内置技能分离）
AGENT_SKILLS_DIR = Path(__file__).parent.parent / "agent_skills"
AGENT_TOOLS_YAML = AGENT_SKILLS_DIR / "agent_tools.yaml"

# ── YAML 配置加载（带缓存 + mtime 热加载） ──
_tools_config_cache = None
_tools_config_mtime = 0.0

def _load_tools_config():
    """加载 tools.yaml 配置（缓存 + 文件修改时自动刷新）。

    防御性设计：解析失败时绝不缓存空/损坏配置。保留上次有效缓存，
    仅当成功解析出合法配置（含 domains 键）时才更新缓存。
    """
    global _tools_config_cache, _tools_config_mtime
    if not TOOLS_CONFIG_PATH.exists():
        # 文件不存在 → 使用内置最小配置，但【不缓存】以便文件出现后自动加载
        print("[WARN] tools.yaml 不存在，使用内置最小配置（未缓存）")
        return {"always_inject": [], "tools": {}, "domains": {}, "personas": {}}
    try:
        current_mtime = TOOLS_CONFIG_PATH.stat().st_mtime
    except OSError:
        current_mtime = 0.0
    if _tools_config_cache is not None and abs(current_mtime - _tools_config_mtime) < 0.001:
        return _tools_config_cache
    try:
        with open(TOOLS_CONFIG_PATH, "r", encoding="utf-8") as f:
            parsed = yaml.safe_load(f)
        # 合法性校验：必须有 domains 键且 always_inject 为非空列表
        if not parsed or not isinstance(parsed, dict):
            raise ValueError("tools.yaml 内容为空或非字典")
        if "domains" not in parsed:
            raise ValueError("tools.yaml 缺少 'domains' 键")
        if not parsed.get("always_inject"):
            print("[WARN] tools.yaml 的 always_inject 为空，System Prompt 工具列表将不包含任何工具！")
        # 校验通过：更新缓存
        _tools_config_cache = parsed
        _tools_config_mtime = current_mtime
        return _tools_config_cache
    except (yaml.YAMLError, OSError, ValueError) as e:
        print(f"[ERROR] tools.yaml 解析失败: {e}，回退到上次有效缓存")
        if _tools_config_cache is not None:
            # 有上次有效缓存 → 保留它，不污染
            _tools_config_mtime = current_mtime  # 更新 mtime 避免下次仍重复解析
            return _tools_config_cache
        # 首次加载就失败 → 返回最小配置（不缓存，下次仍重试）
        print("[ERROR] 首次加载 tools.yaml 失败且无有效缓存，使用内置最小配置")
        return {"always_inject": [], "tools": {}, "domains": {}, "personas": {}}


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
    except (yaml.YAMLError, OSError) as e:
        print(f"[ERROR] agent_tools.yaml 解析失败: {e}，回退到上次有效缓存")
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


def add_domain_to_session(session_id: str, domain: str):
    """将域添加到指定会话的已加载列表"""
    with _SESSION_DOMAINS_LOCK:
        if session_id not in _SESSION_DOMAINS:
            _SESSION_DOMAINS[session_id] = set()
        _SESSION_DOMAINS[session_id].add(domain)


def get_session_domains(session_id: str) -> set:
    """获取指定会话已加载的域列表"""
    with _SESSION_DOMAINS_LOCK:
        return _SESSION_DOMAINS.get(session_id, set()).copy()


def clear_session_domains(session_id: str):
    """清除指定会话的域状态（用于会话结束时清理）"""
    with _SESSION_DOMAINS_LOCK:
        _SESSION_DOMAINS.pop(session_id, None)


@register_tool(timeout=30)
def load_domain_skills(domain: str, reason: str, session_id: str = None) -> str:
    """加载指定的进阶技能域。当基础工具无法满足用户需求时，调用此工具获取对应域的专业工具。
    
    Args:
        domain: 要加载的技能域名称
        reason: 简要说明加载原因，用于日志记录
        session_id: 会话ID，如果提供则真正加载到会话状态
        
    Returns:
        JSON格式的结果，包含加载状态和说明
    """
    import json
    import threading
    
    # 支持的域列表
    supported_domains = ["code_dev", "web_browse", "system_basic", "system_high_risk", "excel_analyze"]
    
    if domain not in supported_domains:
        return json.dumps({
            "success": False,
            "message": f"不支持的域 '{domain}'。支持的域: {', '.join(supported_domains)}"
        }, ensure_ascii=False)
    
    try:
        # 如果提供了session_id，真正加载到会话状态
        if session_id:
            add_domain_to_session(session_id, domain)
            
        # 获取该域的工具列表用于提示
        config = _load_tools_config()
        domain_info = config.get("domains", {}).get(domain, {})
        domain_tools = domain_info.get("tools", [])
        
        tools_list = ", ".join(domain_tools[:3])  # 显示前3个工具
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
    """向用户提问获取更多信息或确认操作
    
    Args:
        question: 要问用户的问题
        options: 可选答案列表
        **kwargs: 额外参数（用于兼容 LLM 可能传入的 confirmed 等参数）
        
    Returns:
        包含确认请求的字典
    """
    import secrets
    token = secrets.token_hex(16)
    return {
        "success": False,
        "message": "需要用户确认",
        "confirmation_required": True,
        "confirmation_token": token,
        "title": "操作确认",
        "command": question,
        "details": {"问题": question, "选项": options if options else []}
    }


# ── 关键词匹配 ──

def _keyword_hit(text: str, keywords: list[str]) -> bool:
    """任一关键词命中即返回 True"""
    text_lower = text.lower()
    for kw in keywords:
        if kw.lower() in text_lower:
            return True
    return False


def match_domains(user_input: str) -> list[str]:
    """基于关键词匹配用户输入可能需要的工具域。

    扫描 tools.yaml 中 domains 下每个域的 keywords 列表，
    命中即加入候选。这是渐进式披露的第一道门：
    - 命中的域 → 工具列表包含该域工具，技能 prompt 注入
    - 未命中的域 → 用户仍可通过 load_domain_skills 显式加载

    保留空结果兼容：无命中时返回空列表，Agent 走通用路径。
    """
    config = _load_tools_config()
    domains_cfg = config.get("domains", {})
    hits = []
    for domain_name, domain_meta in domains_cfg.items():
        keywords = domain_meta.get("keywords", [])
        if keywords and _keyword_hit(user_input, keywords):
            hits.append(domain_name)
    return hits


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


def _format_schema(meta: dict) -> str:
    """首轮提示词不再展开 schema，返回空字符串。schema 仅用于后端校验。"""
    return ""


def _generate_tool_list(domains: list[str], tier2_tools: list[str] = None) -> str:
    """从 tools.yaml 生成精简工具列表：仅 name + desc + 参数。无 schema、无 note、无约束。

    参数:
        domains: 命中域列表
        tier2_tools: 渐进式加载控制。
            - None (默认): 包含所有工具（向后兼容）
            - [] 或 list: 仅包含 tier=1 工具 + 指定的 tier2 工具
    """
    config = _load_tools_config()
    always_inject = set(config.get("always_inject", []))

    # always_inject 为空时仅告警，不硬编码兜底（YAML 是唯一真相源）
    if not always_inject:
        print("[WARN] always_inject 为空，System Prompt 工具列表将不包含任何工具！请检查 tools.yaml")
    
    active = set(always_inject)
    for domain in domains:
        # 优先从 domains 取，fallback 到 domain_groups（向后兼容）
        domain_cfg = config.get("domains", {}).get(domain, {})
        if not domain_cfg:
            domain_cfg = config.get("domain_groups", {}).get(domain, {})
        domain_tools = domain_cfg.get("tools", [])
        active.update(domain_tools)

    tools_meta = config.get("tools", {})
    lines = ["# 工具"]
    for name in sorted(active):
        # 优先从YAML配置获取工具定义
        meta = tools_meta.get(name)
        if meta:
            # 渐进式加载：如果指定了 tier2_tools 且不在白名单中，跳过 tier2 工具
            if tier2_tools is not None:
                tool_tier = meta.get("tier", 1)
                if tool_tier >= 2 and name not in tier2_tools:
                    continue

            desc = meta.get("desc", "")
            params_str = _format_params(meta.get("params", {}))
            if params_str:
                desc = f"{desc}（参数：{params_str}）"
            lines.append(f"## {name}: {desc}")
        elif name in TOOL_REGISTRY:
            # 如果YAML中没有，但REGISTRY中有，使用REGISTRY中的定义
            lines.append(f"## {name}: {TOOL_REGISTRY[name]['description']}")
        else:
            # 工具既不在YAML也不在REGISTRY中，警告但继续
            print(f"[WARN] 工具 '{name}' 在YAML配置和REGISTRY中都不存在")
            continue
    
    return "\n".join(lines)


def get_skill_prompt(domain: str) -> str:
    """按需加载指定域的技能 prompt（从 skills/*.md 加载）"""
    return _load_prompt(f"skills/{domain}.md")


def get_tier1_tools(domains: list[str]) -> str:
    """生成仅包含 tier=1 工具的精简列表（渐进式加载第一层）。"""
    return _generate_tool_list(domains, tier2_tools=[])


def get_tier2_candidates(domains: list[str], failed_tool_name: str) -> list[str]:
    """根据失败的工具名，从域配置中找出可能需要加载的 tier2 候选工具。

    规则：
    1. 扫描所有命中域的 tier2_tools 列表，返回 triggers 中包含 failed_tool_name 的 tier2 工具。
    2. 若 failed_tool_name 是 execute_command / run_python 等通用 executor，
       且 triggers 精确匹配无结果，则回退加载所有域内 tier2 工具（避免 domain-skill 永不加载）。
    """
    config = _load_tools_config()
    candidates = []
    tools_meta = config.get("tools", {})

    # 通用 executor：当 LLM 使用 execute_command/run_python 失败时，
    # 域技能（如 system_audio）可能未通过 triggers 精确关联，需要 fallback 全量加载
    GENERIC_EXECUTORS = {"execute_command", "run_python"}

    for domain in domains:
        domain_cfg = config.get("domains", {}).get(domain, {})
        tier2_list = domain_cfg.get("tier2_tools", [])
        for tool_name in tier2_list:
            meta = tools_meta.get(tool_name, {})
            triggers = meta.get("triggers", [])
            if failed_tool_name in triggers:
                if tool_name not in candidates:
                    candidates.append(tool_name)

    # Fallback：通用 executor 失败且 triggers 未匹配到任何 tier2 → 加载全部域内 tier2
    if failed_tool_name in GENERIC_EXECUTORS and not candidates:
        for domain in domains:
            domain_cfg = config.get("domains", {}).get(domain, {})
            for tool_name in domain_cfg.get("tier2_tools", []):
                if tool_name not in candidates:
                    candidates.append(tool_name)

    return candidates


def _get_skill_prompts(loaded_domains: list[str]) -> str:
    """合并所有已加载域的技能规则（从内置和学习技能加载）。"""
    parts = []
    for domain in loaded_domains:
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


def _generate_skill_overview(loaded_domains: list[str]) -> str:
    """生成已加载域技能概述（仅名称和一句话描述）。"""
    config = _load_tools_config()
    overview = []
    domain_groups = config.get("domain_groups", {})
    
    for domain in loaded_domains:
        if domain in domain_groups:
            group = domain_groups.get(domain, {})
            tools_in_group = group.get("tools", [])
            desc = group.get("description", domain)
            if tools_in_group:
                tools_meta = config.get("tools", {})
                tool_info = []
                for tool_name in tools_in_group[:5]:
                    meta = tools_meta.get(tool_name, {})
                    tool_desc = meta.get("desc", "")[:30]
                    tool_info.append(f"{tool_name}")
                overview.append(f"【{desc}】可用工具：{', '.join(tool_info)}")
    
    return "\n".join(overview)


def build_dynamic_system_prompt(
    domains: list[str] = None,
    plan_mode: bool = False,
    persona_mode: str = None,
    extra_memory: str = "",
    tier2_tools: list[str] = None,
) -> str:
    """
    构建完全静态的系统提示词（仅包含Agent人设、执行规范、安全边际）：
    0. core.md / persona — 身份 + 行为准则（静态）
    1. output.md — 输出格式（静态）

    注意：工具列表、技能描述、记忆、日期时间等动态信息已移至用户消息前面拼接。
    系统提示词完全静态化，不包含任何动态内容。
    """

    parts = []

    # 第 0 层：核心身份（静态）
    if persona_mode:
        persona = _load_prompt(f"modes/{persona_mode}.md")
        parts.append(persona if persona else _load_prompt("core.md"))
    else:
        parts.append(_load_prompt("core.md"))

    # 第 1 层：输出格式（静态）
    parts.append(_load_prompt("output.md"))

    # 拼接所有静态内容（完全静态，无任何动态信息）
    return "\n\n".join(parts)


def build_user_context(
    domains: list[str] = None,
    tier2_tools: list[str] = None,
    extra_memory: str = "",
    plan_mode: bool = False,
    persona_mode: str = None,
    include_tools: bool = False,
) -> str:
    """
    构建用户消息前缀的动态上下文（拼接到用户消息前面）：
    优化版本：确保在有记忆内容时正确构建上下文

    参数:
        domains: 已废弃，保留兼容
        tier2_tools: 渐进式加载。None=全部工具，[]=仅tier1，[tool_names]=tier1+指定tier2
        extra_memory: 长期记忆 + 压缩摘要等动态内容
        include_tools: 是否包含工具列表（默认 False，工具通过专门的 tools 参数传递）
    """

    parts = []

    # 第 1 层：长期记忆 + 压缩摘要等外部注入（动态内容）
    # 优化：确保记忆内容有合适的标题和格式
    if extra_memory and extra_memory.strip():
        # 如果extra_memory没有明显标题，添加一个
        if not any(extra_memory.strip().startswith(prefix) for prefix in ["#", "【", "##"]):
            formatted_memory = f"## 相关信息\n\n{extra_memory.strip()}"
        else:
            formatted_memory = extra_memory.strip()
        parts.append(formatted_memory)

    # 第 2 层：工具列表（仅当 include_tools=True 时包含，默认通过 tools 参数传递）
    if include_tools:
        tool_list_str = _generate_tool_list(domains, tier2_tools=tier2_tools)
        if domains != ["general_chat"]:
            tool_lines = [l for l in tool_list_str.split("\n") if l.startswith("## ")]
            if not tool_lines:
                print(f"[WARN] build_user_context: 域 {domains} 的工具列表为空！请检查 tools.yaml 是否损坏。")
        parts.append(tool_list_str)

    # 第 3 层：域技能（按已加载的域加载）
    skill_parts = _get_skill_prompts(tier2_tools or [])
    if skill_parts:
        parts.append(skill_parts)

    # 第 4 层：计划模式
    if plan_mode and not persona_mode:
        plan_prompt = _load_prompt("modes/plan.md")
        if plan_prompt:
            parts.append(plan_prompt)

    # 第 5 层：当前日期时间（动态，放在末尾）
    # 优化：确保日期时间总是存在
    date_header = _build_date_header()
    if date_header:
        parts.append(date_header)

    # 优化：如果没有找到任何内容，返回一个最小上下文而不是空字符串
    if not parts:
        return "## 当前时间\n" + _build_date_header()

    return "\n\n".join(parts)


def _build_date_header() -> str:
    """生成当前日期时间头，不参与缓存。"""
    now = datetime.datetime.now()
    return (
        f"## 当前时间\n"
        f"- 日期: {now.strftime('%Y-%m-%d')}（{['星期一','星期二','星期三','星期四','星期五','星期六','星期日'][now.weekday()]}）\n"
        f"- 时间: {now.strftime('%H:%M:%S')}\n"
    )


def build_tools_param(domains: list[str] = None, loaded_domains: list[str] = None,
                      extra_tools: set = None) -> list[dict]:
    """
    将工具配置转换为 OpenAI 格式的 tools 参数。
    
    返回格式（兼容 OpenAI/Claude API）：
    [
        {
            "type": "function",
            "function": {
                "name": "execute_command",
                "description": "执行 PowerShell 命令",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "要执行的命令"}
                    },
                    "required": ["command"]
                }
            }
        }
    ]
    
    参数:
        domains: 已废弃，保留兼容
        loaded_domains: 已加载的进阶域列表（通过 load_domain_skills 加载）
        extra_tools: 额外注入的单工具名称集合（不加载全域，仅追加指定工具）
    """
    if loaded_domains is None:
        loaded_domains = []
    if extra_tools is None:
        extra_tools = set()
    
    config = _load_tools_config()
    always_inject = set(config.get("always_inject", []))
    
    # always_inject 为空时仅告警，不硬编码兜底（YAML 是唯一真相源）
    if not always_inject:
        print("[WARN] always_inject 为空，tools_param 将不包含任何工具！请检查 tools.yaml")
    
    active = set(always_inject)
    
    domain_groups = config.get("domain_groups", {})
    for domain_name in loaded_domains:
        group = domain_groups.get(domain_name, {})
        tools_in_group = group.get("tools", [])
        active.update(tools_in_group)
    
    # 追加单工具注入（不加载全域）
    active.update(extra_tools)
    
    tools_meta = config.get("tools", {})
    tools_list = []
    
    for name in sorted(active):
        # 优先从YAML配置获取工具定义
        meta = tools_meta.get(name)
        if meta:
            # 构建OpenAI格式的工具定义
            params = meta.get("params", {})
            properties = {}
            required = []
            
            PYTHON_TO_JSON_TYPE = {
                "str": "string", "int": "integer", "float": "number", 
                "bool": "boolean", "list": "array", "dict": "object", "None": "null",
            }
            
            for param_name, param_info in params.items():
                py_type = param_info.get("type", "string")
                json_type = PYTHON_TO_JSON_TYPE.get(py_type, py_type)
                prop = {
                    "type": json_type,
                    "description": param_info.get("desc", "")
                }
                if "default" in param_info:
                    prop["default"] = param_info["default"]
                if "enum" in param_info:
                    prop["enum"] = param_info["enum"]
                properties[param_name] = prop
                
                if not param_info.get("optional", False):
                    required.append(param_name)
            
            tools_list.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": meta.get("desc", ""),
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required
                    }
                }
            })
        elif name in TOOL_REGISTRY:
            # 如果YAML中没有，使用REGISTRY中的定义
            registry_info = TOOL_REGISTRY[name]
            # 尝试从函数签名推断参数
            import inspect
            try:
                sig = inspect.signature(registry_info["function"])
                properties = {}
                required = []
                
                for param_name, param in sig.parameters.items():
                    properties[param_name] = {
                        "type": "string",  # 默认为string类型
                        "description": f"参数 {param_name}"
                    }
                    if param.default == inspect.Parameter.empty:
                        required.append(param_name)
                
                tools_list.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": registry_info.get("description", ""),
                        "parameters": {
                            "type": "object", 
                            "properties": properties,
                            "required": required
                        }
                    }
                })
            except Exception as e:
                print(f"[WARN] 无法从函数签名推断参数 for {name}: {e}")
                # 降级到最小定义
                tools_list.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": registry_info.get("description", ""),
                        "parameters": {"type": "object", "properties": {}, "required": []}
                    }
                })
        else:
            print(f"[WARN] 工具 '{name}' 在YAML配置和REGISTRY中都不存在，跳过API参数生成")
        
        params = meta.get("params", {})
        properties = {}
        required = []
        
        PYTHON_TO_JSON_TYPE = {
            "str": "string",
            "int": "integer",
            "float": "number",
            "bool": "boolean",
            "list": "array",
            "dict": "object",
            "None": "null",
        }
        
        for param_name, param_info in params.items():
            py_type = param_info.get("type", "string")
            json_type = PYTHON_TO_JSON_TYPE.get(py_type, py_type)
            prop = {
                "type": json_type,
                "description": param_info.get("desc", "")
            }
            if "default" in param_info:
                prop["default"] = param_info["default"]
            if "enum" in param_info:
                prop["enum"] = param_info["enum"]
            properties[param_name] = prop
            
            if not param_info.get("optional", False):
                required.append(param_name)
        
        tools_list.append({
            "type": "function",
            "function": {
                "name": name,
                "description": meta.get("desc", ""),
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required
                }
            }
        })
    
    return tools_list


def get_domain_group_config(domain_name: str) -> dict:
    """
    获取指定域分组的配置。
    
    返回:
        dict: 包含 tools 和 requires_confirmation 字段
    """
    config = _load_tools_config()
    return config.get("domain_groups", {}).get(domain_name, {})


def is_high_risk_domain(domain_name: str) -> bool:
    """
    判断域是否为高危域（需要用户确认）。
    """
    group_config = get_domain_group_config(domain_name)
    return group_config.get("requires_confirmation", False)


# ── 延迟导入子模块 ──
# 在 __init__.py 内部用 from tools import xxx 会循环导入，
# 改用 importlib 延迟加载，触发 @register_tool 注册到 TOOL_REGISTRY
import importlib as _importlib

for _mod_name in [
    "file_tools",
    "word_tools",
    "ppt_tools",
    "system_tools", 
    "code_tools",
    "search_tools",
    "db_tools",
    "web_download",
    "screenshot_tools",
    "gui_tools",  # 添加GUI工具模块
]:
    try:
        _importlib.import_module(f"tools.{_mod_name}")
    except Exception as _e:
        print(f"[WARN] tools.{_mod_name} 导入失败: {_e}")

del _importlib, _mod_name


# ── 手动注册Office工具到工具注册中心 --
# 由于之前移除了装饰器避免循环导入，这里手动注册Office工具
def _register_office_tools():
    """手动注册Office相关工具"""
    try:
        # 导入Office工具模块
        from . import file_tools
        from . import word_tools
        from . import ppt_tools
        
        # 注册Excel工具
        TOOL_REGISTRY.update({
            'excel_pivot': file_tools.excel_pivot,
            'excel_format': file_tools.excel_format,
            'excel_chart_advanced': file_tools.excel_chart_advanced,
            'excel_batch_process': file_tools.excel_batch_process,
            
            # 注册Word工具
            'create_word_document': word_tools.create_word_document,
            'add_word_content': word_tools.add_word_content,
            'extract_word_text': word_tools.extract_word_text,
            'search_replace_word': word_tools.search_replace_word,
            'batch_word_process': word_tools.batch_word_process,
            
            # 注册PPT工具
            'create_presentation': ppt_tools.create_presentation,
            'add_slide_content': ppt_tools.add_slide_content,
            'extract_presentation_info': ppt_tools.extract_presentation_info,
            'export_presentation_text': ppt_tools.export_presentation_text,
            'batch_ppt_process': ppt_tools.batch_ppt_process,
        })
        
        print("Office工具注册完成")
        print(f"  - Excel工具: 4个")
        print(f"  - Word工具: 5个")
        print(f"  - PPT工具: 5个")
        
    except ImportError as e:
        print(f"Office工具注册失败 - 导入失败: {e}")
    except Exception as e:
        print(f"Office工具注册失败: {e}")

# 立即执行注册
_register_office_tools()
