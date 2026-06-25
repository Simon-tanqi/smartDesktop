"""
安全命令执行模块

防护层（由外到内）：
  L0: 路径硬拦截 — 删除类命令命中受保护路径时直接拒绝
  L1: 挑战短语验证 — 用户必须完整打出授权文本才能执行危险操作
  L2: 危险模式检测 — 命令语义匹配破坏性关键词
  L3: subprocess.run(list, shell=False) — 切断 OS shell 元字符注入

设计原则：不限制用户权限，不创建受限账户。通过确认机制阻止模型写出危险代码，
同时保证 pip install / 软件安装等正常操作不受影响。
"""
import subprocess
import re
import secrets
import json
import os
from datetime import datetime


# ═══════════════════════════════════════════════
# 路径硬拦截（L0）
# ═══════════════════════════════════════════════

# 删除类命令识别
_DELETE_COMMAND_PATTERNS = [
    r"\bdel\b", r"\brmdir\b", r"\brd\b", r"\bRemove-Item\b",
    r"\brm\b", r"\brmtree\b", r"\bshutil\.rmtree",
    r"\bClear-RecycleBin\b",
]

# 受保护路径模式：命中任一即拒绝执行
# 匹配逻辑基于命令字符串中子串检测，不依赖路径解析
_PROTECTED_PATH_PATTERNS = [
    # 系统核心目录
    (r"(?i)[A-Z]:\\Windows\\",                 "Windows 系统目录"),
    (r"(?i)[A-Z]:\\Program Files\\",           "Program Files 目录"),
    (r"(?i)[A-Z]:\\Program Files \(x86\)\\",   "Program Files (x86) 目录"),
    (r"(?i)[A-Z]:\\ProgramData\\",             "ProgramData 目录"),

    # 聊天记录数据库
    (r"(?i)WeChat Files\\.+?\\Msg[\\\.]",      "微信聊天记录数据库"),
    (r"(?i)Tencent Files\\.+?\\Msg[0-9]",      "QQ 聊天记录数据库"),
    (r"(?i)DingTalk\\.+?\\db[\\\.]",           "钉钉聊天记录数据库"),

    # 安全凭据
    (r"(?i)\\\.ssh\\",                          "SSH 密钥目录"),
    (r"(?i)\\\.aws\\",                          "AWS 凭据目录"),
    (r"(?i)\\\.kube\\",                         "Kubernetes 配置目录"),
    (r"(?i)\\\.env$",                           "环境变量配置文件"),
    (r"(?i)\\\.git\\",                          "Git 仓库数据"),
]


def _is_delete_command(command: str) -> bool:
    """判断命令是否为删除类操作"""
    for pattern in _DELETE_COMMAND_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return True
    return False


def _check_protected_paths(command: str) -> str | None:
    """
    检查删除类命令是否触碰受保护路径。
    返回被拦截的路径描述，或 None（通过检查）。
    """
    if not _is_delete_command(command):
        return None
    for pattern, desc in _PROTECTED_PATH_PATTERNS:
        if re.search(pattern, command):
            return desc
    return None


# ═══════════════════════════════════════════════
# 挑战短语
# ═══════════════════════════════════════════════

DANGER_CHALLENGE_PHRASE = "我知晓此操作为危险操作，我确认授权给Agent执行此危险操作"


# ═══════════════════════════════════════════════
# 危险模式定义
# ═══════════════════════════════════════════════

DANGER_PATTERNS = [
    # (正则, 中文描述)
    (r"\bdel\b",           "删除文件"),
    (r"\brmdir\b",         "删除目录"),
    (r"\bformat\b",        "格式化磁盘"),
    (r"\brmtree\b",        "递归删除目录"),
    (r"\bshutil\.rmtree",  "递归删除目录"),
    (r"\bRemove-Item\b.*-Recurse",  "强制递归删除"),
    (r"\breg\s+delete\b",  "删除注册表项"),
    (r"\btaskkill\b",      "强制结束进程"),
    (r"\bStop-Process\b",  "强制结束进程"),
    (r"\bStop-Service\b",  "停止系统服务"),
    (r"\bClear-RecycleBin\b", "清空回收站"),
]

# 这些模式始终拦截，不进入挑战流程（无确认可能）
ALWAYS_BLOCKED = [
    r"\bformat\s+\w:\b",        # 格式化盘符
    r"\bdel\s+/[fF]\s+/[sS]",   # del /f /s (递归强制删除)
    r"\brm\s+-rf\s+/",          # rm -rf / (删除根目录)
]


# ═══════════════════════════════════════════════
# 内部状态
# ═══════════════════════════════════════════════

_pending_challenges = {}  # token → {command, desc, timestamp}
_CHALLENGE_TTL_SECONDS = 300  # 挑战令牌 5 分钟未应答自动过期


def _evict_stale_challenges():
    """清理过期的挑战令牌，防止挑战未应答导致永久内存泄漏。"""
    now = datetime.now()
    stale = []
    for token, entry in _pending_challenges.items():
        ts_str = entry.get("timestamp")
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str)
                if (now - ts).total_seconds() > _CHALLENGE_TTL_SECONDS:
                    stale.append(token)
            except ValueError:
                stale.append(token)
        else:
            stale.append(token)  # 无时间戳的记录也视为异常，一并清理
    for token in stale:
        _pending_challenges.pop(token, None)
        _log("EVICT", f"过期挑战令牌清理: {token[:8]}...")
    return len(stale)


def _get_log_path():
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_dir = os.path.join(project_dir, "logs", "safe_exec")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, "safe_exec.log")


def _log(level, message):
    try:
        log_path = _get_log_path()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [{level}] {message}\n")
    except Exception:
        pass


# ═══════════════════════════════════════════════
# 命令安全检查
# ═══════════════════════════════════════════════

def _check_always_blocked(command):
    """检查是否命中永久拦截规则，返回被拦截的模式或 None"""
    for pattern in ALWAYS_BLOCKED:
        if re.search(pattern, command, re.IGNORECASE):
            return pattern
    return None


def _check_danger(command):
    """检查是否命中危险模式，返回 (is_dangerous, danger_desc)"""
    for pattern, desc in DANGER_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return True, desc
    return False, ""


def is_dangerous(command):
    """公开接口：判断命令是否危险（供外部确认流程使用）"""
    blocked = _check_always_blocked(command)
    if blocked:
        return True
    danger, _ = _check_danger(command)
    return danger


# ═══════════════════════════════════════════════
# 安全执行
# ═══════════════════════════════════════════════

def run_powershell(script, working_dir=None, timeout=30, encoding="gbk",
                   challenge_response=None, challenge_token=None):
    """
    安全执行 PowerShell 命令。

    Args:
        script: PowerShell 命令字符串
        working_dir: 工作目录
        timeout: 超时秒数
        encoding: 输出编码
        challenge_response: 用户输入的挑战短语（二次调用时传入）
        challenge_token: 挑战令牌（二次调用时传入）

    Returns:
        dict: {"success": True, "output": "..."}
          或  {"challenge_required": True, "challenge_token": "...", "challenge_phrase": "...", "message": "..."}
          或  {"success": False, "message": "..."}
    """
    # ── TTL 过期清理（入口处执行一次）──
    _evict_stale_challenges()

    # ── 挑战验证流程 ──
    if challenge_response is not None and challenge_token:
        pending = _pending_challenges.pop(challenge_token, None)
        if not pending:
            _log("WARN", f"无效挑战令牌: {challenge_token[:8]}...")
            return {"success": False, "message": "挑战令牌无效或已过期，请重新发起操作"}
        if challenge_response.strip() != DANGER_CHALLENGE_PHRASE:
            _log("WARN", f"挑战短语不匹配: '{challenge_response[:40]}...'")
            return {"success": False, "message": "授权文本不匹配，操作已取消"}
        # 验证通过，恢复原始命令继续执行
        script = pending["command"]
        _log("AUTH", f"用户授权执行: {pending['desc']} | cmd={script[:120]}")

    # ── 永久拦截 ──
    blocked_pattern = _check_always_blocked(script)
    if blocked_pattern:
        _log("BLOCK", f"永久拦截 (匹配: {blocked_pattern}): {script[:120]}")
        return {"success": False, "message": f"安全策略：此命令被永久禁止执行。如需操作请手动完成。"}

    # ── 路径硬拦截（L0）──
    protected_hit = _check_protected_paths(script)
    if protected_hit:
        _log("BLOCK", f"路径保护拦截 ({protected_hit}): {script[:120]}")
        return {"success": False, "message": f"安全策略：禁止删除 {protected_hit}。此路径受硬保护，不可通过命令删除。"}

    # ── 危险检测 → 发起挑战 ──
    is_danger, danger_desc = _check_danger(script)
    if is_danger:
        token = secrets.token_hex(16)
        _pending_challenges[token] = {
            "command": script,
            "desc": danger_desc,
            "timestamp": datetime.now().isoformat(),
        }
        _log("CHALLENGE", f"发起挑战: {danger_desc} | token={token[:8]}...")
        return {
            "challenge_required": True,
            "challenge_token": token,
            "danger_type": danger_desc,
            "challenge_phrase": DANGER_CHALLENGE_PHRASE,
            "message": (
                f"检测到危险操作: {danger_desc}\n\n"
                f"请完整输入以下授权文字以继续:\n"
                f"「{DANGER_CHALLENGE_PHRASE}」"
            ),
        }

    # ── 执行：subprocess.run(list, shell=False) ──
    cmd_list = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    try:
        result = subprocess.run(
            cmd_list,
            shell=False,
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding=encoding,
            errors="replace",
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            error = result.stderr.strip()
            _log("FAIL", f"rc={result.returncode} stderr={error[:120]}")
            return {"success": False, "message": f"命令执行失败 (code {result.returncode}): {error}"}
        _log("OK", f"返回 {len(output)} 字符")
        return {"success": True, "output": output}
    except subprocess.TimeoutExpired:
        _log("TIMEOUT", f"超时 ({timeout}s): {script[:80]}")
        return {"success": False, "message": f"命令执行超时 ({timeout}s)"}
    except Exception as e:
        _log("ERROR", str(e)[:200])
        return {"success": False, "message": str(e)}


def run_cmd(command_list, working_dir=None, timeout=30, encoding="gbk",
            challenge_response=None, challenge_token=None):
    """
    安全执行 CMD 命令（list 形式，shell=False）。

    用于已拆分好参数列表的场景（如 reg 命令）。
    对 reg delete 会走挑战流程。
    """
    # 将 list 转为字符串用于危险检测
    cmd_str = " ".join(command_list)

    # ── TTL 过期清理（入口处执行一次）──
    _evict_stale_challenges()

    # ── 挑战验证 + 永久拦截 + 危险检测 ──
    if challenge_response is not None and challenge_token:
        pending = _pending_challenges.pop(challenge_token, None)
        if not pending:
            _log("WARN", f"无效挑战令牌: {challenge_token[:8]}...")
            return {"success": False, "message": "挑战令牌无效或已过期"}
        if challenge_response.strip() != DANGER_CHALLENGE_PHRASE:
            return {"success": False, "message": "授权文本不匹配，操作已取消"}
        command_list = pending.get("cmd_list", command_list)
        cmd_str = " ".join(command_list)
        _log("AUTH", f"用户授权执行: {pending['desc']}")

    blocked = _check_always_blocked(cmd_str)
    if blocked:
        _log("BLOCK", f"永久拦截: {cmd_str[:120]}")
        return {"success": False, "message": "安全策略：此命令被永久禁止执行"}

    # ── 路径硬拦截（L0）──
    protected_hit = _check_protected_paths(cmd_str)
    if protected_hit:
        _log("BLOCK", f"路径保护拦截 ({protected_hit}): {cmd_str[:120]}")
        return {"success": False, "message": f"安全策略：禁止删除 {protected_hit}。此路径受硬保护，不可通过命令删除。"}

    is_danger, danger_desc = _check_danger(cmd_str)
    if is_danger:
        token = secrets.token_hex(16)
        _pending_challenges[token] = {
            "command": cmd_str,
            "cmd_list": command_list,
            "desc": danger_desc,
        }
        _log("CHALLENGE", f"发起挑战: {danger_desc}")
        return {
            "challenge_required": True,
            "challenge_token": token,
            "danger_type": danger_desc,
            "challenge_phrase": DANGER_CHALLENGE_PHRASE,
            "message": (
                f"检测到危险操作: {danger_desc}\n\n"
                f"请完整输入以下授权文字以继续:\n"
                f"「{DANGER_CHALLENGE_PHRASE}」"
            ),
        }

    # ── 执行 ──
    try:
        result = subprocess.run(
            command_list,
            shell=False,
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding=encoding,
            errors="replace",
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            error = result.stderr.strip() or "未知错误"
            return {"success": False, "message": f"命令执行失败 (code {result.returncode}): {error}"}
        return {"success": True, "output": output}
    except subprocess.TimeoutExpired:
        return {"success": False, "message": f"命令执行超时 ({timeout}s)"}
    except Exception as e:
        return {"success": False, "message": str(e)}
