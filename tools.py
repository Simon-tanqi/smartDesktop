import os
import shutil
import json
import inspect

TOOL_REGISTRY = {}

def path_params(*params):
    """装饰器：标记函数的哪些参数是路径"""
    def decorator(func):
        func._path_params = list(params)
        return func
    return decorator

def register_tool(func):
    sig = inspect.signature(func)
    parameters = {}
    for name, param in sig.parameters.items():
        parameters[name] = {
            "type": str(param.annotation.__name__ if param.annotation != inspect.Parameter.empty else "any"),
            "default": param.default if param.default != inspect.Parameter.empty else "必填"
        }
    path_list = getattr(func, '_path_params', [])
    TOOL_REGISTRY[func.__name__] = {
        "function": func,
        "description": func.__doc__.strip() if func.__doc__ else "无描述",
        "parameters": parameters,
        "path_params": path_list
    }
    return func

def build_dynamic_system_prompt():
    clean_tools_desc = {}
    for name, info in TOOL_REGISTRY.items():
        clean_tools_desc[name] = {
            "description": info["description"],
            "parameters": info["parameters"]
        }
    tools_desc = json.dumps(clean_tools_desc, ensure_ascii=False, indent=2)

    return f'''你是一个智能 AI Agent。你可以通过调用工具来完成用户的复杂指令。

【可用工具列表】
{tools_desc}

【行动准则（非常重要）】
1. **不要猜测**：如果你需要的信息（如文件路径、ID、状态）不明确，**严禁自行编造**。
2. **善用工具**：如果你不确定目标是否存在，请先调用查询类工具（如 `scan_directory`）获取真实信息。
3. **循序渐进**：你可以分多次调用工具。例如：先扫描目录 -> 获取真实文件名 -> 再执行复制/移动。
4. **根据反馈调整**：如果工具返回了错误（如“路径不存在”），请根据错误信息修正你的参数，再次尝试。

【反循环与反思准则（极其重要）】
- 在决定下一步行动前，始终回顾你的操作历史。如果你发现你在重复相似的操作（如使用相同或极相似的工具、查询），这意味着你陷入了循环。
- 一旦意识到可能陷入循环，你必须立即改变策略。例如：尝试一个完全不同的工具、重新评估用户的目标，或者如果你认为已有足够信息就直接给出最终答案。
- 如果工具返回“找不到路径”或类似错误，**严禁**通过随意添加转义符或修改拼写来重试！你必须先调用 `scan_directory` 工具去查看真实的目录结构。
- 中文匹配不到时，可以考虑转化为拼音或英文。
- 若发现目标不明确，列出所有相关文件，找出相关性最高的进行操作。
- 严禁无意义地重复调用同一个工具。

【路径使用注意事项】
- Windows 路径请使用字母盘符 + 冒号 + 反斜杠，如 F:\\tmp，不要写成 F盘。
- 如果你不确定目标文件夹是否存在，先用 scan_directory 查看父目录。

【输出格式】
仅输出一个合法的 JSON 对象：
{{
  "tool_name": "函数名",
  "arguments": {{ "参数名": "参数值" }}
}}
如果任务已完成，请输出 {{"tool_name": "finished", "arguments": {{}}}} 表示结束。
'''

# -------------------- 工具函数 --------------------
@register_tool
@path_params("path")
def scan_directory(path: str):
    """扫描指定绝对路径下的第一层文件和文件夹，返回 JSON 数组。当需要确认目录内容、查找文件时使用。"""
    try:
        entries = [entry.name for entry in os.scandir(path)]
        return json.dumps(entries, ensure_ascii=False)
    except Exception as e:
        return f"扫描失败: {str(e)}"

@register_tool
@path_params("source", "destination")
def move_file(source: str, destination: str, overwrite: bool = True):
    """
    将文件移动到目标目录或重命名。
    destination 若为已存在的目录，则移动文件到该目录下（保留原文件名）。
    若 destination 的父目录存在但本身不存在，则当作重命名。
    其余情况返回错误。
    """
    source = os.path.normpath(source)
    destination = os.path.normpath(destination)

    if not os.path.isfile(source):
        return {"success": False, "message": f"源文件不存在: {source}"}

    if os.path.isdir(destination):
        dest_file = os.path.join(destination, os.path.basename(source))
    elif os.path.isdir(os.path.dirname(destination)):
        dest_file = destination
    else:
        return {"success": False, "message": f"目标路径无效（父目录不存在）: {destination}"}

    if os.path.exists(dest_file) and not overwrite:
        return {"success": False, "message": f"目标已存在且 overwrite=False: {dest_file}"}

    try:
        shutil.move(source, dest_file)
        return {"success": True, "message": f"成功移动到 {dest_file}"}
    except Exception as e:
        return {"success": False, "message": str(e)}

@register_tool
@path_params("source", "destination")
def copy_file(source: str, destination: str, overwrite: bool = True):
    """复制文件，参数含义与 move_file 相同。"""
    source = os.path.normpath(source)
    destination = os.path.normpath(destination)

    if not os.path.isfile(source):
        return {"success": False, "message": f"源文件不存在: {source}"}

    if os.path.isdir(destination):
        dest_file = os.path.join(destination, os.path.basename(source))
    elif os.path.isdir(os.path.dirname(destination)):
        dest_file = destination
    else:
        return {"success": False, "message": f"目标路径无效（父目录不存在）: {destination}"}

    if os.path.exists(dest_file) and not overwrite:
        return {"success": False, "message": f"目标已存在且 overwrite=False: {dest_file}"}

    try:
        shutil.copy2(source, dest_file)
        return {"success": True, "message": f"成功复制到 {dest_file}"}
    except Exception as e:
        return {"success": False, "message": str(e)}

SKIP_DIRS = {
    "Windows", "Program Files", "Program Files (x86)", "ProgramData",
    "$RECYCLE.BIN", "System Volume Information", "Recovery",
    "node_modules", "__pycache__", ".git", ".vscode", ".idea",
    "AppData", "Local Settings", "Application Data", "Temp"
}

@register_tool
@path_params("search_path")
def search_files(keyword: str, search_path: str = None, file_extensions: list = None, max_results: int = 5):
    if not search_path:
        candidates = [
            os.path.expanduser("~"),  # C:\Users\用户名
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
            # 跳过隐藏文件夹和系统目录
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in SKIP_DIRS]

            depth = root[len(base):].count(os.sep)
            if depth > 3:  # 保持深度限制
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

@register_tool
@path_params("parent_dir")
def resolve_folder(parent_dir: str, target_name: str):
    """在 parent_dir 下查找所有子文件夹，返回候选列表及匹配提示。"""
    if not os.path.isdir(parent_dir):
        return json.dumps({"success": False, "message": f"父目录不存在: {parent_dir}"})
    try:
        folders = [d for d in os.listdir(parent_dir) if os.path.isdir(os.path.join(parent_dir, d))]
        return json.dumps({
            "parent": parent_dir,
            "target_name": target_name,
            "folders": folders
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "message": str(e)})

# 其他工具（如 write_file）可继续按需添加，模式相同