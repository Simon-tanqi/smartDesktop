"""文件操作工具：扫描、移动、复制、解析文件夹、终端命令"""
import os
import json
import shutil
import subprocess
import shlex

from . import register_tool, path_params

SKIP_DIRS = {
    "Windows", "Program Files", "Program Files (x86)", "ProgramData",
    "$RECYCLE.BIN", "System Volume Information", "Recovery",
    "node_modules", "__pycache__", ".git", ".vscode", ".idea",
    "AppData", "Local Settings", "Application Data", "Temp"
}


@register_tool
@path_params("path")
def scan_directory(path: str):
    """扫描指定路径下的第一层文件和文件夹，返回 JSON 数组。path: 要扫描的目录绝对路径"""
    try:
        entries = [entry.name for entry in os.scandir(path)]
        return json.dumps(entries, ensure_ascii=False)
    except Exception as e:
        return f"扫描失败: {str(e)}"


@register_tool
@path_params("source", "destination")
def move_file(source: str, destination: str, overwrite: bool = True):
    """移动文件。source: 源文件路径, destination: 目标目录或新路径, overwrite: 是否覆盖已存在文件(默认true)"""
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
    """复制文件。source: 源文件路径, destination: 目标目录或新路径, overwrite: 是否覆盖已存在文件(默认true)"""
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


@register_tool
@path_params("parent_dir")
def resolve_folder(parent_dir: str, target_name: str):
    """在 parent_dir 下查找所有子文件夹，返回候选列表及匹配提示。"""
    if not os.path.isdir(parent_dir):
        return json.dumps({"success": False, "message": f"父目录不存在: {parent_dir}"})
    try:
        folders = [d for d in os.listdir(parent_dir) if os.path.isdir(os.path.join(parent_dir, d))]
        return json.dumps({"parent": parent_dir, "target_name": target_name, "folders": folders}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "message": str(e)})


# ── 命令白名单与危险拦截 ──
ALLOWED_COMMANDS = {
    "dir", "copy", "move", "del", "mkdir", "rmdir", "echo", "type",
    "findstr", "where", "python", "pip", "git", "npm", "node",
    "systeminfo", "wmic", "powershell", "cmd"
}
FORBIDDEN_PATTERNS = ["rm -rf /", "del /f /s", "format", "shutdown"]


@register_tool
def execute_command(command: str, working_dir: str = None) -> str:
    """在终端执行一条命令。command: 完整命令字符串。working_dir: 可选工作目录。"""
    try:
        parts = shlex.split(command)
    except ValueError:
        return json.dumps({"success": False, "message": "命令解析失败，请检查引号配对。"})
    if not parts:
        return json.dumps({"success": False, "message": "空命令"})
    prog = os.path.basename(parts[0]).lower()
    if prog not in ALLOWED_COMMANDS:
        return json.dumps({"success": False, "message": f"安全限制：不允许执行程序 '{parts[0]}'。白名单: {list(ALLOWED_COMMANDS)}"})
    cmd_lower = command.lower()
    for pattern in FORBIDDEN_PATTERNS:
        if pattern.lower() in cmd_lower:
            return json.dumps({"success": False, "message": f"安全限制：命令包含禁止模式 '{pattern}'"})
    try:
        result = subprocess.run(command, shell=True, cwd=working_dir,
                                capture_output=True, text=True, timeout=30, encoding="gbk", errors="replace")
        output = result.stdout.strip()
        if result.returncode != 0:
            error = result.stderr.strip()
            return json.dumps({"success": False, "message": f"命令执行失败 (code {result.returncode}): {error}"})
        return json.dumps({"success": True, "output": output})
    except subprocess.TimeoutExpired:
        return json.dumps({"success": False, "message": "命令执行超时"})
    except Exception as e:
        return json.dumps({"success": False, "message": str(e)})
