import yaml
import inspect
from pathlib import Path
from typing import Dict, List, Any, Set, Optional, Callable
import threading


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
    
    def build_system_prompt_tool_list(self, domains: List[str] = None) -> str:
        """构建系统提示词中的工具列表部分"""
        self._sync_all()
        
        with self._merged_lock:
            always_inject = set(self._merged_always_inject)
            active = set(always_inject)
            
            if domains:
                for domain in domains:
                    domain_info = self._merged_domains.get(domain, {})
                    domain_tools = domain_info.get("tools", [])
                    active.update(domain_tools)
            
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
        self._sync_all()
        
        with self._merged_lock:
            always_inject = set(self._merged_always_inject)
            active = set(always_inject)
            
            if domains:
                for domain in domains:
                    domain_info = self._merged_domains.get(domain, {})
                    domain_tools = domain_info.get("tools", [])
                    active.update(domain_tools)
            
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