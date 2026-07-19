import os
import json
import re
import time
import threading
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

import requests
from memory import log_trace
from logger import get_logger
from utils import normalize_path_component

logger = get_logger()

import keyring

DATA_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'MyAgent')
KEYRING_SERVICE = "MyAgent"
KEYRING_USERNAME = "api_key"

_KEYRING_AVAILABLE = None


def _is_keyring_available():
    global _KEYRING_AVAILABLE
    if _KEYRING_AVAILABLE is None:
        try:
            keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
            _KEYRING_AVAILABLE = True
        except Exception:
            _KEYRING_AVAILABLE = False
    return _KEYRING_AVAILABLE


def _try_keyring_get(model_id=None):
    if not _is_keyring_available():
        return None
    try:
        username = KEYRING_USERNAME if model_id is None else f"{KEYRING_USERNAME}_{model_id}"
        return keyring.get_password(KEYRING_SERVICE, username)
    except Exception:
        return None


def _try_keyring_set(api_key, model_id=None):
    if not _is_keyring_available():
        return False
    try:
        username = KEYRING_USERNAME if model_id is None else f"{KEYRING_USERNAME}_{model_id}"
        keyring.set_password(KEYRING_SERVICE, username, api_key)
        return True
    except Exception:
        return False


def load_config():
    """加载配置：模型配置从 models.json 读取，API Key 从环境变量读取"""
    config = {"backend": "deepseek", "model": "deepseek-chat", "models": {}, "base_url": "https://api.deepseek.com"}

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
                        "base_url": m.get("base_url", "https://api.deepseek.com"),
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
    env_key = os.environ.get("MYAGENT_API_KEY") or os.environ.get("API_KEY")
    if env_key:
        config["api_key"] = env_key

    return config


def _load_config():
    """加载配置（每次重新读取，不缓存，支持运行时 env 覆盖）"""
    config = load_config()
    # 环境变量覆盖（运行时可能变化）
    if os.environ.get("MYAGENT_API_KEY"):
        config["api_key"] = os.environ["MYAGENT_API_KEY"]
    elif os.environ.get("API_KEY"):
        config["api_key"] = os.environ["API_KEY"]
    if os.environ.get("API_BASE_URL"):
        config["base_url"] = os.environ["API_BASE_URL"]
    if os.environ.get("API_MODEL"):
        config["model"] = os.environ["API_MODEL"]
    return config


def get_config():
    return dict(_load_config())


def get_api_key(model_id=None):
    """获取 API Key：环境变量 → models.json 模型配置 → keyring"""
    # 环境变量优先
    env_key = os.environ.get("MYAGENT_API_KEY") or os.environ.get("API_KEY")
    if env_key:
        return env_key

    config = _load_config()
    if model_id and config.get("models") and config["models"].get(model_id):
        model_cfg = config["models"][model_id]
        if model_cfg.get("api_key"):
            return model_cfg["api_key"]

    # 从 keyring 读取（兼容旧配置）
    stored = _try_keyring_get(model_id)
    if stored:
        return stored

    return ""


def get_base_url(model_id=None):
    """获取 Base URL：models.json 模型配置 → 默认值"""
    config = _load_config()
    if model_id and config.get("models") and config["models"].get(model_id):
        return config["models"][model_id].get("base_url", "https://api.deepseek.com")
    return config.get("base_url", "https://api.deepseek.com")


def get_model_config(model_id):
    """获取模型配置：优先从 _load_config 缓存，兜底从 models.json 读取"""
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
    """保留兼容性空函数（不再需要缓存失效）"""
    pass


def save_config(backend, api_key=None, model="deepseek-chat", base_url="https://api.deepseek.com", model_id=None):
    """保存 API Key 到 keyring（模型配置通过 models.json / 前端页面管理）"""
    if backend != "ollama" and api_key:
        _try_keyring_set(api_key, model_id)
    logger.info(f"配置已保存（API Key → 系统凭据管理器）")


def _save_config_raw(backend, api_key=None, model="deepseek-chat", base_url="https://api.deepseek.com"):
    save_config(backend, api_key, model, base_url)


DEFAULT_TEMPERATURE = 0.75

_session = requests.Session()
_retry_strategy = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["POST"]
)
_session.mount("http://", HTTPAdapter(max_retries=_retry_strategy))
_session.mount("https://", HTTPAdapter(max_retries=_retry_strategy))


def _normalize_base_url(base_url):
    url = base_url.rstrip('/')
    url = re.sub(r'/chat/completions/?$', '', url)
    url = re.sub(r'/v\d+/chat/completions/?$', '', url)
    return url


def _map_response_format(response_format):
    if not response_format:
        return None
    fmt_type = response_format.get("type", "")
    if fmt_type == "json_object":
        return "json"
    return fmt_type


def ai_concat(messages, model_name=None, provider=None, trace_id="", 
              tools=None, response_format=None, base_url_override=None, model_id=None, temperature=None):
    """统一 AI 调用入口：Ollama 走专属逻辑，其余走通用 OpenAI 兼容协议"""
    try:
        get_logger().debug(f"[ai_concat] 开始调用", trace_id=trace_id, model_id=model_id, 
                          provider=provider, messages_count=len(messages) if messages else 0)

        config = _load_config()

        model_cfg = get_model_from_cache(model_id) if model_id else None

        # 路由优先级：显式传参 > models.json 的 provider > config.yaml backend
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
            return _ollama_call(messages, model, trace_id=trace_id, 
                               tools=tools, response_format=response_format, temperature=resolved_temperature)

        else:
            resolved_model = model_name or (model_cfg.get("model") if model_cfg else None) or config.get("model", "deepseek-chat")
            resolved_api_key = get_api_key(model_id)
            
            if model_cfg:
                resolved_base_url = base_url_override or model_cfg.get("base_url", "https://api.deepseek.com")
                resolved_api_version = model_cfg.get("api_version", "v1")
                resolved_temperature = temperature if temperature is not None else model_cfg.get("temperature", DEFAULT_TEMPERATURE)
            else:
                resolved_base_url = base_url_override or get_base_url(model_id)
                resolved_api_version = "v1"
                if model_id and config.get("models") and config["models"].get(model_id):
                    resolved_api_version = config["models"][model_id].get("api_version", "v1")
                resolved_temperature = temperature if temperature is not None else DEFAULT_TEMPERATURE

            if not resolved_base_url:
                raise ValueError(f"无法解析 Base URL，请检查 model_id={model_id} 配置或传入 base_url_override")

            get_logger().debug(f"[ai_concat] OpenAI兼容调用", trace_id=trace_id, model=resolved_model, 
                              base_url=resolved_base_url, api_version=resolved_api_version, 
                              temperature=resolved_temperature, api_key_set=bool(resolved_api_key))

            return _openai_compatible_call(
                messages, resolved_model, resolved_api_key, resolved_base_url,
                provider=backend, trace_id=trace_id, tools=tools, 
                response_format=response_format, api_version=resolved_api_version,
                temperature=resolved_temperature
            )

    except Exception as e:
        get_logger().error(f"[ai_concat] AI调用初始化失败: {e}", trace_id=trace_id, model_id=model_id, 
                          exc_info=True)
        raise RuntimeError(f"AI服务不可用: {e}") from e


def _ollama_call(messages, model, trace_id="", tools=None, response_format=None, temperature=DEFAULT_TEMPERATURE):
    url = "http://localhost:11434/api/chat"

    # 防御性检查：拒绝把 DeepSeek 在线模型名错误路由到本地 Ollama
    if model and model in ("deepseek-chat", "deepseek-coder", "deepseek-reasoner", "deepseek-v3"):
        msg = f"拒绝路由：模型 '{model}' 是 DeepSeek 在线模型，不应调用本地 Ollama。请检查 model_choice.provider 配置。"
        get_logger().error(msg, trace_id=trace_id)
        raise ValueError(msg)

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature}
    }
    if tools:
        payload["tools"] = tools
    fmt = _map_response_format(response_format)
    if fmt:
        payload["format"] = fmt
    t0 = time.perf_counter()
    try:
        response = _session.post(url, json=payload, timeout=30)
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
            trace_id=trace_id, span_name="llm.ollama", node_type="llm",
            duration_ms=elapsed_ms, provider="ollama", model_name=model,
            model_params=f'{{"temperature":{temperature}}}', status="error",
            error=error_detail[:500]
        )
        total_chars = sum(len(m.get("content", "")) for m in messages)
        logger.error(f"Ollama 请求失败: {error_detail[:300]} | messages={len(messages)}条, 总计{total_chars}字符")
        return {"message": {"content": "", "error": error_detail, "reasoning_content": ""}}


def _ollama_call_stream(messages, model, trace_id="", tools=None, response_format=None, temperature=DEFAULT_TEMPERATURE):
    url = "http://localhost:11434/api/chat"

    # 防御性检查：拒绝把 DeepSeek 在线模型名错误路由到本地 Ollama
    if model and model in ("deepseek-chat", "deepseek-coder", "deepseek-reasoner", "deepseek-v3"):
        msg = f"拒绝路由：模型 '{model}' 是 DeepSeek 在线模型，不应调用本地 Ollama。请检查 model_choice.provider 配置。"
        get_logger().error(msg, trace_id=trace_id)
        raise ValueError(msg)

    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "options": {"temperature": temperature},
    }
    if tools:
        payload["tools"] = tools
    fmt = _map_response_format(response_format)
    if fmt:
        payload["format"] = fmt
    t0 = time.perf_counter()
    try:
        response = _session.post(url, json=payload, stream=True, timeout=(30,120))
        response.raise_for_status()

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

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        log_trace(
            trace_id=trace_id, span_name="llm.ollama", node_type="llm",
            duration_ms=elapsed_ms, provider="ollama", model_name=model,
            model_params=f'{{"temperature":{temperature},"stream":true}}', status="success",
            input_summary=str(messages)[:200],
            output_summary=full_content[:200]
        )
        yield {"type": "result", "content": full_content, "reasoning_content": full_reasoning}
    except Exception as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        log_trace(
            trace_id=trace_id, span_name="llm.ollama", node_type="llm",
            duration_ms=elapsed_ms, provider="ollama", model_name=model,
            model_params=f'{{"temperature":{temperature},"stream":true}}', status="error",
            error=str(e)[:500]
        )
        logger.error(f"Ollama 流式请求失败: {e}")
        yield {"type": "error", "error": str(e)}


def _openai_compatible_call(messages, model, api_key, base_url, 
                            provider="openai_compatible", trace_id="", 
                            tools=None, response_format=None, api_version="v1", temperature=DEFAULT_TEMPERATURE):
    """通用 OpenAI 兼容接口调用（DeepSeek / 豆包 / 通义等均适用）"""
    if not api_key:
        logger.error(f"{provider} API Key 为空，无法发起调用（model={model}, base_url={base_url}）")
        return {"message": {"content": "", "error": f"{provider} API Key 未配置，请在设置中添加 API Key", "reasoning_content": ""}}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": False
    }
    if tools:
        payload["tools"] = tools
    if response_format:
        payload["response_format"] = response_format

    normalized_url = _normalize_base_url(base_url)
    t0 = time.perf_counter()
    try:
        response = _session.post(f"{normalized_url}/{api_version}/chat/completions", 
                                headers=headers, json=payload, timeout=60)
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
        return {"message": {"content": "", "error": error_detail, "reasoning_content": ""}}


def _openai_compatible_call_stream(messages, model, api_key, base_url,
                                   provider="openai_compatible", trace_id="",
                                   tools=None, response_format=None, api_version="v1", temperature=DEFAULT_TEMPERATURE):
    """通用 OpenAI 兼容流式调用"""
    if not api_key:
        logger.error(f"{provider} API Key 为空，无法发起流式调用（model={model}, base_url={base_url}）")
        yield {"type": "error", "error": f"{provider} API Key 未配置，请在设置中添加 API Key"}
        return
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
    if response_format:
        payload["response_format"] = response_format

    normalized_url = _normalize_base_url(base_url)
    t0 = time.perf_counter()
    try:
        response = _session.post(f"{normalized_url}/{api_version}/chat/completions",
                                headers=headers, json=payload, stream=True, timeout=(30, 120))
        response.raise_for_status()
        full_content = ""
        full_reasoning = ""
        accumulated_tool_calls = {}
        max_duration = 300

        for line in response.iter_lines(decode_unicode=True):
            if time.perf_counter() - t0 > max_duration:
                raise TimeoutError(f"流式请求超时，超过 {max_duration} 秒")
            if not line.strip() or not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            delta = chunk.get("choices", [{}])[0].get("delta", {})
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

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        log_trace(
            trace_id=trace_id, span_name=f"llm.{provider}", node_type="llm",
            duration_ms=elapsed_ms, provider=provider, model_name=model,
            model_params=f'{{"temperature":{temperature},"stream":true}}', status="success",
            input_summary=str(messages)[:200],
            output_summary=full_content[:200]
        )
        yield {"type": "result", "content": full_content, "reasoning_content": full_reasoning}

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
            model_params=f'{{"temperature":{temperature},"stream":true}}', status="error",
            error=error_detail[:500]
        )
        logger.error(f"{provider} 流式请求失败: {error_detail[:300]}")
        yield {"type": "error", "error": str(e)}


def parse_ai_json(content):
    from json_repair import loads as repair_loads
    import logging
    logger = logging.getLogger(__name__)
    try:
        clean_content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()

        action_match = re.search(r'```action\s*([\s\S]*?)\s*```', clean_content)
        if action_match:
            clean_content = action_match.group(1).strip()
            logger.debug(f"parse_ai_json: 提取action代码块成功，内容长度: {len(clean_content)}")
        else:
            reasoning_match = re.search(r'```reasoning\s*([\s\S]*?)\s*```', clean_content)
            if reasoning_match:
                clean_content = reasoning_match.group(1).strip()
                logger.debug(f"parse_ai_json: 提取reasoning代码块成功，内容长度: {len(clean_content)}")
            else:
                json_match = re.search(r'```json\s*([\s\S]*?)\s*```', clean_content)
                if json_match:
                    clean_content = json_match.group(1).strip()
                    logger.debug(f"parse_ai_json: 提取json代码块成功，内容长度: {len(clean_content)}")
                else:
                    logger.debug(f"parse_ai_json: 未找到action/reasoning/json代码块，原始内容前200字符: {content[:200]}")

        start_brace = clean_content.find('{')
        if start_brace != -1:
            depth = 0
            in_string = False
            escape = False
            for i in range(start_brace, len(clean_content)):
                c = clean_content[i]
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
                            obj = repair_loads(clean_content[start_brace:i + 1])
                            if isinstance(obj, dict) and ("tool_name" in obj or "tool_call" in obj or "tool" in obj or "final_message" in obj or "progress" in obj):
                                return obj
                        except Exception:
                            pass

        result = repair_loads(clean_content)
        if result is not None and isinstance(result, dict):
            return result

        start = clean_content.find('{')
        if start != -1:
            depth = 0
            in_string = False
            escape = False
            for i in range(start, len(clean_content)):
                c = clean_content[i]
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
                        return repair_loads(clean_content[start:i + 1])
        return None
    except Exception as e:
        logger.error(f"JSON 解析异常: {e}")
        return None


_models_cache = None
_models_cache_mtime = 0
_models_cache_lock = threading.Lock()


def invalidate_models_cache():
    global _models_cache, _models_cache_mtime
    with _models_cache_lock:
        _models_cache = None
        _models_cache_mtime = 0


def get_model_from_cache(model_id):
    global _models_cache
    if _models_cache is None:
        get_available_models()
    if _models_cache:
        return next((m for m in _models_cache if m["id"] == model_id), None)
    return None


def get_available_models():
    global _models_cache, _models_cache_mtime
    
    env_key = os.environ.get("MYAGENT_API_KEY") or os.environ.get("API_KEY", "")
    env_model = os.environ.get("API_MODEL", "")
    env_url = os.environ.get("API_BASE_URL", "https://api.deepseek.com")

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
                            "base_url": m.get("base_url", "https://api.deepseek.com"),
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
                    "base_url": "https://api.deepseek.com",
                    "api_version": "v1",
                    "temperature": DEFAULT_TEMPERATURE,
                    "stream": True
                },
                {
                    "id": "ollama-r1",
                    "display": "本地 Ollama (deepseek-r1:7b)",
                    "provider": "ollama",
                    "model": "deepseek-r1:7b",
                    "base_url": "http://localhost:11434",
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
                "base_url": "https://api.deepseek.com",
                "api_version": "v1",
                "temperature": DEFAULT_TEMPERATURE,
                "stream": True
            },
            {
                "id": "ollama-r1",
                "display": "本地 Ollama (deepseek-r1:7b)",
                "provider": "ollama",
                "model": "deepseek-r1:7b",
                "base_url": "http://localhost:11434",
                "api_version": "v1",
                "temperature": DEFAULT_TEMPERATURE,
                "stream": True
            }
        ]
    
    return models


def _deepseek_call(messages, model, api_key, base_url,
                   provider="deepseek", trace_id="", tools=None, response_format=None):
    return _openai_compatible_call(messages, model, api_key, base_url,
                                   provider=provider, trace_id=trace_id, 
                                   tools=tools, response_format=response_format)


def _deepseek_call_stream(messages, model, api_key, base_url,
                          provider="deepseek", trace_id="", tools=None, response_format=None,temperature=DEFAULT_TEMPERATURE):
    return _openai_compatible_call_stream(messages, model, api_key, base_url,
                                          provider=provider, trace_id=trace_id,
                                          tools=tools, response_format=response_format,temperature=temperature)


def ai_concat_stream(messages, model_name=None, provider=None, trace_id="",
                     tools=None, response_format=None, base_url_override=None, api_version="v1", model_id=None):
    """统一 AI 流式调用入口：Ollama 走专属逻辑，其余走通用 OpenAI 兼容协议"""
    try:
        get_logger().debug(f"[ai_concat_stream] 开始调用", trace_id=trace_id, model_id=model_id, 
                          provider=provider, messages_count=len(messages) if messages else 0)

        config = _load_config()

        model_cfg = get_model_from_cache(model_id) if model_id else None

        # 路由优先级：显式传参 > models.json 的 provider > config.yaml backend
        if provider:
            backend = provider.lower().strip()
        elif model_cfg:
            backend = model_cfg.get("provider", "deepseek").lower().strip()
        else:
            backend = config.get("backend", "deepseek").lower().strip()

        if backend == "ollama":
            model = model_name or (model_cfg.get("model") if model_cfg else None) or config.get("model", "deepseek-r1:7b")
            temperature = model_cfg.get("temperature", DEFAULT_TEMPERATURE) if model_cfg else DEFAULT_TEMPERATURE
            get_logger().debug(f"[ai_concat_stream] Ollama调用", trace_id=trace_id, model=model, temperature=temperature)
            yield from _ollama_call_stream(messages, model, trace_id=trace_id,
                                           tools=tools, response_format=response_format, temperature=temperature)
        else:
            resolved_model = model_name or (model_cfg.get("model") if model_cfg else None) or config.get("model", "deepseek-chat")
            resolved_api_key = get_api_key(model_id)
            
            if model_cfg:
                resolved_base_url = base_url_override or model_cfg.get("base_url", "https://api.deepseek.com")
                resolved_api_version = model_cfg.get("api_version", "v1")
                resolved_temperature = model_cfg.get("temperature", DEFAULT_TEMPERATURE)
            else:
                resolved_base_url = base_url_override or get_base_url(model_id)
                resolved_api_version = api_version
                if model_id and config.get("models") and config["models"].get(model_id):
                    resolved_api_version = config["models"][model_id].get("api_version", "v1")
                resolved_temperature = DEFAULT_TEMPERATURE

            if not resolved_base_url:
                raise ValueError(f"无法解析 Base URL，请检查 model_id={model_id} 配置或传入 base_url_override")

            get_logger().debug(f"[ai_concat_stream] OpenAI兼容调用", trace_id=trace_id, model=resolved_model, 
                              base_url=resolved_base_url, api_version=resolved_api_version, 
                              temperature=resolved_temperature, api_key_set=bool(resolved_api_key))

            yield from _openai_compatible_call_stream(
                messages, resolved_model, resolved_api_key, resolved_base_url,
                provider=backend, trace_id=trace_id, tools=tools,
                response_format=response_format, api_version=resolved_api_version,
                temperature=resolved_temperature
            )

    except Exception as e:
        get_logger().error(f"[ai_concat_stream] AI调用初始化失败: {e}", trace_id=trace_id, model_id=model_id, 
                          exc_info=True)
        yield {"type": "error", "error": f"AI服务不可用: {e}"}
