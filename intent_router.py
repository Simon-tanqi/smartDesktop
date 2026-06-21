# intent_router.py
# 意图路由已升级为本地关键词域匹配（见 tools/__init__.py: match_domains）。
# 本文件保留 SKILL_PROMPTS 供 tools/__init__.py 的 _get_skill_prompts 引用。

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
    """,
    "db_query": """
    【数据库查询专用规则】
    - 必须先确认数据库连接信息，未提供时询问用户。
    - 生成 SQL 语句后需展示给用户确认再执行（若工具支持）。
    """,
    "user_memory": """
    【记忆管理专用规则】
    - 记忆系统已升级为自动提取 + Markdown 存储。
    - 系统会自动从对话中提取用户信息（姓名/爱好/目标）并持久化。
    - 查询历史记忆时直接进行对话，系统会自动注入相关内容。
    """,
    "web_browse": """
    【网页浏览专用规则】
    - 使用 web_search 搜索互联网获取最新信息。
    - 搜索后可用 fetch_webpage 深入阅读重要链接。
    - 查询天气使用 get_weather，默认城市深圳。
    - 对时效性问题（新闻、最新资讯、实时数据）优先搜索而非凭记忆回答。
    """,
    "general_chat": "使用自然语言回复用户信息",
    "system_config": """
    【系统配置专用规则】
    - 修改注册表前，先用 modify_registry 的 query 操作确认当前状态。
    - 注册表修改后必须告知用户生效方式（重启 explorer.exe 或注销）。
    - winget 安装直接执行，msiexec 卸载需要用户确认（两步流程）。
    - 当 execute_command 返回 confirmation_required 时，输出 final_message 向用户展示待执行的命令并请求确认。
    - 用户确认后，在新一轮对话中使用 confirmed=true 重新调用 execute_command。
    """,
}
