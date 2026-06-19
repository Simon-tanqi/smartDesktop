## read_file

读取文件全部或部分内容。返回 file、total_lines、content。

何时用：查看文件内容、修改前读上下文、验证写入结果。
何时不用：找代码位置 → 先用 search_content；文件超过 200 行 → 必须指定 start_line/end_line。

参数：
- file_path: str（必填）绝对路径
- start_line: int（可选）起始行，1-based
- end_line: int（可选）结束行，1-based，包含该行

大文件(>200行)严禁全量读取。修改代码前只读需要修改的上下文（前后各扩 10 行）。
