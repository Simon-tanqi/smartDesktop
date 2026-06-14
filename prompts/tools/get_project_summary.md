## get_project_summary

**功能**：列出目录下所有代码文件及其大小，帮助快速了解项目结构。

**何时使用**：
- 刚接手一个项目，需要了解文件组成
- 用户问"这个项目有哪些文件"
- 代码生成前，确认 work_space 下已有的文件

**何时不使用**：
- 需要搜索特定文件 → 用 search_files
- 需要搜索文件内容 → 用 search_content
- 需要看某个文件的内容 → 用 read_file
- 只看目录层级不关心文件大小 → 用 scan_directory

**参数**：
- directory: str（可选）要分析的目录，默认 work_space

**关键约束**：
- 只返回代码类文件（.py / .html / .js / .css / .json / .md）
- 自动跳过 node_modules / __pycache__ / .git
- 返回 JSON 数组，每项包含 file / path / size

**错误处理**：
- 目录不存在 → 返回空数组
- 无代码文件 → 返回空数组
