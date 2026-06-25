import os
import json
import inspect
import datetime
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


# ── 域定义 ──
# 每个域包含：关键词列表 + 额外工具列表
# L1 全局兜底工具 ALWAYS_INJECT 始终注入，不在此重复
DOMAIN_DEFINITIONS = {
    "file_manage": {
        "keywords": [
            "文件", "文件夹", "目录", "复制", "副本", "移动", "删除", "备份",
            "拷贝", "重命名", "改名", "移到", "整理", "扫描", "列出", "查找",
            "创建", "新建", "生成文件", "写入", "保存", "读取文件", "打开文件",
            "路径", "桌面", "下载", "文档", "图片", "视频", "音乐", "压缩",
            "解压", "格式转换", "pdf", "excel", "word", "ppt", "csv",
            "txt", "json", "yaml", "md", "pdf转", "转pdf", "转格式",
            "整个副本", "整一份", "来一份", "弄一份", "搞个备份", "存一份",
        ],
        "tools": ["move_file", "copy_file", "resolve_folder", "search_files"],
    },
    "code_generate": {
        "keywords": [
            "代码", "生成", "写一个", "编程", "脚本", "函数", "类", "程序",
            "项目", "模块", "重构", "实现", "开发", "前端", "后端", "api",
            "接口", "算法", "排序", "爬虫", "正则", "sql",
        ],
        "tools": [
            "get_workspace_path", "search_content", "replace_in_file",
            "search_files", "get_project_summary",
        ],
    },
    "web_browse": {
        "keywords": [
            "搜索", "查询", "天气", "新闻", "网页", "链接", "上网",
            "百度", "google", "互联网", "最新", "实时", "热点",
            "汇率", "股价", "比赛", "比分",
        ],
        "tools": ["web_search", "fetch_webpage", "fetch_multiple", "get_weather", "web_download"],
    },
    "system_config": {
        "keywords": [
            "注册表", "安装软件", "卸载", "系统设置", "壁纸", "主题",
            "配置", "服务", "进程", "启动项", "环境变量", "驱动",
            "任务管理器", "控制面板", "磁盘管理", "设备管理器",
        ],
        "tools": ["modify_registry"],
    },
    "user_memory": {
        "keywords": [
            "记住", "我叫", "我的名字", "记忆", "我的爱好", "我的目标",
            "我的信息", "个人信息", "记一下",
        ],
        "tools": [],
    },
    "db_query": {
        "keywords": ["数据库", "sql", "数据表", "表结构", "建表", "字段", "连接", "mongodb", "mysql"],
        "tools": [],
    },
}

# 角色模式关键词（显式触发，替换 persona）
PERSONA_KEYWORDS = {
    "architect":       ["架构师", "架构分析", "系统设计", "用架构师"],
    "code_review":     ["代码审查", "code review", "审查代码", "审查一下"],
    "product_manager": ["产品经理", "产品规划", "需求分析", "需求文档", "prd"],
    "debugger":        ["调试", "debug", "排查bug", "排查 bug", "帮我调试"],
    "growth":          ["增长", "用户获取", "运营策略", "增长方案"],
    "boss":            ["商业决策", "老板模式", "投入产出", "roi"],
}

# ── L1 全局兜底工具（始终注入，能力黑洞） ──
ALWAYS_INJECT = ["execute_command", "run_python", "write_file", "read_file", "scan_directory", "web_download"]

# ── 工具注册（保留原有映射用于兼容） ──
TOOL_INTENT_MAP = {
    "scan_directory":       ["file_manage", "general_chat"],
    "move_file":            ["file_manage"],
    "copy_file":            ["file_manage"],
    "resolve_folder":       ["file_manage"],
    "execute_command":      ["file_manage", "system_config"],
    "search_files":         ["file_manage", "code_generate"],
    "search_content":       ["code_generate"],
    "get_workspace_path":   ["code_generate"],
    "write_file":           ["file_manage", "code_generate", "general_chat"],
    "read_file":            ["file_manage", "code_generate", "general_chat"],
    "replace_in_file":      ["code_generate"],
    "get_project_summary":  ["code_generate"],
    "web_search":           ["web_browse"],
    "fetch_webpage":        ["web_browse"],
    "fetch_multiple":       ["web_browse"],
    "web_download":         ["web_browse", "file_manage"],
    "get_weather":          ["web_browse", "general_chat"],
    "modify_registry":      ["system_config"],
}


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
    本地关键词匹配域，零延迟。
    返回命中域列表 + 始终包含 'general' 兜底。
    """
    domains = set()
    for domain_name, definition in DOMAIN_DEFINITIONS.items():
        if _keyword_hit(user_input, definition["keywords"]):
            domains.add(domain_name)
    # 始终追加兜底域
    domains.add("general_chat")
    return sorted(domains)


def match_persona(user_input: str) -> str | None:
    """检测是否显式触发角色模式"""
    for persona, keywords in PERSONA_KEYWORDS.items():
        if _keyword_hit(user_input, keywords):
            return persona
    return None


# ── 动态 System Prompt ──

def _get_tool_descriptions(domains: list[str]) -> str:
    """
    按域筛选激活的工具：
    - L1 ALWAYS_INJECT 工具始终注入
    - L2 命中域的工具追加注入（去重）
    """
    active_tools = set(ALWAYS_INJECT)
    for domain in domains:
        if domain in DOMAIN_DEFINITIONS:
            active_tools.update(DOMAIN_DEFINITIONS[domain]["tools"])

    lines = ["# 当前可用工具\n"]
    seen = set()
    for name in sorted(active_tools):
        if name not in TOOL_REGISTRY:
            continue
        if name in seen:
            continue
        seen.add(name)
        info = TOOL_REGISTRY[name]
        tool_md = _load_prompt(f"tools/{name}.md")
        if tool_md:
            lines.append(tool_md)
        else:
            lines.append(f"## {name}\n{info['description']}\n")
    return "\n".join(lines)


def _get_skill_prompts(domains: list[str]) -> str:
    """合并所有命中域的技能规则（保留供外部调用，不在 upfront system prompt 中使用）"""
    parts = []
    from intent_router import SKILL_PROMPTS
    for domain in domains:
        skill_md = _load_prompt(f"skills/{domain}.md")
        if skill_md:
            parts.append(skill_md)
        elif domain in SKILL_PROMPTS and SKILL_PROMPTS[domain]:
            parts.append(SKILL_PROMPTS[domain])
    return "\n\n".join(parts)


def get_skill_prompt(domain: str) -> str:
    """按需加载指定域的技能 prompt，供回路护板在基础工具失败后动态注入。"""
    skill_prompt = _load_prompt(f"skills/{domain}.md")
    if skill_prompt:
        return skill_prompt
    from intent_router import SKILL_PROMPTS
    if domain in SKILL_PROMPTS and SKILL_PROMPTS[domain]:
        return SKILL_PROMPTS[domain]
    return ""


def build_dynamic_system_prompt(
    domains: list[str] = None,
    plan_mode: bool = False,
    persona_mode: str = None,
) -> str:
    """
    五层动态组装 System Prompt：
    1. core.md / persona — 身份 + 输出风格
    2. safety.md          — 安全约束
    3. L1兜底工具 + L2域工具 — 工具详细描述
    4. 域技能规则          — 按命中域合并注入
    5. modes/plan.md      — 计划模式

    domains: 关键词命中的域列表，如 ["file_manage", "general_chat"]
    """
    if domains is None:
        domains = ["general_chat"]

    cache_key = f"{'_'.join(domains)}_{plan_mode}"
    if cache_key in _PROMPT_CACHE:
        # Token优化：日期头不缓存，动态注入；其余部分缓存命中后直接拼接
        return _build_date_header() + "\n\n" + _PROMPT_CACHE[cache_key]

    parts = []

    # 第 0 层：当前日期时间（LLM 时间基准，必须先注入）
    date_header = _build_date_header()
    parts.append(date_header)

    # 第 1 层：核心身份 + 输出风格
    if persona_mode:
        persona = _load_prompt(f"modes/{persona_mode}.md")
        parts.append(persona if persona else _load_prompt("core.md"))
    else:
        parts.append(_load_prompt("core.md"))

    # 第 2 层：安全约束
    parts.append(_load_prompt("safety.md"))

    # 第 3 层：工具（L1 兜底 + L2 域命中）
    tools_desc = _get_tool_descriptions(domains)
    parts.append(tools_desc)

    # 第 4 层：域技能规则 — 不再 upfront 注入，改为回路护板按需动态注入
    # skills 由 app.py ReAct 循环中的 _try_inject_skill 在基础工具失败后注入

    # 第 5 层：计划模式
    if plan_mode and not persona_mode:
        parts.append(_load_prompt("modes/plan.md"))

    # 缓存不含日期头的部分（日期头由 _build_date_header 动态生成）
    prompt_core = "\n\n".join(parts[1:])  # parts[0] 是 date_header，不缓存
    _PROMPT_CACHE[cache_key] = prompt_core
    return date_header + "\n\n" + prompt_core


def _build_date_header() -> str:
    """Token优化辅助：生成当前日期时间头，不参与缓存。"""
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
from . import chat_tools
from . import db_tools
from . import registry_tools
from . import web_download
