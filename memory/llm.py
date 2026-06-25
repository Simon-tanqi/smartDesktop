"""
LLM 调用适配层：支持 Ollama / OpenAI / DeepSeek 等后端

v4: 支持从 YAML 配置读取 provider，支持 Ollama 本地模型。
"""
import json
import threading
from memory.config import COMPRESS_MODEL_CONFIG, EXTRACT_MODEL_CONFIG

# ── 缓存 ai_concat 引用 ──
_ai_concat = None
_ai_concat_lock = threading.Lock()


def _get_ai_concat():
    global _ai_concat
    if _ai_concat is None:
        with _ai_concat_lock:
            if _ai_concat is None:
                try:
                    from llm_client import ai_concat
                    _ai_concat = ai_concat
                except ImportError:
                    _ai_concat = False
    return _ai_concat


def _ollama_call(model_config, messages):
    """调用 Ollama API。"""
    import requests
    url = model_config.get("base_url", "http://localhost:11434/v1").rstrip("/")
    model_name = model_config.get("model_name", "deepseek-r1:7b")

    resp = requests.post(
        f"{url}/chat/completions",
        json={
            "model": model_name,
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": 4000,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        return None
    data = resp.json()
    choice = data.get("choices", [{}])[0]
    content = choice.get("message", {}).get("content", "")
    return {"message": {"content": content}}


def _default_call(messages, model):
    """使用 llm_client.ai_concat（原有后端）。"""
    ai_concat = _get_ai_concat()
    if not ai_concat:
        return None
    return ai_concat(messages, model_name=model or "deepseek-chat")


def call_llm(messages, model=None, config=None):
    """
    调用 LLM，自动根据 provider 路由。

    优先级：
      1. config 参数指定的模型配置
      2. 默认 COMPRESS_MODEL_CONFIG（蒸馏压缩）
    """
    cfg = config or COMPRESS_MODEL_CONFIG
    provider = cfg.get("provider", "ollama")

    if provider == "ollama":
        return _ollama_call(cfg, messages)
    else:
        return _default_call(messages, model or cfg.get("model_name", "deepseek-chat"))


def call_llm_with_config(messages, model_config):
    """使用指定模型配置调用 LLM（压缩 / 提取分别用不同配置）。"""
    return call_llm(messages, config=model_config)


def _build_messages(system_prompt, user_input):
    """构建标准 messages 列表。"""
    msgs = []
    if system_prompt:
        msgs.append({"role": "system", "content": system_prompt})
    msgs.append({"role": "user", "content": user_input})
    return msgs


def set_llm_caller(func):
    """替换记忆系统使用的 LLM 调用函数。func 签名: fn(messages, model) -> dict | None"""
    global call_llm
    call_llm = func
