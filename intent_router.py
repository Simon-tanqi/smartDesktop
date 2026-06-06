# intent_router.py
import re
import json
from typing import Callable, Dict

# 意图类型注册表（可扩展）
INTENT_REGISTRY = {
    "file_manage": "文件扫描、复制、移动、删除等本地文件操作",
    "code_generate": "生成、重构、审查代码",
    "db_query": "查询数据库、生成SQL语句",
    "general_chat": "日常闲聊、通用问答"
}

# 各意图对应的参数提取提示模板
EXTRACT_PROMPTS = {
    "file_manage": """从用户指令中提取文件操作的结构化参数，输出 JSON：
{{
  "action": "copy/move/delete/scan",
  "source_desc": "源文件或文件夹的自然描述（如 F盘tmp下的测试图片）",
  "destination_desc": "目标位置的自然描述（如 桌面、DouYin文件夹）"
}}""",
    "code_generate": """从用户指令中提取代码生成需求，输出 JSON：
{{
  "language": "编程语言（如 python, javascript）",
  "task": "具体任务描述（如 写一个快速排序函数）",
  "constraints": "额外约束（如 使用递归， 要求类型注解）"
}}""",
    "db_query": """从用户指令中提取数据库查询需求，输出 JSON：
{{
  "database": "目标数据库名称或描述",
  "tables": ["涉及的表名"],
  "query_intent": "查询意图描述（如 统计本月销售额）"
}}""",
    "general_chat": None  # 无需提取参数
}


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


def extract_params(user_input: str, intent_type: str, llm_call: Callable, model_name: str = None, provider: str = None) -> Dict:
    """根据意图类型提取结构化参数"""
    if intent_type not in EXTRACT_PROMPTS or EXTRACT_PROMPTS[intent_type] is None:
        return {}

    prompt = EXTRACT_PROMPTS[intent_type] + f"\n\n用户输入：{user_input}\n输出 JSON："
    res = llm_call([{"role": "user", "content": prompt}], model_name=model_name, provider=provider)
    if not res:
        return {}

    content = re.sub(r'<think>.*?</think>', '', res["message"]["content"], flags=re.DOTALL).strip()
    json_match = re.search(r'\{.*\}', content, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group())
        except:
            pass
    return {}