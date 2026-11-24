"""LLM 客户端模块：模型配置加载、API Key 管理、Ollama/DeepSeek 调用与流式响应封装。"""
import os
import json
import re
import time
import threading
import datetime
from typing import Dict, List, Any, Generator, Optional
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

import requests
from memory import log_trace
from logger import get_logger, get_llm_request_logger
logger = get_logger()
_llm_req_log = get_llm_request_logger()

try:
    import keyring
except ImportError:  # CI / 最小安装环境没有 keyring 时降级为环境变量 + models.json
    keyring = None

from core.streaming import parse_reasoning_trace, _build_context_memory_summary, strip_code_blocks, inline_compress, strip_temp_system_messages
from utils.error_mapper import translate_error
from config.settings import DEEPSEEK_BASE_URL, OLLAMA_HOST, DEEPSEEK_MODEL_NAMES

DATA_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'smartDesktop')
KEYRING_SERVICE = "smartDesktop"
KEYRING_USERNAME = "api_key"

_KEYRING_AVAILABLE = None
DEFAULT_TEMPERATURE = 0.25

# ── LLM 日志辅助函数 ──

def _brief_tool_names(tools) -> str:
    """返回工具名的简要列表字符串。"""
    if not tools:
        return "[]"
    names = [t.get("function", {}).get("name", "?") for t in tools if isinstance(t, dict)]
    return json.dumps(names, ensure_ascii=False)


def _mask_api_keys(obj: dict) -> dict:
    """递归屏蔽字典中的 API key 类字段。"""
    if not isinstance(obj, dict):
        return obj
    masked = {}
    for k, v in obj.items():
        if k.lower() in ("api_key", "apikey", "authorization", "token", "secret"):
            masked[k] = "***"
        elif isinstance(v, dict):
            masked[k] = _mask_api_keys(v)
        elif isinstance(v, list):
            masked[k] = [_mask_api_keys(i) if isinstance(i, dict) else i for i in v]
        else:
            masked[k] = v
    return masked


def _safe_serialize_messages(messages) -> str:
    """序列化 messages 列表,屏蔽 API key,截断过长内容。"""
    try:
        msgs = []
        for m in (messages or [])[-50:]:  # 最多保留最近 50 条
            safe = _mask_api_keys(dict(m)) if isinstance(m, dict) else {"role": "?", "content": str(m)[:200]}
            content = safe.get("content", "")
            if isinstance(content, str) and len(content) > 2000:
                safe["content"] = content[:2000] + f"...[truncated, {len(content)} chars]"
            msgs.append(safe)
        return json.dumps(msgs, ensure_ascii=False, default=str)
    except Exception:
        return f"[序列化失败: {len(messages) if messages else 0} 条消息]"


def _brief_result(result) -> str:
    """返回 LLM 响应的简要摘要。"""
    if result is None:
        return "None"
    if isinstance(result, dict):
        content = result.get("content") or result.get("message") or ""
        tc = result.get("tool_calls", [])
        tc_names = [c.get("function", {}).get("name", "?") for c in tc] if tc else []
        s = f"len={len(str(content))}"
        if tc_names:
            s += f" tool_calls={tc_names}"
        if result.get("error"):
            s += f" error={str(result['error'])[:100]}"
        return s
    s = str(result)
    return f"len={len(s)} preview={s[:200]}"


def _is_keyring_available():
    """探测系统 keyring 是否可用，结果缓存在全局变量中。"""
    global _KEYRING_AVAILABLE
    if keyring is None:
        _KEYRING_AVAILABLE = False
        return False
    if _KEYRING_AVAILABLE is None:
        try:
            keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
            _KEYRING_AVAILABLE = True
        except Exception:
            _KEYRING_AVAILABLE = False
    return _KEYRING_AVAILABLE


def _try_keyring_get(model_id=None):
    """尝试从 keyring 读取 API Key，失败返回 None。"""
    if not _is_keyring_available():
        return None
    try:
        username = KEYRING_USERNAME if model_id is None else f"{KEYRING_USERNAME}_{model_id}"
        return keyring.get_password(KEYRING_SERVICE, username)
    except Exception:
        return None


def _try_keyring_set(api_key, model_id=None):
    """尝试将 API Key 写入 keyring，成功返回 True。"""
    if not _is_keyring_available():
        return False
    try:
        username = KEYRING_USERNAME if model_id is None else f"{KEYRING_USERNAME}_{model_id}"
        keyring.set_password(KEYRING_SERVICE, username, api_key)
        return True
    except Exception:
        return False


def load_config():
    """加载配置:模型配置从 models.json 读取,API Key 从环境变量读取"""
    config = {"backend": "deepseek", "model": "deepseek-chat", "models": {}, "base_url": DEEPSEEK_BASE_URL}

    # 从 models.json 读取模型配置
    try:
        models_json = os.path.join(DATA_DIR, "models.json")
        if os.path.exists(models_json):
            with open(models_json, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            for m in cfg.get("models", []):
                m_id = m.get("id", m.get("model", ""))
                if m_id:
                    config["models"][m_id] = {
                        "provider": m.get("provider", "deepseek"),
                        "model": m.get("model", "deepseek-chat"),
                        "base_url": m.get("base_url", DEEPSEEK_BASE_URL),
                        "api_version": m.get("api_version", "v1"),
                        "temperature": m.get("temperature", DEFAULT_TEMPERATURE),
                        "api_key": m.get("api_key", ""),
                    }
            # 设置默认 backend/model/base_url 为 active model
            active_id = cfg.get("active_model_id", "")
            if active_id and active_id in config["models"]:
                active = config["models"][active_id]
                config["backend"] = active["provider"]
                config["model"] = active["model"]
                config["base_url"] = active["base_url"]
    except Exception as e:
        logger.warning(f"加载 models.json 失败: {e}")

    # API Key 仅从环境变量读取
    env_key = os.environ.get("SMARTDESKTOP_API_KEY") or os.environ.get("API_KEY")
    if env_key:
        config["api_key"] = env_key

    return config


def _load_config():
    """加载配置(每次重新读取,不缓存,支持运行时 env 覆盖)"""
    config = load_config()
    # 环境变量覆盖(运行时可能变化)
    if os.environ.get("SMARTDESKTOP_API_KEY"):
        config["api_key"] = os.environ["SMARTDESKTOP_API_KEY"]
    elif os.environ.get("API_KEY"):
        config["api_key"] = os.environ["API_KEY"]
    if os.environ.get("API_BASE_URL"):
        config["base_url"] = os.environ["API_BASE_URL"]
    if os.environ.get("API_MODEL"):
        config["model"] = os.environ["API_MODEL"]
    return config


def get_config():
    """返回当前加载的模型配置副本（含运行时环境变量覆盖）。"""
    return dict(_load_config())


def get_api_key(model_id=None):
    """获取 API Key:环境变量 → models.json 模型配置 → keyring"""
    # 环境变量优先
    env_key = os.environ.get("SMARTDESKTOP_API_KEY") or os.environ.get("API_KEY")
    if env_key:
        return env_key

    config = _load_config()
    if model_id and config.get("models") and config["models"].get(model_id):
        model_cfg = config["models"][model_id]
        if model_cfg.get("api_key"):
            return model_cfg["api_key"]

    # 从 keyring 读取(兼容旧配置)
    stored = _try_keyring_get(model_id)
    if stored:
        return stored

    return ""


def get_base_url(model_id=None):
    """获取 Base URL:models.json 模型配置 → 默认值"""
    config = _load_config()
    if model_id and config.get("models") and config["models"].get(model_id):
        return config["models"][model_id].get("base_url", DEEPSEEK_BASE_URL)
    return config.get("base_url", DEEPSEEK_BASE_URL)


def get_model_config(model_id):
    """获取模型配置:优先从 _load_config 缓存,兜底从 models.json 读取"""
    config = _load_config()
    if config.get("models") and config["models"].get(model_id):
        return config["models"][model_id]
    # Fallback: read from models.json directly
    try:
        models_json = os.path.join(DATA_DIR, "models.json")
        if os.path.exists(models_json):
            with open(models_json, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            for m in cfg.get("models", []):
                if m.get("id") == model_id:
                    return m
    except Exception:
        pass
    return None


def invalidate_config_cache():
    """保留兼容性空函数(不再需要缓存失效)"""
    pass


def save_config(backend, api_key=None, model="deepseek-chat", base_url=DEEPSEEK_BASE_URL, model_id=None):
    """保存 API Key 到 keyring(模型配置通过 models.json / 前端页面管理)"""
    if backend != "ollama" and api_key:
        _try_keyring_set(api_key, model_id)
    logger.info(f"配置已保存(API Key → 系统凭据管理器)")


def _save_config_raw(backend, api_key=None, model="deepseek-chat", base_url=DEEPSEEK_BASE_URL):
    """将后端配置持久化（转发到 save_config 实现）。"""
    save_config(backend, api_key, model, base_url)

_session = requests.Session()
_retry_strategy = Retry(
    total=3,
    connect=3,
    read=2,
    backoff_factor=1,
    # 429 为配额类错误，重试无益且会放大单次 timeout（如 8s×3 次重试≈28s 卡顿）。
    # 移除 429：遇限流直接抛 HTTPError，由调用方快速降级（与 stream 路径一致）。
    status_forcelist=[500, 502, 503, 504],
    allowed_methods=["POST"],
    raise_on_status=True
)
_session.mount("http://", HTTPAdapter(max_retries=_retry_strategy))
_session.mount("https://", HTTPAdapter(max_retries=_retry_strategy))


def _assert_system_first(messages, caller="llm"):
    """P0 校验:messages 中存在 system 角色时,messages[0] 必须是 system。

    进度摘要等辅助调用只传 user 消息(无 system),直接跳过,避免误伤。
    主对话 LLM 调用路径(含 system prompt)若注入位置错误则记录错误日志并触发断言。
    """
    if not messages:
        return
    has_system = any(isinstance(m, dict) and m.get("role") == "system" for m in messages)
    if not has_system:
        return
    first_role = messages[0].get("role") if isinstance(messages[0], dict) else None
    if first_role != "system":
        logger.error(
            f"[{caller}] system prompt 注入位置错误:messages[0].role={first_role!r},"
            f"要求第一行必须为 system(P0 修复)"
        )
        assert first_role == "system", (
            f"[{caller}] messages[0] 必须是 system 角色,实际为 {first_role!r}"
        )


def _normalize_base_url(base_url):
    """归一化 Base URL：去除末尾斜杠与接口路径后缀，仅保留服务根地址。"""
    url = base_url.rstrip('/')
    url = re.sub(r'/v\d+/chat/completions/?$', '', url)
    url = re.sub(r'/chat/completions/?$', '', url)
    return url


def _map_response_format(response_format):
    """将 OpenAI 风格 response_format 映射为内部表示（仅支持 json_object→json）。"""
    if not response_format:
        return None
    fmt_type = response_format.get("type", "")
    if fmt_type == "json_object":
        return "json"
    return fmt_type


def ai_concat(messages, model_name=None, provider=None, trace_id="",
              tools=None, response_format=None, base_url_override=None, model_id=None, temperature=None,
              timeout=None):
    """统一 AI 调用入口:Ollama 走专属逻辑,其余走通用 OpenAI 兼容协议

    Args:
        timeout: 非流式 OpenAI 兼容调用的超时秒数(默认 None 走 _openai_compatible_call 内置 60s)。
                 调用方可传短超时实现快速降级(如复杂度判断 timeout=8)。
    """
    try:
        get_logger().debug(f"[ai_concat] 开始调用", trace_id=trace_id, model_id=model_id,
                          provider=provider, messages_count=len(messages) if messages else 0)

        config = _load_config()

        model_cfg = get_model_from_cache(model_id) if model_id else None

        # 路由优先级:显式传参 > models.json 的 provider > config.yaml backend
        if provider:
            backend = provider.lower().strip()
        elif model_cfg:
            backend = model_cfg.get("provider", "deepseek").lower().strip()
        else:
            backend = config.get("backend", "deepseek").lower().strip()

        if backend == "ollama":
            model = model_name or (model_cfg.get("model") if model_cfg else None) or config.get("model", "deepseek-r1:7b")
            resolved_temperature = temperature if temperature is not None else (model_cfg.get("temperature", DEFAULT_TEMPERATURE) if model_cfg else DEFAULT_TEMPERATURE)
            get_logger().debug(f"[ai_concat] Ollama调用", trace_id=trace_id, model=model, temperature=resolved_temperature)

            # ★ LLM 请求日志钩子
            _llm_req_log.info(f"[LLM_REQ] provider=ollama model={model} trace_id={trace_id} "
                             f"temp={resolved_temperature} msgs={len(messages)} "
                             f"tools={_brief_tool_names(tools)}")
            _llm_req_log.debug(f"[LLM_REQ_MSGS] {_safe_serialize_messages(messages)}")
            try:
                result = _ollama_call(messages, model, trace_id=trace_id,
                                     tools=tools, response_format=response_format, temperature=resolved_temperature)
                _llm_req_log.info(f"[LLM_RESP] provider=ollama model={model} trace_id={trace_id} "
                                 f"content={_brief_result(result)}")
                return result
            except Exception:
                _llm_req_log.error(f"[LLM_RESP] provider=ollama model={model} trace_id={trace_id} ERROR", exc_info=True)
                raise

        else:
            resolved_model = model_name or (model_cfg.get("model") if model_cfg else None) or config.get("model", "deepseek-chat")
            resolved_api_key = get_api_key(model_id)

            if model_cfg:
                resolved_base_url = base_url_override or model_cfg.get("base_url", DEEPSEEK_BASE_URL)
                resolved_api_version = model_cfg.get("api_version", "v1")
                resolved_temperature = temperature if temperature is not None else model_cfg.get("temperature", DEFAULT_TEMPERATURE)
            else:
                resolved_base_url = base_url_override or get_base_url(model_id)
                resolved_api_version = "v1"
                if model_id and config.get("models") and config["models"].get(model_id):
                    resolved_api_version = config["models"][model_id].get("api_version", "v1")
                resolved_temperature = temperature if temperature is not None else DEFAULT_TEMPERATURE

            if not resolved_base_url:
                raise ValueError(f"无法解析 Base URL,请检查 model_id={model_id} 配置或传入 base_url_override")

            get_logger().debug(f"[ai_concat] OpenAI兼容调用", trace_id=trace_id, model=resolved_model,
                              base_url=resolved_base_url, api_version=resolved_api_version,
                              temperature=resolved_temperature, api_key_set=bool(resolved_api_key))

            # ★ LLM 请求日志钩子
            _llm_req_log.info(f"[LLM_REQ] provider={backend} model={resolved_model} trace_id={trace_id} "
                             f"base_url={resolved_base_url} temp={resolved_temperature} msgs={len(messages)} "
                             f"tools={_brief_tool_names(tools)}")
            _llm_req_log.debug(f"[LLM_REQ_MSGS] {_safe_serialize_messages(messages)}")
            try:
                result = _openai_compatible_call(
                    messages, resolved_model, resolved_api_key, resolved_base_url,
                    provider=backend, trace_id=trace_id, tools=tools,
                    response_format=response_format, api_version=resolved_api_version,
                    temperature=resolved_temperature,
                    timeout=timeout
                )
                _llm_req_log.info(f"[LLM_RESP] provider={backend} model={resolved_model} trace_id={trace_id} "
                                 f"content={_brief_result(result)}")
                return result
            except Exception:
                _llm_req_log.error(f"[LLM_RESP] provider={backend} model={resolved_model} trace_id={trace_id} ERROR",
                                   exc_info=True)
                raise

    except Exception as e:
        get_logger().error(f"[ai_concat] AI调用初始化失败: {e}", trace_id=trace_id, model_id=model_id,
                          exc_info=True)
        raise RuntimeError(f"AI服务不可用: {e}") from e


class OllamaAPIError(RuntimeError):
    """Ollama API 返回 4xx/5xx 时显式上抛,禁止静默吞没为空 content。

    status_code / body 保留给上层诊断;与旧版"返回空 content + error 字段"
    的静默降级行为不同,调用方可在 except 中捕获并如实呈现给用户。
    """

    def __init__(self, message, status_code=None, body=None):
        """初始化 Ollama 调用异常，携带状态码与响应体。"""
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _normalize_ollama_tools(tools):
    """将 OpenAI 方言 tools 规范化为 Ollama /api/chat 期望的结构。

    背景:Ollama 要求每个 tool 的 function 至少含 name / description /
    parameters,且 parameters 必须是 JSON Schema 对象(type="object" 且带
    properties)。直接透传 OpenAI 风格 tools 时,若缺少 description、
    parameters 为空/字符串/不含 properties,Qwen3.5-9B 本地模型的工具调用
    请求会被 Ollama 以 400 Bad Request 拒绝(对应 T-25/T-30/T-31/T-32/T-34
    与 T-44/T-45/T-47 的根因)。

    本函数做结构补全与兜底,不改变工具语义,只保证请求格式合法。
    """
    if not tools:
        return tools
    normalized = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") or {}
        name = fn.get("name") or t.get("name") or ""
        if not name:
            continue
        parameters = fn.get("parameters") or {}
        if isinstance(parameters, str):
            try:
                parameters = json.loads(parameters)
            except (json.JSONDecodeError, TypeError):
                parameters = {"type": "object", "properties": {}}
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}}
        if "type" not in parameters:
            parameters["type"] = "object"
        parameters.setdefault("properties", {})
        normalized.append({
            "type": "function",
            "function": {
                "name": name,
                "description": fn.get("description") or t.get("description") or name,
                "parameters": parameters,
            },
        })
    return normalized


def _ollama_call(messages, model, trace_id="", tools=None, response_format=None, temperature=None):
    """调用本地 Ollama 非流式接口，返回含 content/reasoning_content 的结果字典。"""
    _assert_system_first(messages, caller="ollama")
    url = f"{OLLAMA_HOST}/api/chat"

    # 防御性检查:拒绝把 DeepSeek 在线模型名错误路由到本地 Ollama
    if model and model in DEEPSEEK_MODEL_NAMES:
        msg = f"拒绝路由:模型 '{model}' 是 DeepSeek 在线模型,不应调用本地 Ollama。请检查 model_choice.provider 配置。"
        get_logger().error(msg, trace_id=trace_id)
        raise ValueError(msg)

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature}
    }
    if tools:
        payload["tools"] = _normalize_ollama_tools(tools)
    fmt = _map_response_format(response_format)
    if fmt:
        payload["format"] = fmt
    t0 = time.perf_counter()
    try:
        response = _session.post(url, json=payload, timeout=(10, 120))
        response.raise_for_status()
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        result = response.json()
        msg = result.get("message", {})
        reasoning = msg.get("reasoning_content", "")
        content = msg.get("content", "") or ""
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                fn = tc.get("function", {})
                tool_name = fn.get("name", "")
                args_str = fn.get("arguments", "{}")
                if isinstance(args_str, dict):
                    args_obj = args_str
                else:
                    try:
                        args_obj = json.loads(args_str) if args_str else {}
                    except (json.JSONDecodeError, TypeError):
                        args_obj = {"raw": str(args_str)}
                tool_call_json = json.dumps({"tool_name": tool_name, "arguments": args_obj}, ensure_ascii=False)
                if content and not content.endswith("\n"):
                    content += "\n"
                content += f"```action\n{tool_call_json}\n```"
        output_summary = str(content)[:200]
        log_trace(
            trace_id=trace_id, span_name="llm.ollama", node_type="llm",
            duration_ms=elapsed_ms, provider="ollama", model_name=model,
            model_params=f'{{"temperature":{temperature}}}', status="success",
            input_summary=str(messages)[:200],
            output_summary=output_summary
        )
        # LLM 调用日志兜底记录
        try:
            get_logger().log_llm_call(
                trace_id=trace_id, session_id="", turn=0, child_turn=0,
                user_task="[llm_client直接调用]",
                response=content,
                model_name=model, provider="ollama",
                elapsed_ms=elapsed_ms,
                messages=messages,
            )
        except Exception:
            pass
        return {"message": {"content": content, "reasoning_content": reasoning}}
    except Exception as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        error_detail = str(e)
        error_body = ""
        status_code = None
        try:
            if 'response' in locals():
                status_code = getattr(response, "status_code", None)
                error_body = response.text[:500]
                error_detail = f"{e} | body: {error_body}"
        except Exception:
            pass
        log_trace(
            trace_id=trace_id, span_name="llm.ollama", node_type="llm",
            duration_ms=elapsed_ms, provider="ollama", model_name=model,
            model_params=f'{{"temperature":{temperature}}}', status="error",
            error=error_detail[:500]
        )
        total_chars = sum(len(m.get("content", "")) for m in messages)
        logger.error(f"Ollama 请求失败: {error_detail[:300]} | messages={len(messages)}条, 总计{total_chars}字符")
        # LLM 调用日志兜底记录(非流式失败)
        try:
            get_logger().log_llm_call(
                trace_id=trace_id, session_id="", turn=0, child_turn=0,
                user_task="[llm_client直接调用]",
                response=f"[LLM调用失败] {error_detail}",
                model_name=model, provider="ollama",
                elapsed_ms=elapsed_ms,
                messages=messages,
            )
        except Exception:
            pass
        # 显式上抛 4xx/5xx:禁止静默吞没为"空 content",便于上层如实呈现错误
        if status_code is not None and status_code >= 400:
            raise OllamaAPIError(
                f"Ollama 请求失败(HTTP {status_code}): {error_detail[:300]}",
                status_code=status_code, body=error_body,
            ) from e
        # 网络/超时等非 HTTP 错误保持旧行为(返回 error dict),避免破坏既有降级
        return {"message": {"content": "", "error": error_detail, "reasoning_content": ""}}


def _ollama_call_stream(messages, model, trace_id="", tools=None, response_format=None, temperature=DEFAULT_TEMPERATURE, stream=True):
    """调用本地 Ollama 接口，逐块产出 (type, data) 事件。

    stream=False 时发起非流式请求，一次性产出 result 事件（协议不变）。
    """
    _assert_system_first(messages, caller="ollama_stream")
    url = f"{OLLAMA_HOST}/api/chat"

    # 防御性检查:拒绝把 DeepSeek 在线模型名错误路由到本地 Ollama
    if model and model in DEEPSEEK_MODEL_NAMES:
        msg = f"拒绝路由:模型 '{model}' 是 DeepSeek 在线模型,不应调用本地 Ollama。请检查 model_choice.provider 配置。"
        get_logger().error(msg, trace_id=trace_id)
        raise ValueError(msg)

    payload = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "options": {"temperature": temperature},
    }
    if tools:
        payload["tools"] = _normalize_ollama_tools(tools)
    fmt = _map_response_format(response_format)
    if fmt:
        payload["format"] = fmt
    t0 = time.perf_counter()
    try:
        response = _session.post(url, json=payload, stream=stream, timeout=(30,120))
        response.raise_for_status()

        if not stream:
            data = response.json()
            msg_chunk = data.get("message", {})
            full_content = msg_chunk.get("content", "") or ""
            full_reasoning = msg_chunk.get("reasoning_content", "") or ""
            raw_tool_calls = msg_chunk.get("tool_calls") or []
            native_tool_calls = []
            if raw_tool_calls:
                action_lines = []
                for i, tc in enumerate(raw_tool_calls):
                    fn = tc.get("function", {})
                    fn_name = fn.get("name", "")
                    fn_args = fn.get("arguments", "")
                    if isinstance(fn_args, dict):
                        fn_args = json.dumps(fn_args, ensure_ascii=False)
                    action_lines.append(json.dumps({"tool_name": fn_name, "arguments": fn_args}, ensure_ascii=False))
                    native_tool_calls.append({
                        "id": tc.get("id") or f"call_{i}_{int(time.time())}",
                        "type": "function",
                        "function": {"name": fn_name, "arguments": fn_args},
                    })
                if full_content and not full_content.endswith("\n"):
                    full_content += "\n"
                full_content += "\n".join(f"```action\n{a}\n```" for a in action_lines)
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            log_trace(
                trace_id=trace_id, span_name="llm.ollama", node_type="llm",
                duration_ms=elapsed_ms, provider="ollama", model_name=model,
                model_params=f'{{"temperature":{temperature},"stream":false}}', status="success",
                input_summary=str(messages)[:200],
                output_summary=full_content[:200]
            )
            yield {"type": "result", "content": full_content, "reasoning_content": full_reasoning, "tool_calls": native_tool_calls}
            return

        full_content = ""
        full_reasoning = ""
        accumulated_tool_calls = {}

        for line in response.iter_lines(decode_unicode=True):
            if not line.strip():
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_chunk = chunk.get("message", {})
            content_chunk = msg_chunk.get("content", "")
            reasoning_chunk = msg_chunk.get("reasoning_content", "")

            if content_chunk:
                full_content += content_chunk
            if reasoning_chunk:
                full_reasoning += reasoning_chunk

            delta_tool_calls = msg_chunk.get("tool_calls")
            if delta_tool_calls:
                for i, tc in enumerate(delta_tool_calls):
                    idx = tc.get("index", i)
                    if idx not in accumulated_tool_calls:
                        accumulated_tool_calls[idx] = {"name": "", "arguments": ""}
                    fn = tc.get("function", {})
                    if fn.get("name"):
                        accumulated_tool_calls[idx]["name"] = fn["name"]
                    if fn.get("arguments"):
                        args_val = fn["arguments"]
                        if isinstance(args_val, str):
                            accumulated_tool_calls[idx]["arguments"] += args_val
                        elif isinstance(args_val, dict):
                            accumulated_tool_calls[idx]["arguments"] += json.dumps(args_val, ensure_ascii=False)

            yield {"type": "chunk", "content": content_chunk, "reasoning": reasoning_chunk}

            if chunk.get("done"):
                break

        if accumulated_tool_calls:
            for idx in sorted(accumulated_tool_calls.keys()):
                tc = accumulated_tool_calls[idx]
                tool_name = tc.get("name", "")
                args_str = tc.get("arguments", "{}")
                try:
                    args_obj = json.loads(args_str) if args_str else {}
                except json.JSONDecodeError:
                    args_obj = {"raw": args_str}
                tool_call_json = json.dumps({"tool_name": tool_name, "arguments": args_obj}, ensure_ascii=False)
                if full_content and not full_content.endswith("\n"):
                    full_content += "\n"
                full_content += f"```action\n{tool_call_json}\n```"
            # 原生 tool_calls 结构
            native_tool_calls = []
            for idx in sorted(accumulated_tool_calls.keys()):
                tc = accumulated_tool_calls[idx]
                native_tool_calls.append({
                    "id": f"call_{idx}_{int(time.time())}",
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc["arguments"]
                    }
                })
        else:
            native_tool_calls = []

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        log_trace(
            trace_id=trace_id, span_name="llm.ollama", node_type="llm",
            duration_ms=elapsed_ms, provider="ollama", model_name=model,
            model_params=f'{{"temperature":{temperature},"stream":true}}', status="success",
            input_summary=str(messages)[:200],
            output_summary=full_content[:200]
        )
        # LLM 调用日志兜底记录(流式入口,防止绕过 react_engine 的流式调用漏记)
        try:
            get_logger().log_llm_call(
                trace_id=trace_id, session_id="", turn=0, child_turn=0,
                user_task="[llm_client直接调用]",
                response=full_content,
                model_name=model, provider="ollama",
                elapsed_ms=elapsed_ms,
                reasoning=full_reasoning,
                messages=messages,
            )
        except Exception:
            pass
        yield {"type": "result", "content": full_content, "reasoning_content": full_reasoning, "tool_calls": native_tool_calls}
    except Exception as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        error_detail = str(e)
        status_code = None
        error_body = ""
        try:
            if 'response' in locals() and response is not None:
                status_code = getattr(response, "status_code", None)
                error_body = response.text[:500]
                error_detail = f"{e} | body: {error_body}"
        except Exception:
            pass
        log_trace(
            trace_id=trace_id, span_name="llm.ollama", node_type="llm",
            duration_ms=elapsed_ms, provider="ollama", model_name=model,
            model_params=f'{{"temperature":{temperature},"stream":{stream}}}', status="error",
            error=error_detail[:500]
        )
        # LLM 调用日志兜底记录(流式失败)
        try:
            get_logger().log_llm_call(
                trace_id=trace_id, session_id="", turn=0, child_turn=0,
                user_task="[llm_client直接调用]",
                response=f"[LLM调用失败] {str(e)}",
                model_name=model, provider="ollama",
                elapsed_ms=elapsed_ms,
                messages=messages,
            )
        except Exception:
            pass
        logger.error(f"Ollama 流式请求失败: {e}")
        # 4xx/5xx 显式上报状态码与响应体,供上层识别 Ollama 400 类故障
        if status_code is not None and status_code >= 400:
            yield {"type": "error", "error": error_detail[:500], "status_code": status_code, "ollama_error": True}
            return
        yield {"type": "error", "error": str(e)}


def _openai_compatible_call(messages, model, api_key, base_url,
                            provider="openai_compatible", trace_id="",
                            tools=None, response_format=None, api_version="v1", temperature=DEFAULT_TEMPERATURE,
                            timeout=None):
    """通用 OpenAI 兼容接口调用(DeepSeek / 豆包 / 通义等均适用)

    Args:
        timeout: 非流式请求超时秒数。默认 None 时保持原行为(60s);
                 传短超时(如 8)时超时抛 requests.exceptions.Timeout,由调用方降级。
    """
    if not api_key:
        logger.error(f"{provider} API Key 为空,无法发起调用(model={model}, base_url={base_url})")
        return {"message": {"content": "", "error": f"{provider} API Key 未配置,请在设置中添加 API Key", "reasoning_content": ""}}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 4096,
        "stream": False
    }
    if tools:
        payload["tools"] = tools
    if response_format:
        payload["response_format"] = response_format

    normalized_url = _normalize_base_url(base_url)
    t0 = time.perf_counter()
    try:
        _timeout = timeout if timeout is not None else 60
        response = _session.post(f"{normalized_url}/{api_version}/chat/completions",
                                headers=headers, json=payload, timeout=_timeout)
        response.raise_for_status()
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            raise ValueError("API 返回为空")

        msg = choices[0].get("message", {})
        content = msg.get("content", "") or ""
        reasoning = msg.get("reasoning_content", "")
        tool_calls = msg.get("tool_calls")

        if tool_calls:
            for tc in tool_calls:
                fn = tc.get("function", {})
                tool_name = fn.get("name", "")
                args_str = fn.get("arguments", "{}")
                try:
                    args_obj = json.loads(args_str) if args_str else {}
                except json.JSONDecodeError:
                    args_obj = {"raw": args_str}
                tool_call_json = json.dumps({"tool_name": tool_name, "arguments": args_obj}, ensure_ascii=False)
                if content and not content.endswith("\n"):
                    content += "\n"
                content += f"```action\n{tool_call_json}\n```"

        log_trace(
            trace_id=trace_id, span_name=f"llm.{provider}", node_type="llm",
            duration_ms=elapsed_ms, provider=provider, model_name=model,
            model_params=f'{{"temperature":{temperature}}}', status="success",
            input_summary=str(messages)[:200],
            output_summary=content[:200]
        )
        # LLM 调用日志兜底记录(修复:llm_client 缺失 log_llm_call 导致日志丢失)
        try:
            usage = data.get("usage", {})
            get_logger().log_llm_call(
                trace_id=trace_id, session_id="", turn=0, child_turn=0,
                user_task="[llm_client直接调用]",
                response=content,
                model_name=model, provider=provider,
                elapsed_ms=elapsed_ms,
                token_estimate=usage.get("total_tokens", 0),
                messages=messages,
            )
        except Exception:
            pass
        return {"message": {"content": content, "reasoning_content": reasoning}}

    except Exception as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        error_detail = str(e)
        try:
            if 'response' in locals():
                error_body = response.text[:500]
                error_detail = f"{e} | body: {error_body}"
        except Exception:
            pass
        log_trace(
            trace_id=trace_id, span_name=f"llm.{provider}", node_type="llm",
            duration_ms=elapsed_ms, provider=provider, model_name=model,
            model_params=f'{{"temperature":{temperature}}}', status="error",
            error=error_detail[:500]
        )
        total_chars = sum(len(m.get("content", "")) for m in messages)
        logger.error(f"{provider} 请求失败: {error_detail[:300]} | messages={len(messages)}条, 总计{total_chars}字符")
        # LLM 调用日志兜底记录(非流式失败)
        try:
            get_logger().log_llm_call(
                trace_id=trace_id, session_id="", turn=0, child_turn=0,
                user_task="[llm_client直接调用]",
                response=f"[LLM调用失败] {error_detail}",
                model_name=model, provider=provider,
                elapsed_ms=elapsed_ms,
                messages=messages,
            )
        except Exception:
            pass
        return {"message": {"content": "", "error": error_detail, "reasoning_content": ""}}


def _openai_compatible_call_stream(messages, model, api_key, base_url,
                                   provider="openai_compatible", trace_id="",
                                   tools=None, response_format=None, api_version="v1", temperature=DEFAULT_TEMPERATURE, stream=True):
    """通用 OpenAI 兼容调用（流式/非流式）。

    stream=False 时发起非流式请求，一次性产出 result 事件（协议不变）。
    """
    _assert_system_first(messages, caller=provider or "openai_compatible_stream")
    if not api_key:
        logger.error(f"{provider} API Key 为空,无法发起流式调用(model={model}, base_url={base_url})")
        yield {"type": "error", "error": f"{provider} API Key 未配置,请在设置中添加 API Key"}
        return
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 4096,
        "stream": stream,
    }
    if tools:
        payload["tools"] = tools
    if response_format:
        payload["response_format"] = response_format

    normalized_url = _normalize_base_url(base_url)
    t0 = time.perf_counter()
    try:
        response = _session.post(f"{normalized_url}/{api_version}/chat/completions",
                                headers=headers, json=payload, stream=stream, timeout=(30, 120))
        response.raise_for_status()

        if not stream:
            data = response.json()
            choices = data.get("choices") or []
            msg = (choices[0].get("message", {}) if choices else {})
            full_content = msg.get("content", "") or ""
            full_reasoning = msg.get("reasoning_content", "") or ""
            raw_tool_calls = msg.get("tool_calls") or []
            native_tool_calls = []
            if raw_tool_calls:
                action_lines = []
                for i, tc in enumerate(raw_tool_calls):
                    fn = tc.get("function", {})
                    fn_name = fn.get("name", "")
                    fn_args = fn.get("arguments", "")
                    if isinstance(fn_args, dict):
                        fn_args = json.dumps(fn_args, ensure_ascii=False)
                    action_lines.append(json.dumps({"tool_name": fn_name, "arguments": fn_args}, ensure_ascii=False))
                    native_tool_calls.append({
                        "id": tc.get("id") or f"call_{i}_{int(time.time())}",
                        "type": "function",
                        "function": {"name": fn_name, "arguments": fn_args},
                    })
                if full_content and not full_content.endswith("\n"):
                    full_content += "\n"
                full_content += "\n".join(f"```action\n{a}\n```" for a in action_lines)
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            log_trace(
                trace_id=trace_id, span_name=f"llm.{provider}", node_type="llm",
                duration_ms=elapsed_ms, provider=provider, model_name=model,
                model_params=f'{{"temperature":{temperature},"stream":false}}', status="success",
                input_summary=str(messages)[:200],
                output_summary=full_content[:200]
            )
            try:
                get_logger().log_llm_call(
                    trace_id=trace_id, session_id="", turn=0, child_turn=0,
                    user_task="[llm_client直接调用]",
                    response=full_content,
                    model_name=model, provider=provider,
                    elapsed_ms=elapsed_ms,
                    token_estimate=0,
                    reasoning=full_reasoning,
                    messages=messages,
                )
            except Exception:
                pass
            yield {"type": "result", "content": full_content, "reasoning_content": full_reasoning, "tool_calls": native_tool_calls}
            return

        full_content = ""
        full_reasoning = ""
        accumulated_tool_calls = {}
        max_duration = 300

        for line in response.iter_lines(decode_unicode=True):
            if time.perf_counter() - t0 > max_duration:
                raise TimeoutError(f"流式请求超时,超过 {max_duration} 秒")
            if not line.strip() or not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            choices = chunk.get("choices", [])
            if not choices:
                continue
            delta = choices[0].get("delta", {})
            content_chunk = delta.get("content", "")
            reasoning_chunk = delta.get("reasoning_content", "")

            if content_chunk:
                full_content += content_chunk
            if reasoning_chunk:
                full_reasoning += reasoning_chunk

            delta_tool_calls = delta.get("tool_calls")
            if delta_tool_calls:
                for tc in delta_tool_calls:
                    idx = tc.get("index", 0)
                    if idx not in accumulated_tool_calls:
                        accumulated_tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc.get("id"):
                        accumulated_tool_calls[idx]["id"] = tc["id"]
                    fn = tc.get("function", {})
                    if fn.get("name"):
                        accumulated_tool_calls[idx]["name"] = fn["name"]
                    if fn.get("arguments"):
                        accumulated_tool_calls[idx]["arguments"] += fn["arguments"]

            yield {"type": "chunk", "content": content_chunk, "reasoning": reasoning_chunk}

        if accumulated_tool_calls:
            for idx in sorted(accumulated_tool_calls.keys()):
                tc = accumulated_tool_calls[idx]
                tool_name = tc.get("name", "")
                args_str = tc.get("arguments", "{}")
                try:
                    args_obj = json.loads(args_str) if args_str else {}
                except json.JSONDecodeError:
                    args_obj = {"raw": args_str}
                tool_call_json = json.dumps({"tool_name": tool_name, "arguments": args_obj}, ensure_ascii=False)
                if full_content and not full_content.endswith("\n"):
                    full_content += "\n"
                full_content += f"```action\n{tool_call_json}\n```"
            # 原生 tool_calls 结构
            native_tool_calls = []
            for idx in sorted(accumulated_tool_calls.keys()):
                tc = accumulated_tool_calls[idx]
                native_tool_calls.append({
                    "id": tc.get("id") or f"call_{idx}_{int(time.time())}",
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc["arguments"]
                    }
                })
        else:
            native_tool_calls = []

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        log_trace(
            trace_id=trace_id, span_name=f"llm.{provider}", node_type="llm",
            duration_ms=elapsed_ms, provider=provider, model_name=model,
            model_params=f'{{"temperature":{temperature},"stream":true}}', status="success",
            input_summary=str(messages)[:200],
            output_summary=full_content[:200]
        )
        # LLM 调用日志兜底记录(流式入口,防止绕过 react_engine 的流式调用漏记)
        try:
            get_logger().log_llm_call(
                trace_id=trace_id, session_id="", turn=0, child_turn=0,
                user_task="[llm_client直接调用]",
                response=full_content,
                model_name=model, provider=provider,
                elapsed_ms=elapsed_ms,
                token_estimate=0,
                reasoning=full_reasoning,
                messages=messages,
            )
        except Exception:
            pass
        yield {"type": "result", "content": full_content, "reasoning_content": full_reasoning, "tool_calls": native_tool_calls}

    except Exception as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        error_detail = str(e)
        try:
            if 'response' in locals():
                error_body = response.text[:500]
                error_detail = f"{e} | body: {error_body}"
        except Exception:
            pass
        log_trace(
            trace_id=trace_id, span_name=f"llm.{provider}", node_type="llm",
            duration_ms=elapsed_ms, provider=provider, model_name=model,
            model_params=f'{{"temperature":{temperature},"stream":{stream}}}', status="error",
            error=error_detail[:500]
        )
        # LLM 调用日志兜底记录(流式失败)
        try:
            get_logger().log_llm_call(
                trace_id=trace_id, session_id="", turn=0, child_turn=0,
                user_task="[llm_client直接调用]",
                response=f"[LLM调用失败] {str(e)}",
                model_name=model, provider=provider,
                elapsed_ms=elapsed_ms,
                messages=messages,
            )
        except Exception:
            pass
        logger.error(f"{provider} 流式请求失败: {error_detail[:300]}")
        yield {"type": "error", "error": str(e)}


# ═══════════════════════════════════════════════════════════
# 自然语言兜底工具名校验
# Python re 默认 Unicode 模式,\w 会匹配中文,导致"调用任何工具"把"任何"
# 捕获为 tool_name,虚构出不存在的工具调用并陷入幻觉循环。
# 兜底解析提取的工具名必须可信:优先命中调用方传入的白名单,
# 否则要求 ASCII 工具名形态且不在泛指词黑名单内。
# ═══════════════════════════════════════════════════════════
_NL_VAGUE_WORDS = frozenset({
    "任何", "一个", "一些", "某些", "这个", "那个", "这些", "那些",
    "其他", "其它", "别的", "工具", "函数", "一下", "下", "个",
    "python", "shell", "代码", "命令", "脚本", "应用", "软件",
    "系统", "文件", "数据", "信息", "结果", "内容",
})
_TOOL_NAME_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_-]*$')


def _is_valid_tool_name(name, valid_tool_names=None):
    """自然语言兜底提取的工具名是否可信。

    valid_tool_names 提供时(推荐):必须命中白名单;
    未提供时:必须是 ASCII 工具名形态且非泛指词(保守兜底)。
    """
    if not name:
        return False
    if valid_tool_names is not None:
        return name in valid_tool_names
    if name.lower() in _NL_VAGUE_WORDS:
        return False
    return bool(_TOOL_NAME_RE.match(name))


def parse_ai_json(content, valid_tool_names: Optional[set] = None):
    """从 LLM 输出中解析结构化 JSON：依次尝试 action 代码块、json 代码块、全文搜索与自然语言兜底。"""
    from json_repair import loads as repair_loads
    try:
        clean_content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()

        # ── 优先级 1: action 代码块(最可靠的来源) ──
        action_match = re.search(r'```action\s*([\s\S]*?)\s*```', clean_content)
        if action_match:
            extracted = action_match.group(1).strip()
            logger.debug(f"parse_ai_json: 提取action代码块成功,内容长度: {len(extracted)}")
            return _try_parse_json_obj(extracted)

        # ── 优先级 2: json 代码块 ──
        json_match = re.search(r'```json\s*([\s\S]*?)\s*```', clean_content)
        if json_match:
            extracted = json_match.group(1).strip()
            logger.debug(f"parse_ai_json: 提取json代码块成功,内容长度: {len(extracted)}")
            result = _try_parse_json_obj(extracted)
            if result is not None:
                return result
            # json 块解析失败不直接丢弃,继续用全文搜索

        # ── 移除 reasoning 块后,在剩余内容中搜索 JSON ──
        # reasoning 块是 LLM 的思考过程,不应作为 action/final_message 来源
        stripped = re.sub(r'```reasoning\s*[\s\S]*?\s*```', '', clean_content).strip()
        if stripped and stripped != clean_content:
            logger.debug(f"parse_ai_json: 移除reasoning块后搜索,剩余长度: {len(stripped)}")
            result = _try_parse_json_obj(stripped)
            if result is not None:
                return result
            # 继续用全文搜索

        # ── 最后兜底:在全文(含 reasoning)中搜索 JSON 模式 ──
        result = _try_parse_json_obj(clean_content)
        if result is not None:
            return result

        # ── 自然语言兜底解析 ──
        # 模式1:检测工具调用意图
        # 注意:Python re 默认 Unicode 模式,\w 可匹配中文,导致"调用任何工具"
        # 捕获"任何"等泛指词并虚构工具调用。提取后必须做工具名校验。
        tool_mention_patterns = [
            r'(?:调用|使用|执行|运行)\s*(\w+)\s*(?:工具|函数|来)',
            r'(?:我需要|我应该|接下来)\s*(?:调用|使用)\s*(\w+)',
            r'(?:tool|工具)[::]\s*(\w+)',
        ]
        for pattern in tool_mention_patterns:
            tool_match = re.search(pattern, clean_content, re.IGNORECASE)
            if tool_match:
                tool_name = tool_match.group(1)
                if not _is_valid_tool_name(tool_name, valid_tool_names):
                    logger.info(f"parse_ai_json 自然语言兜底: 候选工具名 '{tool_name}' 不可信,跳过工具调用解析")
                    continue
                args_match = re.search(r'\{[^{}]*\}', clean_content)
                args = {}
                if args_match:
                    try:
                        args = repair_loads(args_match.group(0))
                    except:
                        args = {}
                logger.info(f"parse_ai_json 自然语言兜底: 提取到工具 {tool_name}, 参数: {args}")
                return {"tool_name": tool_name, "arguments": args}

        # 模式2:检测终结信号(扩展更多自然语言完成模式)
        final_patterns = [
            r'(?:任务完成|已完成|最终回复|final answer|done)',
            r'(?:文件已生成|已生成|已创建|已保存)',
            # 成功操作类:✅/已将/已经/搞定/没问题/成功设置/成功调整/成功执行
            r'[✅✔✓]\s*(?:已|已经|成功)',
            r'(?:已将|已经|搞定|弄好了|没问题了|完成了|搞好了)',
            r'(?:设置为|调整到|调到|设为|改成|切换到)',
            r'(?:成功|已成功|已经成功)',
        ]
        for pattern in final_patterns:
            if re.search(pattern, clean_content, re.IGNORECASE):
                logger.info(f"parse_ai_json 自然语言兜底: 检测到终结信号 pattern={pattern}")
                return {"final_message": clean_content.strip()}

        # 模式3:短文本+非工具调用特征 → 视为纯文本回复
        # 条件:内容不长 + 不含工具调用特征 + 含自然语言
        if (len(clean_content) <= 200
            and not re.search(r'\{\s*"tool_name"', clean_content)
            and not re.search(r'\{\s*"tool_call"', clean_content)
            and not re.search(r'```action', clean_content)
            and not re.search(r'```json', clean_content)
            and (re.search(r'[\u4e00-\u9fff]', clean_content) or re.match(r'^[A-Za-z✅✔✓]', clean_content))):
            logger.info("parse_ai_json 自然语言兜底: 短文本视为final_message")
            return {"final_message": clean_content.strip()}

        return None
    except Exception as e:
        logger.error(f"JSON 解析异常: {e}")
        return None


def _try_parse_json_obj(text):
    """从给定文本中尝试提取并解析有效 JSON 对象。
    只返回包含 tool_name/tool_call/tool/final_message/progress 等有效字段的 dict,
    或无法判断时返回 None(避免误把推理 JSON 当成 action)。
    """
    from json_repair import loads as repair_loads

    # 方法1: 整体解析
    try:
        obj = repair_loads(text)
        if isinstance(obj, dict):
            # 如果包含有效字段,直接返回
            if any(k in obj for k in ("tool_name", "tool_call", "tool", "final_message", "progress")):
                return obj
            # 特殊处理:包含 next_step 等推理字段但无有效 action 字段 → 视为无意义
            # 可能是推理 JSON(如 {"level": 56, "next_step": "任务完成"})
            # 这种情况返回 None 让上层用纯文本兜底
            if "next_step" in obj and len(obj) <= 3:
                return None
            # 其他 dict 也无有效字段 → 返回 None
            return None
    except Exception:
        pass

    # 方法2: 逐字符扫描提取完整 JSON
    start_brace = text.find('{')
    if start_brace == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    for i in range(start_brace, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == '\\':
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                try:
                    obj = repair_loads(text[start_brace:i + 1])
                    if isinstance(obj, dict):
                        if any(k in obj for k in ("tool_name", "tool_call", "tool", "final_message", "progress")):
                            return obj
                        if "next_step" in obj and len(obj) <= 3:
                            return None
                except Exception:
                    pass
                # 继续搜索下一个 {
                depth = 0
                in_string = False
                escape = False
                start_brace = text.find('{', i + 1)
                if start_brace == -1:
                    break
                # 重新开始扫描
                i = start_brace
                depth = 1
                continue

    return None


_models_cache = None
_models_cache_mtime = 0
_models_cache_lock = threading.Lock()


def invalidate_models_cache():
    """清空模型缓存，下次调用强制重新读取 models.json。"""
    global _models_cache, _models_cache_mtime
    with _models_cache_lock:
        _models_cache = None
        _models_cache_mtime = 0


def get_model_from_cache(model_id):
    """从模型缓存中按 id 查找模型配置，缓存为空时先加载。"""
    global _models_cache
    if _models_cache is None:
        get_available_models()
    if _models_cache:
        return next((m for m in _models_cache if m["id"] == model_id), None)
    return None


def get_available_models():
    """获取可用模型列表：优先读 models.json 缓存，缺失时返回内置默认模型。"""
    global _models_cache, _models_cache_mtime

    env_key = os.environ.get("SMARTDESKTOP_API_KEY") or os.environ.get("API_KEY", "")
    env_model = os.environ.get("API_MODEL", "")
    env_url = os.environ.get("API_BASE_URL", DEEPSEEK_BASE_URL)

    try:
        config_path = os.path.join(DATA_DIR, "models.json")

        if os.path.exists(config_path):
            current_mtime = os.path.getmtime(config_path)
            with _models_cache_lock:
                if _models_cache is None or current_mtime > _models_cache_mtime:
                    with open(config_path, "r", encoding="utf-8") as f:
                        cfg = json.load(f)

                    models = []
                    if env_key and env_model:
                        models.append({
                            "id": "env_" + env_model,
                            "display": f"环境变量 ({env_model})",
                            "provider": "deepseek",
                            "model": env_model,
                            "base_url": env_url,
                            "api_version": "v1",
                            "temperature": DEFAULT_TEMPERATURE,
                            "stream": True
                        })

                    for m in cfg.get("models", []):
                        m_id = m.get("id", m.get("model", ""))
                        if env_key and env_model and m_id == "env_" + env_model:
                            continue
                        provider = m.get("provider", "deepseek").lower()
                        models.append({
                            "id": m_id,
                            "display": m.get("name", m.get("model", m_id)),
                            "provider": provider,
                            "model": m.get("model", "deepseek-chat"),
                            "base_url": m.get("base_url", DEEPSEEK_BASE_URL),
                            "api_version": m.get("api_version", "v1"),
                            "temperature": m.get("temperature", DEFAULT_TEMPERATURE),
                            "stream": m.get("stream", True)
                        })

                    _models_cache = models
                    _models_cache_mtime = current_mtime
                else:
                    models = _models_cache
        else:
            models = [
                {
                    "id": "deepseek-chat",
                    "display": "DeepSeek (deepseek-chat)",
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "base_url": DEEPSEEK_BASE_URL,
                    "api_version": "v1",
                    "temperature": DEFAULT_TEMPERATURE,
                    "stream": True
                },
                {
                    "id": "ollama-r1",
                    "display": "本地 Ollama (deepseek-r1:7b)",
                    "provider": "ollama",
                    "model": "deepseek-r1:7b",
                    "base_url": OLLAMA_HOST,
                    "api_version": "v1",
                    "temperature": DEFAULT_TEMPERATURE,
                    "stream": True
                }
            ]
            _models_cache = models

    except Exception as e:
        logger.warning(f"加载 models.json 失败: {e}")
        models = _models_cache or [
            {
                "id": "deepseek-chat",
                "display": "DeepSeek (deepseek-chat)",
                "provider": "deepseek",
                "model": "deepseek-chat",
                "base_url": DEEPSEEK_BASE_URL,
                "api_version": "v1",
                "temperature": DEFAULT_TEMPERATURE,
                "stream": True
            },
            {
                "id": "ollama-r1",
                "display": "本地 Ollama (deepseek-r1:7b)",
                "provider": "ollama",
                "model": "deepseek-r1:7b",
                "base_url": OLLAMA_HOST,
                "api_version": "v1",
                "temperature": DEFAULT_TEMPERATURE,
                "stream": True
            }
        ]

    return models


def _deepseek_call(messages, model, api_key, base_url,
                   provider="deepseek", trace_id="", tools=None, response_format=None):
    """调用 DeepSeek 非流式接口（转发到通用 OpenAI 兼容协议实现）。"""
    return _openai_compatible_call(messages, model, api_key, base_url,
                                   provider=provider, trace_id=trace_id,
                                   tools=tools, response_format=response_format)


def _deepseek_call_stream(messages, model, api_key, base_url,
                          provider="deepseek", trace_id="", tools=None, response_format=None, temperature=DEFAULT_TEMPERATURE, stream=True):
    """调用 DeepSeek 流式接口（转发到通用 OpenAI 兼容协议实现）。"""
    _assert_system_first(messages, caller="deepseek_stream")
    return _openai_compatible_call_stream(messages, model, api_key, base_url,
                                          provider=provider, trace_id=trace_id,
                                          tools=tools, response_format=response_format,
                                          temperature=temperature, stream=stream)


def ai_concat_stream(messages, model_name=None, provider=None, trace_id="",
                     tools=None, response_format=None, base_url_override=None, api_version="v1", model_id=None, temperature=DEFAULT_TEMPERATURE):
    """统一 AI 流式调用入口:Ollama 走专属逻辑,其余走通用 OpenAI 兼容协议"""
    try:
        get_logger().debug(f"[ai_concat_stream] 开始调用", trace_id=trace_id, model_id=model_id,
                          provider=provider, messages_count=len(messages) if messages else 0)

        config = _load_config()

        model_cfg = get_model_from_cache(model_id) if model_id else None

        # 路由优先级:显式传参 > models.json 的 provider > config.yaml backend
        if provider:
            backend = provider.lower().strip()
        elif model_cfg:
            backend = model_cfg.get("provider", "deepseek").lower().strip()
        else:
            backend = config.get("backend", "deepseek").lower().strip()

        if backend == "ollama":
            model = model_name or (model_cfg.get("model") if model_cfg else None) or config.get("model", "deepseek-r1:7b")
            resolved_temperature = temperature if temperature is not None else (model_cfg.get("temperature", DEFAULT_TEMPERATURE) if model_cfg else DEFAULT_TEMPERATURE)
            get_logger().debug(f"[ai_concat_stream] Ollama调用", trace_id=trace_id, model=model, temperature=resolved_temperature)

            # ★ LLM 流式请求日志钩子
            _llm_req_log.info(f"[LLM_REQ_STREAM] provider=ollama model={model} trace_id={trace_id} "
                             f"temp={resolved_temperature} msgs={len(messages)} "
                             f"tools={_brief_tool_names(tools)}")
            _llm_req_log.debug(f"[LLM_REQ_MSGS] {_safe_serialize_messages(messages)}")
            stream_chunks = []
            stream_error = None
            try:
                for chunk in _ollama_call_stream(messages, model, trace_id=trace_id,
                                                 tools=tools, response_format=response_format, temperature=resolved_temperature):
                    stream_chunks.append(chunk)
                    yield chunk
            except Exception as e:
                stream_error = e
                raise
            finally:
                if stream_error:
                    _llm_req_log.error(f"[LLM_RESP_STREAM] provider=ollama model={model} trace_id={trace_id} ERROR",
                                       exc_info=True)
                else:
                    _llm_req_log.info(f"[LLM_RESP_STREAM] provider=ollama model={model} trace_id={trace_id} "
                                     f"chunks={len(stream_chunks)}")
        else:
            resolved_model = model_name or (model_cfg.get("model") if model_cfg else None) or config.get("model", "deepseek-chat")
            resolved_api_key = get_api_key(model_id)

            if model_cfg:
                resolved_base_url = base_url_override or model_cfg.get("base_url", DEEPSEEK_BASE_URL)
                resolved_api_version = model_cfg.get("api_version", "v1")
                resolved_temperature = temperature if temperature is not None else model_cfg.get("temperature", DEFAULT_TEMPERATURE)
            else:
                resolved_base_url = base_url_override or get_base_url(model_id)
                resolved_api_version = api_version
                if model_id and config.get("models") and config["models"].get(model_id):
                    resolved_api_version = config["models"][model_id].get("api_version", "v1")
                resolved_temperature = temperature if temperature is not None else DEFAULT_TEMPERATURE

            if not resolved_base_url:
                raise ValueError(f"无法解析 Base URL,请检查 model_id={model_id} 配置或传入 base_url_override")

            get_logger().debug(f"[ai_concat_stream] OpenAI兼容调用", trace_id=trace_id, model=resolved_model,
                              base_url=resolved_base_url, api_version=resolved_api_version,
                              temperature=resolved_temperature, api_key_set=bool(resolved_api_key))

            # ★ LLM 流式请求日志钩子
            _llm_req_log.info(f"[LLM_REQ_STREAM] provider={backend} model={resolved_model} trace_id={trace_id} "
                             f"base_url={resolved_base_url} temp={resolved_temperature} msgs={len(messages)} "
                             f"tools={_brief_tool_names(tools)}")
            _llm_req_log.debug(f"[LLM_REQ_MSGS] {_safe_serialize_messages(messages)}")
            stream_chunks2 = []
            stream_error2 = None
            try:
                for chunk in _openai_compatible_call_stream(
                    messages, resolved_model, resolved_api_key, resolved_base_url,
                    provider=backend, trace_id=trace_id, tools=tools,
                    response_format=response_format, api_version=resolved_api_version,
                    temperature=resolved_temperature
                ):
                    stream_chunks2.append(chunk)
                    yield chunk
            except Exception as e:
                stream_error2 = e
                raise
            finally:
                if stream_error2:
                    _llm_req_log.error(f"[LLM_RESP_STREAM] provider={backend} model={resolved_model} "
                                       f"trace_id={trace_id} ERROR", exc_info=True)
                else:
                    _llm_req_log.info(f"[LLM_RESP_STREAM] provider={backend} model={resolved_model} "
                                     f"trace_id={trace_id} chunks={len(stream_chunks2)}")

    except Exception as e:
        get_logger().error(f"[ai_concat_stream] AI调用初始化失败: {e}", trace_id=trace_id, model_id=model_id,
                          exc_info=True)
        yield {"type": "error", "error": f"AI服务不可用: {e}"}


def stream_llm_response(messages: List[Dict[str, Any]], system_prompt: str = "",
                        user_input: str = "",
                        call_seq: list = None, _tools_param=None, base_url: str = "",
                        default_provider: str = "", default_model: str = "",
                        model_id: str = "", temperature=None, stream: bool = True,
                        cancel_event=None,
                        layered_cache=None,
                        *, trace_id: str = "", session_id: str = "",
                        session_name: str = "", turn_state=None) -> Generator[Any, None, None]:
    """流式 LLM 响应入口：带重试、推理块解析与 SSE 事件归一化的生成器。

    stream=False 时对 LLM 发起非流式请求，仍以统一的 chunk/result 事件协议产出。"""
    idx = call_seq[0]
    call_seq[0] += 1
    t0 = time.perf_counter()

    # 复用 run() 入口缓存的 tool_schemas,避免每轮重复构建(问题2)
    tools_param = _tools_param

    max_retries = 2
    retry_delays = [5, 15]

    _in_reasoning_block = False
    for attempt in range(max_retries + 1):
        full_content = ""
        full_reasoning = ""
        retry_needed = False

        try:
            api_key = get_api_key(model_id)
            base_url = get_base_url(model_id) or base_url
            get_logger().info(
                f"LLM调用: provider={default_provider}, model={default_model}, "
                f"api_key={'已配置' if api_key else '⚠️ 为空'}, base_url={base_url}",
                trace_id=trace_id, session_id=session_id
            )
            if default_provider == "ollama":
                stream_gen = _ollama_call_stream(messages, default_model, trace_id=trace_id, tools=tools_param,
                                                 temperature=temperature, stream=stream)
            elif not api_key:
                get_logger().error(f"API Key 为空,无法调用{default_provider}模型", trace_id=trace_id)
                yield {"type": "error", "message": f"{default_provider} API Key 未配置,请在设置中添加 API Key"}
                return
            else:
                stream_gen = _deepseek_call_stream(messages, default_model,
                    api_key, base_url,
                    trace_id=trace_id, tools=tools_param, temperature=temperature, stream=stream)

            last_heartbeat = time.time()
            try:
                for chunk in stream_gen:
                    if cancel_event and cancel_event.is_set():
                        yield {"type": "cancelled", "message": "任务已取消"}
                        return

                    if time.time() - last_heartbeat > 10:
                        yield ": keepalive\n\n"
                        last_heartbeat = time.time()

                    content = chunk.get("content", "")
                    reasoning = chunk.get("reasoning", "")

                    # 先检查 "result" 类型,避免"result"事件以 content 身份被二次累加导致内容重复
                    if chunk.get("type") == "result":
                        full_content = chunk.get("content", full_content)
                        full_reasoning = chunk.get("reasoning_content", full_reasoning)

                        # ✅ 提前返回前记录 LLM 调用日志,防止跳过 for 循环后的日志写入
                        elapsed = int((time.perf_counter() - t0) * 1000)
                        reasoning_trace = parse_reasoning_trace(full_content)
                        reasoning_trace_str = json.dumps(reasoning_trace, ensure_ascii=False) if reasoning_trace else ""

                        actual_user_input = user_input
                        for m in reversed(messages):
                            if m.get("role") == "user":
                                actual_user_input = m.get("content", "")
                                break

                        try:
                            get_logger().log_llm_call(
                                trace_id=trace_id, session_id=session_id,
                                turn=turn_state.turn, child_turn=turn_state.child_turn,
                                user_task=turn_state.user_task,
                                response=full_content,
                                model_name=default_model, provider=default_provider,
                                elapsed_ms=elapsed, session_name=session_name,
                                reasoning=reasoning_trace_str if reasoning_trace_str else full_reasoning,
                                system_prompt=system_prompt,
                                user_input=actual_user_input,
                                messages=messages,
                                user_task_time=turn_state.user_task_time,
                                route_level="",
                                context_memory=_build_context_memory_summary(layered_cache),
                            )
                        except Exception as _llm_log_err2:
                            get_logger().warning(f"log_llm_call(result分支) 异常(已忽略): {_llm_log_err2}",
                                                 trace_id=trace_id, session_id=session_id)
                        turn_state.child_turn += 1

                        yield {
                            "type": "result",
                            "content": full_content,
                            "reasoning": full_reasoning,
                            "tool_calls": chunk.get("tool_calls", []),  # ✅ 透传原生 tool_calls
                        }
                        return

                    if content or reasoning:
                        full_content += content if content else ""
                        full_reasoning += reasoning if reasoning else ""

                        get_logger().debug(f"llm_stream原始chunk: content前100字符={repr((content or '')[:100])}, reasoning前100字符={repr((reasoning or '')[:100])}, in_block={_in_reasoning_block}",
                                          trace_id=trace_id, session_id=session_id)

                        if _in_reasoning_block:
                            _c = content or ''
                            reasoning_end = _c.find("```")
                            if reasoning_end != -1:
                                reasoning_content = _c[:reasoning_end]
                                content = _c[reasoning_end + 3:]
                                _in_reasoning_block = False
                            else:
                                reasoning_content = _c
                                content = ""
                        else:
                            _c = content or ''
                            reasoning_start = _c.find("```reasoning")
                            if reasoning_start != -1:
                                reasoning_end = _c.find("```", reasoning_start + 12)
                                if reasoning_end != -1:
                                    reasoning_content = _c[reasoning_start + 12:reasoning_end]
                                    content = _c[:reasoning_start] + _c[reasoning_end + 3:]
                                else:
                                    reasoning_content = _c[reasoning_start + 12:]
                                    content = _c[:reasoning_start]
                                    _in_reasoning_block = True
                            else:
                                reasoning_content = ""

                        if reasoning_content.strip():
                            yield {
                                "type": "chunk",
                                "content": content,
                                "reasoning": reasoning_content.strip(),
                            }
                        else:
                            yield {
                                "type": "chunk",
                                "content": content,
                                "reasoning": reasoning,
                            }
                        continue

                    if chunk.get("type") == "error":
                        err_msg = chunk.get("message") or chunk.get("error") or "未知错误"
                        # 429 限流为配额类错误,重试无益且叠加 5s/15s 延迟形成 30s 卡顿,
                        _is_rate_limited = ("429" in str(err_msg)) or ("Too Many Requests" in str(err_msg))
                        if _is_rate_limited:
                            get_logger().warning(f"LLM 429 限流,快速失败不重试: {err_msg}", trace_id=trace_id)
                            yield {"type": "error", "message": translate_error(err_msg)}
                            return
                        if attempt < max_retries:
                            full_content = ""
                            full_reasoning = ""
                            get_logger().warning(f"LLM调用错误,第{attempt+1}/{max_retries}次重试", trace_id=trace_id, error=err_msg)
                            yield {"type": "retry", "attempt": attempt + 1, "max_retries": max_retries, "message": f"LLM调用失败,第{attempt+1}/{max_retries+1}次重试..."}
                            retry_needed = True
                            break
                        yield {"type": "error", "message": translate_error(err_msg)}
                        return

            finally:
                stream_gen.close()
            if retry_needed:
                continue

            actual_user_input = user_input
            for m in reversed(messages):
                if m.get("role") == "user":
                    actual_user_input = m.get("content", "")
                    break

            reasoning_trace = parse_reasoning_trace(full_content)
            reasoning_trace_str = json.dumps(reasoning_trace, ensure_ascii=False) if reasoning_trace else ""

            elapsed = int((time.perf_counter() - t0) * 1000)
            try:
                get_logger().log_llm_call(
                    trace_id=trace_id, session_id=session_id,
                    turn=turn_state.turn, child_turn=turn_state.child_turn,
                    user_task=turn_state.user_task,
                    response=full_content,
                    model_name=default_model, provider=default_provider,
                    elapsed_ms=elapsed, session_name=session_name,
                    reasoning=reasoning_trace_str if reasoning_trace_str else full_reasoning,
                    system_prompt=system_prompt,
                    user_input=actual_user_input,
                    messages=messages,
                    user_task_time=turn_state.user_task_time,
                    route_level="",
                    context_memory=_build_context_memory_summary(layered_cache),
                )
            except Exception as _llm_log_err:
                get_logger().warning(f"log_llm_call 异常(已忽略,不影响主流程): {_llm_log_err}",
                                     trace_id=trace_id, session_id=session_id)
            turn_state.child_turn += 1
            yield {"content": full_content, "reasoning_content": full_reasoning, "reasoning_trace": reasoning_trace}
            return

        except Exception as e:
            if attempt < max_retries:
                get_logger().warning(f"LLM流式调用异常,第{attempt+1}次重试",
                                    trace_id=trace_id, error=str(e))
                if cancel_event is not None:
                    cancel_event.wait(retry_delays[attempt])
                else:
                    time.sleep(retry_delays[attempt])
            else:
                # 记录所有重试耗尽后的失败 LLM 调用
                elapsed = int((time.perf_counter() - t0) * 1000)
                try:
                    get_logger().log_llm_call(
                        trace_id=trace_id, session_id=session_id,
                        turn=turn_state.turn, child_turn=turn_state.child_turn,
                        user_task=turn_state.user_task,
                        response=f"[LLM调用失败] {translate_error(str(e))}",
                        model_name=default_model, provider=default_provider,
                        elapsed_ms=elapsed, session_name=session_name,
                        system_prompt=system_prompt,
                        user_input=user_input,
                        messages=messages,
                        user_task_time=turn_state.user_task_time,
                    )
                except Exception:
                    pass
                yield {"type": "error", "message": translate_error(str(e))}
                return




# ═══════════════════════════════════════════════════════════════════════════════
# 从 react_engine.py 迁移的 LLM 交互函数
# ═══════════════════════════════════════════════════════════════════════════════

def call_llm_and_parse(engine, rs, state, user_message: str, step: int,
                       cancel_event, call_seq, tools_param,
                       base_url: str, default_provider: str, default_model: str,
                       model_id, temperature, stream=True, remaining_steps=[]
                       ) -> Generator[str, None, tuple]:
    """调用 LLM 并解析响应。可变状态通过 rs(RunState) 承载。

    从 run() 主循环提取。包含:token 压缩、stream_llm_response 流式调用、
    chunk 处理、空内容检查、reasoning_trace 解析、parse_ai_json、纯文本回复分发。

    可变状态(conversation_messages / thinking_summaries / tool_call_history /
    layered_cache / consecutive_empty_turns / final_reply_saved / full_content /
    full_reasoning / llm_reasoning_trace)通过 rs 引用,方法内修改会同步回 rs。

    Returns:
        (flow_control, visible_content, parsed_action, llm_native_tool_calls)
        flow_control: "return" / "continue" / "proceed"
    """
    # 从 rs 取回可变状态(用局部变量名保持原代码可读性)
    conversation_messages = rs.conversation_messages
    layered_cache = rs.layered_cache
    _thinking_summaries = rs.thinking_summaries
    _tool_call_history = rs.tool_call_history
    consecutive_empty_turns = rs.consecutive_empty_turns
    final_reply_saved = rs.final_reply_saved

    full_content = ""
    full_reasoning = ""
    _llm_reasoning_trace = None
    _llm_native_tool_calls = []
    visible_content = ""
    parsed_action = None

    try:
        get_logger().debug(f"===== 第{step}轮 Multi-Turn LLM 请求 =====",
                          trace_id=engine.trace_id, session_id=engine.session_id)
        get_logger().debug(f"用户任务: {user_message[:200]}",
                          trace_id=engine.trace_id, session_id=engine.session_id)
        get_logger().debug(f"长期记忆: {'有' if layered_cache.get('long_term') else '无'}",
                          trace_id=engine.trace_id, session_id=engine.session_id)
        get_logger().debug(f"近三轮结构化: {'有' if layered_cache.get('recent_structured') else '无'}",
                          trace_id=engine.trace_id, session_id=engine.session_id)
        get_logger().debug(f"思考摘要数量: {len(_thinking_summaries)}",
                          trace_id=engine.trace_id, session_id=engine.session_id)
        for i, rt in enumerate(_thinking_summaries[-2:]):
            if isinstance(rt, dict):
                get_logger().debug(f"  思考摘要{i+1}: intent={rt.get('current_intent','')}, rationale={rt.get('decision_rationale','')[:50]}",
                                  trace_id=engine.trace_id, session_id=engine.session_id)
        get_logger().debug(f"工具记录数量: {len(_tool_call_history)}",
                          trace_id=engine.trace_id, session_id=engine.session_id)
        for i, tc in enumerate(_tool_call_history):
            get_logger().debug(f"  工具记录{i+1}: name={tc['name']}, status={tc['status']}",
                              trace_id=engine.trace_id, session_id=engine.session_id)
        get_logger().debug(f"Multi-Turn 消息数: {len(conversation_messages)}",
                          trace_id=engine.trace_id, session_id=engine.session_id)
        get_logger().debug(f"===============================",
                          trace_id=engine.trace_id, session_id=engine.session_id)

        # LLM 调用前检查取消信号
        if cancel_event is not None and cancel_event.is_set():
            get_logger().info("检测到取消信号,终止任务(LLM 调用前)",
                              trace_id=engine.trace_id, session_id=engine.session_id)
            from core.agent_state import safe_final
            yield from safe_final(engine, state, "任务已取消", outcome="cancelled")
            final_reply_saved = True
            return ("return", visible_content, parsed_action, _llm_native_tool_calls)

        yield engine.streamer.emit_step("正在调用模型思考...")

        _reasoning_streamed = False
        last_heartbeat = time.time()

        # ✅ 先注入记忆(在 token 估算之前,确保压缩能准确计算注入后的上下文用量)
        if layered_cache:
            from core.agent_state import inject_memory_if_needed
            conversation_messages = inject_memory_if_needed(conversation_messages, layered_cache)

        # ── Token 估算与上下文窗口管理(使用 conversation_messages) ──
        try:
            from memory.compressor import estimate_messages_tokens, compress_messages
            UNIFIED_THRESHOLD = (engine.MAX_CONTEXT_TOKENS - engine.SAFETY_MARGIN
                                 if engine.COMPRESSOR_ENABLED else float("inf"))
            total_tokens = estimate_messages_tokens(conversation_messages)
            layer_flags = []

            # ── Layer 1: 轻量压缩(压缩思考摘要,~ms 级) ──
            if total_tokens > UNIFIED_THRESHOLD:
                get_logger().warning(
                    f"Token 预算超限 ({total_tokens}/{UNIFIED_THRESHOLD}),Layer1: 压缩思考摘要",
                    trace_id=engine.trace_id, session_id=engine.session_id
                )
                if _thinking_summaries:
                    _thinking_summaries = inline_compress(_thinking_summaries)
                total_tokens = estimate_messages_tokens(conversation_messages)
                layer_flags.append("L1")

            # ── Layer 2: 中量压缩(压缩工具历史,~ms 级) ──
            if total_tokens > UNIFIED_THRESHOLD:
                get_logger().warning(
                    f"Token 预算仍超限 ({total_tokens}/{UNIFIED_THRESHOLD}),Layer2: 压缩工具历史",
                    trace_id=engine.trace_id, session_id=engine.session_id
                )
                if len(_tool_call_history) > 5:
                    _tool_call_history[:] = _tool_call_history[-5:]
                for tc in _tool_call_history:
                    if tc.get("result") and isinstance(tc["result"], str) and len(tc["result"]) > 200:
                        tc["result"] = tc["result"][:200] + "..."
                total_tokens = estimate_messages_tokens(conversation_messages)
                layer_flags.append("L2")

            # ── Layer 3: 重量压缩(压缩 conversation_messages 历史轮次,~10ms 级) ──
            _skip_l3 = getattr(engine, '_is_first_recovery_turn', False)
            if total_tokens > UNIFIED_THRESHOLD and not _skip_l3:
                get_logger().warning(
                    f"Token 预算仍超限 ({total_tokens}/{UNIFIED_THRESHOLD}),Layer3: 压缩消息历史",
                    trace_id=engine.trace_id, session_id=engine.session_id
                )
                conversation_messages = compress_messages(conversation_messages, target_tokens=UNIFIED_THRESHOLD,
            session_id=engine.session_id, trace_id=engine.trace_id,
            max_result_chars=engine.MAX_RESULT_CHARS)
                layer_flags.append("L3")
            if _skip_l3:
                get_logger().info(
                    f"恢复首轮：跳过 Layer 3 压缩以保留完整对话历史（共 {len(conversation_messages)} 条消息）",
                    trace_id=engine.trace_id, session_id=engine.session_id
                )
                engine._is_first_recovery_turn = False

            if layer_flags:
                get_logger().info(
                    f"Token 预算压缩完成,触发层级: {'→'.join(layer_flags)},最终 token: {total_tokens}",
                    trace_id=engine.trace_id, session_id=engine.session_id
                )
        except ImportError:
            pass
        except Exception as _tok_e:
            get_logger().warning(f"token 估算失败(不影响主流程): {_tok_e}",
                                trace_id=engine.trace_id, session_id=engine.session_id)

        for chunk in stream_llm_response(
            conversation_messages,
            call_seq=call_seq, _tools_param=tools_param,
            base_url=base_url, default_provider=default_provider,
            default_model=default_model, model_id=model_id,
            temperature=temperature, stream=stream, cancel_event=cancel_event,
            layered_cache=layered_cache,
            trace_id=engine.trace_id, session_id=engine.session_id,
            session_name=engine.session_name, turn_state=engine._turn_state,
        ):
            if isinstance(chunk, str) and chunk.startswith(":"):
                yield chunk
                continue

            if time.time() - last_heartbeat > 10:
                yield ": keepalive\n\n"
                last_heartbeat = time.time()

            if isinstance(chunk, dict):
                if chunk.get("type") == "error":
                    err_msg = chunk.get("message") or chunk.get("error") or "未知错误"
                    yield engine.streamer.emit_error(err_msg)
                    from core.agent_state import save_checkpoint
                    save_checkpoint(engine, user_message, state, remaining_steps, step, progress=f"第{step}次LLM调用失败: {err_msg}", messages_param=conversation_messages)
                    final_reply_saved = True
                    return ("return", visible_content, parsed_action, _llm_native_tool_calls)
                elif chunk.get("type") == "cancelled":
                    from core.agent_state import safe_final
                    yield from safe_final(engine, state, chunk.get("message") or chunk.get("error") or "", outcome="cancelled")
                    final_reply_saved = True
                    return ("return", visible_content, parsed_action, _llm_native_tool_calls)
                elif chunk.get("type") == "retry":
                    yield engine.streamer.emit_step(chunk.get("message", "LLM调用重试中..."))
                    continue
                elif chunk.get("type") == "chunk":
                    reasoning = chunk.get("reasoning", "")
                    content = chunk.get("content", "")
                    get_logger().debug(f"流式chunk: content前100字符={repr((content or '')[:100])}, reasoning前100字符={repr((reasoning or '')[:100])}",
                                      trace_id=engine.trace_id, session_id=engine.session_id)
                    if reasoning:
                        _reasoning_streamed = True
                        yield engine.streamer.emit_reasoning(reasoning)
                    continue

                full_content = chunk.get("content", "")
                full_reasoning = chunk.get("reasoning_content", chunk.get("reasoning", ""))
                _llm_reasoning_trace = chunk.get("reasoning_trace")
                _llm_native_tool_calls = chunk.get("tool_calls", [])
                from core.streaming import _strip_reasoning_trace
                visible_content, _ = _strip_reasoning_trace(full_content)

        if not full_content.strip():
            consecutive_empty_turns += 1
            get_logger().warning(f"LLM返回空内容,第{consecutive_empty_turns}次",
                                  trace_id=engine.trace_id, session_id=engine.session_id)
            if consecutive_empty_turns >= 3:
                from core.agent_state import safe_final
                yield from safe_final(engine, state, "模型连续多次返回空内容,任务已暂停。请稍后重试或换一种方式描述您的需求。",
                    timestamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                final_reply_saved = True
                return ("return", visible_content, parsed_action, _llm_native_tool_calls)
            return ("continue", visible_content, parsed_action, _llm_native_tool_calls)
        consecutive_empty_turns = 0

        reasoning_trace = _llm_reasoning_trace if _llm_reasoning_trace else parse_reasoning_trace(full_content)
        if reasoning_trace is None:
            # fallback: 从 full_content 取前 200 字符作为 intent
            intro = full_content.strip()[:200].replace("\n", " ")
            reasoning_trace = {
                "current_intent": intro[:200] + ("..." if len(intro) > 200 else ""),
                "decision_rationale": "fallback: LLM未输出结构化思考轨迹",
                "active_hypothesis": ""
            }
        if reasoning_trace:
            if _thinking_summaries and _thinking_summaries[-1].get("current_intent") == reasoning_trace.get("current_intent"):
                _thinking_summaries[-1] = {**_thinking_summaries[-1], **reasoning_trace}
            else:
                _thinking_summaries.append(reasoning_trace)
            get_logger().debug(f"思考摘要已追加: intent={reasoning_trace.get('current_intent','')}, rationale={reasoning_trace.get('decision_rationale','')[:50]}",
                              trace_id=engine.trace_id, session_id=engine.session_id)

        if not _reasoning_streamed:
            extracted_full_reasoning = full_reasoning
            if not extracted_full_reasoning and full_content:
                reasoning_match = re.search(r'```reasoning\s*([\s\S]*?)\s*```', full_content)
                if reasoning_match:
                    extracted_full_reasoning = reasoning_match.group(1).strip()
                else:
                    trace_match = re.search(r'<reasoning_trace>\s*(\{[\s\S]*?\})\s*</reasoning_trace>', full_content)
                    if trace_match:
                        extracted_full_reasoning = trace_match.group(1).strip()
            if extracted_full_reasoning.strip():
                yield engine.streamer.emit_reasoning(extracted_full_reasoning)

        # 自然语言兜底需工具名白名单,防止从自由文本(如"调用任何工具")
        # 提取泛指词虚构工具调用;获取失败时降级为无白名单(仍有形态+黑名单保守校验)
        _valid_tool_names = None
        try:
            from core.tool_data_provider import tool_data_provider
            _valid_tool_names = set(tool_data_provider.get_all_tools().keys())
        except Exception:
            _valid_tool_names = None
        parsed_action = parse_ai_json(full_content, valid_tool_names=_valid_tool_names)

        get_logger().info(f"parse_ai_json结果: parsed_action={parsed_action}",
                      trace_id=engine.trace_id, session_id=engine.session_id)

        if parsed_action is None:
            _flow, parsed_action, final_reply_saved, conversation_messages = yield from handle_pure_text_reply(
                engine, full_content, state, visible_content, conversation_messages, final_reply_saved
            )
            if _flow == "return":
                return ("return", visible_content, parsed_action, _llm_native_tool_calls)
            if _flow == "continue":
                return ("continue", visible_content, parsed_action, _llm_native_tool_calls)

        return ("proceed", visible_content, parsed_action, _llm_native_tool_calls)
    finally:
        # 同步可变状态回 rs(无论从哪个 return 点退出都执行)
        rs.conversation_messages = conversation_messages
        rs.layered_cache = layered_cache
        rs.thinking_summaries = _thinking_summaries
        rs.tool_call_history = _tool_call_history
        rs.consecutive_empty_turns = consecutive_empty_turns
        rs.final_reply_saved = final_reply_saved
        rs.full_content = full_content
        rs.full_reasoning = full_reasoning
        rs.llm_reasoning_trace = _llm_reasoning_trace


def handle_pure_text_reply(engine, full_content: str, state, visible_content: str,
                           conversation_messages: list, final_reply_saved: bool
                           ) -> Generator[str, None, tuple]:
    """解析失败兜底:检测纯文本回复或注入纠错提示。

    当 parsed_action is None 时调用。返回控制流信号。

    Returns:
        (flow_control, parsed_action, final_reply_saved, conversation_messages)
        flow_control: "return" / "continue" / "proceed"
    """
    get_logger().warning(f"parse_ai_json返回None,full_content前500字符: {full_content[:500]}",
                  trace_id=engine.trace_id, session_id=engine.session_id)

    # ── 智能兜底:检测是否为"纯文本最终回复" ──
    # 场景:LLM在工具执行成功后直接输出自然语言回复(而非 ```action {"final_message": ...} ```),
    # 这是模型的合理行为(尤其是 deepseek-chat 等非严格指令跟随模型)。
    # 检测条件:内容是纯自然语言(不含JSON/代码块/工具调用特征)
    # 不再要求 completed_steps 非空 - 首轮也可能直接返回纯文本回复
    content_text = full_content.strip()
    looks_like_plain_text = (
        len(content_text) > 3
        and not re.search(r'```(?:json|action)\s*\{', content_text)
        and not re.search(r'\{\s*"tool_name"', content_text)
        and not re.search(r'\{\s*"final_message"', content_text)
        and not re.search(r'\{\s*"tool_call"', content_text)
        and not re.search(r'\{\s*"progress"', content_text)
        and not re.search(r'\{\s*"next_step"', content_text)
        and (
            re.search(r'[\u4e00-\u9fff]', content_text)
            or re.match(r'^[A-Za-z]', content_text)
            or re.match(r'^[✅✔✓🎯📋🔧📄💻🎨⚡🔥🌟✨]', content_text)
        )
    )
    parsed_action = None
    if looks_like_plain_text:
        get_logger().info(f"检测到纯文本回复,视为final_message,跳过兜底重试",
                          trace_id=engine.trace_id, session_id=engine.session_id)
        parsed_action = {
            "final_message": content_text,
            "_auto_detected": True,
        }
        state.parse_fail = 0
        # 跳过重试逻辑,直接走 final_message 处理分支
        pass
    else:
        state.parse_fail = getattr(state, "parse_fail", 0) + 1

    if parsed_action is None and state.parse_fail >= 2:
        fallback = ("[系统兜底] 模型连续两次输出无法解析为合法动作,以下为原始输出,"
                    "任务可能未完成,请人工核对:\n" + strip_code_blocks(visible_content))
        from core.agent_state import safe_final
        yield from safe_final(engine, state, fallback)
        return ("return", parsed_action, True, conversation_messages)
    if parsed_action is None:
        # 注入纠错提示让模型重试
        format_fix_prompt = """[格式纠错] 上一次输出无法解析为合法动作。请严格按以下格式之一输出:
【调用工具时】:
```action
{"tool_name": "工具名称", "arguments": {"参数1": "值1", "参数2": "值2"}}
```
【任务完成时】:
```action
{"final_message": "给用户的最终回复内容"}
```
注意:
1. 必须输出 ```action 代码块包裹的 JSON
2. tool_name 必须是已注册的工具名
3. 不要输出自然语言,必须输出结构化 JSON
"""
        conversation_messages = strip_temp_system_messages(conversation_messages)
        conversation_messages.append({"role":"system","content":format_fix_prompt})
        return ("continue", parsed_action, False, conversation_messages)
    return ("proceed", parsed_action, False, conversation_messages)
