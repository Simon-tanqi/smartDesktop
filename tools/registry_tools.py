"""注册表操作工具：安全的注册表读写（仅限 HKCU + 白名单路径）"""
import json
import os
from datetime import datetime

from . import register_tool
from . import safe_exec

# ── 注册表二级白名单 ──
ALLOWED_ROOT_KEYS = {"HKCU", "HKEY_CURRENT_USER"}

ALLOWED_PATH_PREFIXES = [
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
    r"Software\Microsoft\Windows\CurrentVersion\Explorer",
    r"Control Panel\Desktop",
    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\Accent",
]

ALLOWED_OPERATIONS = {"query", "add"}

FORBIDDEN_VALUES = {
    "disableregistrytools", "notaskmgr", "norun", "noclose",
    "disablecmd", "disabletaskmgr", "nofolderoptions",
    "disablechangepassword", "nolock", "disableremote"
}

FORBIDDEN_OPERATIONS = {"delete", "import", "export", "save", "restore", "copy", "load", "unload", "compare", "flags"}


def _get_log_path() -> str:
    """获取确认日志文件路径"""
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_dir = os.path.join(project_dir, "logs", "registry")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, "registry_operations.log")


def _log_operation(operation: str, command: str, result: str):
    """记录注册表操作到日志"""
    log_path = _get_log_path()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = (
        f"[{timestamp}] {operation}\n"
        f"  Command: {command}\n"
        f"  Result:  {result}\n"
        f"{'-' * 60}\n"
    )
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry)


def _validate_registry_command(command: str) -> dict:
    """校验注册表命令是否安全"""
    parts = command.strip().split()
    if len(parts) < 2:
        return {"valid": False, "message": "命令格式错误"}

    if parts[0].lower() != "reg":
        return {"valid": False, "message": "不是 reg 命令"}

    operation = parts[1].lower()
    if operation in FORBIDDEN_OPERATIONS:
        return {"valid": False, "message": f"禁止的操作: {operation}，仅允许 query / add"}
    if operation not in ALLOWED_OPERATIONS:
        return {"valid": False, "message": f"不允许的操作: {operation}，仅允许 query / add"}

    # 提取注册表路径
    path = None
    for i, p in enumerate(parts):
        if i >= 2:
            if p.startswith("/") or p.startswith("-"):
                continue
            if not p.startswith("/") and not p.startswith("-"):
                path = p.strip('"')
                break

    if not path:
        return {"valid": False, "message": "未找到注册表路径"}

    root_key = path.split("\\")[0].upper()
    if root_key == "HKCU":
        root_key = "HKEY_CURRENT_USER"
    if root_key not in ALLOWED_ROOT_KEYS:
        return {"valid": False, "message": f"禁止的根键: {root_key}，仅允许 HKCU (当前用户)"}

    # 检查路径前缀
    path_without_root = "\\".join(path.split("\\")[1:])
    allowed = False
    for prefix in ALLOWED_PATH_PREFIXES:
        if path_without_root.lower().startswith(prefix.lower()):
            allowed = True
            break
    if not allowed:
        return {
            "valid": False,
            "message": f"注册表路径不在白名单中",
            "allowed_paths": ALLOWED_PATH_PREFIXES
        }

    # add 操作：检查值名和类型
    if operation == "add":
        value_name = None
        value_type = None
        for i, p in enumerate(parts):
            if p.lower() == "/v" and i + 1 < len(parts):
                value_name = parts[i + 1].lower()
            if p.lower() == "/t" and i + 1 < len(parts):
                value_type = parts[i + 1].upper()

        if value_name and value_name in FORBIDDEN_VALUES:
            return {"valid": False, "message": f"禁止修改的安全策略值名: {value_name}"}

        if value_type and value_type not in ("REG_DWORD", "REG_SZ", "REG_QWORD"):
            return {"valid": False, "message": f"不允许的值类型: {value_type}，仅允许 REG_DWORD / REG_SZ / REG_QWORD"}

    return {"valid": True}


@register_tool
def modify_registry(command: str) -> str:
    """安全地执行注册表查询或修改。
    command: 完整的 reg 命令字符串。
    仅允许 HKCU 根键下白名单路径的 query 和 add 操作。

    示例:
      reg query "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced" /v ShowSecondsInSystemClock
      reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced" /v ShowSecondsInSystemClock /t REG_DWORD /d 1 /f
    """
    validation = _validate_registry_command(command)
    operation_tag = command.strip().split()[1].lower() if len(command.strip().split()) >= 2 else "unknown"

    if not validation["valid"]:
        fail_msg = validation["message"]
        _log_operation(f"BLOCKED ({operation_tag})", command, fail_msg)
        return json.dumps({
            "success": False,
            "blocked": True,
            "message": f"注册表安全校验失败: {fail_msg}",
            "allowed_operations": list(ALLOWED_OPERATIONS),
            "allowed_paths": ALLOWED_PATH_PREFIXES
        }, ensure_ascii=False)

    # 安全执行：shell=False，命令拆为 list
    cmd_list = [p.strip('"') if p.startswith('"') and p.endswith('"') else p for p in command.strip().split()]
    result = safe_exec.run_cmd(cmd_list, timeout=15)

    if result.get("challenge_required"):
        _log_operation(f"CHALLENGE ({operation_tag})", command, result.get("danger_type", ""))
        return json.dumps(result, ensure_ascii=False)

    if result.get("success"):
        _log_operation(f"SUCCESS ({operation_tag})", command, result["output"][:200] if result["output"] else "(无输出)")
        return json.dumps({"success": True, "output": result["output"]}, ensure_ascii=False)

    _log_operation(f"FAILED ({operation_tag})", command, result.get("message", ""))
    return json.dumps({"success": False, "message": result.get("message", "")}, ensure_ascii=False)
