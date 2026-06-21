import os
import json
import base64
import requests
import re
import time
from memory import log_trace

# ================== 配置加解密（cryptography.fernet，密钥来自环境变量） ==================
import os as _os_for_config
from cryptography.fernet import Fernet

CONFIG_FILE = "config.enc"
KEY_FILE = ".myagent_key"
ENCODING = "utf-8"

# 旧版 XOR 密钥，仅用于一次性迁移已有 config.enc
_LEGACY_XOR_KEY = "Z2VuZXJhdGUtYS1yYW5kb20tc2VjcmV0LWtleS0xMjM0NTY3OA=="


def _get_fernet():
    if not _os_for_config.path.exists(KEY_FILE):
        raise RuntimeError(
            "加密密钥文件 .myagent_key 不存在。\n"
            "请先运行 setup_config.py 生成配置。"
        )
    with open(KEY_FILE, 'r', encoding=ENCODING) as f:
        key = f.read().strip()
    if not key:
        raise RuntimeError("密钥文件 .myagent_key 为空，请重新运行 setup_config.py。")
    return Fernet(key.encode())


def _fernet_encrypt(plain_text: str) -> str:
    return _get_fernet().encrypt(plain_text.encode()).decode()


def _fernet_decrypt(encrypted_text: str) -> str:
    return _get_fernet().decrypt(encrypted_text.encode()).decode()


def _legacy_xor_decrypt(encrypted_text: str) -> str:
    """一次性：用旧 XOR 方案解密已有 config.enc，供迁移用"""
    key_bytes = _LEGACY_XOR_KEY.encode(ENCODING)
    data = base64.urlsafe_b64decode(encrypted_text.encode(ENCODING))
    result = bytearray()
    for i, b in enumerate(data):
        result.append(b ^ key_bytes[i % len(key_bytes)])
    return result.decode(ENCODING)


def load_config():
    if not _os_for_config.path.exists(CONFIG_FILE):
        print("⚠️ 未找到加密配置文件，默认使用本地 Ollama。")
        return {"backend": "ollama", "model": "deepseek-r1:7b"}

    try:
        with open(CONFIG_FILE, 'r', encoding=ENCODING) as f:
            encrypted = f.read()
        # 优先 Fernet，失败则尝试旧 XOR 格式迁移
        try:
            decrypted = _fernet_decrypt(encrypted)
        except RuntimeError:
            raise
        except Exception:
            print("🔄 检测到旧版加密格式，正在迁移...")
            decrypted = _legacy_xor_decrypt(encrypted)
            config = json.loads(decrypted)
            _save_config_raw(config["backend"], config.get("api_key"),
                             config.get("model", "deepseek-chat"),
                             config.get("base_url", "https://api.deepseek.com"))
            print("✅ 已迁移至 cryptography.fernet 加密。")
            return config
        config = json.loads(decrypted)
        return config
    except RuntimeError:
        raise  # 环境变量缺失，直接上抛
    except Exception as e:
        print(f"❌ 配置文件解密失败: {e}，回退到本地 Ollama。")
        return {"backend": "ollama", "model": "deepseek-r1:7b"}


def _save_config_raw(backend, api_key=None, model="deepseek-chat", base_url="https://api.deepseek.com"):
    """内部写盘函数（不依赖 load_config 的模块级调用）"""
    config = {"backend": backend, "model": model, "base_url": base_url}
    if backend != "ollama" and api_key:
        config["api_key"] = api_key
    plain = json.dumps(config, ensure_ascii=False)
    encrypted = _fernet_encrypt(plain)
    with open(CONFIG_FILE, 'w', encoding=ENCODING) as f:
        f.write(encrypted)


def save_config(backend, api_key=None, model="deepseek-chat", base_url="https://api.deepseek.com"):
    _save_config_raw(backend, api_key, model, base_url)
    print(f"✅ 配置已加密保存至 {CONFIG_FILE}")


# ================== 通用 AI 调用接口 ==================
# 运行时加载配置
AI_CONFIG = load_config()

# ── 可配置的 LLM 参数 ──
# 后续可从前端读取覆盖；当前默认值为 0.3（Agent 场景需要一定随机性）
DEFAULT_TEMPERATURE = 0.3


# 修改原有的 ai_concat，增加 provider 参数以覆盖后端
def ai_concat(messages, model_name=None, provider=None, trace_id=""):
    """
    统一的 AI 调用入口。
    provider: 可选 "ollama" 或 "deepseek"，若不传则使用配置文件的后端。
    model_name: 可选模型名，若不传则使用配置或默认值。
    trace_id: 可选追踪 ID，用于日志关联。
    """
    backend = (provider or AI_CONFIG.get("backend", "ollama")).lower().strip()

    if backend == "ollama":
        return _ollama_call(messages, model_name or AI_CONFIG.get("model", "deepseek-r1:7b"), trace_id=trace_id)
    else:
        # OpenAI 兼容 API（DeepSeek / 豆包 / 千问 等）
        resolved_model = model_name or AI_CONFIG.get("model", "deepseek-chat")
        # ── 模型名校验：过滤明显非 OpenAI 兼容模型名 ──
        if not resolved_model or "/" in resolved_model or "\\" in resolved_model:
            print(f"⚠️ 模型名异常 '{resolved_model}'，回退为 deepseek-chat")
            resolved_model = "deepseek-chat"
        elif ":" in resolved_model:
            print(f"⚠️ 模型名 '{resolved_model}' 疑似 Ollama 风格（含冒号），但后端为 {backend}，回退为 deepseek-chat")
            resolved_model = "deepseek-chat"
        return _deepseek_call(messages, resolved_model,
                              AI_CONFIG.get("api_key", ""),
                              AI_CONFIG.get("base_url", "https://api.deepseek.com"),
                              trace_id=trace_id)

def _ollama_call(messages, model, trace_id=""):
    """原有的 Ollama 调用逻辑"""
    url = "http://localhost:11434/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": DEFAULT_TEMPERATURE}
    }
    t0 = time.perf_counter()
    try:
        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        result = response.json()
        msg = result.get("message", {})
        # 保留 DeepSeek-R1 的 reasoning_content，后续在 app.py 中用作思考展示
        reasoning = msg.get("reasoning_content", "")
        output_summary = str(msg.get("content", ""))[:200]
        log_trace(
            trace_id=trace_id, span_name="llm.ollama", node_type="llm",
            duration_ms=elapsed_ms, provider="ollama", model_name=model,
            model_params=f'{{"temperature":{DEFAULT_TEMPERATURE}}}', status="success",
            input_summary=str(messages)[:200],
            output_summary=output_summary
        )
        return {"message": {"content": msg.get("content", ""), "reasoning_content": reasoning}}
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


def _ollama_call_stream(messages, model, trace_id=""):
    """Ollama 流式调用 — 逐步产出思考过程 chunk，最后产出完整结果"""
    url = "http://localhost:11434/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "format": "json",
        "options": {"temperature": DEFAULT_TEMPERATURE}
    }
    t0 = time.perf_counter()
    try:
        response = requests.post(url, json=payload, stream=True, timeout=(30, 120))
        response.raise_for_status()

        full_content = ""
        full_reasoning = ""

        for line in response.iter_lines(decode_unicode=True):
            if not line.strip():
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue

            content_chunk = chunk.get("message", {}).get("content", "")
            reasoning_chunk = chunk.get("message", {}).get("reasoning_content", "")

            if content_chunk:
                full_content += content_chunk
            if reasoning_chunk:
                full_reasoning += reasoning_chunk

            yield {"type": "chunk", "content": content_chunk, "reasoning": reasoning_chunk}

            if chunk.get("done"):
                break

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


def _deepseek_call(messages, model, api_key, base_url, trace_id=""):
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
    t0 = time.perf_counter()
    try:
        response = requests.post(f"{base_url}/v1/chat/completions", headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        data = response.json()
        content = data["choices"][0]["message"]["content"]
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


def _deepseek_call_stream(messages, model, api_key, base_url, trace_id=""):
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
        "response_format": {"type": "json_object"}
    }
    t0 = time.perf_counter()
    try:
        response = requests.post(f"{base_url}/v1/chat/completions", headers=headers, json=payload, stream=True, timeout=(30, 120))
        response.raise_for_status()

        full_content = ""
        full_reasoning = ""

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

            yield {"type": "chunk", "content": content_chunk, "reasoning": reasoning_chunk}

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
        json_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', clean_content)
        if json_match:
            clean_content = json_match.group(1).strip()

        # 优先检测多 JSON（json_repair 对多 JSON 不会失败，而是返回最后一个，会吞掉前面对象）
        lines = [l.strip() for l in clean_content.splitlines() if l.strip().startswith("{") and l.strip().endswith("}")]
        if len(lines) > 1:
            for line in lines:
                try:
                    obj = repair_loads(line)
                    if isinstance(obj, dict) and ("tool_name" in obj or "final_message" in obj or "progress" in obj):
                        return obj
                except Exception:
                    continue
            return None

        result = repair_loads(clean_content)
        if result is not None:
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
    返回可用模型列表
    字段说明：
        display: 前端下拉框展示名称
        provider: 模型服务商标识 (ollama / deepseek)
        model: 具体模型名称
    """
    # 内置双模型：本地Ollama + DeepSeek在线API
    models = [
        {
            "display": "本地 Ollama (deepseek-r1:7b)",
            "provider": "ollama",
            "model": "deepseek-r1:7b"
        },
        {
            "display": "DeepSeek (deepseek-chat)",
            "provider": "deepseek",
            "model": "deepseek-chat"
        }
    ]
    return models