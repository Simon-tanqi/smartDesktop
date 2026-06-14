## read_file

**功能**：读取文件的全部或部分内容，返回文件路径、总行数和文本内容。

**何时使用**：
- 需要查看某个文件的内容
- 修改代码前，先读取要修改的上下文（指定 start_line / end_line）
- 验证 write_file 或 replace_in_file 的结果

**何时不使用**：
- 查找某段代码在哪个文件 → 先用 search_content 定位
- 只想看项目有哪些文件 → 用 get_project_summary 或 scan_directory
- 文件超过 200 行 → 务必指定 start_line / end_line，不要全量读取

**参数**：
- file_path: str（必填）文件绝对路径
- start_line: int（可选）起始行号（1-based），不传则从第 1 行开始
- end_line: int（可选）结束行号（1-based，包含该行），不传则读到末尾

**关键约束**：
- 大文件必须指定行范围，避免 token 浪费
- 修改代码前只读需要修改的上下文（前后各扩 5 行）
- 返回 JSON：`{"success": true, "file": "...", "total_lines": N, "content": "..."}`

**错误处理**：
- 文件不存在 → `{"success": false, "message": "文件不存在: ..."}`
- start_line > 总行数 → content 为空字符串
- 文件编码非 UTF-8 → 返回解码错误
