"""搜索工具：文件搜索"""
import os
import json

from . import register_tool, path_params

SKIP_DIRS = {
    "Windows", "Program Files", "Program Files (x86)", "ProgramData",
    "$RECYCLE.BIN", "System Volume Information", "Recovery",
    "node_modules", "__pycache__", ".git", ".vscode", ".idea",
    "AppData", "Local Settings", "Application Data", "Temp"
}


@register_tool
@path_params("search_path")
def search_files(keyword: str, search_path: str = None, file_extensions: list = None, max_results: int = 5):
    """按文件名关键词搜索文件。search_path 可选，默认搜索用户目录及 D/E/F 盘。"""
    if not search_path:
        candidates = [
            os.path.expanduser("~"),
            os.path.join(os.path.expanduser("~"), "Desktop"),
            "D:\\", "E:\\", "F:\\"
        ]
    else:
        candidates = [search_path]
    results = []
    for base in candidates:
        if not os.path.exists(base):
            continue
        for root, dirs, files in os.walk(base, topdown=True, onerror=lambda e: None):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in SKIP_DIRS]
            depth = root[len(base):].count(os.sep)
            if depth > 3:
                continue
            for f in files:
                if keyword.lower() in f.lower():
                    if file_extensions:
                        if os.path.splitext(f)[1].lower() not in file_extensions:
                            continue
                    results.append(os.path.join(root, f))
                    if len(results) >= max_results:
                        return json.dumps(results, ensure_ascii=False)
    return json.dumps(results, ensure_ascii=False)
