"""
LLM 调用适配层：支持 Ollama / OpenAI / DeepSeek 等后端

v5: 主模型 deepseek-chat 默认复用；可选 summary_model 覆盖，不可用时自动降级回主模型。
"""
import json
import threading
import logging
from memory.config import COMPRESS_MODEL_CONFIG, EXTRACT_MODEL_CONFIG, SUMMARY_MODEL_CONFIG

logger = logging.getLogger(__name__)

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


def _try_call(cfg, messages, model):
    """尝试用指定配置调用 LLM，返回结果或 None。"""
    provider = cfg.get("provider", "deepseek")
    if provider == "ollama":
        return _ollama_call(cfg, messages)
    else:
        return _default_call(messages, model or cfg.get("model_name", "deepseek-chat"))


def call_llm(messages, model=None, config=None):
    """
    调用 LLM，自动根据 provider 路由，内置降级链：

      1. summary_model（如果配置了）── 优先使用
      2. 当前 domain 配置（compression / extraction）── 摘要模型失败时回退
      3. 主模型 _default_call ── 最终兜底
    """
    cfg = config or COMPRESS_MODEL_CONFIG

    # 第 1 层：摘要模型优先
    if SUMMARY_MODEL_CONFIG:
        result = _try_call(SUMMARY_MODEL_CONFIG, messages, model)
        if result is not None:
            return result
        logger.warning("summary_model 调用失败，降级到 domain 配置")

    # 第 2 层：domain 配置
    result = _try_call(cfg, messages, model)
    if result is not None:
        return result

    # 第 3 层：主模型兜底
    logger.warning("domain 模型调用失败，降级到主模型 ai_concat")
    return _default_call(messages, "deepseek-chat")


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
