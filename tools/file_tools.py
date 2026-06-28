"""文件操作工具：扫描、移动、复制、解析文件夹、终端命令"""
import os
import json
import shutil
import secrets
from datetime import datetime
import subprocess

from . import register_tool, path_params
from . import safe_exec

# ── 确认令牌存储：阻止 LLM 自行加 confirmed=true 绕过确认 ──
# token → (command, expiry_timestamp)
_pending_confirmations: dict = {}

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


# ── 命令白名单已移除（2025-06-26）──
# 原 ALLOWED_COMMANDS 硬拒绝已替换为「免确认列表 + 确认弹窗」两级机制：
#   - 免确认列表内的命令直接执行（静默）
#   - 列表外的命令弹出确认，用户看到完整命令后决定（类似 msiexec 确认流程）
# 危险命令（reg delete 等）由 safe_exec 挑战短语兜底
FORBIDDEN_PATTERNS = ["rm -rf /", "del /f /s", "format ", "shutdown"]  # "format " 带空格仅拦截 format 磁盘格式化命令，不影响 Format-Table/Format-List

# 注册表写入命令：直接拒绝，引导 LLM 使用 modify_registry 工具
REGISTRY_WRITE_COMMANDS = ["reg add", "reg delete", "reg import"]
REGISTRY_WRITE_REDIRECT_MSG = (
    "注册表写入操作已被 execute_command 拦截（安全限制）。\n"
    "请改用 modify_registry 工具，调用格式: modify_registry(command='你的完整 reg add 命令')\n"
    "示例: modify_registry(command='reg add \"HKCU\\\\Control Panel\\\\International\" /v sShortTime /t REG_SZ /d HH:mm:ss /f')\n"
    "该工具内置自动验证和路径推荐机制，无需先用 reg query 检查。"
)

# 免确认程序列表：这些程序执行时不弹确认窗，直接透传执行
SKIP_CONFIRM_PROGRAMS = {
    "dir", "copy", "move", "del", "mkdir", "rmdir", "echo", "type",
    "findstr", "where", "python", "pip",
    "git", "npm", "node", "npx", "yarn",
    "systeminfo", "wmic", "tasklist", "reg",  # reg query 只读安全；reg add/delete 由 REGISTRY_WRITE_COMMANDS 硬拦截
}

# ── 命令预检：低效/错误模式（代码层拦截，不靠 LLM 自觉）──
# 命中后不执行命令，直接返回错误信息指引正确方向
COMMAND_ANTI_PATTERNS = [
    # dir /s 全量输出后试图用 findstr 按文件属性过滤 → 方向性错误
    (r"dir\s+\S+\s+/[sb].*\|.*findstr",
     "dir /s /b 只输出文件名（不含大小/日期/权限）。"
     "如果要按文件属性（大小/日期/内容等）过滤，dir 做不到。请改用 python os.walk + 条件判断，在扫描时直接过滤。"),
    # dir 递归全量重定向到临时文件 → 可能极慢且无意义
    (r"dir\s+\S+\s+/[sb].*\s*>\s*\S+[Tt]emp",
     "dir 递归全量输出并重定向到临时文件可能极其缓慢。"
     "如果最终目标是过滤，请在源头用 python os.walk 加条件判断，不要先生成全量列表再过滤。"),
]

# ── 输出截断阈值 ──
MAX_OUTPUT_CHARS = 2000


def _get_confirm_log_path() -> str:
    """获取确认日志文件路径"""
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_dir = os.path.join(project_dir, "logs", "confirm")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, "confirm_operations.log")


def _log_confirmation(command: str, status: str, detail: str = ""):
    """记录确认类操作到日志"""
    log_path = _get_confirm_log_path()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = (
        f"[{timestamp}] {status}\n"
        f"  Command: {command}\n"
        f"  Detail:  {detail}\n"
        f"{'-' * 60}\n"
    )
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry)


@register_tool
def execute_command(command: str, working_dir: str = None, confirmed: bool = False,
                    challenge_response: str = None, challenge_token: str = None,
                    confirmation_token: str = None) -> str:
    """在终端执行一条命令。

    安全机制（两级）：
    1. 免确认列表检查：dir/copy/git/python 等常用命令直接执行；powershell/reg/winget 等
       不在免确认列表的命令弹出确认，用户看到完整命令后决定。
    2. safe_exec 四层防护：L0路径保护 + L1挑战短语 + L2危险检测 + L3 subprocess.run(shell=False)。

    command: 完整命令字符串。
    working_dir: 可选工作目录。
    confirmed: 收到 confirmation_required 后，用户回复"确认"时设为 true 重新调用。
    challenge_response: 危险操作授权文本（用户完整打出挑战短语后传入）。
    challenge_token: 挑战令牌（safe_exec 返回的临时凭证）。
    """
    # ── 命令确认检查：提取第一个可执行程序名，不在免确认列表则弹确认窗 ──
    # 用户可在 UI 看到完整命令后决定是否执行
    stripped = command.lstrip()
    if not stripped:
        return json.dumps({"success": False, "message": "空命令"})

    # 提取第一个 token（支持双引号/单引号包裹）
    first_token = ""
    i = 0
    if stripped[0] == '"':
        i = 1
        while i < len(stripped) and stripped[i] != '"':
            i += 1
        first_token = stripped[1:i] if i < len(stripped) else stripped[1:]
    elif stripped[0] == "'":
        i = 1
        while i < len(stripped) and stripped[i] != "'":
            i += 1
        first_token = stripped[1:i] if i < len(stripped) else stripped[1:]
    else:
        i = 0
        while i < len(stripped) and stripped[i] not in (' ', '\t'):
            i += 1
        first_token = stripped[:i]

    prog = os.path.basename(first_token).lower()
    if prog and prog not in SKIP_CONFIRM_PROGRAMS:
        # 清理过期令牌（5分钟有效期）
        _now = datetime.now().timestamp()
        _pending_confirmations.clear()  # 单令牌模式：每次只保留最新一个
        if not confirmed:
            # 生成一次性确认令牌，LLM 无法伪造
            _token = secrets.token_hex(8)
            _pending_confirmations[_token] = (command, _now + 300)
            _log_confirmation(command, "CONFIRM_REQUIRED", f"程序 '{first_token}' 不在免确认列表 | token={_token}")
            return json.dumps({
                "confirmation_required": True,
                "confirmation_token": _token,
                "message": f"即将执行命令（程序 '{first_token}' 需要确认）:\n{command}\n\n请在界面点击确认以执行此命令。",
                "command": command
            }, ensure_ascii=False)
        else:
            # 验证令牌
            if not confirmation_token or confirmation_token not in _pending_confirmations:
                # 可能是 LLM 自行加 confirmed=true 绕过，拒绝
                _log_confirmation(command, "BYPASS_BLOCKED", f"LLM 尝试绕过确认，无有效令牌")
                return json.dumps({
                    "success": False,
                    "confirmation_required": True,
                    "message": f"确认已失效或无有效令牌。请重新发起命令以获取新的确认提示。",
                }, ensure_ascii=False)
            # 令牌有效，清理并放行
            del _pending_confirmations[confirmation_token]
            _log_confirmation(command, "CONFIRMED", f"令牌验证通过")

    cmd_lower = command.lower()
    for pattern in FORBIDDEN_PATTERNS:
        if pattern.lower() in cmd_lower:
            return json.dumps({"success": False, "message": f"安全限制：命令包含禁止模式 '{pattern}'"})

    # ── 注册表写入拦截：reg add/delete/import 直接拒绝，引导 LLM 用 modify_registry ──
    for _rw_cmd in REGISTRY_WRITE_COMMANDS:
        if _rw_cmd in cmd_lower:
            _log_confirmation(command, "REGISTRY_BLOCKED", f"命中注册表写入命令: {_rw_cmd}")
            return json.dumps({
                "success": False,
                "message": REGISTRY_WRITE_REDIRECT_MSG,
            }, ensure_ascii=False)

    # ── 命令预检：拦截低效/错误模式 ──
    import re as _re
    for _pat, _msg in COMMAND_ANTI_PATTERNS:
        if _re.search(_pat, command):
            _log_confirmation(command, "ANTI_PATTERN_BLOCKED", f"命中模式: {_pat}")
            return json.dumps({
                "success": False,
                "message": f"命令预检拦截：{_msg}",
                "blocked_pattern": _pat,
            }, ensure_ascii=False)

    # 执行命令——通过 safe_exec 安全层
    result = safe_exec.run_powershell(
        command,
        working_dir=working_dir,
        challenge_response=challenge_response,
        challenge_token=challenge_token,
    )

    if result.get("challenge_required"):
        _log_confirmation(command, "CHALLENGE", result.get("danger_type", ""))
        return json.dumps(result, ensure_ascii=False)

    if result.get("success"):
        _log_confirmation(command, "SUCCESS", f"返回 {len(result.get('output', ''))} 字符")
        output = result["output"]
        output_len = len(output)
        truncated = output_len > MAX_OUTPUT_CHARS
        if truncated:
            display = output[:MAX_OUTPUT_CHARS] + (
                f"\n\n[输出已截断：{output_len} 字符 → {MAX_OUTPUT_CHARS} 字符。"
                f"如果命令产生极大量输出而您只需要其中一小部分，请在源头加过滤条件而非全量输出后再筛选。]"
            )
        else:
            display = output
        return json.dumps({
            "success": True,
            "output": display,
            "output_total_chars": output_len,
            "truncated": truncated,
        }, ensure_ascii=False)

    _log_confirmation(command, "FAILED", result.get("message", ""))
    return json.dumps({"success": False, "message": result.get("message", "")}, ensure_ascii=False)


# ═══════════════════════════════════════════════
# Python 代码执行工具
# ═══════════════════════════════════════════════

PYTHON_OUTPUT_MAX_CHARS = 8000
PYTHON_TIMEOUT_DEFAULT = 300


@register_tool(timeout=320)
def run_python(code: str, timeout: int = PYTHON_TIMEOUT_DEFAULT) -> str:
    """直接执行 Python 代码并返回输出结果。适用于复杂文件遍历、数据处理等命令行难以胜任的任务。

    code: 完整的 Python 代码字符串（不是文件路径）。可以用 print() 输出结果，结果会被自动捕获返回。
    timeout: 超时秒数，默认 300 秒（5 分钟），全盘扫描等耗时操作可设更长。

    使用场景：
    - 遍历多层目录、按条件过滤文件（如查找大于某大小的文件）
    - 多步文件操作需要一次性完成，避免反复调用命令
    - 数据处理、格式转换等复杂逻辑
    - 命令行工具超时时，改用本工具

    注意事项：
    - 代码中不要使用 input()（无交互终端）
    - 代码中不要使用 os.system()（应使用 subprocess）
    - 结果通过 print() 输出，stdout 和 stderr 会被分别捕获
    - 文件操作优先在 work_space 目录下进行"""
    if not code or not code.strip():
        return json.dumps({"success": False, "message": "代码不能为空"})

    # ── 注册表操作扫描：拦截 Python 中的注册表写入代码，引导 LLM 用 modify_registry ──
    code_lower = code.lower()
    _reg_indicators = [
        "import winreg", "from winreg",
        "os.system(\"reg ", "os.system('reg ",
        "subprocess.run([\"reg", "subprocess.run(['reg",
        "subprocess.call([\"reg", "subprocess.call(['reg",
        "subprocess.popen([\"reg", "subprocess.popen(['reg",
        "reg add", "reg delete", "reg import",
    ]
    for _indicator in _reg_indicators:
        if _indicator in code_lower:
            return json.dumps({
                "success": False,
                "message": (
                    "Python 代码中包含注册表写入操作（检测到关键字），已被 run_python 拦截。\n"
                    "请改用专用工具 modify_registry 来修改注册表，该工具内置自动验证和路径推荐机制。"
                ),
            }, ensure_ascii=False)

    import sys
    import tempfile

    code = code.strip()

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(code)
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            [sys.executable, tmp_path],
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )

        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if result.returncode != 0:
            error_msg = stderr if stderr else f"脚本退出码: {result.returncode}"
            return json.dumps({
                "success": False,
                "message": f"Python 执行失败:\n{error_msg}",
                "stdout": stdout[:2000] if stdout else "",
            }, ensure_ascii=False)

        output = stdout
        truncated = len(output) > PYTHON_OUTPUT_MAX_CHARS
        if truncated:
            output = output[:PYTHON_OUTPUT_MAX_CHARS] + (
                f"\n\n[输出已截断：原始长度 {len(stdout)} 字符 → {PYTHON_OUTPUT_MAX_CHARS} 字符。"
                f"请优化代码减少输出量，或写入文件后用 read_file 读取。]"
            )

        return json.dumps({
            "success": True,
            "output": output,
            "output_total_chars": len(stdout),
            "truncated": truncated,
        }, ensure_ascii=False)

    except subprocess.TimeoutExpired:
        return json.dumps({
            "success": False,
            "message": f"Python 代码执行超时 ({timeout} 秒)。请优化代码逻辑或增大 timeout 参数。"
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "message": f"执行异常: {str(e)}"})
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
