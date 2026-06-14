## search_content

**功能**：在目录下递归搜索文件内容，返回包含关键词的行及其文件路径和行号。

**何时使用**：
- 查找某个函数/类/变量的定义位置
- 搜索代码中的 TODO / FIXME 标记
- 找到使用了某个配置项的所有文件
- 修改代码前定位需要改动的位置

**何时不使用**：
- 已知文件和行号，只需读那几行 → 用 read_file（带 start_line/end_line）
- 搜索文件名而非内容 → 用 search_files
- 只是看项目有哪些文件 → 用 get_project_summary

**参数**：
- keyword: str（必填）搜索关键词，不区分大小写
- directory: str（可选）搜索目录，默认 work_space
- file_pattern: str（默认 `*`）文件名匹配模式，如 `*.py`
- max_results: int（默认 10）最大返回条数

**关键约束**：
- 自动跳过 node_modules / __pycache__ / .git 等目录
- 只搜索文本文件，二进制文件自动跳过
- 结果按搜索顺序返回，命中即加入

**错误处理**：
- 目录不存在 → 返回空数组
- 无匹配 → 返回空数组 `[]`
- 文件读取失败 → 跳过该文件继续搜索
