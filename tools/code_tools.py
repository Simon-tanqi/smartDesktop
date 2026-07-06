"""代码操作工具：工作空间读写、内容搜索、文件替换、项目结构分析"""
import os
import json
import fnmatch

from . import register_tool, path_params


@register_tool
def get_workspace_path():
    """返回当前项目 work_space 文件夹的绝对路径。所有代码文件必须生成在此目录下。"""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    workspace = os.path.join(base, "work_space")
    os.makedirs(workspace, exist_ok=True)
    return json.dumps({"workspace": workspace})


@register_tool
@path_params("file_path")
def write_file(file_path: str, content: str, overwrite: bool = True):
    """安全写入文件。content 原样写入不转义。路径必须在 work_space 内。"""
    try:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        workspace = os.path.join(base, "work_space")
        os.makedirs(workspace, exist_ok=True)
        target_abs = os.path.abspath(file_path)
        workspace_abs = os.path.abspath(workspace)
        if not target_abs.startswith(workspace_abs):
            return json.dumps({"success": False, "message": f"非法路径！只能在 work_space 内操作: {target_abs}"}, ensure_ascii=False)
        parent_dir = os.path.dirname(target_abs)
        os.makedirs(parent_dir, exist_ok=True)
        if os.path.exists(target_abs) and not overwrite:
            return json.dumps({"success": False, "message": f"文件已存在且不允许覆盖: {target_abs}"}, ensure_ascii=False)
        with open(target_abs, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        return json.dumps({"success": True, "message": f"文件写入成功: {target_abs}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "message": f"写入失败: {str(e)}"}, ensure_ascii=False)


@register_tool
@path_params("file_path")
def read_file(file_path: str, start_line: int = None, end_line: int = None):
    """读取指定文件全部或行范围，返回 JSON（file, total_lines, content）。

    ⚠️ 大文件(>200行)严禁直接全量读取。正确策略：
    1. 先用 search_content(keyword, directory, file_pattern) 定位关键词所在行号
    2. 再用 read_file(start_line=行号-5, end_line=行号+30) 局部读取上下文
    3. 严禁通过缩小 start_line/end_line 范围来"搜索"——你只会重复读到文件开头区域
    """
    file_path = os.path.normpath(file_path)
    if not os.path.isfile(file_path):
        return json.dumps({"success": False, "message": f"文件不存在: {file_path}"})
    
    encodings = ["utf-8", "gbk", "gb2312", "gb18030", "big5"]
    
    for encoding in encodings:
        try:
            with open(file_path, "r", encoding=encoding) as f:
                lines = f.readlines()
            total = len(lines)
            if start_line is not None or end_line is not None:
                start = max(0, (start_line or 1) - 1)
                end = min(total, end_line or total)
                selected = lines[start:end]
            else:
                selected = lines
            content = "".join(selected)
            result = {"success": True, "file": file_path, "total_lines": total, "content": content, "encoding": encoding}
            is_full_read = (start_line is None and end_line is None)
            read_lines = len(selected)
            if is_full_read and total > 200 and len(content) > 4000:
                result["content"] = content[:4000]
                result["truncated"] = True
                result["hint"] = f"文件共{total}行，已截断仅显示前4000字符。请使用 search_content 定位关键代码后再局部读取。"
            return json.dumps(result, ensure_ascii=False)
        except UnicodeDecodeError:
            continue
        except Exception as e:
            return json.dumps({"success": False, "message": str(e)})
    
    try:
        with open(file_path, "rb") as f:
            raw = f.read(2000)
        return json.dumps({
            "success": False,
            "message": f"无法以文本模式读取文件 {file_path}，可能是二进制文件。文件前2000字节的十六进制预览: {raw.hex()[:200]}",
            "is_binary": True
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "message": str(e)})


@register_tool
@path_params("directory")
def search_content(keyword: str, directory: str = None, file_pattern: str = "*", max_results: int = 10):
    """在目录下搜索包含关键词的文件行。返回匹配文件、行号、行内容。"""
    if not directory:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        directory = os.path.join(base, "work_space")
    results = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('node_modules', '__pycache__', '.git')]
        for file in files:
            if not fnmatch.fnmatch(file, file_pattern):
                continue
            file_path = os.path.join(root, file)
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                for idx, line in enumerate(lines, 1):
                    if keyword.lower() in line.lower():
                        results.append({"file": file_path, "line": idx, "content": line.rstrip('\n')})
                        if len(results) >= max_results:
                            return json.dumps(results, ensure_ascii=False)
            except:
                continue
    return json.dumps(results, ensure_ascii=False)


@register_tool
@path_params("file_path")
def replace_in_file(file_path: str, old_str: str, new_str: str, occurrence: int = 1):
    """将文件中第 occurrence 次出现的 old_str 替换为 new_str。"""
    file_path = os.path.normpath(file_path)
    if not os.path.isfile(file_path):
        return json.dumps({"success": False, "message": f"文件不存在: {file_path}"})
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        return json.dumps({"success": False, "message": str(e)})
    count = content.count(old_str)
    if count == 0:
        return json.dumps({"success": False, "message": "未找到要替换的字符串"})
    if occurrence > count:
        return json.dumps({"success": False, "message": f"仅有 {count} 处出现，无法替换第 {occurrence} 次"})
    parts = content.split(old_str)
    before = old_str.join(parts[:occurrence])
    after = old_str.join(parts[occurrence:])
    new_content = before + new_str + after
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        return json.dumps({"success": True, "message": f"替换成功，第 {occurrence} 处出现已被替换"})
    except Exception as e:
        return json.dumps({"success": False, "message": str(e)})


@register_tool
def get_project_summary(directory: str = None):
    """列出目录下所有代码文件及大小，帮助快速了解项目结构。"""
    if not directory:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        directory = os.path.join(base, "work_space")
    summary = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('node_modules', '__pycache__', '.git')]
        for file in files:
            if file.endswith(('.py', '.html', '.js', '.css', '.json', '.md')):
                path = os.path.join(root, file)
                size = os.path.getsize(path)
                summary.append({"file": file, "path": path, "size": size})
    return json.dumps(summary, ensure_ascii=False)
