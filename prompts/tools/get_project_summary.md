## get_project_summary

列出目录下所有代码文件及大小，快速了解项目结构。

何时用：接手新项目、确认已有文件列表。
何时不用：搜特定文件 → search_files；搜文件内容 → search_content；看内容 → read_file。

参数：directory: str（可选）默认 work_space。
只返回代码类文件（.py/.html/.js/.css/.json/.md），自动跳过依赖目录。
