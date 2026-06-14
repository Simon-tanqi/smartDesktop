## execute_command

**功能**：在终端执行一条 Windows 命令，返回执行结果。受白名单和安全模式约束。

**何时使用**：
- 需要创建/删除目录（mkdir / rmdir）
- 需要列出文件详情（dir）
- 需要运行 Python 脚本（python）
- 需要执行 git / npm / pip 等开发命令
- 需要查看系统信息（systeminfo）

**何时不使用**：
- 移动/复制单个文件 → 优先用 move_file / copy_file（更安全）
- 扫描目录 → 用 scan_directory（返回结构化 JSON）
- 搜索文件 → 用 search_files
- 需要读写文件内容 → 用 read_file / write_file

**参数**：
- command: str（必填）完整的命令字符串，如 `dir C:\Users /b`
- working_dir: str（可选）命令执行的工作目录

**关键约束**：
- 命令必须在白名单内（dir / copy / move / del / mkdir / rmdir / echo / type / findstr / where / python / pip / git / npm / node / systeminfo / wmic / powershell / cmd）
- 禁止模式：`rm -rf /`、`del /f /s`、`format`、`shutdown`
- 路径使用反斜杠 `\\`，优先用绝对路径
- 超时时间 30 秒
- 编码使用 GBK（Windows 中文环境）

**错误处理**：
- 命令不在白名单 → 返回安全限制提示，列出可用命令
- 包含禁止模式 → 返回安全限制提示
- 执行失败（非零返回码）→ 返回 stderr 内容
- 超时 → 返回 "命令执行超时"
- 命令解析失败（引号不配对）→ 提示检查引号
