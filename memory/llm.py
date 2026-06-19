"""
LLM 调用适配层：通过 llm_client.ai_concat 统一路由，支持所有已配置的后端。
"""

# 默认模型（仅当调用方未指定 model 时生效）
_default_model = "deepseek-chat"


def call_llm(messages, model=None):
    """
    调用 LLM（通过 llm_client 统一入口，自动路由到配置的后端）。
    返回 dict 或 None，dict 需包含 {"message": {"content": "..."}}。
    """
    from llm_client import ai_concat  # 延迟导入，避免 memory 包初始化时尚未就绪

    result = ai_concat(messages, model_name=model or _default_model)
    if result is None:
        return None
    # ai_concat 返回 {"message": {"content": "..."}}，与旧接口兼容
    return result


def set_llm_caller(func):
    """
    替换记忆系统使用的 LLM 调用函数（保留兼容，但不再使用内部 caller）。
    func 签名: fn(messages, model) -> dict | None
    """
    global call_llm
    call_llm = func
