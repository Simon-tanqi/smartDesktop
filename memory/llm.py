"""
LLM 调用适配层：默认使用 Ollama，可通过 set_llm_caller 替换为任意 provider。
"""
import requests

_default_model = "qwen2:7b"


def _ollama_call(messages, model=None):
    """调用本地 Ollama 模型（内部实现）。"""
    url = "http://localhost:11434/api/chat"
    payload = {
        "model": model or _default_model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.3},
    }
    try:
        response = requests.post(url, json=payload, timeout=120)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"[Ollama] 请求失败: {e}")
        return None


# ── 可替换的 LLM 调用入口 ──
_llm_caller = _ollama_call


def set_llm_caller(func):
    """
    替换记忆系统使用的 LLM 调用函数。
    func 签名: fn(messages: list[dict], model: str) -> dict | None
    返回 dict 需包含 {"message": {"content": "..."}} 结构。
    """
    global _llm_caller
    _llm_caller = func


def call_llm(messages, model=None):
    """调用 LLM（使用当前注册的 caller）。"""
    return _llm_caller(messages, model=model)
