import os
import json
import base64
import requests
import re
import time
import yaml
from pathlib import Path
from memory import log_trace

# ================== 配置管理（keyring + 环境变量 + YAML 兜底） ==================
import keyring

_PROJECT_DIR = Path(__file__).parent
DATA_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'MyAgent')
CONFIG_YAML = os.path.join(DATA_DIR, 'config.yaml')
KEYRING_SERVICE = "MyAgent"
KEYRING_USERNAME = "api_key"


def _try_keyring_get():
    """安全地从 keyring 读取 API Key，失败返回 None。"""
    try:
        return keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except Exception:
        return None


def _try_keyring_set(api_key):
    """安全地向 keyring 写入 API Key，失败返回 False。"""
    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, api_key)
        return True
    except Exception:
        return False


def load_config():
    """
    加载配置。
    读取优先级：环境变量 > keyring > YAML 明文兜底。
    非敏感字段（backend / model / base_url）从 YAML 读取。
    """
    config = {"backend": "ollama", "model": "deepseek-r1:7b"}
    yaml_config = {}

    # 读 YAML（非敏感字段）
    if os.path.exists(CONFIG_YAML):
        try:
            with open(CONFIG_YAML, 'r', encoding='utf-8') as f:
                yaml_config = yaml.safe_load(f) or {}
            config.update({k: v for k, v in yaml_config.items() if k != "api_key"})
        except Exception as e:
            print(f"⚠️ 配置文件读取失败: {e}，使用默认配置。")

    # API Key：环境变量 > keyring > YAML 明文 > 空
    env_key = os.environ.get("MYAGENT_API_KEY") or os.environ.get("API_KEY")
    if env_key:
        config["api_key"] = env_key
    else:
        stored = _try_keyring_get()
        if stored:
            config["api_key"] = stored
        elif yaml_config.get("api_key"):
            config["api_key"] = yaml_config["api_key"]

    return config


def save_config(backend, api_key=None, model="deepseek-chat", base_url="https://api.deepseek.com"):
    """保存配置：API Key → keyring，非敏感 → YAML（keyring 不可用时明文兜底）。"""
    os.makedirs(DATA_DIR, exist_ok=True)

    yaml_config = {"backend": backend, "model": model, "base_url": base_url}
    keyring_ok = True

    if backend != "ollama" and api_key:
        if not _try_keyring_set(api_key):
            print("⚠️ 系统凭据管理器不可用，API Key 将明文存储。")
            yaml_config["api_key"] = api_key
            keyring_ok = False

    with open(CONFIG_YAML, 'w', encoding='utf-8') as f:
        yaml.dump(yaml_config, f, allow_unicode=True, default_flow_style=False)

    storage = "系统凭据管理器" if keyring_ok else "配置文件（明文）"
    print(f"✅ 配置已保存至 {CONFIG_YAML}（API Key → {storage}）")


def _save_config_raw(backend, api_key=None, model="deepseek-chat", base_url="https://api.deepseek.com"):
    """内部写盘（兼容旧调用）。"""
    save_config(backend, api_key, model, base_url)


# ================== 通用 AI 调用接口 ==================
# 惰性加载配置（不再作为模块级全局变量暴露解密后的 API Key）
_config_cache = None

def _load_config():
    """内部惰性加载，调用方不直接访问。环境变量优先于配置文件。"""
    global _config_cache
    if _config_cache is None:
        _config_cache = load_config()
    merged = dict(_config_cache)
    if os.environ.get("API_KEY"):
        merged["api_key"] = os.environ["API_KEY"]
    if os.environ.get("API_BASE_URL"):
        merged["base_url"] = os.environ["API_BASE_URL"]
    if os.environ.get("API_MODEL"):
        merged["model"] = os.environ["API_MODEL"]
    return merged

def get_config():
    """获取完整配置的浅拷贝（避免调用方意外修改缓存）。"""
    return dict(_load_config())

def get_api_key():
    return os.environ.get("API_KEY") or _load_config().get("api_key", "")

def get_base_url():
    return os.environ.get("API_BASE_URL") or _load_config().get("base_url", "https://api.deepseek.com")

# ── 可配置的 LLM 参数 ──
# 后续可从前端读取覆盖；当前默认值为 0.3（Agent 场景需要一定随机性）
DEFAULT_TEMPERATURE = 0.3


# 修改原有的 ai_concat，增加 provider 参数以覆盖后端
def ai_concat(messages, model_name=None, provider=None, trace_id="", tools=None, response_format=None):
    """
    统一的 AI 调用入口。
    provider: 可选 "ollama" 或 "deepseek"，若不传则使用配置文件的后端。
    model_name: 可选模型名，若不传则使用配置或默认值。
    trace_id: 可选追踪 ID，用于日志关联。
    tools: 工具列表，OpenAI 格式。
    response_format: 响应格式，如 {"type": "json_object"}。
    """
    env_key = os.environ.get("API_KEY", "")
    env_model = os.environ.get("API_MODEL", "")

    backend = (provider or _load_config().get("backend", "ollama")).lower().strip()
    if env_key and backend == "ollama":
        backend = "deepseek"

    if backend == "ollama":
        return _ollama_call(messages, model_name or _load_config().get("model", "deepseek-r1:7b"), 
                            trace_id=trace_id, tools=tools, response_format=response_format)
    else:
        resolved_model = model_name or env_model or _load_config().get("model", "deepseek-chat")
        if not resolved_model or "/" in resolved_model or "\\" in resolved_model:
            print(f"⚠️ 模型名异常 '{resolved_model}'，回退为 deepseek-chat")
            resolved_model = "deepseek-chat"
        elif ":" in resolved_model:
            print(f"⚠️ 模型名 '{resolved_model}' 疑似 Ollama 风格（含冒号），但后端为 {backend}，回退为 deepseek-chat")
            resolved_model = "deepseek-chat"
        return _deepseek_call(messages, resolved_model,
                              get_api_key(),
                              get_base_url(),
                              trace_id=trace_id, tools=tools, response_format=response_format)

def _ollama_call(messages, model, trace_id="", tools=None, response_format=None):
    """原有的 Ollama 调用逻辑"""
    url = "http://localhost:11434/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": DEFAULT_TEMPERATURE}
    }
    if tools:
        payload["tools"] = tools
    if response_format:
        payload["format"] = response_format.get("type", "")
    t0 = time.perf_counter()
    try:
        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        result = response.json()
        msg = result.get("message", {})
        # 保留 DeepSeek-R1 的 reasoning_content，后续在 app.py 中用作思考展示
        reasoning = msg.get("reasoning_content", "")
        content = msg.get("content", "") or ""
        # 处理 function calling：Ollama 也可能通过 tool_calls 返回
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
            model_params=f'{{"temperature":{DEFAULT_TEMPERATURE}}}', status="success",
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
            model_params=f'{{"temperature":{DEFAULT_TEMPERATURE}}}', status="error",
            error=error_detail[:500]
        )
        total_chars = sum(len(m.get("content", "")) for m in messages)
        print(f"❌ Ollama 请求失败: {error_detail[:300]} | messages={len(messages)}条, 总计{total_chars}字符")
        return None


def _ollama_call_stream(messages, model, trace_id="", format=None, tools=None, response_format=None):
    """Ollama 流式调用 — 逐步产出思考过程 chunk，最后产出完整结果"""
    url = "http://localhost:11434/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "options": {"temperature": DEFAULT_TEMPERATURE},
    }
    if format:
        payload["format"] = format
    if tools:
        payload["tools"] = tools
    if response_format:
        payload["format"] = response_format.get("type", "")
    t0 = time.perf_counter()
    try:
        response = requests.post(url, json=payload, stream=True, timeout=(30, 120))
        response.raise_for_status()

        full_content = ""
        full_reasoning = ""
        accumulated_tool_calls = {}  # {index: {"name":..., "arguments": ""}}

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

            # Ollama 流式 function calling
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

        # 将 function calling 的 tool_calls 转为 ```action 代码块
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
            model_params=f'{{"temperature":{DEFAULT_TEMPERATURE},"stream":true}}', status="success",
            input_summary=str(messages)[:200],
            output_summary=full_content[:200]
        )
        yield {"type": "result", "content": full_content, "reasoning_content": full_reasoning}
    except Exception as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        log_trace(
            trace_id=trace_id, span_name="llm.ollama", node_type="llm",
            duration_ms=elapsed_ms, provider="ollama", model_name=model,
            model_params=f'{{"temperature":{DEFAULT_TEMPERATURE},"stream":true}}', status="error",
            error=str(e)[:500]
        )
        print(f"❌ Ollama 流式请求失败: {e}")
        yield {"type": "error", "error": str(e)}


def _deepseek_call(messages, model, api_key, base_url, trace_id="", tools=None, response_format=None):
    """DeepSeek API 调用（兼容 OpenAI 格式）"""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": DEFAULT_TEMPERATURE,
        "stream": False
    }
    if tools:
        payload["tools"] = tools
    if response_format:
        payload["response_format"] = response_format
    t0 = time.perf_counter()
    try:
        response = requests.post(f"{base_url}/v1/chat/completions", headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        data = response.json()
        msg = data["choices"][0]["message"]
        content = msg.get("content", "") or ""
        # 处理 function calling：如果 LLM 通过 tool_calls 返回工具调用，转为 ```action 代码块
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
            trace_id=trace_id, span_name="llm.deepseek", node_type="llm",
            duration_ms=elapsed_ms, provider="deepseek", model_name=model,
            model_params=f'{{"temperature":{DEFAULT_TEMPERATURE}}}', status="success",
            input_summary=str(messages)[:200],
            output_summary=content[:200]
        )
        return {"message": {"content": content}}
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
            trace_id=trace_id, span_name="llm.deepseek", node_type="llm",
            duration_ms=elapsed_ms, provider="deepseek", model_name=model,
            model_params=f'{{"temperature":{DEFAULT_TEMPERATURE}}}', status="error",
            error=error_detail[:500]
        )
        total_chars = sum(len(m.get("content", "")) for m in messages)
        print(f"❌ DeepSeek API 请求失败: {error_detail[:300]} | messages={len(messages)}条, 总计{total_chars}字符")
        return None


def _deepseek_call_stream(messages, model, api_key, base_url, trace_id="", tools=None, response_format=None):
    """DeepSeek API 流式调用（兼容 OpenAI SSE 格式）"""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": DEFAULT_TEMPERATURE,
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
    if response_format:
        payload["response_format"] = response_format
    t0 = time.perf_counter()
    try:
        response = requests.post(f"{base_url}/v1/chat/completions", headers=headers, json=payload, stream=True, timeout=(30, 120))
        response.raise_for_status()

        full_content = ""
        full_reasoning = ""
        # 累积 function calling 的 tool_calls（DeepSeek 在传 tools 参数时通过此字段返回）
        accumulated_tool_calls = {}  # {index: {"id":..., "name":..., "arguments": ""}}

        for line in response.iter_lines(decode_unicode=True):
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

            # 处理 function calling 的 tool_calls 流式增量
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

        # 如果 LLM 通过 function calling 返回了工具调用，将其转换为 ```action 代码块追加到 content 末尾
        # 这样 parse_ai_json 可以正常解析，无需修改 agent.py 的解析逻辑
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
                # 如果 content 已有自然语言，在末尾追加 action 代码块
                if full_content and not full_content.endswith("\n"):
                    full_content += "\n"
                full_content += f"```action\n{tool_call_json}\n```"

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        log_trace(
            trace_id=trace_id, span_name="llm.deepseek", node_type="llm",
            duration_ms=elapsed_ms, provider="deepseek", model_name=model,
            model_params=f'{{"temperature":{DEFAULT_TEMPERATURE},"stream":true}}', status="success",
            input_summary=str(messages)[:200],
            output_summary=full_content[:200]
        )
        yield {"type": "result", "content": full_content, "reasoning_content": full_reasoning}
    except Exception as e:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        # 尝试读取响应体获取详细错误信息
        error_detail = str(e)
        try:
            if 'response' in locals():
                error_body = response.text[:500]
                error_detail = f"{e} | body: {error_body}"
        except Exception:
            pass
        log_trace(
            trace_id=trace_id, span_name="llm.deepseek", node_type="llm",
            duration_ms=elapsed_ms, provider="deepseek", model_name=model,
            model_params=f'{{"temperature":{DEFAULT_TEMPERATURE},"stream":true}}', status="error",
            error=error_detail[:500]
        )
        print(f"❌ DeepSeek API 流式请求失败: {error_detail[:300]}")
        yield {"type": "error", "error": str(e)}


# ================== 保留原有的工具函数 ==================
def parse_ai_json(content):
    from json_repair import loads as repair_loads
    try:
        clean_content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
        json_match = re.search(r'```(?:json|action)?\s*([\s\S]*?)\s*```', clean_content)
        if json_match:
            clean_content = json_match.group(1).strip()

        # 优先检测多 JSON（json_repair 对多 JSON 不会失败，而是返回最后一个，会吞掉前面对象）
        lines = [l.strip() for l in clean_content.splitlines() if l.strip().startswith("{") and l.strip().endswith("}")]
        if len(lines) > 1:
            for line in lines:
                try:
                    obj = repair_loads(line)
                    if isinstance(obj, dict) and ("tool_name" in obj or "tool_call" in obj or "tool" in obj or "final_message" in obj or "progress" in obj):
                        return obj
                except Exception:
                    continue
            # 多行 JSON 但都不匹配预期 key → 不返回 None，继续走后续 fallback

        result = repair_loads(clean_content)
        if result is not None and isinstance(result, dict):
            return result

        # 兜底：LLM 可能在 JSON 前后输出了文本，从 clean_content 中提取第一个完整 {...} 对象
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
        print(f"❌ JSON 解析异常: {e}")
        return None


def normalize_path_component(path_str):
    path_str = re.sub(r'([A-Za-z])\s*盘', r'\1:', path_str)
    path_str = path_str.replace('/', '\\')
    path_str = re.sub(r'^([A-Za-z]:)(?!\\)', r'\1\\', path_str)
    return os.path.normpath(path_str)

# 新增：获取可用模型列表（供 GUI 使用）
def get_available_models():
    """
    返回可用模型列表，优先从 models.json 配置文件读取。
    环境变量 API_KEY + API_MODEL 设置后，自动置顶为默认模型。
    """
    env_key = os.environ.get("API_KEY", "")
    env_model = os.environ.get("API_MODEL", "")
    env_url = os.environ.get("API_BASE_URL", "https://api.deepseek.com")

    models = []
    if env_key and env_model:
        models.append({
            "id": "env_" + env_model,
            "display": f"环境变量 ({env_model})",
            "provider": "deepseek",
            "model": env_model,
            "base_url": env_url,
            "temperature": 0.3,
            "stream": True
        })

    # 从 models.json 加载配置的模型
    try:
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models.json")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            for m in cfg.get("models", []):
                m_id = m.get("id", m.get("model", ""))
                if env_key and env_model and m_id == "env_" + env_model:
                    continue
                models.append({
                    "id": m_id,
                    "display": m.get("name", m.get("model", m_id)),
                    "provider": m.get("provider", "deepseek"),
                    "model": m.get("model", "deepseek-chat"),
                    "base_url": m.get("base_url", "https://api.deepseek.com"),
                    "temperature": m.get("temperature", 0.3),
                    "stream": m.get("stream", True)
                })
    except Exception:
        pass

    if not models:
        models = [
            {
                "id": "deepseek-chat",
                "display": "DeepSeek (deepseek-chat)",
                "provider": "deepseek",
                "model": "deepseek-chat",
                "base_url": "https://api.deepseek.com",
                "temperature": 0.3,
                "stream": True
            },
            {
                "id": "ollama-r1",
                "display": "本地 Ollama (deepseek-r1:7b)",
                "provider": "ollama",
                "model": "deepseek-r1:7b",
                "base_url": "http://localhost:11434",
                "temperature": 0.3,
                "stream": True
            }
        ]
    return models