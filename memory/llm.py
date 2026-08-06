"""
LLM 调用适配层：支持 Ollama / OpenAI / DeepSeek 等后端

v5: 主模型 deepseek-chat 默认复用；可选 summary_model 覆盖，不可用时自动降级回主模型。
"""
import json
import threading
import logging
from memory.config import COMPRESS_MODEL_CONFIG, EXTRACT_MODEL_CONFIG, SUMMARY_MODEL_CONFIG

logger = logging.getLogger(__name__)
if not logger.handlers:
    logger.setLevel(logging.WARNING)
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[%(name)s] %(levelname)s: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)

_ai_concat = None
_ai_concat_lock = threading.Lock()

# 会话级 provider/model 跟随（由 agent_state.prepare_run_state 注入）：
# 会话使用 ollama 本地模型时，记忆系统的辅助调用（压缩/提取/摘要）同样走
# ollama，避免回落 deepseek 云端造成 402（T-52~T-55 根因）。
_session_provider = None
_session_model = None
_session_lock = threading.Lock()


def set_session_provider(provider, model=None):
    """记录当前会话使用的 provider / model，供辅助调用路由跟随。"""
    global _session_provider, _session_model
    with _session_lock:
        _session_provider = provider
        if model:
            _session_model = model


def get_session_provider():
    return _session_provider


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
    try:
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
        choices = data.get("choices", [])
        if not choices:
            return None
        choice = choices[0]
        content = choice.get("message", {}).get("content", "")
        return {"message": {"content": content}}
    except Exception:
        return None


def _default_call(messages, model=None, config=None):
    """使用 llm_client.ai_concat（原有后端）。

    路由跟随会话：若会话已注入 provider（如 ollama），则显式传给 ai_concat，
    避免回落 config.backend 静默切到 DeepSeek 云端（T-52~T-55 402 根因）。
    """
    ai_concat = _get_ai_concat()
    if not ai_concat:
        return None
    use_provider = _session_provider or (config or {}).get("provider")
    use_model = model or _session_model or "deepseek-chat"
    # 会话为 ollama 时，禁止把 DeepSeek 在线模型名传给 ollama 路由
    # （会触发 llm_client._ollama_call 的防御性检查 ValueError）
    if use_provider == "ollama" and use_model in (
            "deepseek-chat", "deepseek-coder", "deepseek-reasoner", "deepseek-v3"):
        use_model = _session_model or "deepseek-r1:7b"
    return ai_concat(messages, model_name=use_model, provider=use_provider)


def _try_call(cfg, messages, model=None):
    """尝试用指定配置调用 LLM，返回结果或 None。"""
    provider = cfg.get("provider", "deepseek")
    if provider == "ollama":
        return _ollama_call(cfg, messages)
    else:
        return _default_call(messages, model or cfg.get("model_name", "deepseek-chat"))


_call_llm_lock = threading.Lock()


def call_llm(messages, model=None, config=None):
    """
    调用 LLM，自动根据 provider 路由，内置降级链：

      1. 当前 domain 配置（compression / extraction）── 优先使用
      2. summary_model（如果配置了）── domain 失败时回退
      3. 主模型 _default_call ── 最终兜底
    """
    cfg = config or COMPRESS_MODEL_CONFIG

    result = _try_call(cfg, messages, model)
    if result is not None:
        return result

    if SUMMARY_MODEL_CONFIG:
        logger.warning("domain 模型调用失败，降级到 summary_model")
        result = _try_call(SUMMARY_MODEL_CONFIG, messages, model)
        if result is not None:
            return result

    logger.warning("summary_model 调用失败，降级到主模型 ai_concat")
    return _default_call(messages, "deepseek-chat")


def call_llm_with_config(messages, model_config):
    """使用指定模型配置调用 LLM（压缩 / 提取分别用不同配置）。"""
    return call_llm(messages, config=model_config)


def set_llm_caller(func):
    """替换记忆系统使用的 LLM 调用函数。
    func 签名: fn(messages, model=None, config=None) -> dict | None
    """
    global call_llm
    with _call_llm_lock:
        call_llm = func