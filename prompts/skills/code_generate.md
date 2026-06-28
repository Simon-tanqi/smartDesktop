# 代码生成规则

1. 先调 get_workspace_path 获取工作空间路径
2. 修改已有代码：search_content 定位 → read_file 读片段 → replace_in_file 精准替换
3. 新建文件：write_file，路径基于工作空间路径拼接
4. 前后端项目：分别建 frontend/ 和 api/ 子目录
5. 绝不全量读取大文件（>50行），先用 search_content 定位
6. read_file 指定 start_line/end_line，只读修改上下文（前后各扩10行）
7. 了解项目结构用 scan_directory，不逐个打开所有文件
