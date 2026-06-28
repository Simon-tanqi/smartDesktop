"""注册表操作工具：安全的注册表读写（仅限 HKCU + 白名单路径）"""
import json
import os
import re
from datetime import datetime

from . import register_tool
from . import safe_exec

# ── 注册表二级白名单 ──
ALLOWED_ROOT_KEYS = {"HKCU", "HKEY_CURRENT_USER"}

ALLOWED_PATH_PREFIXES = [
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
    r"Software\Microsoft\Windows\CurrentVersion\Explorer",
    r"Control Panel\Desktop",
    r"Control Panel\International",
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


def _auto_verify_before_add(command: str) -> dict | None:
    """reg add 前自动验证目标键是否存在，不存在则搜索父路径返回候选项。

    返回 None 表示验证通过（键存在或无需验证），直接放行。
    返回 dict 表示验证失败，包含候选项信息，应直接 return 给 LLM。
    """
    parts = command.strip().split()
    # 提取路径和值名
    path = None
    value_name = None
    in_quotes = False
    quote_char = None
    path_done = False
    for i, p in enumerate(parts):
        if i >= 2:
            # 处理引号包裹的路径（可能跨多个 token），仅第一次
            if not path_done and p.startswith('"') and not in_quotes:
                in_quotes = True
                quote_char = '"'
                path = p[1:]
                continue
            if in_quotes:
                path += " " + p
                if p.endswith(quote_char):
                    path = path[:-1]  # 去掉末尾引号
                    in_quotes = False
                    quote_char = None
                    path_done = True
                continue
            if p.lower() == "/v" and i + 1 < len(parts):
                value_name = parts[i + 1]
            elif not p.startswith("/") and not p.startswith("-") and path is None:
                path = p
                path_done = True

    if not path:
        return None

    # 先 query 目标键
    query_cmd = ["reg", "query", path]
    if value_name:
        query_cmd += ["/v", value_name]
    result = safe_exec.run_cmd(query_cmd, timeout=10)

    if result.get("success") and result.get("output"):
        return None  # 键存在，放行

    # 键不存在，自动搜索父路径下所有值
    search_result = safe_exec.run_cmd(["reg", "query", path], timeout=10)

    # 即使 reg query path（不带 /v）也失败，尝试搜索
    candidates = []
    if search_result.get("success") and search_result.get("output"):
        output = search_result["output"]
        # 解析 reg query 输出，提取值名和类型
        for line in output.split("\n"):
            line = line.strip()
            if not line or line.startswith("HKEY"):
                continue
            # 格式: "    value_name    REG_DWORD    0x1"
            m = re.match(r"^\s*(\S+)\s+(REG_\S+)\s+(.*)", line)
            if m:
                candidates.append({
                    "value_name": m.group(1),
                    "type": m.group(2),
                    "data": m.group(3).strip()
                })

    return {
        "success": False,
        "auto_verified": True,
        "message": (
            f"目标键值 '{value_name or '(默认)'}' 在 '{path}' 中不存在。\n"
            f"以下为该路径下实际存在的键值（共 {len(candidates)} 个），请从中选择正确目标："
        ),
        "existing_values": candidates,
        "query_path": path
    }
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

    # 提取注册表路径（处理引号包裹含空格的路径）
    path = None
    in_quotes = False
    quote_char = None
    path_done = False
    for i, p in enumerate(parts):
        if i >= 2:
            if not path_done and p.startswith('"') and not in_quotes:
                in_quotes = True
                quote_char = '"'
                path = p[1:]
                continue
            if in_quotes:
                path += " " + p
                if p.endswith(quote_char):
                    path = path[:-1]
                    path_done = True
                    break
                continue
            if not p.startswith("/") and not p.startswith("-"):
                path = p
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
    """安全执行注册表查询或修改。
    只有一个参数 command: str —— 完整的 reg 命令字符串。不支持 action/path/name/type/data 等拆分参数。

    仅允许 HKCU 根键下白名单路径的 query 和 add 操作(add 前自动验证目标键是否存在)。

    示例:
      modify_registry(command='reg add "HKCU\\Control Panel\\International" /v sShortTime /t REG_SZ /d HH:mm:ss /f')
      modify_registry(command='reg add "HKCU\\Software\\...\\Advanced" /v ShowSecondsInSystemClock /t REG_DWORD /d 1 /f')
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

    # ── reg add 自动验证：键不存在时阻断并返回同路径下真实键值列表 ──
    if operation_tag == "add":
        verify_result = _auto_verify_before_add(command)
        if verify_result is not None:
            _log_operation(f"VERIFY_FAILED (add)", command, verify_result["message"])
            return json.dumps(verify_result, ensure_ascii=False)

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
