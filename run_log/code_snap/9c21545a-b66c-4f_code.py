@log_execution()
def classify_intent(user_input: str, llm_call: Callable, model_name: str = None, provider: str = None) -> str:
    """
    使用传入的 llm_call 函数进行意图分类。
    llm_call 的签名：llm_call(messages, model_name=..., provider=...)
    """
    intent_list = "\n".join([f"- {k}: {v}" for k, v in INTENT_REGISTRY.items()])
    prompt = f"""将用户指令归类到以下意图之一，仅输出意图类型（如 file_manage）：
{intent_list}

用户输入：{user_input}
意图类型："""

    res = llm_call([{"role": "user", "content": prompt}], model_name=model_name, provider=provider)
    if not res:
        return "general_chat"

    content = res["message"]["content"].strip().lower()
    for intent_type in INTENT_REGISTRY:
        if intent_type in content:
            return intent_type
    return "general_chat"
