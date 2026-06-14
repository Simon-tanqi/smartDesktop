# intent_router.py
import re
import json
from typing import Callable, Dict

# 意图类型注册表（可扩展）
INTENT_REGISTRY = {
    "file_manage":       "文件扫描、复制、移动、删除等本地文件操作",
    "code_generate":     "生成、重构、审查代码",
    "db_query":          "查询数据库、生成SQL语句",
    "general_chat":      "日常闲聊、通用问答",
    "architect":         "系统架构分析与方案设计",
    "code_review":       "代码审查，找 Bug 和安全隐患",
    "product_manager":   "产品规划与需求探索",
    "debugger":          "按流程排查 Bug 和异常",
    "growth":            "内容增长与用户获取策略",
    "boss":              "商业决策，投入产出分析与风险控制",
}

# 角色模式（这些意图会触发 persona_mode，替换 core.md 身份）
PERSONA_MODES = {"architect", "code_review", "product_manager", "debugger", "growth", "boss"}

# 各意图对应的参数提取提示模板
EXTRACT_PROMPTS = {
    "file_manage": """
    【文件操作规则】
    - 你可以使用 execute_command 工具在终端执行 Windows 命令（如 dir, copy, move, del, mkdir 等）。
    - 命令中的路径必须使用绝对路径，分隔符用 \\ 或 / 均可，但推荐用 \\。
    - 移动/复制文件前，先用 dir 扫描目录确认文件名。
    - 同样支持模糊目标：如果不知道目标文件夹全名，先 dir 父目录，然后根据输出匹配。
    - 若命令执行失败，请根据错误信息修正。
    """,
    "code_generate": """
    【代码生成专用规则】
    - 必须调用 get_workspace_path 获取工作空间绝对路径。
    - 自行编写完整、可运行的代码，并调用 write_file 将代码写入 workspace 目录下。
    - 文件名需自拟（如 bubble_sort.py），除非用户指定。
    - 如果任务需要多步骤（如创建项目、生成多个文件、编写测试），请在每完成一个关键步骤后用 progress 消息汇报进展。
    - 当你需要优化自己的代码参数（例如调整 LLM 温度、增加会话长度限制、修改工具路径等）时，你可以使用 read_file 读取当前源代码，然后使用 edit_file 进行修改。
    - 请在修改前备份原文件，或确保修改逻辑正确。
    """,
    "db_query": """
    【数据库查询专用规则】
    - 必须先确认数据库连接信息，未提供时询问用户。
    - 生成 SQL 语句后需展示给用户确认再执行（若工具支持）。
    """,
    "user_memory": """从用户指令中提取记忆操作，仅输出合法 JSON：
    {
      "action": "remember/recall",
      "key": "name/hobbies/long_term_goal",
      "value": "要记住的内容（仅 remember 时填写）"
    }
    注意：用户可能说“我叫张三”、“我的长期目标是...”、“我的爱好是...”，即使这些内容混合在其他需求中，也请提取。""",
    "general_chat": None  # 通用对话无需额外规则
}


def classify_intent(user_input: str, llm_call: Callable, model_name: str = None, provider: str = None) -> str:
    """
    使用传入的 llm_call 函数进行意图分类。
    llm_call 的签名：llm_call(messages, model_name=..., provider=...)
    """
    intent_list = "\n".join([f"- {k}: {v}" for k, v in INTENT_REGISTRY.items()])
    prompt = f"""将用户指令归类到以下意图之一，仅输出意图类型（如 file_manage）：
    {intent_list}

    如果无法匹配任何意图，请输出 general_chat，系统将尽力以通用方式处理。
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

# skills.py 或直接放在 intent_router.py 末尾
SKILL_PROMPTS = {
    "file_manage": """
    【文件操作专用规则】
    - 先使用 scan_directory 确认目录内容，再执行复制/移动/删除。
    - 目标文件夹不明确时，必须使用 resolve_folder 查找真实文件夹名（支持拼音/英文匹配）。
    - 源路径必须包含完整文件名（含扩展名），不得猜测。
    """,
    "code_generate": """
    【代码生成强制规则】
    1. **第一步**：必须调用 `get_workspace_path` 获取项目工作空间绝对路径。
    2. **生成位置**：所有代码文件都必须放在工作空间目录下，可以创建子文件夹（如 `frontend/`、`api/`）。
    3. **修改已有代码**：先用 `search_content` 定位，再用 `read_file` 读取片段，最后用 `replace_in_file` 精准替换，避免重写整个文件。
    4. **新建文件**：使用 `write_file`，文件路径必须基于第一步获取的工作空间路径拼接。
    5. **如果涉及前后端**：建议在 workspace 下分别创建 `frontend/` 和 `api/` 目录。
    6. **绝不** 直接读取整个文件（除非文件极小，小于50行）。修改代码前，必须先用 `search_content` 搜索关键函数名、类名或变量，定位到具体文件和行号。
    7. 使用 `read_file` 时，指定 `start_line` 和 `end_line`，只读取需要修改的上下文（前后各扩5行）。
    8. 若需了解项目结构，使用 `scan_directory` 查看文件列表，或要求用户提供说明，不要逐个打开所有文件。
    【路径强制规则】
    - 生成任何文件前，必须调用 `get_workspace_path` 获取工作空间根目录。
    - 所有文件路径必须是 `{workspace}/子目录/文件名` 的形式，严禁在 workspace 外创建文件。
    - 如果目标目录不存在，先使用 `create_dir` 创建（如果工具可用）或由 `write_file` 自动创建父目录。
    """,
    "db_query": """
    【数据库查询专用规则】
    - 必须先确认数据库连接信息，未提供时询问用户。
    - 生成 SQL 语句后需展示给用户确认再执行（若工具支持）。
    """,
    "user_memory": """
    【记忆管理专用规则】
    - 使用 remember_user_info 保存长期记忆（姓名/爱好/目标）。
    - 使用 recall_user_info 或 recall_past 查询历史记忆。
    """,
    "general_chat": "使用自然语言回复用户信息"  # 通用对话无需额外规则
}