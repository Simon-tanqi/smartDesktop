## get_workspace_path

**功能**：返回项目 work_space 目录的绝对路径。所有代码生成任务的**第一步必须调用此工具**。

**何时使用**：
- 任何代码生成任务的第一步（强制）
- 需要知道代码文件应该放在哪里
- 拼接 write_file 的文件路径时

**何时不使用**：
- 已有 workspace 路径且本次任务无需生成新文件
- 操作的是用户本地文件（非代码生成场景）→ 用 scan_directory / search_files

**参数**：无

**关键约束**：
- 返回 JSON：`{"workspace": "D:\\PyCharm Project\\MyAgent\\work_space"}`
- 如果 work_space 目录不存在会自动创建
- 所有 write_file 的路径必须以此路径为前缀

**错误处理**：
- 极少失败；创建目录失败时返回异常信息
