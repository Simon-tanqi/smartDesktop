## write_file

将内容写入新文件。父目录自动创建。路径限制在 work_space 内。

何时用：创建新代码文件、保存处理结果。
何时不用：修改已有文件的几行 → 用 replace_in_file。

参数：
- file_path: str（必填）绝对路径，必须在 work_space 内
- content: str（必填）原样写入，UTF-8 编码
- overwrite: bool（默认 true）

新建文件前，先判断：这应该是一个独立文件（代码>10行/用户要求生成），还是内联回答即可？
