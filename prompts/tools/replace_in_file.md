## replace_in_file

将文件中第 N 次出现的 old_str 替换为 new_str。精准替换，不重写全文件。

何时用：修改函数名/变量名/配置值、删除某段代码、修复单行 bug。
何时不用：创建新文件 → 用 write_file；不确定改什么 → 先用 search_content。

参数：
- file_path: str（必填）绝对路径
- old_str: str（必填）必须与文件内容完全一致（含空格、缩进、换行）
- new_str: str（必填）替换后的新内容
- occurrence: int（默认 1）第几次出现

old_str 匹配失败时，用 read_file 重新确认上下文，不要退化为 write_file 重写全文件。
