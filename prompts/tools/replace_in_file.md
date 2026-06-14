## replace_in_file

**功能**：将文件中第 N 次出现的指定字符串替换为新字符串。精准替换，不重写整个文件。

**何时使用**：
- 修改函数名、变量名、配置值
- 删除某段代码（new_str 为空字符串）
- 修复某一行的小错误

**何时不使用**：
- 创建全新文件 → 用 write_file
- 不确定要改什么 → 先用 search_content 定位，再用 read_file 确认
- 需要同时替换多个不同位置 → 多次调用本工具，每次一个替换点
- 需要替换所有出现 → 循环调用直到 occurrence 超出 count

**参数**：
- file_path: str（必填）文件绝对路径
- old_str: str（必填）要被替换的原始字符串，需与文件中完全一致
- new_str: str（必填）替换后的新字符串
- occurrence: int（默认 1）替换第几次出现

**关键约束**：
- old_str 必须与文件内容精确匹配（包括空格、缩进、换行）
- 修改前必须先用 read_file 确认要替换的上下文
- occurrence 从 1 开始计数
- 替换后不会自动格式化，保持原文件编码风格

**错误处理**：
- 文件不存在 → `{"success": false, "message": "文件不存在: ..."}`
- old_str 未找到 → `{"success": false, "message": "未找到要替换的字符串"}`
- occurrence 超出范围 → 提示仅有 N 处出现
