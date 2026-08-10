from __future__ import annotations
import json
import time
import yaml
import inspect
import uuid
import os
import re
import datetime
from pathlib import Path
from typing import Dict, List, Any, Set, Optional, Callable
import threading

from logger import get_logger, get_tool_execution_logger
from .streaming import _safe_json_loads, strip_temp_system_messages
from utils.error_mapper import translate_error
from memory import append_session_message
from config.settings import PROGRESS_EVALUATOR_ENABLED
from core.progress_controller import Verdict
from concurrent.futures import ThreadPoolExecutor


class ToolDataProvider:
    """统一的工具数据提供器，作为唯一真相源。
    
    核心职责：
    1. 加载并缓存工具配置（YAML + 运行时注册）
    2. 提供统一的工具信息查询接口
    3. 自动同步系统提示词、API参数、运行时注册三者的数据
    
    设计原则：
    - 单一数据源：所有工具信息从这里获取
    - 自动同步：新增工具只需修改一处
    - 热加载：YAML修改后自动刷新
    """
    
    def __init__(self):
        self._prompts_dir = Path(__file__).parent.parent / "prompts"
        self._tools_config_path = self._prompts_dir / "tools.yaml"
        self._agent_skills_dir = Path(__file__).parent.parent / "agent_skills"
        self._agent_tools_yaml = self._agent_skills_dir / "agent_tools.yaml"
        
        self._config_cache = None
        self._config_mtime = 0.0
        self._agent_config_cache = None
        self._agent_config_mtime = 0.0
        
        self._runtime_tools: Dict[str, Dict[str, Any]] = {}
        self._runtime_lock = threading.Lock()
        
        self._merged_tools: Dict[str, Dict[str, Any]] = {}
        self._merged_domains: Dict[str, Dict[str, Any]] = {}
        self._merged_personas: Dict[str, List[str]] = {}
        self._merged_always_inject: List[str] = []
        
        self._sync_required = True
        self._merged_lock = threading.Lock()
    
    def _load_yaml_config(self) -> Dict[str, Any]:
        if not self._tools_config_path.exists():
            return {"always_inject": [], "tools": {}, "domains": {}, "personas": {}}
        
        try:
            current_mtime = self._tools_config_path.stat().st_mtime
        except OSError:
            current_mtime = 0.0
        
        if self._config_cache is not None and abs(current_mtime - self._config_mtime) < 0.001:
            return self._config_cache
        
        try:
            with open(self._tools_config_path, "r", encoding="utf-8") as f:
                parsed = yaml.safe_load(f)
            
            if not parsed or not isinstance(parsed, dict):
                raise ValueError("tools.yaml 内容为空或非字典")
            
            self._config_cache = parsed
            self._config_mtime = current_mtime
            self._sync_required = True
            return self._config_cache
        except (yaml.YAMLError, OSError, ValueError) as e:
            if self._config_cache is not None:
                return self._config_cache
            return {"always_inject": [], "tools": {}, "domains": {}, "personas": {}}
    
    def _load_agent_config(self) -> Dict[str, Any]:
        if not self._agent_tools_yaml.exists():
            self._agent_config_cache = {"domains": {}}
            return self._agent_config_cache
        
        try:
            current_mtime = self._agent_tools_yaml.stat().st_mtime
        except OSError:
            current_mtime = 0.0
        
        if self._agent_config_cache is not None and abs(current_mtime - self._agent_config_mtime) < 0.001:
            return self._agent_config_cache
        
        try:
            with open(self._agent_tools_yaml, "r", encoding="utf-8") as f:
                self._agent_config_cache = yaml.safe_load(f)
            if not self._agent_config_cache:
                self._agent_config_cache = {"domains": {}}
            
            self._agent_config_mtime = current_mtime
            self._sync_required = True
            return self._agent_config_cache
        except (yaml.YAMLError, OSError) as e:
            if self._agent_config_cache is None:
                self._agent_config_cache = {"domains": {}}
            return self._agent_config_cache
    
    def _sync_all(self):
        """同步 YAML配置 + 运行时注册工具，生成统一视图"""
        # 先检测文件变更，再决定是否跳过。否则 _sync_required=False 时
        # 永远不会调用 _load_yaml_config，mtime 变化永远无法被检测到。
        yaml_config = self._load_yaml_config()
        agent_config = self._load_agent_config()
        
        if not self._sync_required:
            return
        
        merged_tools = {}
        merged_domains = {}
        merged_personas = {}
        merged_always_inject = []
        
        merged_tools.update(yaml_config.get("tools", {}))
        
        with self._runtime_lock:
            for name, tool_info in self._runtime_tools.items():
                if name not in merged_tools:
                    merged_tools[name] = {
                        "desc": tool_info.get("description", ""),
                        "params": tool_info.get("parameters", {}),
                        "tier": 2,
                        "tags": ["runtime"],
                        "function": tool_info.get("function"),
                        "timeout": tool_info.get("timeout", 60)
                    }
                else:
                    merged_tools[name]["function"] = tool_info.get("function")
                    merged_tools[name]["timeout"] = tool_info.get("timeout", 60)
                    merged_tools[name]["desc"] = tool_info.get("description", merged_tools[name].get("desc", ""))
                    # 参数级合并：YAML 约束优先于运行时自省
                    rt_params = tool_info.get("parameters", {})
                    yml_params = merged_tools[name].get("params", {})
                    for pname, pinfo in rt_params.items():
                        yml_pinfo = yml_params.get(pname, {})
                        # desc：运行时为空时取 YAML
                        if not pinfo.get("desc") and yml_pinfo.get("desc"):
                            pinfo["desc"] = yml_pinfo["desc"]
                        # optional：YAML 无 optional+无 default → 必填，覆盖运行时
                        if ("optional" not in yml_pinfo and "default" not in yml_pinfo
                                and pinfo.get("optional")):
                            pinfo["optional"] = False
                    merged_tools[name]["params"] = rt_params
        
        merged_domains.update(yaml_config.get("domains", {}))
        merged_domains.update(agent_config.get("domains", {}))
        
        merged_personas.update(yaml_config.get("personas", {}))
        
        merged_always_inject = list(yaml_config.get("always_inject", []))
        
        with self._merged_lock:
            self._merged_tools = merged_tools
            self._merged_domains = merged_domains
            self._merged_personas = merged_personas
            self._merged_always_inject = merged_always_inject
            self._sync_required = False
    
    def _get_type_name(self, annotation) -> str:
        """安全获取类型名称，兼容 typing 模块的泛型/联合类型"""
        if annotation == inspect.Parameter.empty:
            return "any"
        try:
            if hasattr(annotation, "__name__"):
                return annotation.__name__
            elif hasattr(annotation, "__origin__"):
                origin_name = annotation.__origin__.__name__ if hasattr(annotation.__origin__, "__name__") else str(annotation.__origin__)
                args = []
                for arg in annotation.__args__:
                    args.append(self._get_type_name(arg))
                if args:
                    return f"{origin_name}[{','.join(args)}]"
                return origin_name
            elif hasattr(annotation, "__args__"):
                args = [self._get_type_name(arg) for arg in annotation.__args__]
                return f"Union[{','.join(args)}]"
            else:
                return str(annotation).replace("typing.", "").replace("builtins.", "")
        except Exception:
            return "any"
    
    # 内部确认/授权标志：仅在引擎重调工具时由 react_engine 经 kwargs 注入，
    # 严禁出现在 LLM 可见的 JSON Schema / 系统提示词参数摘要中，
    # 防止 LLM 直接传 confirmed=True 等参数绕过安全检查。
    _INTERNAL_PARAM_NAMES = frozenset({
        "confirmed", "confirmation_token", "challenge_token", "challenge_response",
        "domain_confirmed", "_skip_validation", "prevalidated_uninstall_paths",
    })

    def _is_internal_param(self, name: str) -> bool:
        return name in self._INTERNAL_PARAM_NAMES

    def register_tool(self, func: Callable, *, timeout: int = 60) -> Dict[str, Any]:
        """注册运行时工具，自动同步到统一视图"""
        # 延迟导入 schema_override（避免 tools.ppt_tools ↔ tool_data_provider 循环导入）
        # 捕获 NameError（ppt_tools 正在加载中模块不完整）和 AttributeError（函数不存在）
        try:
            from tools.ppt_tools import get_schema_override
            override_schema = get_schema_override(func.__name__)
        except (NameError, AttributeError, ImportError, Exception):
            override_schema = None

        sig = inspect.signature(func)
        parameters = {}
        for name, param in sig.parameters.items():
            # 内部确认/授权参数不出现在 schema 中（问题 #4：防 LLM 绕过）
            # 变长参数（**kwargs / *args）为引擎内部通道，同样不暴露给 LLM
            if self._is_internal_param(name) or param.kind in (
                    inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL):
                continue
            parameters[name] = {
                "type": self._get_type_name(param.annotation),
                "desc": "",
                "optional": param.default != inspect.Parameter.empty,
                "default": param.default if param.default != inspect.Parameter.empty else None
            }

        # 优先使用覆盖 schema（有完整 items 结构定义）
        if override_schema is not None:
            # 兼容 OpenAI function-calling 风格 schema（{type:"object", properties:{...}}）
            # 自动展平为扁平格式 {param_name: {type, desc, optional, default}}
            # 这样 _sync_all 的参数级合并逻辑（line 148 的 pinfo.get('desc')）就不会因
            # 'str' object has no attribute 'get' 而崩溃
            if isinstance(override_schema, dict) and override_schema.get("type") == "object" and "properties" in override_schema:
                required_set = set(override_schema.get("required") or [])
                flat = {}
                for pname, pinfo in override_schema["properties"].items():
                    if not isinstance(pinfo, dict):
                        # 防御：properties 的项也必须是 dict
                        continue
                    # enum 补到 desc 里（提示 LLM）
                    desc = pinfo.get("description", "") or ""
                    if "enum" in pinfo and pinfo["enum"]:
                        desc = (desc + " (可选: " + ", ".join(str(x) for x in pinfo["enum"]) + ")").strip()
                    flat[pname] = {
                        "type": pinfo.get("type", "string"),
                        "desc": desc,
                        "optional": (pname not in required_set) or ("default" in pinfo),
                        "default": pinfo.get("default"),
                    }
                parameters = flat
            else:
                parameters = override_schema

        path_list = getattr(func, '_path_params', [])
        tool_info = {
            "function": func,
            "description": func.__doc__.strip() if func.__doc__ else "无描述",
            "parameters": parameters,
            "path_params": path_list,
            "timeout": timeout
        }
        
        with self._runtime_lock:
            self._runtime_tools[func.__name__] = tool_info
        
        self._sync_required = True
        return tool_info
    
    def get_tool(self, name: str) -> Optional[Dict[str, Any]]:
        """获取工具信息（统一视图）"""
        self._sync_all()
        with self._merged_lock:
            tool = self._merged_tools.get(name)
            if tool and tool.get("deprecated"):
                return None
            return tool
    
    def get_all_tools(self) -> Dict[str, Dict[str, Any]]:
        """获取所有工具（统一视图）"""
        self._sync_all()
        with self._merged_lock:
            return self._merged_tools.copy()
    
    def get_domain_tools(self, domain: str) -> List[str]:
        """获取指定域的工具列表"""
        self._sync_all()
        with self._merged_lock:
            domain_info = self._merged_domains.get(domain, {})
            return domain_info.get("tools", [])
    
    def get_domain_keywords(self, domain: str) -> List[str]:
        """获取指定域的关键词"""
        self._sync_all()
        with self._merged_lock:
            domain_info = self._merged_domains.get(domain, {})
            return domain_info.get("keywords", [])
    
    def match_domains(self, user_input: str) -> List[str]:
        """基于关键词匹配用户输入可能需要的工具域（含伴生域）"""
        self._sync_all()
        with self._merged_lock:
            hits = []
            for domain_name, domain_meta in self._merged_domains.items():
                keywords = domain_meta.get("keywords", [])
                if keywords and self._keyword_hit(user_input, keywords):
                    hits.append(domain_name)
            # 伴生域：高风险系统操作时同时启用 GUI 自动化域
            if "system_high_risk" in hits and "gui_automation" in self._merged_domains:
                if "gui_automation" not in hits:
                    hits.append("gui_automation")
            return hits
    
    def match_persona(self, user_input: str) -> Optional[str]:
        """检测是否显式触发角色模式"""
        self._sync_all()
        with self._merged_lock:
            for persona, keywords in self._merged_personas.items():
                if self._keyword_hit(user_input, keywords):
                    return persona
            return None
    
    def get_all_domains(self) -> Set[str]:
        """获取所有已注册域的集合"""
        self._sync_all()
        with self._merged_lock:
            return set(self._merged_domains.keys())
    
    def get_always_inject_tools(self) -> List[str]:
        """获取始终注入的工具列表"""
        self._sync_all()
        with self._merged_lock:
            return self._merged_always_inject.copy()
    
    def _resolve_active_tool_names(self, domains: List[str] = None) -> set:
        """单一真相源：返回当前激活的工具名集合（always_inject + 指定域工具）。
        
        所有工具列表构建（系统提示词、OpenAI tools 参数）和工具可用性检查
        均通过此方法获得统一视图，杜绝"提示词有但执行器无"的不一致。
        """
        self._sync_all()
        with self._merged_lock:
            active = set(self._merged_always_inject)
            if domains:
                for domain in domains:
                    domain_info = self._merged_domains.get(domain, {})
                    active.update(domain_info.get("tools", []))
            return active

    def build_system_prompt_tool_list(self, domains: List[str] = None) -> str:
        """构建系统提示词中的工具列表部分"""
        active = self._resolve_active_tool_names(domains)
        
        with self._merged_lock:
            lines = ["# 工具"]
            for name in sorted(active):
                tool_info = self._merged_tools.get(name)
                if tool_info:
                    if tool_info.get("deprecated"):
                        continue
                    desc = tool_info.get("desc", "")
                    params = tool_info.get("params", {})
                    params_str = self._format_params(params)
                    if params_str:
                        desc = f"{desc}（参数：{params_str}）"
                    lines.append(f"## {name}: {desc}")
            
            return "\n".join(lines)
    
    def build_api_tools_param(self, domains: List[str] = None) -> List[Dict[str, Any]]:
        """构建 OpenAI API 的 tools 参数"""
        active = self._resolve_active_tool_names(domains)
        
        with self._merged_lock:
            param_tools = []
            for name in sorted(active):
                tool_info = self._merged_tools.get(name)
                if tool_info:
                    if tool_info.get("deprecated"):
                        continue
                    tool_param = {
                        "type": "function",
                        "function": {
                            "name": name,
                            "description": tool_info.get("desc", "") or tool_info.get("description", ""),
                            "parameters": self._build_parameter_schema(tool_info.get("params", {}))
                        }
                    }
                    param_tools.append(tool_param)
            
            return param_tools if param_tools else None
    
    def _keyword_hit(self, text: str, keywords: List[str]) -> bool:
        text_lower = text.lower()
        for kw in keywords:
            if kw.lower() in text_lower:
                return True
        return False
    
    def _format_params(self, params: Dict[str, Any]) -> str:
        if not params:
            return ""
        parts = []
        for pname, pinfo in params.items():
            # 双保险：内部参数绝不出现在系统提示词参数摘要中
            if self._is_internal_param(pname):
                continue
            ptype = pinfo.get("type", "str")
            opt = "可选" if pinfo.get("optional") else "必填"
            default = ""
            if "default" in pinfo and pinfo["default"] is not None:
                default = f"={pinfo['default']}"
            parts.append(f"{pname}({ptype},{opt}{default})")
        return "，".join(parts)
    
    PYTHON_TO_JSON_TYPE = {
        "str": "string",
        "int": "integer",
        "float": "number",
        "bool": "boolean",
        "list": "array",
        "dict": "object",
        "any": "string",
        "NoneType": "null"
    }
    
    def _parse_complex_type(self, py_type: str) -> Dict[str, Any]:
        """解析复杂类型字符串，返回 JSON Schema 兼容的类型定义"""
        py_type = py_type.strip()
        
        if py_type.startswith("Optional[") and py_type.endswith("]"):
            inner_type = py_type[9:-1].strip()
            inner_schema = self._parse_complex_type(inner_type)
            return {"anyOf": [inner_schema, {"type": "null"}]}
        
        if py_type.startswith("Union[") and py_type.endswith("]"):
            types = py_type[6:-1].split(",")
            type_schemas = []
            for t in types:
                type_schemas.append(self._parse_complex_type(t.strip()))
            return {"anyOf": type_schemas}
        
        if "[" in py_type and "]" in py_type:
            bracket_start = py_type.index("[")
            bracket_end = py_type.rindex("]")
            base_type = py_type[:bracket_start].strip()
            inner_type = py_type[bracket_start + 1:bracket_end].strip()
            
            json_type = self.PYTHON_TO_JSON_TYPE.get(base_type, "string")
            if json_type == "array":
                item_schema = self._parse_complex_type(inner_type)
                return {"type": "array", "items": item_schema}
            elif json_type == "object":
                return {"type": "object"}
            else:
                return {"type": json_type}
        
        return {"type": self.PYTHON_TO_JSON_TYPE.get(py_type, "string")}
    
    def _build_parameter_schema(self, params: Dict[str, Any]) -> Dict[str, Any]:
        schema = {"type": "object", "properties": {}, "required": []}
        for pname, pinfo in params.items():
            # 双保险：即使上游 YAML/运行时合并遗漏，内部参数也不进 LLM schema
            if self._is_internal_param(pname):
                continue
            py_type = pinfo.get("type", "string")
            prop = self._parse_complex_type(py_type)
            prop["description"] = pinfo.get("desc", "")
            if "default" in pinfo and pinfo["default"] is not None:
                prop["default"] = pinfo["default"]
            schema["properties"][pname] = prop
            if not pinfo.get("optional"):
                schema["required"].append(pname)
        return schema


tool_data_provider = ToolDataProvider()


# ── 工具循环检测（从 react_engine.py 迁入，独立类只依赖 json/hashlib/collections） ──


class SearchCircuitBreaker:
    """搜索熔断器：连续 N 次失败/低质量后，自动降级跳过搜索。"""
    def __init__(self, failure_threshold: int = 3, cooldown_seconds: int = 60):
        self._consecutive_failures = 0
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._last_failure_time = 0.0
        self._state = "closed"  # closed / open / half_open
        self._lock = threading.Lock()

    def record_result(self, success: bool, relevance_passed: bool):
        with self._lock:
            if success and relevance_passed:
                self._consecutive_failures = 0
                self._state = "closed"
            else:
                self._consecutive_failures += 1
                self._last_failure_time = time.time()
                if self._consecutive_failures >= self._threshold:
                    self._state = "open"
                    get_logger().warning(
                        f"搜索熔断器开启: 连续 {self._consecutive_failures} 次失败"
                    )

    def should_skip_search(self) -> bool:
        with self._lock:
            if self._state == "closed":
                return False
            if self._state == "open":
                if time.time() - self._last_failure_time > self._cooldown:
                    self._state = "half_open"
                    return False
                return True
            return False  # half_open

    def get_degradation_message(self) -> str:
        return (
            "搜索服务连续失败，已自动降级。"
            "以下内容基于模型知识生成，未经实时搜索验证。"
            "建议用户自行核实关键数据。"
        )


class ToolLoopError(Exception):
    """工具循环检测异常，触发后终止当前任务。"""
    pass


class ToolLoopPolicy:
    """工具循环检测策略配置。"""

    def __init__(self, exact_repeat_limit: int = 2,
                 same_tool_soft_limit: int = 5,
                 same_tool_hard_limit: int = 10,
                 global_max_turns: int = 15,
                 exempt_tools: set = None):
        self.exact_repeat_limit = exact_repeat_limit          # 同工具+同参数连续重复次数 → 硬终止
        self.same_tool_soft_limit = same_tool_soft_limit      # 同工具累计次数 → 软警告（不终止）
        self.same_tool_hard_limit = same_tool_hard_limit      # 同工具累计次数 → 硬终止
        self.global_max_turns = global_max_turns              # 全局最大步数兜底
        self.exempt_tools = exempt_tools or {
            "web_search", "file_search", "database_query",
            "api_request", "code_execute", "shell_command",
        }


LOOP_POLICY = ToolLoopPolicy()


def compute_call_signature(tool_name: str, arguments: dict) -> str:
    """计算工具调用签名：tool_name + 排序后的 arguments 的 MD5 哈希。
    只有 tool_name 和 arguments 完全相同，签名才相同。
    """
    import hashlib
    normalized = json.dumps(
        {"tool": tool_name, "args": arguments},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.md5(normalized.encode()).hexdigest()


class ToolLoopDetector:
    """三层递进式工具循环检测器。

    Layer 1: 精确签名去重 → 同工具+同参数连续重复 → 硬终止
    Layer 2: 同名工具频率 → 同工具不同参数累计过多 → 软警告/硬终止
    Layer 3: 全局步数兜底 → 由外层 MAX_TURNS 控制
    """

    def __init__(self, policy: ToolLoopPolicy = None):
        self.policy = policy or LOOP_POLICY
        # 完整调用历史: [(signature, tool_name, step_num), ...]
        self._call_history = []
        # 最近一次调用的签名（用于连续重复检测）
        self._last_signature = None
        self._consecutive_exact_repeat = 0

    def check(self, tool_name: str, arguments: dict, step_num: int) -> None:
        """在每次工具调用前检查。检测到真循环时抛出 ToolLoopError。"""
        signature = compute_call_signature(tool_name, arguments)

        # ── Layer 1: 精确签名连续重复 ──
        if signature == self._last_signature:
            self._consecutive_exact_repeat += 1
        else:
            self._consecutive_exact_repeat = 1
        self._last_signature = signature

        if self._consecutive_exact_repeat > self.policy.exact_repeat_limit:
            raise ToolLoopError(
                f"工具 '{tool_name}' 以完全相同的参数连续调用 "
                f"{self._consecutive_exact_repeat} 次，判定为无效循环并终止。"
                f"\n参数: {json.dumps(arguments, ensure_ascii=False)}"
            )

        # ── Layer 2: 同名工具频率（豁免工具跳过） ──
        if tool_name not in self.policy.exempt_tools:
            same_tool_count = sum(
                1 for _, name, _ in self._call_history if name == tool_name
            ) + 1  # +1 是本次

            if same_tool_count > self.policy.same_tool_hard_limit:
                raise ToolLoopError(
                    f"工具 '{tool_name}' 累计调用 {same_tool_count} 次"
                    f"超过硬限制 ({self.policy.same_tool_hard_limit})，任务终止。"
                )

        # ── 记录历史 ──
        self._call_history.append((signature, tool_name, step_num))

    def get_warning_hint(self) -> str | None:
        """返回软警告提示，注入到下一轮 LLM 调用以引导模型收敛。"""
        if not self._call_history:
            return None

        from collections import Counter
        recent = self._call_history[-5:]
        counter = Counter(name for _, name, _ in recent)
        most_common_name, most_common_count = counter.most_common(1)[0]

        if most_common_count >= 4 and most_common_name not in self.policy.exempt_tools:
            return (
                f"[系统提示] 你已连续多次调用 '{most_common_name}'，"
                f"请评估是否已收集到足够信息，考虑进入总结/执行阶段。"
            )
        return None

    def reset(self):
        """重置状态（新任务时调用）。"""
        self._call_history.clear()
        self._last_signature = None
        self._consecutive_exact_repeat = 0


# ── 工具执行管线（从 react_engine.py 迁入，封装工具执行校验/确认/摘要） ──


class ToolExecutionPipeline:
    """工具执行管线：封装工具结果校验、风险分级、确认等待、摘要构建。

    ReActEngine 持有 self.tool_pipeline = ToolExecutionPipeline(...)，
    将原 ReActEngine 的6个工具执行相关方法收敛至此。
    """

    def __init__(self, search_breaker: SearchCircuitBreaker,
                 risk_levels: dict, write_tools: set,
                 max_result_chars: int):
        self._search_breaker = search_breaker
        self._risk_levels = risk_levels
        self._write_tools = write_tools
        self._max_result_chars = max_result_chars
        self._last_step_summary = None

    def build_futile_final(self, tool, args, reason, result):
        """纯模板，绝不再调 LLM。诚实总结+建议。"""
        try:
            args_preview = json.dumps(args, ensure_ascii=False, default=str)[:300]
        except Exception:
            args_preview = str(args)[:300]
        return (f"[系统收敛] 检测到针对工具 `{tool}` 的循环已无进展，已自动停止以避免空转。\n"
                f"- 判定原因：{reason}\n"
                f"- 最近参数：{args_preview}\n"
                f"- 最近结果：{str(result)[:300]}\n"
                f"建议：更换思路/参数，或拆分为更小步骤后重试。")

    def check_tool_risk_level(self, tool_name: str, arguments: dict) -> tuple:
        """工具执行前安全分级检查。

        根据 risk_levels 映射表检查工具的风险等级：
        - P0/P1：返回 (确认消息, 挑战短语, 风险等级) — 调用方应弹出确认窗口
        - P2/P3：返回 (None, None, None) — 直接执行

        Returns:
            (msg, challenge_phrase, risk_level):
            - msg: 描述性确认消息
            - challenge_phrase: 挑战短语（P1 带固定短语供前端渲染输入框；P0 为 None 渲染按钮）
            - risk_level: "P0" / "P1" / None
        """
        risk = self._risk_levels.get(tool_name, "P3")
        if risk in ("P0", "P1"):
            args_preview = json.dumps(arguments, ensure_ascii=False)[:300]
            level_label = "P0（最高风险）" if risk == "P0" else "P1（高风险）"
            msg = (
                f"[安全拦截] 工具 '{tool_name}' 风险等级 {level_label}，"
                f"参数：{args_preview}。"
            )
            challenge = "我知晓此操作为危险操作，确认执行。" if risk == "P1" else None
            return msg, challenge, risk
        return None, None, None

    def execute_with_validation(self, action: str, action_input: dict,
                                 raw_result, trace_id: str, latency_ms: float = 0):
        """工具结果校验与增强包装器。

        对 web_search 等工具的结果进行后处理校验：
        - 检测相关性校验失败 → 返回 {"status": "low_quality", ...}
        - 检测空结果 → 返回 {"status": "empty", ...}
        - 对 snippet 过长做截断
        - 结构化日志记录

        Returns:
            正常情况返回 raw_result 原值；
            web_search 检测到问题时返回包含 status/suggestion 的 dict。
        """
        import time

        # ── P0-1/P0-2: 副作用工具自动写后验证 + 失败自动回滚（管线自动消费验证器） ──
        try:
            from tools.post_action_verify import auto_verify_tool_result, _AUTO_VERIFY_TOOL_NAMES
            from tools.side_effect_journal import side_effect_journal as _sej
        except ImportError:
            auto_verify_tool_result = None
            _AUTO_VERIFY_TOOL_NAMES = ()
            _sej = None

        def _auto_rollback(reason: str):
            """后续工具失败时，自动补偿已执行的有副作用工具（P0-2 事务语义）。"""
            if _sej is not None and _sej.count() > 0:
                try:
                    results = _sej.rollback_all()
                    get_logger().warning(
                        f"[{trace_id}] 工具 '{action}' {reason}，已自动回滚 {len(results)} 条已执行副作用操作"
                    )
                except Exception as _e:
                    get_logger().warning(f"[{trace_id}] 自动回滚副作用操作异常: {_e}")

        if action == "web_search":
            # 熔断器检查：连续失败/低质量后跳过搜索
            if self._search_breaker.should_skip_search():
                _auto_rollback("触发搜索熔断降级")
                return {
                    "status": "degraded",
                    "message": self._search_breaker.get_degradation_message(),
                    "results": [],
                }

            result_data = _safe_json_loads(raw_result)
            if not isinstance(result_data, dict):
                result_data = {"success": True, "output": result_data}

            # 相关性校验未通过
            if result_data.get("success") and not result_data.get("relevance_passed", True):
                self._search_breaker.record_result(False, False)
                get_logger().warning(
                    f"[{trace_id}] 搜索结果相关性校验未通过，query={action_input.get('query')}"
                )
                _auto_rollback("搜索结果相关性校验未通过")
                return {
                    "status": "low_quality",
                    "message": "搜索结果与查询无关，已全部过滤。",
                    "suggestion": (
                        "请更换更具体的搜索关键词，"
                        "或使用 fetch_webpage 直接访问目标网站。"
                    ),
                    "results": [],
                }

            results = result_data.get("results", [])
            if len(results) == 0:
                self._search_breaker.record_result(False, False)
                _auto_rollback("搜索结果为空")
                return {
                    "status": "empty",
                    "message": "未找到相关结果。",
                    "suggestion": (
                        "搜索未获取到有效素材。"
                        "建议：1) 缩短关键词重试  2) 或直接基于模型已有知识生成内容，"
                        "在数据处标注'(基于模型知识，未实时验证)'后继续任务。"
                        "不要反复搜索相同主题，避免无效循环。"
                    ),
                }

            # snippet 截断
            for r in results:
                snippet = r.get("snippet", "")
                if isinstance(snippet, str) and len(snippet) > 200:
                    r["snippet"] = snippet[:200] + "..."

            # 正常结果：记录成功，重置熔断器
            self._search_breaker.record_result(True, result_data.get("relevance_passed", True))
            return raw_result

        # ── 通用空结果检测（所有工具） ──
        if not raw_result or (isinstance(raw_result, str) and raw_result.strip() == ""):
            get_logger().warning(
                f"[{trace_id}] 工具 '{action}' 返回空结果",
            )
            _auto_rollback("返回空结果")
            return {
                "status": "empty",
                "success": False,
                "message": f"工具 {action} 返回空结果。引擎已注入占位提示，请勿重复调用同一工具，改用其他方案。",
            }

        # ── P0-1: 管线自动消费写后验证器（原 post_action_verify 为 LLM 可选工具，现自动执行） ──
        if _AUTO_VERIFY_TOOL_NAMES and action in _AUTO_VERIFY_TOOL_NAMES and auto_verify_tool_result is not None:
            try:
                _vrep = auto_verify_tool_result(action, action_input, raw_result)
                if _vrep is not None and not _vrep.get("verified"):
                    _vmsg = str(_vrep.get("verify_message", "验证失败"))
                    get_logger().warning(
                        f"[{trace_id}] 工具 '{action}' 写后验证失败: {_vmsg}"
                    )
                    _auto_rollback("写后验证失败")
                    return {
                        "success": False,
                        "status": "verify_failed",
                        "message": f"工具 {action} 执行后验证失败：{_vmsg}。已按失败处理，请勿重复执行。",
                        "verification": _vrep,
                    }
            except Exception:
                pass  # 验证器自身异常不阻塞主流程（保留原结果）

        # 结构化日志（若 structured_logger 存在）
        try:
            from utils.structured_logger import log_tool_call, ToolCallLog

            if action == "web_search":
                rd = _safe_json_loads(raw_result)
                log_tool_call(ToolCallLog(
                    tool_name=action,
                    tool_input=action_input,
                    tool_output={
                        "result_count": len(rd.get("results", [])) if isinstance(rd, dict) else 1
                    },
                    success=True,
                    latency_ms=latency_ms,
                    provider_used=rd.get("provider", "") if isinstance(rd, dict) else "",
                    raw_result_count=rd.get("raw_result_count", 0) if isinstance(rd, dict) else 0,
                    filtered_result_count=rd.get("filtered_result_count", 0) if isinstance(rd, dict) else 0,
                    relevance_passed=rd.get("relevance_passed", True) if isinstance(rd, dict) else True,
                ))
            else:
                log_tool_call(ToolCallLog(
                    tool_name=action,
                    tool_input=action_input,
                    tool_output={"result_count": len(raw_result) if isinstance(raw_result, str) else 1},
                    success=True,
                    latency_ms=latency_ms,
                ))
        except ImportError:
            pass

        return raw_result

    def build_step_summary(self, tool_name: str, arguments: dict, result_str: str,
                             status: str, error_msg: str = "", step: int = 0) -> str:
        """构建子轮次的工具执行摘要消息。

        只传递事实：做了什么、结果如何、接下来该做什么。不传原始思考链。
        """
        import datetime as _dt
        now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        args_preview = json.dumps(arguments, ensure_ascii=False)
        if len(args_preview) > 300:
            args_preview = args_preview[:297] + "..."

        if status == "failed":
            error_preview = error_msg
            if len(error_preview) > self._max_result_chars:
                error_preview = error_preview[:self._max_result_chars - 3] + "..."

            content = f"""【上一步执行摘要】
工具  : {tool_name}
参数  : {args_preview}
状态  : ❌ 失败
失败原因: {error_preview}

⚠️ 重要提示：
- 不要重复调用同一个工具（相同参数）
- 如果工具不可用，尝试替代方案或直接告知用户
- 如果所有方案都不可行，输出 Final Answer 说明原因"""
            self._last_step_summary = {
                "timestamp": now, "tool": tool_name,
                "arguments": arguments, "error": error_msg[:500],
                "status": "failed", "step": step,
            }
        else:
            result_preview = result_str
            if len(result_preview) > self._max_result_chars:
                result_preview = result_preview[:self._max_result_chars - 3] + "..."

            content = f"""【上一步执行摘要】
工具  : {tool_name}
参数  : {args_preview}
状态  : ✅ 成功
结果  : {result_preview}
上下文: 操作已完成，请根据此结果继续后续任务"""
            self._last_step_summary = {
                "timestamp": now, "tool": tool_name,
                "arguments": arguments, "result_preview": result_preview[:500],
                "status": "success", "step": step,
            }
        return content

    def get_tool(self, tool_name: str, session_id: str) -> Optional[Dict[str, Any]]:
        """获取工具执行函数（与提示词构建使用同一 _resolve_active_tool_names 真相源）"""
        _tinfo = tool_data_provider.get_tool(tool_name)
        if not _tinfo or not _tinfo.get("function"):
            return None

        if session_id:
            from tools import get_session_domains
            loaded_domains = get_session_domains(session_id)
            active = tool_data_provider._resolve_active_tool_names(list(loaded_domains))
            if tool_name not in active:
                return None

        return {"function": _tinfo["function"], "timeout": _tinfo.get("timeout", 60)}

    def wait_for_confirmation(self, token: str, confirmation_handler: Callable, timeout: int = 60) -> Dict[str, Any]:
        """带超时的确认等待"""
        result_container = {"result": None}

        def wait_worker():
            try:
                result_container["result"] = confirmation_handler(token)
            except Exception as e:
                result_container["result"] = {"confirmed": False, "error": str(e)}

        wait_thread = threading.Thread(target=wait_worker, daemon=True)
        wait_thread.start()
        wait_thread.join(timeout=timeout)

        if wait_thread.is_alive():
            return {"confirmed": False, "timeout": True, "message": f"确认超时（{timeout}秒），任务已取消"}

        return result_container["result"] or {"confirmed": False}

# ═══════════════════════════════════════════════════════════════════════════════
# 从 react_engine.py 迁移的工具执行函数
# ═══════════════════════════════════════════════════════════════════════════════

# 注意：以下模块使用延迟导入以避免循环依赖
# from core.agent_state import safe_final
# from core.streaming import classify_tool_failure, validate_tool_dependency, is_file_not_found


def _persist_tool_pair(engine, full_content, tool_call_id, tc, thinking_summary, tool_content):
    """持久化 assistant(tool_call) + tool(tool_result) 消息对。

    修复 P0：工具结果跨轮丢失。原则：工具结果全量以 role="tool" 拼入上下文。
    - assistant 行额外落 tool_calls JSON（tc 原文）与 thinking_summary，
      供 load_cross_turn_history 跨轮恢复完整 API 序列（assistant tool_calls → tool）；
    - tool 行 content 与轮内 conversation_messages 完全一致（全量 JSON，非摘要），
      保证跨轮注入后模型看到与轮内一致的工具结果。
    """
    tool_calls_json = ""
    if tc and isinstance(tc, dict) and tc.get("id"):
        try:
            tool_calls_json = json.dumps([tc], ensure_ascii=False)
        except Exception:
            tool_calls_json = ""
    append_session_message(
        engine.session_id, "assistant", full_content,
        trace_id=engine.trace_id, msg_type="tool_call",
        tool_call_id=tool_call_id,
        tool_calls=tool_calls_json,
        thinking_summary=thinking_summary or "",
    )
    append_session_message(
        engine.session_id, "tool", tool_content,
        trace_id=engine.trace_id, msg_type="tool_result",
        tool_call_id=tool_call_id,
    )


# ── 工具执行日志辅助函数 ──

def _safe_args_summary(arguments: dict) -> str:
    """安全序列化工具参数摘要，截断过长值。"""
    try:
        items = {}
        for k, v in (arguments or {}).items():
            s = json.dumps(v, ensure_ascii=False, default=str)
            if len(s) > 500:
                s = s[:500] + "..."
            items[k] = s
        return json.dumps(items, ensure_ascii=False)
    except Exception:
        return str(arguments)[:500]


def _safe_result_summary(result) -> str:
    """安全摘要工具执行结果。"""
    if result is None:
        return "None"
    try:
        if isinstance(result, dict):
            msg = result.get("message", "") or result.get("result", "") or ""
            s = str(msg)[:300]
            if "success" in result:
                s = f"success={result.get('success')} " + s
            return s
        s = str(result)[:300]
        return s
    except Exception:
        return str(result)[:300]


def _build_tool_call_kwargs(func, arguments, confirmed_approved=False):
    """构建实际传给工具的参数：确认标记与工具 kwargs 分离。

    - 工具接受 **kwargs：透传全部参数（含 confirmed 确认标记，工具内部自行消费）；
    - 固定签名工具：仅保留签名接受的参数，避免 confirmed 等内部参数引发 TypeError；
    - inspect 无法解析（内置/包装函数）时回退透传全部参数，保持原有行为。
    """
    kwargs = dict(arguments)
    if confirmed_approved:
        kwargs["confirmed"] = True
    try:
        sig = inspect.signature(func)
        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        if has_var_kw:
            return kwargs
        return {k: v for k, v in kwargs.items() if k in sig.parameters}
    except (ValueError, TypeError):
        return kwargs


def execute_one_step(engine, rs: RunState, state, tc: dict, step: int,
                      full_content: str, full_reasoning: str,
                      _tl, _sl, cancel_event, confirmation_handler,
                      user_message: str, remaining_steps
                      ) -> Generator[str, None, str]:
    """执行单个 tool_call 的完整流程。

    包含：参数解析、依赖验证、循环检测、安全确认、工具执行（主进程/子进程）、
    结果校验、成功/失败路径分发。

    可变状态（conversation_messages / tool_call_history / layered_cache /
    final_reply_saved）通过 rs(RunState) 承载。

    Returns:
        flow_control: "return"（终止 run）/ "continue"（跳过当前 tc）/ "proceed"（正常完成）
    """
    # 延迟导入避免循环依赖
    from core.agent_state import safe_final, RunState, save_checkpoint
    from core.streaming import (classify_tool_failure, validate_tool_dependency,
        is_file_not_found, _tool_worker, _build_context_memory_summary,
        _truncate_result, tool_call_signature)
    from memory import append_session_message
    conversation_messages = rs.conversation_messages
    _tool_call_history = rs.tool_call_history
    layered_cache = rs.layered_cache
    final_reply_saved = rs.final_reply_saved

    try:
        tool_call_id = tc.get("id") or f"call_{uuid.uuid4().hex}"
        func = tc.get("function") or {}
        tool_name = func.get("name", "")
        _raw_args = func.get("arguments", "{}")
        try:
            arguments = json.loads(_raw_args) if isinstance(_raw_args, str) else _raw_args
        except (json.JSONDecodeError, TypeError):
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {"raw": arguments}

        if not tool_name:
            conversation_messages.append({
                "role": "tool", "tool_call_id": tool_call_id,
                "content": "[系统] 工具调用格式异常，已忽略。",
            })
            return "continue"

        get_logger().info(f"LLM决定调用工具: {tool_name}",
                      trace_id=engine.trace_id, session_id=engine.session_id)
        yield engine.streamer.emit_reasoning(f"决定调用工具: {tool_name}")
        
        dep_error = validate_tool_dependency(tool_name)
        if dep_error:
            yield engine.streamer.emit_step(f"工具依赖验证失败: {dep_error}", "error")
            args_preview = json.dumps(arguments, ensure_ascii=False)[:500]
            if len(json.dumps(arguments, ensure_ascii=False)) > 500:
                args_preview += "..."
            _tool_call_history.append({
                "name": tool_name,
                "args": args_preview,
                "status": "failed",
                "result": f"依赖验证失败: {dep_error[:500]}..." if len(dep_error) > 500 else dep_error
            })
            step_msg = engine.tool_pipeline.build_step_summary(tool_name, arguments, "", "failed", dep_error, step)
            _persist_tool_pair(engine, full_content, tool_call_id, tc, full_reasoning,
                               json.dumps({"success": False, "message": f"依赖验证失败: {dep_error}"}, ensure_ascii=False))
            conversation_messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": tool_name,
                "content": json.dumps({"success": False, "message": f"依赖验证失败: {dep_error}"}, ensure_ascii=False)
            })
            return "continue"
        
        yield engine.streamer.emit_tool_call(tool_name, arguments)

        # ===== 三层递进式循环检测（ToolLoopDetector） =====
        # Layer 1: 精确签名去重（同工具+同参数 → 硬终止）
        # Layer 2: 同名工具频率（同工具不同参数 → 硬终止，搜索类豁免）
        # Layer 3: 全局步数上限（MAX_TURNS 兜底，在主循环 for step 中）
        try:
            engine._loop_detector.check(tool_name, arguments, step)
        except ToolLoopError as _loop_err:
            get_logger().warning(f"[熔断] {_loop_err}", trace_id=engine.trace_id, session_id=engine.session_id)
            yield from safe_final(engine,state,
                f"⚠️ {_loop_err}\n\n"
                f"可能原因：工具返回结果不满足需求，或任务超出当前能力范围。\n"
                f"建议：请尝试更具体的描述，或确认是否需要安装额外工具。"
            )
            final_reply_saved = True
            return "return"

        # ── SubTurn 开始 ──
        _sl.start_sub_turn(
            tool_name=tool_name,
            tool_params=arguments,
            reasoning=full_reasoning,
            context_memory=_build_context_memory_summary(layered_cache),
            context_memory_note="",
            metadata={},
        )
        
        try:
            tool_func = None
            timeout = 60
            
            tool_info = engine.tool_pipeline.get_tool(tool_name, engine.session_id)
            if tool_info:
                tool_func = tool_info["function"]
                timeout = tool_info["timeout"]
            
            if not tool_func:
                yield engine.streamer.emit_step(f"工具 '{tool_name}' 不存在", "error")
                args_preview = json.dumps(arguments, ensure_ascii=False)[:500]
                if len(json.dumps(arguments, ensure_ascii=False)) > 500:
                    args_preview += "..."
                _tool_call_history.append({
                    "name": tool_name,
                    "args": args_preview,
                    "status": "failed",
                    "result": f"工具不存在"
                })
                step_msg = engine.tool_pipeline.build_step_summary(tool_name, arguments, "", "failed",
                    f"工具 '{tool_name}' 不存在，请检查是否已加载对应技能域", step)
                _persist_tool_pair(engine, full_content, tool_call_id, tc, full_reasoning,
                                   json.dumps({"success": False, "message": f"工具 '{tool_name}' 不存在"}, ensure_ascii=False))
                conversation_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": tool_name,
                    "content": json.dumps({"success": False, "message": f"工具 '{tool_name}' 不存在"}, ensure_ascii=False)
                })
                # 幻觉工具拦截强化（P1-6）：未注册工具计入全局连续失败计数，
                # 复用 MAX_GLOBAL_FAILURES 熔断，防止模型持续虚构工具名空转。
                engine._breaker_state._global_consecutive_failures += 1
                return "continue"
            
            # 工具执行前检查取消信号
            if cancel_event is not None and cancel_event.is_set():
                get_logger().info(f"检测到取消信号，终止工具执行: {tool_name}",
                                  trace_id=engine.trace_id, session_id=engine.session_id)
                yield from safe_final(engine,state, "任务已取消", outcome="cancelled")
                return "return"

            # ── 安全分级检查（P0/P1 需确认） ──
            risk_msg, challenge_phrase, risk_level = engine.tool_pipeline.check_tool_risk_level(tool_name, arguments)
            _confirm_approved = False
            if risk_msg:
                yield engine.streamer.emit_reasoning(risk_msg)
                # ── 确认缓存：同一工具+完整参数（120s 内）已确认过 → 直接放行，不再弹确认框 ──
                # 注：key 绑定全部参数而非仅 file_path/path/command，避免不同 operation/key 的
                # 注册表等高风险调用因空缓存键互相命中而绕过确认（安全加固）。
                cache_key = f"{tool_name}:{json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)}"
                _cached_at = engine._confirmed_cache.get(cache_key)
                if _cached_at is not None and (time.time() - _cached_at) < 120:
                    get_logger().info(f"confirm cache hit: {tool_name}, skip",
                                      trace_id=engine.trace_id, session_id=engine.session_id)
                elif confirmation_handler:
                    token = f"risk_confirm_{tool_name}_{int(__import__('time').time())}"
                    yield engine.streamer.emit_confirm(
                        token,
                        title=f"安全风险确认：{tool_name}（{risk_level}）",
                        command=risk_msg,
                        tool_name=tool_name,
                        details=arguments,
                        challenge_phrase=challenge_phrase,
                    )
                    confirm_result = engine.tool_pipeline.wait_for_confirmation(token, confirmation_handler,
                                                                               timeout=engine.CONFIRMATION_TIMEOUT)
                    confirm_result = confirm_result or {}

                    # ✅ 校验（对齐路径 B）
                    if challenge_phrase:
                        ok = bool(confirm_result.get("confirmed")) and \
                             (confirm_result.get("challenge_response", "").strip() == challenge_phrase)
                    else:
                        ok = bool(confirm_result.get("confirmed"))
                    if not ok:
                        yield engine.streamer.emit_step(f"用户拒绝/挑战校验失败 '{tool_name}'，已跳过", "warning")
                        args_preview = json.dumps(arguments, ensure_ascii=False)[:500]
                        if len(json.dumps(arguments, ensure_ascii=False)) > 500:
                            args_preview += "..."
                        _tool_call_history.append({
                            "name": tool_name, "args": args_preview,
                            "status": "rejected", "result": "用户拒绝/挑战校验失败"
                        })
                        step_msg = engine.tool_pipeline.build_step_summary(tool_name, arguments, "", "failed",
                            f"用户拒绝/挑战校验失败 '{tool_name}'", step)
                        _persist_tool_pair(engine, full_content, tool_call_id, tc, full_reasoning,
                                           json.dumps({"success": False, "message": "用户拒绝/挑战校验失败"}, ensure_ascii=False))
                        conversation_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "name": tool_name,
                            "content": json.dumps({"success": False, "message": "用户拒绝/挑战校验失败"}, ensure_ascii=False)
                        })
                        return "continue"

                    # ✅ 确认通过：按是否带挑战短语注入对应确认参数
                    # 注：已删除向 tools.system_tools._pending_confirmations 的预注册——
                    # system_tools 不存在该属性（AttributeError 被吞），且 execute_command
                    # 已从 Path A 移除，该预注册从不生效，属死代码。
                    if not challenge_phrase:
                        _confirm_approved = True

                    # ✅ 确认通过：写入确认缓存（同工具+同关键参数 120s 内不再重复确认）
                    engine._confirmed_cache[cache_key] = time.time()
                else:
                    # ── P0-3 fail-closed：无确认处理器时拒绝执行高风险工具，绝不默认放行 ──
                    get_logger().warning(
                        f"安全确认处理器缺失（confirmation_handler=None），已拒绝执行高风险工具 {tool_name}（{risk_level}）",
                        trace_id=engine.trace_id, session_id=engine.session_id)
                    yield engine.streamer.emit_step(
                        f"安全确认处理器缺失，已拒绝执行高风险工具 '{tool_name}'（{risk_level}）。"
                        f"请在具备确认能力的会话中重试。", "warning")
                    args_preview = json.dumps(arguments, ensure_ascii=False)[:500]
                    if len(json.dumps(arguments, ensure_ascii=False)) > 500:
                        args_preview += "..."
                    _tool_call_history.append({
                        "name": tool_name, "args": args_preview,
                        "status": "rejected", "result": "安全确认处理器缺失，已拒绝执行"
                    })
                    step_msg = engine.tool_pipeline.build_step_summary(tool_name, arguments, "", "failed",
                        f"安全确认处理器缺失，已拒绝执行高风险工具 '{tool_name}'", step)
                    _persist_tool_pair(engine, full_content, tool_call_id, tc, full_reasoning,
                                       json.dumps({"success": False, "message": "安全确认处理器缺失，已拒绝执行高风险工具"}, ensure_ascii=False))
                    conversation_messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "name": tool_name,
                        "content": json.dumps({"success": False, "message": "安全确认处理器缺失，已拒绝执行高风险工具"}, ensure_ascii=False)
                    })
                    return "continue"

            # 注入 session_id 到 load_domain_skills 等需要会话上下文的工具
            if tool_name == "load_domain_skills":
                arguments["session_id"] = engine.session_id
            
            # 需要修改主进程状态的工具：直接在主进程执行（不走子进程）
            _IN_PROCESS_TOOLS = {"load_domain_skills", "ask_user_clarification", "web_download"}
            if tool_name in _IN_PROCESS_TOOLS:
                try:
                    import time as _vt
                    _vt0 = _vt.time()
                    _call_kwargs = _build_tool_call_kwargs(tool_func, arguments, _confirm_approved)
                    raw_result = tool_func(**_call_kwargs)
                    _latency = (_vt.time() - _vt0) * 1000
                    validated = engine.tool_pipeline.execute_with_validation(
                        tool_name, arguments, raw_result, engine.trace_id, _latency
                    )
                    if isinstance(validated, dict) and validated.get("status") in ("low_quality", "empty"):
                        result_data = validated
                    else:
                        result_data = _safe_json_loads(raw_result) if isinstance(raw_result, str) else raw_result
                        if not isinstance(result_data, dict):
                            result_data = {"success": True, "result": raw_result}
                except Exception as _e:
                    result_data = {"success": False, "message": f"{type(_e).__name__}: {_e}"}

                # ===== 修复：ask_user_clarification 统一走 Path B 确认分支 =====
                # 内联工具返回 confirmation_required 时，必须与 handle_tool_success 一致，
                # 执行 emit_confirm → wait_for_confirmation → 超时/拒绝处理；
                # 否则 confirmation_required 被当作普通工具结果回传 LLM，
                # LLM 会生成"等待确认" final_answer 结束任务，确认弹窗/超时提示从未触发。
                if (tool_name == "ask_user_clarification" and confirmation_handler
                        and isinstance(result_data, dict) and result_data.get("confirmation_required")):
                    token = result_data.get("confirmation_token") or result_data.get("challenge_token")
                    if token:
                        title = result_data.get("title", result_data.get("message", "操作需要确认"))
                        command = result_data.get("command", "")
                        details = result_data.get("details", {})
                        yield engine.streamer.emit_confirm(token, title=title, command=command,
                                                          details=details, tool_name=tool_name)
                        confirmation_result = engine.tool_pipeline.wait_for_confirmation(
                            token, confirmation_handler, timeout=engine.CONFIRMATION_TIMEOUT)
                        if confirmation_result.get("confirmed"):
                            if cancel_event is not None and cancel_event.is_set():
                                yield from safe_final(engine, state, "任务已取消", outcome="cancelled")
                                final_reply_saved = True
                                return "return"
                            confirmed_args = {**arguments, "confirmation_token": token}
                            try:
                                confirm_result = tool_func(**_build_tool_call_kwargs(tool_func, confirmed_args))
                            except Exception as e:
                                yield engine.streamer.emit_step(f"工具执行失败: {e}", "failed")
                                final_reply_saved = True
                                return "return"
                            raw_data = _safe_json_loads(confirm_result) if isinstance(confirm_result, str) else confirm_result
                            result_data = raw_data if isinstance(raw_data, dict) else {"success": True, "result": raw_data}
                        else:
                            if confirmation_result.get("timeout"):
                                timeout_msg = confirmation_result.get("message", "确认超时，任务已取消")
                                # 持久化确认超时状态到 checkpoint，恢复时可感知
                                save_checkpoint(engine, user_message, state, remaining_steps, step,
                                                tool_name, arguments, progress="确认超时，任务已取消",
                                                messages_param=conversation_messages)
                                yield engine.streamer.emit_step(timeout_msg, "failed")
                                yield from safe_final(engine, state, timeout_msg, outcome="cancelled")
                            else:
                                yield from safe_final(engine, state, "用户拒绝了操作，任务已取消", outcome="cancelled")
                            final_reply_saved = True
                            return "return"
                    # 无 token 时降级为普通结果返回（保持原行为）

                yield engine.streamer.emit_tool_result(tool_name, result_data)
                
                args_preview = json.dumps(arguments, ensure_ascii=False)[:500]
                _tool_call_history.append({"name": tool_name, "args": args_preview, "status": "success" if result_data.get("success") else "failed", "result": result_data.get("message", "")[:200]})
                step_msg = engine.tool_pipeline.build_step_summary(tool_name, arguments, result_data.get("message", ""), "success" if result_data.get("success") else "failed", "", step)
                _persist_tool_pair(engine, full_content, tool_call_id, tc, full_reasoning,
                                   json.dumps(result_data, ensure_ascii=False))
                # 记录 tool 结果到 conversation_messages
                conversation_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": tool_name,
                    "content": json.dumps(result_data, ensure_ascii=False)
                })
                save_checkpoint(engine,user_message, state, remaining_steps, step, tool_name, arguments, progress="完成" if result_data.get("success") else result_data.get("message", ""), messages_param=conversation_messages)

                # ★ 工具执行日志钩子（主进程）
                _tl = get_tool_execution_logger()
                _tl.info(f"[TOOL] name={tool_name} duration={_latency:.0f}ms success={result_data.get('success')} "
                         f"args={_safe_args_summary(arguments)} result_summary={_safe_result_summary(result_data)}")

                return "continue"  # 跳过子进程路径，进入下一轮 LLM 调用

            # 注入预验证的卸载路径到 execute_command（跨进程安全设计）
            if tool_name == "execute_command":
                try:
                    from tools.system_tools import _uninstall_path_cache, _PATH_CACHE_TTL_SECONDS, _uninstall_path_lock, _is_uninstall_command
                    import time as _t
                    prevalidated = []
                    with _uninstall_path_lock:
                        for _pn, (_ts, _paths) in list(_uninstall_path_cache.items()):
                            if _t.time() - _ts < _PATH_CACHE_TTL_SECONDS:
                                prevalidated.extend(list(_paths))
                    
                    cmd = arguments.get("command", "")
                    if _is_uninstall_command(cmd) and not prevalidated:
                        get_logger().info(f"检测到卸载命令但缓存为空，自动获取卸载路径",
                                          trace_id=engine.trace_id, session_id=engine.session_id)
                        try:
                            exe_match = re.search(r'"?([^"\s]+\\\.exe)"?', cmd, re.IGNORECASE)
                            if exe_match:
                                exe_path = exe_match.group(1)
                                parent_dir = os.path.dirname(exe_path)
                                program_name = os.path.basename(parent_dir)
                                if not program_name or program_name.lower() in ["uninstall", "bin"]:
                                    program_name = re.sub(r'\.exe$', '', os.path.basename(exe_path), flags=re.IGNORECASE)
                                
                                get_uninstall_func = engine.tool_pipeline.get_tool("get_uninstall_string", engine.session_id)
                                if get_uninstall_func:
                                    func = get_uninstall_func["function"]
                                    result = func(program_name)
                                    raw_data = _safe_json_loads(result) if isinstance(result, str) else result
                                    if isinstance(raw_data, dict) and raw_data.get("programs"):
                                        _paths_set = set()
                                        for _p in raw_data["programs"]:
                                            _us = _p.get("UninstallString") or _p.get("QuietUninstallString") or ""
                                            if _us:
                                                _paths_set.add(_us.strip('"').strip("'"))
                                        if _paths_set:
                                            with _uninstall_path_lock:
                                                _uninstall_path_cache[program_name.lower()] = (
                                                    _t.time(),
                                                    list(_paths_set)
                                                )
                                                prevalidated.extend(list(_paths_set))
                                                get_logger().info(f"成功获取卸载路径: {len(_paths_set)} 条",
                                                                  trace_id=engine.trace_id, session_id=engine.session_id)
                        except Exception as _auto_e:
                            get_logger().debug(f"自动获取卸载路径失败（不影响主流程）: {_auto_e}",
                                               trace_id=engine.trace_id, session_id=engine.session_id)
                    
                    if prevalidated:
                        arguments["prevalidated_uninstall_paths"] = prevalidated
                except Exception as _e:
                    get_logger().debug(f"注入预验证路径失败（不影响主流程）: {_e}",
                                       trace_id=engine.trace_id, session_id=engine.session_id)

            result_container = {"result": None, "error": None}
            _call_kwargs = _build_tool_call_kwargs(tool_func, arguments, _confirm_approved)

            def _tool_thread():
                try:
                    result_container["result"] = tool_func(**_call_kwargs)
                except Exception as e:
                    result_container["error"] = f"{type(e).__name__}: {e}"

            worker_thread = ThreadPoolExecutor(max_workers=1)
            future = worker_thread.submit(_tool_thread)
            try:
                future.result(timeout=timeout)
            except TimeoutError:
                worker_thread.shutdown(wait=False)
                get_logger().warning(f"工具 '{tool_name}' 执行超时，正在终止线程",
                                  trace_id=engine.trace_id, session_id=engine.session_id)
                yield engine.streamer.emit_step(f"工具 '{tool_name}' 执行超时（{timeout}秒），已终止线程", "failed")
                # P0-2: 后续工具失败时，自动补偿已执行的有副作用工具（避免半配置状态）
                try:
                    from tools.side_effect_journal import side_effect_journal as _sej
                    if _sej.count() > 0:
                        _rollback_results = _sej.rollback_all()
                        get_logger().warning(f"工具 '{tool_name}' 执行超时，自动回滚 {len(_rollback_results)} 条已执行副作用操作",
                                             trace_id=engine.trace_id, session_id=engine.session_id)
                except Exception:
                    pass
                _timeout_sig = tool_call_signature(tool_name, arguments)
                if _timeout_sig == engine._breaker_state.last_tool_call_signature:
                    engine._breaker_state.consecutive_fail_count += 1
                else:
                    engine._breaker_state.consecutive_fail_count = 1
                    engine._breaker_state.last_tool_call_signature = _timeout_sig
                engine._breaker_state.global_consecutive_failures += 1
                if engine._breaker_state.consecutive_fail_count >= engine.MAX_CONSECUTIVE_FAILURES:
                    yield from safe_final(engine,state, f"工具 {tool_name} 以相同参数连续失败 {engine.MAX_CONSECUTIVE_FAILURES} 次，任务终止。")
                    final_reply_saved = True
                    return "return"
                if engine._breaker_state.global_consecutive_failures >= engine.MAX_GLOBAL_FAILURES:
                    yield from safe_final(engine,state, f"检测到连续失败 {engine._breaker_state.global_consecutive_failures} 次（跨工具），任务已终止。")
                    final_reply_saved = True
                    return "return"
                args_preview = json.dumps(arguments, ensure_ascii=False)[:500]
                if len(json.dumps(arguments, ensure_ascii=False)) > 500:
                    args_preview += "..."
                conversation_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": tool_name,
                    "content": json.dumps({"success": False, "message": f"执行超时（{timeout}秒）"}, ensure_ascii=False)
                })
                _tool_call_history.append({
                    "name": tool_name,
                    "args": args_preview,
                    "status": "failed",
                    "result": f"执行超时（{timeout}秒）"
                })
                step_msg = engine.tool_pipeline.build_step_summary(tool_name, arguments, "", "failed",
                    f"工具 '{tool_name}' 执行超时（{timeout}秒）", step)
                _persist_tool_pair(engine, full_content, tool_call_id, tc, full_reasoning,
                                   json.dumps({"success": False, "message": f"执行超时（{timeout}秒）"}, ensure_ascii=False))
                save_checkpoint(engine,user_message, state, remaining_steps, step, tool_name, arguments, progress=f"失败: 执行超时", messages_param=conversation_messages)
                return "continue"
            finally:
                worker_thread.shutdown(wait=False)

            result = result_container["result"]

            if result_container["error"]:
                # result_container["error"] 已包含 "ExceptionType: 原文" 格式
                raw_err = str(result_container["error"])
                error_msg = translate_error(raw_err)
                # 确保 LLM 上下文中包含完整错误类型信息
                full_error_msg = f"{raw_err} | {error_msg}" if raw_err not in error_msg else error_msg
                yield engine.streamer.emit_step(f"工具执行失败: {full_error_msg}", "failed")
                # P0-2: 后续工具失败时，自动补偿已执行的有副作用工具（避免半配置状态）
                try:
                    from tools.side_effect_journal import side_effect_journal as _sej
                    if _sej.count() > 0:
                        _rollback_results = _sej.rollback_all()
                        get_logger().warning(f"工具 '{tool_name}' 执行失败，自动回滚 {len(_rollback_results)} 条已执行副作用操作",
                                             trace_id=engine.trace_id, session_id=engine.session_id)
                except Exception:
                    pass
                _error_sig = tool_call_signature(tool_name, arguments)
                if _error_sig == engine._breaker_state.last_tool_call_signature:
                    engine._breaker_state.consecutive_fail_count += 1
                else:
                    engine._breaker_state.consecutive_fail_count = 1
                    engine._breaker_state.last_tool_call_signature = _error_sig
                engine._breaker_state.global_consecutive_failures += 1
                if engine._breaker_state.consecutive_fail_count >= engine.MAX_CONSECUTIVE_FAILURES:
                    yield from safe_final(engine,state, f"工具 {tool_name} 以相同参数连续失败 {engine.MAX_CONSECUTIVE_FAILURES} 次，任务终止。")
                    final_reply_saved = True
                    return "return"
                if engine._breaker_state.global_consecutive_failures >= engine.MAX_GLOBAL_FAILURES:
                    yield from safe_final(engine,state, f"检测到连续失败 {engine._breaker_state.global_consecutive_failures} 次（跨工具），任务已终止。")
                    final_reply_saved = True
                    return "return"
                args_preview = json.dumps(arguments, ensure_ascii=False)[:500]
                if len(json.dumps(arguments, ensure_ascii=False)) > 500:
                    args_preview += "..."
                conversation_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": tool_name,
                    "content": json.dumps({"success": False, "message": full_error_msg}, ensure_ascii=False)
                })
                _tool_call_history.append({
                    "name": tool_name,
                    "args": args_preview,
                    "status": "failed",
                    "result": error_msg
                })
                step_msg = engine.tool_pipeline.build_step_summary(tool_name, arguments, error_msg, "failed",
                    f"工具 '{tool_name}' 执行失败: {error_msg}", step)
                _persist_tool_pair(engine, full_content, tool_call_id, tc, full_reasoning,
                                   json.dumps({"success": False, "message": f"执行失败: {full_error_msg[:500]}"}, ensure_ascii=False))
                save_checkpoint(engine,user_message, state, remaining_steps, step, tool_name, arguments, progress=f"失败: {error_msg}", messages_param=conversation_messages)
                return "continue"

            # 工具执行已完成（子进程），对结果进行校验
            validated = engine.tool_pipeline.execute_with_validation(
                tool_name, arguments, result, engine.trace_id, latency_ms=0
            )

            # ===== 判断是否为有效成功 =====
            # 处理 validated 为 JSON 字符串（如 web_search 正常返回）的情况
            _validated_data = validated
            if isinstance(validated, str):
                _validated_data = _safe_json_loads(validated)
            is_valid_success = (
                isinstance(_validated_data, dict)
                and _validated_data.get("success", False) is True
                and _validated_data.get("status") not in ("empty", "low_quality", "error")
            )
            # 检测确认请求：工具返回 success=False + confirmation_required=True 是正常确认流程，不应路由到失败处理
            is_confirmation_required = (
                isinstance(_validated_data, dict)
                and _validated_data.get("confirmation_required") is True
            )

            if not is_valid_success and not is_confirmation_required:
                # ── 阶段方法：工具失败路径处理（_handle_tool_failure） ──
                rs.conversation_messages = conversation_messages
                rs.tool_call_history = _tool_call_history
                rs.final_reply_saved = final_reply_saved
                _flow, final_reply_saved = yield from handle_tool_failure(
                    engine, rs, state, tool_name, arguments, validated, _validated_data,
                    step, full_content, tool_call_id, user_message, remaining_steps,
                    tc=tc, full_reasoning=full_reasoning
                )
                conversation_messages = rs.conversation_messages
                _tool_call_history = rs.tool_call_history
                if _flow == "return":
                    return "return"
                if _flow == "continue":
                    return "continue"  # ← 跳过后续成功逻辑，进入下一轮

            # ── 阶段方法：工具成功路径处理（_handle_tool_success） ──
            rs.conversation_messages = conversation_messages
            rs.tool_call_history = _tool_call_history
            rs.layered_cache = layered_cache
            rs.final_reply_saved = final_reply_saved
            _flow, final_reply_saved = yield from handle_tool_success(
                engine, rs, state, tool_name, arguments, result, None,
                step, full_content, full_reasoning, _tl, _sl,
                cancel_event, confirmation_handler, tool_func,
                tool_call_id, user_message, remaining_steps, tc=tc
            )
            conversation_messages = rs.conversation_messages
            _tool_call_history = rs.tool_call_history
            layered_cache = rs.layered_cache
            if _flow == "return":
                return "return"
            # _flow == "proceed": 正常完成，for 循环进入下一次迭代

            # ★ 工具执行日志钩子（子进程成功路径）
            _tel = get_tool_execution_logger()
            _tel.info(f"[TOOL] name={tool_name} success=True "
                     f"args={_safe_args_summary(arguments)} result_summary={_safe_result_summary(result)}")

        except Exception as e:
            error_msg = f"{type(e).__name__}: {translate_error(str(e))}"
            yield engine.streamer.emit_step(f"工具执行失败: {error_msg}", "failed")
            # P0-2: 兜底——工具执行链路任意异常时，自动补偿已执行的有副作用工具
            try:
                from tools.side_effect_journal import side_effect_journal as _sej
                if _sej.count() > 0:
                    _rollback_results = _sej.rollback_all()
                    get_logger().warning(f"工具执行链路异常，自动回滚 {len(_rollback_results)} 条已执行副作用操作",
                                         trace_id=engine.trace_id, session_id=engine.session_id)
            except Exception:
                pass
            # 回填工具执行失败结果到 LLM 调用日志
            get_logger().update_tool_result(
                session_id=engine.session_id,
                session_name=engine.session_name,
                turn=state.turn,
                child_turn=state.child_turn - 1,
                tool_result=f"工具执行失败: {error_msg}"
            )

            # ── ChildTurn 记录 + SubTurn 结束（失败） ──
            _tl.log_child_turn(
                child_index=state.child_turn - 1,
                ai_thinking=full_reasoning,
                ai_output=full_content,
                tool_name=tool_name,
                tool_input=arguments,
                tool_result=f"工具执行失败: {error_msg}",
                context_memory=_build_context_memory_summary(layered_cache),
                context_memory_note="",
            )
            _sl.end_sub_turn(
                tool_result=f"工具执行失败: {error_msg}",
                status="failed",
                error=error_msg,
            )
            
            _exc_sig = tool_call_signature(tool_name, arguments)
            if _exc_sig == engine._breaker_state.last_tool_call_signature:
                engine._breaker_state.consecutive_fail_count += 1
            else:
                engine._breaker_state.consecutive_fail_count = 1
                engine._breaker_state.last_tool_call_signature = _exc_sig
            engine._breaker_state.global_consecutive_failures += 1
            
            if engine._breaker_state.consecutive_fail_count >= engine.MAX_CONSECUTIVE_FAILURES:
                yield from safe_final(engine,state, f"工具 {tool_name} 以相同参数连续失败 {engine.MAX_CONSECUTIVE_FAILURES} 次，任务已终止。")
                final_reply_saved = True
                return "return"
            
            if engine._breaker_state.global_consecutive_failures >= engine.MAX_GLOBAL_FAILURES:
                yield from safe_final(engine,state, f"检测到连续失败 {engine._breaker_state.global_consecutive_failures} 次（跨工具），任务已终止以防止无限循环。")
                final_reply_saved = True
                return "return"
            
            args_preview = json.dumps(arguments, ensure_ascii=False)[:500]
            if len(json.dumps(arguments, ensure_ascii=False)) > 500:
                args_preview += "..."
            # 防重复回填：handle_tool_success 内部异常（如 STALLED 分支）可能已 append 过同 tool_call_id 的
            # tool 消息（成功结果），此处若再 append 一条失败 tool 消息，会导致下一次 LLM 请求出现
            # "同 tool_call_id 的两条 tool 消息、第二条无前置 assistant.tool_calls"，
            # 被 OpenAI/DeepSeek 以 400 "tool must be a response to a preceding message with 'tool_calls'" 拒绝，
            # 最终回复永不生成（表现为刷新后最新 AI 回复丢失）。
            _has_tool_msg = any(
                _m.get("role") == "tool" and _m.get("tool_call_id") == tool_call_id
                for _m in conversation_messages
            )
            if not _has_tool_msg:
                conversation_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": tool_name,
                    "content": json.dumps({"success": False, "message": f"执行失败: {error_msg[:500]}"}, ensure_ascii=False)
                })
            _tool_call_history.append({
                "name": tool_name,
                "args": args_preview,
                "status": "failed",
                "result": f"执行失败: {error_msg[:500]}..." if len(error_msg) > 500 else error_msg
            })

            step_msg = engine.tool_pipeline.build_step_summary(tool_name, arguments, "", "failed", error_msg, step)

            _persist_tool_pair(engine, full_content, tool_call_id, tc, full_reasoning,
                               json.dumps({"success": False, "message": f"执行失败: {error_msg[:500]}"}, ensure_ascii=False))
            save_checkpoint(engine,user_message, state, remaining_steps, step, tool_name, arguments, progress=f"失败: {error_msg}", messages_param=conversation_messages)

        return "proceed"
    finally:
        rs.conversation_messages = conversation_messages
        rs.tool_call_history = _tool_call_history
        rs.layered_cache = layered_cache
        rs.final_reply_saved = final_reply_saved




def handle_tool_failure(engine, rs: RunState, state, tool_name: str, arguments: dict,
                          validated, _validated_data, step: int, full_content: str,
                          tool_call_id: str, user_message: str, remaining_steps,
                          tc=None, full_reasoning=""
                          ) -> Generator[str, None, tuple]:
    """工具执行失败路径处理（is_valid_success == False）。

    包含：失败分类、签名熔断、结果记录、自适应降级、熔断检查。

    可变状态（conversation_messages / tool_call_history / final_reply_saved）
    通过 rs(RunState) 承载。

    Returns:
        (flow_control, final_reply_saved)
        flow_control: "return"（熔断终止）/ "continue"（跳过成功逻辑，进入下一轮 for tc）
    """
    # 延迟导入避免循环依赖
    from core.agent_state import safe_final, RunState, save_checkpoint
    from core.streaming import (classify_tool_failure, validate_tool_dependency,
        is_file_not_found, _tool_worker, _build_context_memory_summary,
        _truncate_result, tool_call_signature)
    from memory import append_session_message
    conversation_messages = rs.conversation_messages
    _tool_call_history = rs.tool_call_history
    final_reply_saved = rs.final_reply_saved

    try:
        # ===== 走失败路径 =====
        fail_msg = ""
        if isinstance(validated, dict):
            fail_msg = validated.get("message", validated.get("error", "工具执行异常"))
            # 将降级建议附加到失败信息中，使 LLM 能看到回退选项
            suggestion = validated.get("suggestion", "")
            if suggestion:
                fail_msg = f"{fail_msg}\n\n[降级建议] {suggestion}"
        else:
            fail_msg = str(validated)[:200]

        # 失败分类与签名熔断
        _cur_sig = tool_call_signature(tool_name, arguments)
        _failure_type = classify_tool_failure(
            _validated_data if isinstance(_validated_data, dict) else {}
        )

        if _failure_type == "fatal":
            # 致命失败：不计数，直接标记结果让 LLM 换方案
            fail_msg += "\n[此工具当前不可用，请使用其他工具完成目标]"
        elif _failure_type == "file_not_found":
            # 文件不存在：不计入任何熔断计数，重置循环检测使 LLM 可换路径重试
            fail_msg += "\n[文件不存在，请检查路径或尝试其他位置]"
            engine._loop_detector._consecutive_exact_repeat = 0
            engine._loop_detector._last_signature = None
        elif _failure_type == "retryable" and _cur_sig == engine._breaker_state.last_tool_call_signature:
            engine._breaker_state.consecutive_fail_count += 1
        elif _failure_type == "empty_result" or _failure_type == "unknown":
            # 空结果/软失败不计入熔断计数
            pass
        else:
            # 同名同参数真失败才累计
            if _cur_sig == engine._breaker_state.last_tool_call_signature:
                engine._breaker_state.consecutive_fail_count += 1
            else:
                engine._breaker_state.consecutive_fail_count = 1
                engine._breaker_state.last_tool_call_signature = _cur_sig

        if _failure_type not in ("file_not_found", "empty_result", "unknown"):
            engine._breaker_state.global_consecutive_failures += 1

        get_logger().warning(
            f"[Step {step}] 工具 {tool_name} 失败/空结果 "
            f"(连续失败: {engine._breaker_state.consecutive_fail_count}, "
            f"全局: {engine._breaker_state.global_consecutive_failures})"
        )

        args_preview = json.dumps(arguments, ensure_ascii=False)[:500]
        if len(json.dumps(arguments, ensure_ascii=False)) > 500:
            args_preview += "..."
        conversation_messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": json.dumps({"success": False, "message": fail_msg[:500] if isinstance(fail_msg, str) else str(fail_msg)[:500]}, ensure_ascii=False)
        })
        _tool_call_history.append({
            "name": tool_name,
            "args": args_preview,
            "status": "failed",
            "result": fail_msg[:200]
        })
        step_msg = engine.tool_pipeline.build_step_summary(tool_name, arguments, "", "failed", fail_msg, step)
        _persist_tool_pair(engine, full_content, tool_call_id, tc, full_reasoning,
                           json.dumps({"success": False, "message": fail_msg[:500] if isinstance(fail_msg, str) else str(fail_msg)[:500]}, ensure_ascii=False))

        # ── 自适应降级：连续空搜索结果 → 注入回退提示 ──
        if tool_name == "web_search" and _failure_type == "empty_result":
            engine._consecutive_empty_search += 1
            if engine._consecutive_empty_search >= 2:
                _degrade_hint = (
                    "[系统提示] web_search 连续多次未获取到有效素材。"
                    "请停止搜索，直接基于模型已有知识完成任务，"
                    "在数据/结论处标注'基于模型知识，未经实时搜索验证'。"
                    "不要反复重试搜索。"
                )
                conversation_messages.append({
                    "role": "system",
                    "content": _degrade_hint,
                })
                get_logger().warning(
                    f"自适应降级: 连续 {engine._consecutive_empty_search} 次空搜索，已注入回退提示",
                    trace_id=engine.trace_id, session_id=engine.session_id
                )
        elif tool_name == "web_search":
            engine._consecutive_empty_search = 0  # 非空结果 → 重置

        # 熔断检查：只有同名同参数真失败才触发熔断
        if engine._breaker_state.consecutive_fail_count >= engine.MAX_CONSECUTIVE_FAILURES:
            yield from safe_final(engine,state,
                f"工具 {tool_name} 以相同参数连续失败 {engine.MAX_CONSECUTIVE_FAILURES} 次，任务终止。\n"
                f"失败原因：{fail_msg}\n请更换方案或提供更多上下文。"
            )
            final_reply_saved = True
            return ("return", final_reply_saved)

        if engine._breaker_state.global_consecutive_failures >= engine.MAX_GLOBAL_FAILURES:
            yield from safe_final(engine,state,
                f"连续 {engine.MAX_GLOBAL_FAILURES} 次工具调用失败，任务终止。"
            )
            final_reply_saved = True
            return ("return", final_reply_saved)

        return ("continue", final_reply_saved)
    finally:
        rs.conversation_messages = conversation_messages
        rs.tool_call_history = _tool_call_history
        rs.final_reply_saved = final_reply_saved




def handle_tool_success(engine, rs: RunState, state, tool_name: str, arguments: dict,
                          result, result_data, step: int, full_content: str,
                          full_reasoning: str, _tl, _sl, cancel_event,
                          confirmation_handler, tool_func, tool_call_id: str,
                          user_message: str, remaining_steps, tc=None
                          ) -> Generator[str, None, tuple]:
    """工具执行成功路径处理。

    包含：重置熔断、结果解析、confirmation_required 处理、成功记录、进展评估。

    可变状态（conversation_messages / tool_call_history / layered_cache /
    final_reply_saved）通过 rs(RunState) 承载。

    Returns:
        (flow_control, final_reply_saved)
        flow_control: "return"（GUI/FUTILE/确认失败等终止）/"proceed"（正常完成，for tc 下一次）
    """
    # 延迟导入避免循环依赖
    from core.agent_state import safe_final, RunState, save_checkpoint
    from core.streaming import (classify_tool_failure, validate_tool_dependency,
        is_file_not_found, _tool_worker, _build_context_memory_summary,
        _truncate_result, tool_call_signature)
    from memory import append_session_message
    conversation_messages = rs.conversation_messages
    _tool_call_history = rs.tool_call_history
    layered_cache = rs.layered_cache
    final_reply_saved = rs.final_reply_saved

    try:
        # ===== 以下是真正的成功路径 =====
        # 重置熔断计数：只要一次调用成功就清零
        engine._breaker_state.consecutive_fail_count = 0
        engine._breaker_state.last_tool_call_signature = ""

        raw_data = _safe_json_loads(result) if isinstance(result, str) else result
        # 防御：工具可能返回 list/str 等非 dict 类型，统一包装为 dict
        result_data = raw_data if isinstance(raw_data, dict) else {"success": True, "result": raw_data}

        # get_uninstall_string 路径缓存到主进程，使 execute_command 能通过验证
        if tool_name == "get_uninstall_string" and result_data.get("programs"):
            try:
                import tools.system_tools as _st
                _paths_set = set()
                for _p in result_data["programs"]:
                    _us = _p.get("UninstallString") or _p.get("QuietUninstallString") or ""
                    if _us:
                        _paths_set.add(_us.strip('"').strip("'"))
                if _paths_set:
                    _prog_name = (arguments.get("program_name") or
                                  (result_data["programs"][0].get("DisplayName", "")))
                    if _prog_name:
                        with _st._uninstall_path_lock:
                            _st._uninstall_path_cache[_prog_name.lower()] = (
                                __import__("time").time(),
                                list(_paths_set)
                            )
            except Exception:
                pass

        get_logger().info(f"工具 '{tool_name}' 执行完成: success={result_data.get('success')}, message={result_data.get('message','')[:200]}",
                      trace_id=engine.trace_id, session_id=engine.session_id)

        get_logger().debug(f"confirmation_required={result_data.get('confirmation_required')}, confirmation_handler={confirmation_handler}",
                      trace_id=engine.trace_id, session_id=engine.session_id)
        if result_data.get("confirmation_required"):
            get_logger().info(f"[CONFIRM-DEBUG] handle_tool_success detected confirmation_required for {tool_name}")
            get_logger().debug(f"confirmation_required is True", trace_id=engine.trace_id, session_id=engine.session_id)
            get_logger().debug(f"confirmation_handler: {confirmation_handler}", trace_id=engine.trace_id, session_id=engine.session_id)
            get_logger().debug(f"confirmation_token: {result_data.get('confirmation_token')}", trace_id=engine.trace_id, session_id=engine.session_id)
            if confirmation_handler:
                token = result_data.get("confirmation_token") or result_data.get("challenge_token")
                challenge_phrase = result_data.get("challenge_phrase")

                if token:
                    get_logger().debug(f"Got token, emitting confirm event", trace_id=engine.trace_id, session_id=engine.session_id)
                    title = result_data.get("message", "操作需要确认")
                    command = arguments.get("command", "")
                    details = result_data.get("details", {})
                    yield engine.streamer.emit_confirm(token, title=title, command=command,
                                                      details=details, tool_name=tool_name,
                                                      challenge_phrase=challenge_phrase)
                    get_logger().info(f"[CONFIRM-DEBUG] emit_confirm yielded, entering wait_for_confirmation")
                    get_logger().debug(f"Waiting for confirmation...", trace_id=engine.trace_id, session_id=engine.session_id)
                    confirmation_result = engine.tool_pipeline.wait_for_confirmation(token, confirmation_handler, timeout=engine.CONFIRMATION_TIMEOUT)
                    get_logger().debug(f"Confirmation result: {confirmation_result}", trace_id=engine.trace_id, session_id=engine.session_id)
                    if confirmation_result.get("confirmed"):
                        if cancel_event is not None and cancel_event.is_set():
                            yield from safe_final(engine,state, "任务已取消", outcome="cancelled")
                            final_reply_saved = True
                            return ("return", final_reply_saved)

                        challenge_response = confirmation_result.get("challenge_response", "")
                        if challenge_phrase:
                            confirmed_args = {**arguments, "challenge_token": token, "challenge_response": challenge_response}
                        else:
                            confirmed_args = {**arguments, "confirmation_token": token}
                        # 预注册 token 到主进程 confirmation_manager。
                        # web_download：工具不自存 token，引擎代写。
                        # execute_command：工具在子进程中 create_challenge，
                        #   子进程结束后 token 丢失，需引擎在主进程补注册，否则重放时
                        #   _get_confirmation_manager().pop(token) 返回 None。
                        # 注意：这里必须无条件 store（不能先 pop 探测）——
                        #   pop 是破坏性操作，会消耗 token，导致重放时 execute_command L442
                        #   pop 失败而报"挑战验证失败"；无条件覆盖写同一 command 可保持 token 有效。
                        if tool_name == "web_download" or (tool_name == "execute_command" and challenge_phrase):
                            try:
                                import time as _time
                                from core.confirmation_manager import confirmation_manager as _cm
                                if tool_name == "web_download":
                                    _cm.store(token, {
                                        "type": "download",
                                        "url": arguments.get("url", ""),
                                        "output_dir": arguments.get("output_dir", ""),
                                        "expiry": _time.time() + 300,
                                    })
                                else:
                                    _cm.store(token, {
                                        "type": "challenge",
                                        "command": arguments.get("command", ""),
                                        "desc": "execute_command 确认（引擎预注册）",
                                        "source": "system_tools",
                                        "level": "high",
                                        "timestamp": _time.time(),
                                        "_expiry": _time.time() + 300,
                                    })
                            except Exception:
                                pass
                        try:
                            confirm_result = tool_func(**_build_tool_call_kwargs(tool_func, confirmed_args))
                        except Exception as e:
                            yield engine.streamer.emit_step(f"工具执行失败: {e}", "failed")
                            final_reply_saved = True
                            return ("return", final_reply_saved)

                        raw_data = _safe_json_loads(confirm_result) if isinstance(confirm_result, str) else confirm_result
                        result_data = raw_data if isinstance(raw_data, dict) else {"success": True, "result": raw_data}
                        yield engine.streamer.emit_tool_result(tool_name, result_data)
                        if result_data.get("is_gui_operation"):
                            yield from safe_final(engine,state, result_data.get("warning", "GUI程序已启动，需要用户手动操作完成。"), outcome="cancelled")
                            final_reply_saved = True
                            return ("return", final_reply_saved)
                        if not result_data.get("success"):
                            yield from safe_final(engine,state, f"工具执行失败: {result_data.get('message', '未知错误')}")
                            final_reply_saved = True
                            return ("return", final_reply_saved)
                    else:
                        if confirmation_result.get("timeout"):
                            # 持久化确认超时状态到 checkpoint，恢复时可感知
                            save_checkpoint(engine, user_message, state, remaining_steps, step,
                                            tool_name, arguments, progress="确认超时，任务已取消",
                                            messages_param=conversation_messages)
                            yield from safe_final(engine,state, confirmation_result.get("message", "确认超时，任务已取消"), outcome="cancelled")
                        else:
                            save_checkpoint(engine, user_message, state, remaining_steps, step,
                                            tool_name, arguments, progress="用户拒绝确认，任务已取消",
                                            messages_param=conversation_messages)
                            yield from safe_final(engine,state, "用户拒绝了操作，任务已取消", outcome="cancelled")
                        final_reply_saved = True
                        return ("return", final_reply_saved)
                elif challenge_phrase:
                    challenge_token = result_data.get("challenge_token")
                    title = result_data.get("message", "高危操作需要确认")
                    command = arguments.get("command", "")
                    details = {"挑战短语": challenge_phrase}
                    yield engine.streamer.emit_confirm(challenge_token, title=title, command=command,
                                                      details=details, tool_name=tool_name,
                                                      challenge_phrase=challenge_phrase)
                    confirmation_result = engine.tool_pipeline.wait_for_confirmation(challenge_token, confirmation_handler, timeout=engine.CONFIRMATION_TIMEOUT)
                    if confirmation_result.get("confirmed") and confirmation_result.get("challenge_response"):
                        if cancel_event is not None and cancel_event.is_set():
                            yield from safe_final(engine,state, "任务已取消", outcome="cancelled")
                            final_reply_saved = True
                            return ("return", final_reply_saved)

                        confirmed_args = {**arguments, "challenge_token": challenge_token, "challenge_response": confirmation_result["challenge_response"]}
                        # 注：不再预注册 challenge token——execute_command 工具侧已在返回
                        # confirmation_required 前自行 create_challenge 存入 confirmation_manager，
                        # 引擎此处重复预注册到 _st._pending_confirmations（属性不存在）从不生效。
                        try:
                            confirm_result = tool_func(**_build_tool_call_kwargs(tool_func, confirmed_args))
                        except Exception as e:
                            yield engine.streamer.emit_step(f"工具执行失败: {e}", "failed")
                            final_reply_saved = True
                            return ("return", final_reply_saved)

                        raw_data = _safe_json_loads(confirm_result) if isinstance(confirm_result, str) else confirm_result
                        result_data = raw_data if isinstance(raw_data, dict) else {"success": True, "result": raw_data}
                        yield engine.streamer.emit_tool_result(tool_name, result_data)
                        if result_data.get("is_gui_operation"):
                            yield from safe_final(engine,state, result_data.get("warning", "GUI程序已启动，需要用户手动操作完成。"), outcome="cancelled")
                            final_reply_saved = True
                            return ("return", final_reply_saved)
                        if not result_data.get("success"):
                            yield from safe_final(engine,state, f"工具执行失败: {result_data.get('message', '未知错误')}")
                            final_reply_saved = True
                            return ("return", final_reply_saved)
                    else:
                        if confirmation_result.get("timeout"):
                            # 持久化确认超时状态到 checkpoint，恢复时可感知
                            save_checkpoint(engine, user_message, state, remaining_steps, step,
                                            tool_name, arguments, progress="确认超时，任务已取消",
                                            messages_param=conversation_messages)
                            yield from safe_final(engine,state, confirmation_result.get("message", "确认超时，任务已取消"), outcome="cancelled")
                        else:
                            save_checkpoint(engine, user_message, state, remaining_steps, step,
                                            tool_name, arguments, progress="用户拒绝确认或挑战短语验证失败，任务已取消",
                                            messages_param=conversation_messages)
                            yield from safe_final(engine,state, "用户拒绝了操作或挑战短语验证失败，任务已取消", outcome="cancelled")
                        final_reply_saved = True
                        return ("return", final_reply_saved)
                else:
                    yield engine.streamer.emit_tool_result(tool_name, result_data)
            else:
                yield engine.streamer.emit_error("安全确认机制未配置，高危操作已被拒绝，请联系管理员启用确认功能。")
                final_reply_saved = True
                return ("return", final_reply_saved)
        else:
            get_logger().info(f"工具 '{tool_name}' 执行成功，发送工具结果事件",
                          trace_id=engine.trace_id, session_id=engine.session_id)
            yield engine.streamer.emit_tool_result(tool_name, result_data)

        engine.completed_steps.append({
            "step": step,
            "tool_name": tool_name,
            "arguments": arguments,
            "result": result_data,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

        get_logger().info(f"步骤已记录，completed_steps数量: {len(engine.completed_steps)}",
                      trace_id=engine.trace_id, session_id=engine.session_id)

        # 重置熔断计数：工具执行成功
        engine._breaker_state.consecutive_fail_count = 0
        engine._breaker_state.last_tool_call_signature = ""
        engine._breaker_state.global_consecutive_failures = 0

        # 回填工具执行结果到 LLM 调用日志
        _tool_result_str = json.dumps(result_data, ensure_ascii=False) if not isinstance(result_data, str) else result_data
        get_logger().update_tool_result(
            session_id=engine.session_id,
            session_name=engine.session_name,
            turn=state.turn,
            child_turn=state.child_turn - 1,
            tool_result=_tool_result_str
        )

        # ── ChildTurn 记录 + SubTurn 结束 ──
        _tl.log_child_turn(
            child_index=state.child_turn - 1,
            ai_thinking=full_reasoning,
            ai_output=full_content,
            tool_name=tool_name,
            tool_input=arguments,
            tool_result=_tool_result_str,
            context_memory=_build_context_memory_summary(layered_cache),
            context_memory_note="",
        )
        _sl.end_sub_turn(
            tool_result=_tool_result_str,
            status="completed",
        )

        args_preview = json.dumps(arguments, ensure_ascii=False)[:500]
        if len(json.dumps(arguments, ensure_ascii=False)) > 500:
            args_preview += "..."
        result_preview = _truncate_result(_tool_result_str, max_length=engine.MAX_RESULT_CHARS)
        # 记录 tool 结果到 conversation_messages
        conversation_messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": json.dumps(result_data, ensure_ascii=False)
        })
        _tool_call_history.append({
            "name": tool_name,
            "args": args_preview,
            "status": "success",
            "result": result_preview
        })

        if len(_tool_call_history) > 20:
            _tool_call_history = _tool_call_history[-20:]

        step_msg = engine.tool_pipeline.build_step_summary(tool_name, arguments, _tool_result_str, "success", "", step)

        if result_data.get("is_gui_operation"):
            yield from safe_final(engine,state, result_data.get("warning", "GUI程序已启动，需要用户手动操作完成。"), outcome="cancelled")
            final_reply_saved = True
            return ("return", final_reply_saved)

        _persist_tool_pair(engine, full_content, tool_call_id, tc, full_reasoning,
                           json.dumps(result_data, ensure_ascii=False))

        save_checkpoint(engine,user_message, state, remaining_steps, step, tool_name, arguments, messages_param=conversation_messages)

        # ── Progress 进展评估 ──
        if PROGRESS_EVALUATOR_ENABLED:
            verdict, reason = engine.progress.decide(
                tool_name, arguments, result_data,
                changed=result_data.get("changed"),
                polling=result_data.get("polling"))
        else:
            verdict, reason = Verdict.PROGRESS, "feature_disabled"

        if verdict == Verdict.PROGRESS:
            pass  # 有用功，照常继续
        elif verdict == Verdict.STALLED:
            conversation_messages = strip_temp_system_messages(conversation_messages)
            conversation_messages.append({"role":"system","content":
                f"[进展控制器] {reason}。上一步结果摘要：{str(result_data)[:200]}。"
                f"禁止重复上一步；请更换参数/工具，或直接给出最终答复。"})
            yield engine.streamer.emit_step(f"进展停滞：{reason}", "warning")
        elif verdict == Verdict.FUTILE:
            final = engine.tool_pipeline.build_futile_final(tool_name, arguments, reason, result_data)
            yield from safe_final(engine,state, final)
            return ("return", final_reply_saved)

        return ("proceed", final_reply_saved)
    finally:
        rs.conversation_messages = conversation_messages
        rs.tool_call_history = _tool_call_history
        rs.layered_cache = layered_cache
        rs.final_reply_saved = final_reply_saved




def do_final_cleanup(engine, state, user_message: str, remaining_steps: list,
                      cancel_event, default_model: str, default_provider: str,
                      model_id: str, temperature) -> Generator[str, None, None]:
    """循环结束后的收尾：生成进度摘要、保存checkpoint、yield最终结果。

    从 run() 主循环后提取，通过 yield from 委托。
    """
    # 延迟导入避免循环依赖
    from core.agent_state import safe_final, RunState, save_checkpoint
    from core.streaming import (classify_tool_failure, validate_tool_dependency,
        is_file_not_found, _tool_worker, _build_context_memory_summary,
        _truncate_result, tool_call_signature)
    from memory import append_session_message
    from llm_client import ai_concat
    if cancel_event is not None and cancel_event.is_set():
        get_logger().info("检测到取消信号，跳过最终进度摘要生成",
                          trace_id=engine.trace_id, session_id=engine.session_id)
        yield from safe_final(engine,state, "任务已取消", outcome="cancelled")
        return
        
    if engine.completed_steps:
        steps_summary_lines = []
        for idx, step_data in enumerate(engine.completed_steps, 1):
            tool_name = step_data.get("tool_name", "")
            arguments = step_data.get("arguments", {})
            result = step_data.get("result", {})
            step_msg = f"{idx}. 使用 {tool_name}"
            if isinstance(arguments, dict):
                args_str = ", ".join([f"{k}={v}" for k, v in arguments.items()])
                if args_str:
                    step_msg += f" ({args_str})"
            elif isinstance(arguments, str):
                step_msg += f" ({arguments})"
            if isinstance(result, dict):
                success = result.get("success", True)
                message = result.get("message", "")
                if message:
                    step_msg += f" - {message}"
            steps_summary_lines.append(step_msg)
        steps_summary = "\n".join(steps_summary_lines)
    else:
        steps_summary = "无"
        
    progress_prompt = f"""请总结当前任务执行进度：
1. 已完成的步骤
2. 未完成的步骤
3. 当前状态

任务：{user_message}
已完成步骤：
{steps_summary}
"""
    try:
        progress_result = ai_concat(
            messages=[{"role": "user", "content": progress_prompt}],
            model_name=default_model,
            provider=default_provider,
            trace_id=engine.trace_id,
            model_id=model_id,
            temperature=temperature
        )
        progress_summary = progress_result["message"].get("content", "").strip()
    except Exception as e:
        progress_summary = f"任务执行到第{engine.MAX_TURNS}轮终止。已完成{len(engine.completed_steps)}个步骤。"
        
    save_checkpoint(engine,user_message, state, remaining_steps, engine.MAX_TURNS, progress=progress_summary)
        
    yield from safe_final(engine,state,
        f"[系统] 已达到最大推理步数 {engine.MAX_TURNS}，自动结束。"
        f"已完成的步骤见上文；若任务未完成，请缩小范围后重试。",
        outcome="max_steps")
    return


