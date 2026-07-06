"""
安全命令执行模块

防护层（由外到内）：
  L0: 路径硬拦截 — 删除类命令命中受保护路径时直接拒绝
  L1: 挑战短语验证 — 用户必须完整打出授权文本才能执行危险操作
  L2: 危险模式检测 — 命令语义匹配破坏性关键词
  L3: subprocess.run(list, shell=False) — 切断 OS shell 元字符注入

设计原则：不限制用户权限，不创建受限账户。通过确认机制阻止模型写出危险代码，
同时保证 pip install / 软件安装等正常操作不受影响。

安全集合数据源：config/safety_rules.yaml（修改后自动热加载，无需重启）
"""
import subprocess
import re
import secrets
import json
import os
from datetime import datetime

from config.config_loader import get_safety_config
from config.settings import COMMAND_TIMEOUT


# ═══════════════════════════════════════════════
# 命令规范化：自动修正 LLM 常见的参数幻觉错误
# 每一条必须是高置信度模式，宁可漏修不可误伤正确命令
# ═══════════════════════════════════════════════

_COMMAND_NORMALIZATIONS = [
    # taskkill /F /m  →  taskkill /F /IM（LLM 常把 /IM 幻觉为 /m）
    (r'\btaskkill\s+(/F\s+)/m\b',  r'taskkill \1/IM'),
]

def _normalize_command(script: str) -> str:
    """对命令做无害的已知错误模式修正，返回修正后的命令。"""
    for pattern, replacement in _COMMAND_NORMALIZATIONS:
        script = re.sub(pattern, replacement, script, flags=re.IGNORECASE)
    return script


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
            stale.append(token)
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
# 命令安全检查（数据源：config/safety_rules.yaml）
# ═══════════════════════════════════════════════

def _is_delete_command(command: str) -> bool:
    """判断命令是否为删除类操作"""
    patterns = get_safety_config().delete_command_patterns
    for pattern in patterns:
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
    for pattern, desc in get_safety_config().protected_path_patterns:
        if re.search(pattern, command):
            return desc
    return None


def _check_always_blocked(command):
    """检查是否命中永久拦截规则，返回被拦截的模式或 None"""
    for pattern in get_safety_config().always_blocked:
        if re.search(pattern, command, re.IGNORECASE):
            return pattern
    return None


def _check_danger(command):
    """检查是否命中危险模式，返回 (is_dangerous, danger_desc, challenge_level)"""
    for pattern, desc, level in get_safety_config().danger_patterns:
        if re.search(pattern, command, re.IGNORECASE):
            return True, desc, level
    return False, "", ""


def _get_challenge_phrase() -> str:
    """获取当前挑战短语（从热加载配置实时读取）"""
    return get_safety_config().danger_challenge_phrase


def is_dangerous(command):
    """公开接口：判断命令是否危险（供外部确认流程使用）"""
    blocked = _check_always_blocked(command)
    if blocked:
        return True
    danger, _, _ = _check_danger(command)
    return danger


# ═══════════════════════════════════════════════
# 安全执行
# ═══════════════════════════════════════════════

def run_powershell(script, working_dir=None, timeout=COMMAND_TIMEOUT, encoding="gbk",
                   challenge_response=None, challenge_token=None, domain_confirmed=False):
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

    # ── 命令规范化：修正 LLM 幻觉产生的语法错误（如 taskkill /m → /IM）──
    script = _normalize_command(script)

    challenge_phrase = _get_challenge_phrase()
    challenge_verified = False  # 标记 challenge 是否已通过验证

    # ── 挑战验证流程（分级）──
    if challenge_response is not None and challenge_token:
        pending = _pending_challenges.pop(challenge_token, None)
        if not pending:
            _log("WARN", f"无效挑战令牌: {challenge_token[:8]}...")
            return {"success": False, "message": "挑战令牌无效或已过期，请重新发起操作"}
        if not challenge_response or not challenge_response.strip():
            _log("WARN", "挑战短语为空")
            return {"success": False, "message": "授权文本为空，操作已取消"}
        # 挑战级操作仍需匹配挑战短语
        if pending.get("level") == "challenge" and challenge_response.strip() != challenge_phrase:
            _log("WARN", f"挑战短语不匹配: '{challenge_response[:40]}...'")
            return {"success": False, "message": "授权文本不匹配，操作已取消"}
        # 验证通过，恢复原始命令继续执行
        script = pending["command"]
        challenge_verified = True  # 标记已通过验证，跳过后续 danger 重复检查
        _log("AUTH", f"用户授权执行: {pending['desc']} | cmd={script[:120]}")

    # ── 永久拦截（Layer 1：始终生效，即使是已验证的 challenge 也要再查）──
    blocked_pattern = _check_always_blocked(script)
    if blocked_pattern:
        _log("BLOCK", f"永久拦截 (匹配: {blocked_pattern}): {script[:120]}")
        return {"success": False, "message": f"安全策略：此命令被永久禁止执行。如需操作请手动完成。"}

    # ── 路径硬拦截（L0）──
    protected_hit = _check_protected_paths(script)
    if protected_hit:
        _log("BLOCK", f"路径保护拦截 ({protected_hit}): {script[:120]}")
        return {"success": False, "message": f"安全策略：禁止删除 {protected_hit}。此路径受硬保护，不可通过命令删除。"}

    # ── 危险检测 → 发起挑战（分级提示）。已验证过的 challenge 或域已确认则跳过重复检查 ──
    if not challenge_verified and not domain_confirmed:
        is_danger, danger_desc, danger_level = _check_danger(script)
    else:
        is_danger = False
    if is_danger:
        token = secrets.token_hex(16)
        _pending_challenges[token] = {
            "command": script,
            "desc": danger_desc,
            "level": danger_level,
            "timestamp": datetime.now().isoformat(),
        }
        if danger_level == "challenge":
            _log("CHALLENGE", f"发起挑战(挑战级): {danger_desc} | token={token[:8]}...")
            return {
                "challenge_required": True,
                "challenge_token": token,
                "danger_type": danger_desc,
                "challenge_phrase": challenge_phrase,
                "message": (
                    f"检测到危险操作: {danger_desc}\n\n"
                    f"请完整输入以下授权文字以继续:\n"
                    f"「{challenge_phrase}」"
                ),
            }
        else:
            _log("CHALLENGE", f"发起挑战(轻量级): {danger_desc} | token={token[:8]}...")
            return {
                "challenge_required": True,
                "challenge_token": token,
                "danger_type": danger_desc,
                "challenge_phrase": challenge_phrase,
                "message": (
                    f"检测到操作: {danger_desc}\n\n"
                    f"回复「确认」或「OK」即可执行，回复「取消」放弃。"
                ),
            }

    # ── 执行：subprocess.run(list, shell=False) ──
    # 检测是否为 CMD 专有命令（wmic/reg/msiexec 等），需要通过 cmd /c 执行
    cmd_exclusive_programs = {"wmic", "reg", "msiexec", "sc", "net", "taskkill", "tasklist", "ipconfig", "ping", "tracert", "nslookup", "systeminfo", "where", "findstr", "fc", "comp", "certutil", "shutdown", "format", "diskpart", "assoc", "ftype", "chkdsk", "defrag", "sfc", "chcp", "mode", "title", "color", "prompt", "cls", "ver", "vol", "label", "date", "time", "path", "set", "copy", "move", "del", "rd", "md", "ren", "type", "more", "find", "sort", "tree", "attrib", "xcopy", "robocopy", "for", "if", "goto", "call", "start", "exit", "rem"}
    first_token = script.strip().split()[0] if script.strip() else ""
    first_prog = first_token.lower().replace('"', '').replace("'", '')
    
    if first_prog in cmd_exclusive_programs:
        cmd_list = ["cmd", "/c", script]
    else:
        cmd_list = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    
    is_gui_operation = False
    if first_prog.endswith(".exe") or "Start-Process" in script or "start-process" in script.lower():
        is_gui_operation = True
    
    try:
        if is_gui_operation:
            subprocess.Popen(
                cmd_list,
                shell=False,
                cwd=working_dir,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0,
            )
            _log("OK", f"GUI程序已启动: {script[:120]}")
            return {"success": True, "output": "", "is_gui_operation": True}
        else:
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


def run_cmd(command_list, working_dir=None, timeout=COMMAND_TIMEOUT, encoding="gbk",
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

    challenge_phrase = _get_challenge_phrase()
    challenge_verified = False

    # ── 挑战验证（分级）──
    if challenge_response is not None and challenge_token:
        pending = _pending_challenges.pop(challenge_token, None)
        if not pending:
            _log("WARN", f"无效挑战令牌: {challenge_token[:8]}...")
            return {"success": False, "message": "挑战令牌无效或已过期"}
        if not challenge_response or not challenge_response.strip():
            return {"success": False, "message": "授权文本为空，操作已取消"}
        # 挑战级操作仍需匹配挑战短语
        if pending.get("level") == "challenge" and challenge_response.strip() != challenge_phrase:
            return {"success": False, "message": "授权文本不匹配，操作已取消"}
        command_list = pending.get("cmd_list", command_list)
        cmd_str = " ".join(command_list)
        challenge_verified = True
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

    if not challenge_verified:
        is_danger, danger_desc, danger_level = _check_danger(cmd_str)
    else:
        is_danger = False
    if is_danger:
        token = secrets.token_hex(16)
        _pending_challenges[token] = {
            "command": cmd_str,
            "cmd_list": command_list,
            "desc": danger_desc,
            "level": danger_level,
        }
        if danger_level == "challenge":
            _log("CHALLENGE", f"发起挑战(挑战级): {danger_desc}")
            return {
                "challenge_required": True,
                "challenge_token": token,
                "danger_type": danger_desc,
                "challenge_phrase": challenge_phrase,
                "message": (
                    f"检测到危险操作: {danger_desc}\n\n"
                    f"请完整输入以下授权文字以继续:\n"
                    f"「{challenge_phrase}」"
                ),
            }
        else:
            _log("CHALLENGE", f"发起挑战(轻量级): {danger_desc}")
            return {
                "challenge_required": True,
                "challenge_token": token,
                "danger_type": danger_desc,
                "challenge_phrase": challenge_phrase,
                "message": (
                    f"检测到操作: {danger_desc}\n\n"
                    f"回复「确认」或「OK」即可执行，回复「取消」放弃。"
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
