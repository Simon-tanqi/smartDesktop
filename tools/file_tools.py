"""文件操作工具：扫描、移动、复制、解析文件夹、终端命令"""
import os
import json
import shutil
import secrets
from datetime import datetime
import subprocess

from . import register_tool, path_params
from . import safe_exec
from config.config_loader import get_safety_config
from config.settings import MAX_OUTPUT_CHARS

# ── 确认令牌存储：阻止 LLM 自行加 confirmed=true 绕过确认 ──
# token → (command, expiry_timestamp)
_pending_confirmations: dict = {}

# ── 只读命令白名单：这些命令/program 不触发确认，直接执行 ──
# 包括：打开应用、信息查询、文件读取等无副作用操作
READONLY_PATTERNS: set = {
    # 应用启动（只读打开，无副作用）
    "explorer", "notepad", "calc", "mspaint", "charmap",
    # PowerShell 查询 cmdlet
    "get-childitem", "get-itemproperty", "get-process", "get-service",
    "get-content", "get-wmiobject", "get-ciminstance", "get-item",
    "get-date", "get-location", "get-history", "get-command",
    "get-module", "get-variable", "get-alias", "get-psdrive",
    "get-psprovider", "get-host", "get-culture", "get-uiculture",
    # CMD 查询 / 输出命令
    "dir", "cat", "type", "echo", "where", "findstr",
    "select-string", "write-host", "write-output",
    # 系统信息查询
    "whoami", "hostname", "systeminfo", "tasklist",
    "net", "ipconfig", "ping", "tracert", "nslookup",
    # 文件比较 / 校验
    "fc", "comp", "certutil",
    # Python 只读类（python 本身已在 skip_confirm_programs，此处防遗漏）
    # Git 只读类
    # 其他只读工具
    "wmic",
}

# ── 低风险注册表操作关键词：用户偏好类（桌面图标/任务栏/资源管理器外观）直接放行 ──
# 命中且路径在 HKCU 下的操作不触发确认弹窗
LOW_RISK_REGISTRY_PATTERNS = {
    # 桌面图标显隐（纯UI偏好，零风险）
    "HideDesktopIcons",
    "ShowDesktopIcons",
    # 任务栏设置
    "ShowSecondsInSystemClock",
    "TaskbarSi",
    "TaskbarDa",
    "TaskbarGlomLevel",
    "EnableAutoTray",
    # 文件资源管理器偏好
    "ShowStatusBar",
    "ShowPreviewHandlers",
    "Hidden",
    "HideFileExt",
    "ShowSuperHidden",
    # 开始菜单/搜索
    "Start_Show",
    "DisableSearchBoxSuggestions",
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
# 安全集合（FORBIDDEN_PATTERNS / REGISTRY_WRITE_COMMANDS / SKIP_CONFIRM_PROGRAMS / COMMAND_ANTI_PATTERNS）统一托管到 config/safety_rules.yaml

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
                    confirmation_token: str = None, **kwargs) -> str:
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

    _confirmed_note = ""  # 所有代码路径统一初始化，避免 UnboundLocalError

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

    # ── 只读命令白名单优先放行：匹配则跳过所有确认流程 ──
    if prog and prog in READONLY_PATTERNS:
        pass  # 只读命令直接执行，不弹确认
    elif first_token.lower() in READONLY_PATTERNS:
        pass  # 完整 token 命中（如 "Get-ChildItem"）
    elif any(kw in command for kw in LOW_RISK_REGISTRY_PATTERNS) and "hkcu" in command.lower():
        pass  # 用户偏好类注册表操作（HKCU 路径），直接放行
    elif challenge_response or challenge_token:
        pass  # safe_exec 挑战回调：用户已在 execute_command 确认过，不重复弹窗
    elif prog and prog not in get_safety_config().skip_confirm_programs:
        _now = datetime.now().timestamp()
        # 清理过期令牌（过期时间 300s + 10s 宽松缓冲）
        _expired = [t for t, (_, exp) in _pending_confirmations.items() if _now > exp + 10]
        for _t in _expired:
            _pending_confirmations.pop(_t, None)
        # 过期令牌清理
        if not confirmed:
            # 生成一次性确认令牌，LLM 无法伪造
            _pending_confirmations.clear()  # 单令牌模式：每次只保留最新一个
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
            # 验证令牌：存在性 + 过期时间（含 10s 缓冲）
            if not confirmation_token or confirmation_token not in _pending_confirmations:
                # 可能是 LLM 自行加 confirmed=true 绕过，或令牌已过期/已使用，拒绝
                _log_confirmation(command, "BYPASS_BLOCKED", f"LLM 尝试绕过确认或令牌已失效，无有效令牌")
                return json.dumps({
                    "success": False,
                    "confirmation_required": True,
                    "message": f"确认已失效或无有效令牌。请重新发起命令以获取新的确认提示。",
                }, ensure_ascii=False)
            _stored_cmd, _expiry = _pending_confirmations[confirmation_token]
            if _now > _expiry + 10:
                # 过期超过 10 秒缓冲，拒绝
                _pending_confirmations.pop(confirmation_token, None)
                _log_confirmation(command, "TOKEN_EXPIRED", f"令牌已过期 (expiry={_expiry:.0f}, now={_now:.0f}, diff={_now - _expiry:.0f}s)")
                return json.dumps({
                    "success": False,
                    "confirmation_required": True,
                    "message": f"确认已过期（{(_now - _expiry):.0f}秒前）。请重新发起命令以获取新的确认提示。",
                }, ensure_ascii=False)
            # 令牌有效，清理并放行
            del _pending_confirmations[confirmation_token]
            _log_confirmation(command, "CONFIRMED", f"令牌验证通过 (剩余 {(_expiry - _now):.0f}s)")
            # 确认回调上下文：告知 LLM 命令已由用户确认执行，禁止重复发起
            _confirmed_note = (
                f"【系统指令】以下命令已由用户确认并执行：\n"
                f"  {_stored_cmd}\n"
                f"请基于执行结果继续完成原始任务。严禁再次调用 execute_command 执行相同或等效的命令。"
            )


    cmd_lower = command.lower()
    for pattern in get_safety_config().forbidden_patterns:
        if pattern.lower() in cmd_lower:
            return json.dumps({"success": False, "message": f"安全限制：命令包含禁止模式 '{pattern}'"})

    # ── 命令预检：拦截低效/错误模式 ──
    import re as _re
    for _pat, _msg in get_safety_config().command_anti_patterns:
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
        if _confirmed_note:
            result["_confirmation_context"] = _confirmed_note
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
            **({"_confirmation_context": _confirmed_note} if _confirmed_note else {}),
        }, ensure_ascii=False)

    _log_confirmation(command, "FAILED", result.get("message", ""))
    return json.dumps({
        "success": False,
        "message": result.get("message", ""),
        **({"_confirmation_context": _confirmed_note} if _confirmed_note else {}),
    }, ensure_ascii=False)


# ═══════════════════════════════════════════════
# Python 代码执行工具
# ═══════════════════════════════════════════════

PYTHON_OUTPUT_MAX_CHARS = 8000
PYTHON_TIMEOUT_DEFAULT = 300


# ── 工业级鲁棒性：自动注入标准库导入，消除 NameError 类低级错误 ──
_PY_RUN_PRELUDE = (
    "import sys, os, json, traceback, subprocess, pathlib, shutil, importlib, re\n"
)


def _parse_traceback_info(stderr_text: str) -> dict:
    """从 stderr 中解析 traceback，提取结构化错误信息。"""
    lines = stderr_text.split('\n')
    exc_type = ""
    exc_msg = ""
    last_frames = []
    in_tb = False

    for line in lines:
        stripped = line.rstrip()
        if stripped.startswith('Traceback (most recent call last):'):
            in_tb = True
            continue
        if in_tb and stripped.startswith('  File '):
            last_frames.append(stripped.strip())
            continue
        if in_tb and stripped and not stripped.startswith(' '):
            exc_type = stripped
            exc_msg = stripped
            break

    # 压缩帧信息：只保留最后 3 帧，去除工作目录前缀
    relevant_frames = last_frames[-3:] if last_frames else []
    short_frames = []
    for f in relevant_frames:
        # 去掉冗长的路径前缀
        if ', line ' in f:
            parts = f.split(', line ', 1)
            fname = os.path.basename(parts[0].replace('  File "', '').rstrip('"'))
            short_frames.append(f'  File "{fname}", line {parts[1]}')
        else:
            short_frames.append(f)

    return {
        "exception_type": exc_type.split(':')[0] if ':' in exc_type else exc_type,
        "exception_message": exc_msg,
        "last_frames": short_frames,
    }


@register_tool(timeout=320)


def run_python(code: str, timeout: int = PYTHON_TIMEOUT_DEFAULT, **kwargs) -> str:
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
                    "请改用专用工具 modify_registry 来修改注册表，该工具内置自动验证和路径推荐机制。\n"
                    "如果 modify_registry 也失败：直接告知用户手动操作步骤，不要再尝试 execute_command 或其他工具。"
                ),
            }, ensure_ascii=False)

    import sys
    import tempfile

    # ── 工业级鲁棒性：自动注入标准库前导，消除 NameError ──
    code = _PY_RUN_PRELUDE + code.strip()

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
            # ── 结构化错误：解析 traceback，注入错误分类和修复线索 ──
            tb_info = _parse_traceback_info(stderr) if stderr else {}
            error_msg = stderr if stderr else f"脚本退出码: {result.returncode}"

            exc_type = tb_info.get("exception_type", "")
            frames = tb_info.get("last_frames", [])

            # 错误分类映射
            ERROR_CATEGORIES = {
                "ModuleNotFoundError": ("dependency", "缺失依赖包，请执行 pip install <包名>"),
                "ImportError": ("dependency", "导入失败，检查包名是否正确或是否需要 pip install"),
                "NameError": ("logic", "变量/函数未定义，检查是否缺少 import 或拼写错误"),
                "TypeError": ("logic", "类型错误，检查函数参数类型是否匹配 API 要求"),
                "AttributeError": ("logic", "对象不存在该属性/方法，检查 API 调用方式是否正确"),
                "FileNotFoundError": ("io", "文件路径不存在，请先确认路径"),
                "PermissionError": ("io", "权限不足，可能需要管理员权限"),
                "ValueError": ("logic", "值错误，检查传入参数值是否合法"),
                "KeyError": ("logic", "键不存在，检查字典/配置中是否包含该键"),
                "IndexError": ("logic", "索引越界，检查列表/序列长度"),
                "SyntaxError": ("syntax", "代码语法错误，检查引号/括号/缩进"),
                "IndentationError": ("syntax", "缩进错误，检查代码块缩进是否一致"),
            }

            category, fix_hint = ERROR_CATEGORIES.get(
                exc_type, ("unknown", "未知错误类型，请仔细阅读错误信息")
            )

            return json.dumps({
                "success": False,
                "error_category": category,
                "error_fix_hint": fix_hint,
                "exception_type": exc_type,
                "exception_message": tb_info.get("exception_message", ""),
                "last_frames": frames,
                "message": f"Python 执行失败 [{exc_type}]:\n{error_msg}",
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
