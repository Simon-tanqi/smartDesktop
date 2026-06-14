"""对话工具：记忆管理、日志追加"""
import json

from . import register_tool
from memory import load_long_term, save_long_term, save_ai_log


@register_tool
def remember_user_info(key: str, value: str):
    """记住用户的长期信息。key 可以是: name, hobbies, long_term_goal"""
    mem = load_long_term()
    if key not in mem:
        return json.dumps({"success": False, "message": f"无效的记忆字段: {key}"})
    if key == "hobbies" and isinstance(value, str):
        value = [h.strip() for h in value.replace("和", ",").split(",") if h.strip()]
    mem[key] = value
    save_long_term(mem)
    return json.dumps({"success": True, "message": f"记忆已更新: {key} = {value}"})


@register_tool
def append_to_log(content: str, filename: str = None):
    """将内容保存到 Markdown 日志文件中。filename 可选，默认按日期。"""
    save_ai_log(content, filename)
    return json.dumps({"success": True, "message": "内容已保存到日志"})
