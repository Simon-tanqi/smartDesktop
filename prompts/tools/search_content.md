## search_content

在目录下递归搜索包含关键词的文件行。返回 file、line、content 列表。

何时用：找函数/类/变量定义、定位代码位置、修改前精确定位。
何时不用：已知文件和行号 → 用 read_file；搜文件名 → 用 search_files。

参数：
- keyword: str（必填）不区分大小写
- directory: str（可选）默认 work_space
- file_pattern: str（默认 *）如 *.py、*.html
- max_results: int（默认 10）

自动跳过 node_modules / __pycache__ / .git。修改代码前，先 search 定位，再 read 确认，最后 replace。
