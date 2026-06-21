## search_files

按文件名关键词递归搜索文件，默认搜索用户目录及 D/E/F 盘。

**参数：** keyword（必填）、search_path（可选）、file_extensions（可选，如 [".pdf"]）、max_results（默认 5）

**约束：** 深度限制 3 层，自动跳过系统目录。无匹配返回空数组。如需按内容搜索用 search_content。
