import inspect
import threading
import yaml
import copy
import time
from pathlib import Path
from typing import Dict, Set, Any, Callable, Optional, List, Union, get_origin, get_args
from logger import get_logger

logger = get_logger()


class ToolRegistry:
    def __init__(self):
        self._registry: Dict[str, Dict[str, Any]] = {}
        self._path_params_cache: Dict[str, list] = {}
        self._session_domains: Dict[str, dict] = {}
        self._session_domains_lock = threading.Lock()
        self._session_cleanup_interval = 3600
        self._session_max_age = 7200
        
        self._prompts_dir = Path(__file__).parent.parent / "prompts"
        self._tools_config_path = self._prompts_dir / "tools.yaml"
        self._agent_skills_dir = Path(__file__).parent.parent / "agent_skills"
        self._agent_tools_yaml = self._agent_skills_dir / "agent_tools.yaml"
        
        self._tools_config_cache = None
        self._tools_config_mtime = 0.0
        self._agent_tools_config_cache = None
        self._agent_tools_config_mtime = 0.0
        self._prompt_cache: Dict[str, str] = {}
        self._prompt_cache_order: List[str] = []
        self._prompt_cache_max_size = 100
        self._prompt_cache_lock = threading.Lock()
        self._config_cache_lock = threading.Lock()
        
        self._start_session_cleanup_thread()
    
    def _start_session_cleanup_thread(self):
        """启动会话清理线程"""
        def cleanup_loop():
            while True:
                time.sleep(self._session_cleanup_interval)
                self._cleanup_expired_sessions()
        
        thread = threading.Thread(target=cleanup_loop, daemon=True)
        thread.start()

    def _cleanup_expired_sessions(self):
        """清理过期会话域"""
        now = time.time()
        with self._session_domains_lock:
            expired = [sid for sid, data in self._session_domains.items() 
                       if now - data.get("last_access", 0) > self._session_max_age]
            for sid in expired:
                del self._session_domains[sid]

    def _get_type_name(self, annotation) -> str:
        if annotation == inspect.Parameter.empty:
            return "any"
        if isinstance(annotation, str):
            return annotation
        if hasattr(annotation, '__name__'):
            return annotation.__name__
        origin = get_origin(annotation)
        if origin is not None:
            origin_name = origin.__name__ if hasattr(origin, '__name__') else str(origin)
            args = get_args(annotation)
            if args:
                arg_names = [self._get_type_name(arg) for arg in args]
                return f"{origin_name}[{', '.join(arg_names)}]"
            return origin_name
        return str(annotation)

    def _clean_docstring(self, docstring: str) -> str:
        """清洗 docstring，去除多余空格和换行"""
        if not docstring:
            return ""
        lines = docstring.strip().split('\n')
        cleaned_lines = []
        for line in lines:
            cleaned_lines.append(line.strip())
        return ' '.join(cleaned_lines).replace('  ', ' ')

    def register_tool(self, func: Callable = None, *, timeout: int = 60) -> Callable:
        def decorator(fn: Callable) -> Callable:
            sig = inspect.signature(fn)
            parameters = {}
            for name, param in sig.parameters.items():
                try:
                    type_name = self._get_type_name(param.annotation)
                except Exception:
                    type_name = "any"
                parameters[name] = {
                    "type": type_name,
                    "default": param.default if param.default != inspect.Parameter.empty else "必填"
                }
            path_list = getattr(fn, '_path_params', [])
            cleaned_doc = self._clean_docstring(fn.__doc__)
            self._registry[fn.__name__] = {
                "function": fn,
                "description": cleaned_doc,
                "parameters": parameters,
                "path_params": path_list,
                "timeout": timeout
            }
            return fn
        
        if func is not None:
            return decorator(func)
        return decorator
    
    def path_params(self, *params: str) -> Callable:
        valid_params = []
        seen = set()
        for param in params:
            if isinstance(param, str) and param:
                if param not in seen:
                    seen.add(param)
                    valid_params.append(param)
        
        def decorator(func: Callable) -> Callable:
            func._path_params = valid_params
            return func
        return decorator
    
    def get_tool(self, name: str) -> Optional[Dict[str, Any]]:
        return self._registry.get(name)
    
    def list_tools(self) -> Dict[str, Dict[str, Any]]:
        result = {}
        for name, tool_info in self._registry.items():
            result[name] = {
                "function": tool_info["function"],
                "description": tool_info["description"],
                "parameters": copy.deepcopy(tool_info["parameters"]),
                "path_params": list(tool_info["path_params"]),
                "timeout": tool_info["timeout"]
            }
        return result
    
    def add_domain_to_session(self, session_id: str, domain: str):
        with self._session_domains_lock:
            if session_id not in self._session_domains:
                self._session_domains[session_id] = {"domains": set(), "last_access": time.time()}
            self._session_domains[session_id]["domains"].add(domain)
            self._session_domains[session_id]["last_access"] = time.time()
    
    def get_session_domains(self, session_id: str) -> Set[str]:
        with self._session_domains_lock:
            data = self._session_domains.get(session_id)
            if data:
                data["last_access"] = time.time()
                return data["domains"].copy()
            return set()
    
    def clear_session_domains(self, session_id: str):
        with self._session_domains_lock:
            if session_id in self._session_domains:
                del self._session_domains[session_id]
    
    def _load_tools_config(self) -> Dict[str, Any]:
        if not self._tools_config_path.exists():
            return {"always_inject": [], "tools": {}, "domains": {}, "personas": {}}
        
        try:
            current_mtime = self._tools_config_path.stat().st_mtime
        except OSError:
            current_mtime = 0.0
        
        with self._config_cache_lock:
            if self._tools_config_cache is not None and abs(current_mtime - self._tools_config_mtime) < 0.001:
                return self._tools_config_cache
            
            try:
                with open(self._tools_config_path, "r", encoding="utf-8") as f:
                    parsed = yaml.safe_load(f)
                
                if not parsed or not isinstance(parsed, dict):
                    raise ValueError("tools.yaml 内容为空或非字典")
                if "domains" not in parsed:
                    raise ValueError("tools.yaml 缺少 'domains' 键")
                
                self._tools_config_cache = parsed
                self._tools_config_mtime = current_mtime
                return self._tools_config_cache
            except (yaml.YAMLError, OSError, ValueError) as e:
                logger.error(f"加载 tools.yaml 失败: {e}")
                if self._tools_config_cache is not None:
                    return self._tools_config_cache
                return {"always_inject": [], "tools": {}, "domains": {}, "personas": {}}
    
    def _load_agent_tools_config(self) -> Dict[str, Any]:
        if not self._agent_tools_yaml.exists():
            self._agent_tools_config_cache = {"domains": {}}
            return self._agent_tools_config_cache
        
        try:
            current_mtime = self._agent_tools_yaml.stat().st_mtime
        except OSError:
            current_mtime = 0.0
        
        with self._config_cache_lock:
            if self._agent_tools_config_cache is not None and abs(current_mtime - self._agent_tools_config_mtime) < 0.001:
                return self._agent_tools_config_cache
            
            try:
                with open(self._agent_tools_yaml, "r", encoding="utf-8") as f:
                    parsed = yaml.safe_load(f)
                if not parsed:
                    parsed = {"domains": {}}
                self._agent_tools_config_cache = parsed
                self._agent_tools_config_mtime = current_mtime
                return self._agent_tools_config_cache
            except (yaml.YAMLError, OSError) as e:
                logger.error(f"加载 agent_tools.yaml 失败: {e}")
                if self._agent_tools_config_cache is not None:
                    return self._agent_tools_config_cache
                return {"domains": {}}
    
    def _load_prompt(self, filename: str) -> str:
        path = self._prompts_dir / filename
        if not path.exists():
            path = self._agent_skills_dir / filename
        
        if not path.exists():
            logger.warning(f"prompt 文件不存在: {filename}")
            return ""
        
        mtime = path.stat().st_mtime
        cache_key = f"_file_{str(path)}_{mtime}"
        
        with self._prompt_cache_lock:
            if cache_key in self._prompt_cache:
                self._prompt_cache_order.remove(cache_key)
                self._prompt_cache_order.append(cache_key)
                return self._prompt_cache[cache_key]
            
            text = path.read_text(encoding="utf-8").strip()
            
            if len(self._prompt_cache) >= self._prompt_cache_max_size:
                oldest_key = self._prompt_cache_order.pop(0)
                del self._prompt_cache[oldest_key]
            
            self._prompt_cache[cache_key] = text
            self._prompt_cache_order.append(cache_key)
            return text


tool_registry = ToolRegistry()