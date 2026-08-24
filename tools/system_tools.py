"""系统工具：命令执行、Python脚本运行、注册表操作"""
import os
import json
import secrets
import time
import subprocess
import re
import shlex
import shutil
import threading
import tempfile
from datetime import datetime

from . import register_tool, path_params
from . import safe_exec
from . import post_action_verify
from .side_effect_journal import side_effect_journal
from config.config_loader import get_safety_config
from config.settings import MAX_OUTPUT_CHARS, PYTHON_TIMEOUT

# 注意：tools/system_tools.py 已统一使用 core.confirmation_manager
# 惰性导入：避免 tools → core → tools 循环导入
_confirmation_manager = None

def _get_confirmation_manager():
    """懒加载并缓存全局确认管理器实例。"""
    global _confirmation_manager
    if _confirmation_manager is None:
        from core.confirmation_manager import confirmation_manager as _cm
        _confirmation_manager = _cm
    return _confirmation_manager

# ============================================================
# 敏感系统路径读取拦截（工具层 Path B：P0/BLOCK，直接拒绝，不弹确认框）
# ============================================================
# 命中 C:\Windows、C:\Program Files、C:\Program Files (x86)、C:\ProgramData
# 及子目录的「读取命令」一律直接拒绝；写入命令（Remove-Item 等）走既有通道。

# 读取语义命令识别：命令中包含这些只读命令即视为"读取"。
_READ_COMMAND_RE = re.compile(
    r"\b(get-content|get-childitem|get-item|get-itemproperty|type|cat|dir|findstr|select-string|fc|comp|more)\b",
    re.IGNORECASE,
)
_CMD_PATH_RE = re.compile(
    r""""([A-Za-z]:[\\/][^"]+)"|'([A-Za-z]:[\\/][^']+)'|([A-Za-z]:[\\/][^\s"'`;|&]+)"""
)

# 注册表写操作（HKCU）正则：命中即 P2 确认（不得直接放行）。
# 覆盖 reg add/delete/import 与 Set-ItemProperty/New-ItemProperty/Remove-ItemProperty
# 的目标为 HKCU/HKEY_CURRENT_USER 的变体；HKLM 变体由 danger_patterns 直接拒绝。
# 注意：禁止使用 .* 连接远端关键词（超长无匹配输入会二次回溯卡死，实测 120KB 耗时 7s+），
# 统一使用 (?=[^\r\n]{0,512}\b...) 限长前瞻，语义等价且线性。
_REGISTRY_WRITE_HKCU_RE = re.compile(
    r"\breg\s+(?:add|delete|import)\b(?=[^\r\n]{0,512}\b(?:HKCU|HKEY_CURRENT_USER)\b)"
    r"|\b(?:Set-ItemProperty|New-ItemProperty|Remove-ItemProperty)\b(?=[^\r\n]{0,512}\b(?:HKCU|HKEY_CURRENT_USER)\b)",
    re.IGNORECASE,
)

# P2-9：其他注册表根键（HKCR/HKU/HKCC 等）写 → P2 确认，与 HKCU 同级；HKLM 由 danger_patterns 直接拒绝
_REGISTRY_WRITE_OTHER_RE = re.compile(
    r"\breg\s+(?:add|delete|import)\b(?=[^\r\n]{0,512}\b(?:HKCR|HKU|HKCC|HKEY_CLASSES_ROOT|HKEY_USERS|HKEY_CURRENT_CONFIG|HKEY_DYN_DATA)\b)"
    r"|\b(?:Set-ItemProperty|New-ItemProperty|Remove-ItemProperty)\b(?=[^\r\n]{0,512}\b(?:HKCR|HKU|HKCC|HKEY_CLASSES_ROOT|HKEY_USERS|HKEY_CURRENT_CONFIG|HKEY_DYN_DATA)\b)",
    re.IGNORECASE,
)

# reg import 单独出现即拒绝：导入的 .reg 文件内容无法静态解析（可能含 HKLM 系统键），
# 且 registry_tools.forbidden_operations 已禁止 import，不允许 Agent 直接执行。
_REG_IMPORT_RE = re.compile(r"\breg\s+import\b", re.IGNORECASE)

# readonly 白名单绕过防护：首 token 命中 readonly_patterns 时，若命令携带管道/分号/逻辑与或
# 组合段，不得整体标记为只读放行（如 "echo xxx | Remove-Item ..." 会被 echo 白名单放行）。
# 带组合段的命令必须继续走完整分类流程（danger/delete/safe_exec 规则）。
_HAS_COMBINED_SEGMENTS_RE = re.compile(r"[|;&]|\b(?:&&|\|\|)\b")

# ── P2-10：low_risk_registry + HKCU 放行收紧 ──
# 仅放行"纯 HKCU 单条注册表写"：命令必须以 reg/注册表 cmdlet 开头、目标根键仅 HKCU/
# HKEY_CURRENT_USER、不含其他注册表根键、不含分号/管道/&&/|| 等组合分隔符。
_OTHER_REGISTRY_ROOTS_RE = re.compile(
    r"\b(?:HKLM|HKCR|HKU|HKCC|HKEY_LOCAL_MACHINE|HKEY_CLASSES_ROOT|HKEY_USERS|HKEY_CURRENT_CONFIG)\b",
    re.IGNORECASE,
)
_REG_CMD_TOKENS = {"reg", "set-itemproperty", "new-itemproperty", "remove-itemproperty", "set-item", "new-item", "remove-item"}


def _is_hkcu_only_low_risk_command(command, first_token):
    """P2-10：判断是否为纯 HKCU 低风险注册表写（夹带其他命令/根键一律不放行）。"""
    lower = command.lower()
    if "hkcu" not in lower and "HKEY_CURRENT_USER" not in command:
        return False
    if _HAS_COMBINED_SEGMENTS_RE.search(command):
        return False
    if _OTHER_REGISTRY_ROOTS_RE.search(command):
        return False
    # 目标根键必须明确为 HKCU（拒绝"未指明根键但含关键词"的模糊命令）
    if not re.search(r"\b(?:HKCU|HKEY_CURRENT_USER)\b", command, re.IGNORECASE):
        return False
    # 首 token 必须是注册表操作入口
    if first_token.lower() not in _REG_CMD_TOKENS:
        return False
    return True

# ── P1-5：net / wmic 从 readonly 白名单移除后，仅放行明确只读子命令 ──
# net 只读查询（负面断言排除修改参数：/add /delete /active /expires /passwordchg 等）
_NET_READONLY_RE = re.compile(
    r"\bnet\s+(?:view|accounts|statistics|session|config)\b"
    r"|\bnet\s+user\b(?![^\r\n]*(?:/add|/delete|/active|/expires|/passwordchg|/comment|/domain))"
    r"|\bnet\s+share\b(?![^\r\n]*(?:=|/delete|/grant|/remark|/add|/users|/cache))",
    re.IGNORECASE,
)
# wmic 只读查询（list/get 形式，负面断言排除 terminate/call/delete/set/create 修改语义）
_WMIC_READONLY_RE = re.compile(
    r"\bwmic\b(?![^\r\n]*\b(?:terminate|call|delete|set|create|stop|start)\b)",
    re.IGNORECASE,
)

# ── P1-6：系统级操作分级确认（挑战级 P1）──
# 服务停止/删除、启动配置修改、ACL 修改等一律 P1 挑战确认，不得静默放行。
_SYSTEM_LEVEL_P1_PATTERNS = [
    (r"\bsc\s+(?:config|create|delete|stop|start|pause|continue|failure)\b", "服务配置/状态修改 (sc)"),
    (r"\bnet\s+(?:stop|start|pause|continue|use|add|delete|print|localgroup|group|computer|send|time|file|name)\b", "网络/账户/服务操作 (net)"),
    (r"\bnet\s+user\b(?=[^\r\n]*(?:/add|/delete|/active|/expires|/passwordchg|/comment|/domain))", "账户修改 (net user)"),
    (r"\bnet\s+share\b(?=[^\r\n]*(?:=|/delete|/grant|/remark|/add|/users|/cache))", "共享目录修改 (net share)"),
    (r"\btaskkill\b[^\r\n]*\s/F\b", "强制结束进程 (taskkill /F)"),
    (r"\b(?:Set-Service|New-Service|Remove-Service|Restart-Service|Stop-Service|Start-Service|Suspend-Service|Resume-Service)\b", "服务操作 (PowerShell)"),
    (r"\bbcdedit\b(?![^\r\n]*/enum\b)", "启动配置修改 (bcdedit)"),
    (r"\bnetsh\b[^\r\n]*\b(?:set|add|delete|reset|exec|commit)\b", "网络配置修改 (netsh)"),
    (r"\bschtasks\s+/(?:create|delete|change|run|end|register)\b", "计划任务操作 (schtasks)"),
    (r"\b(?:icacls|takeown)\b", "ACL 权限修改 (icacls/takeown)"),
    (r"\bwmic\b[^\r\n]*\b(?:terminate|call|delete|set|create)\b", "WMI 修改操作 (wmic)"),
    # P2-8：diskpart 整体挑战级（clean 由 always_blocked 先拦 P0/REJECT，其余子命令走 P1）
    (r"\bdiskpart\b", "磁盘分区工具 (diskpart)"),
]
_SYSTEM_LEVEL_P1_RE = re.compile(
    "|".join(f"(?:{p})" for p, _ in _SYSTEM_LEVEL_P1_PATTERNS),
    re.IGNORECASE,
)

# ── P1-7：解释器入口带内联代码载荷（无法静态解析确认无危险）→ P1 挑战确认 ──
# -c / -e / /c / /k / -Command 为内联代码参数；python -m 属模块运行（pip 等）不拦截。
_INLINE_INTERPRETER_RE = re.compile(
    r"\b(?:python|python3|pythonw|py|node|nodejs|powershell|pwsh|cmd|bash|sh|zsh|perl|ruby)\b"
    r"[^\r\n]*\s(?:-c|-e|/c|/k|-Command)\b",
    re.IGNORECASE,
)


def _cmd_path_matches(command: str):
    """提取命令中的 Windows 绝对路径（支持引号包裹的含空格路径）。"""
    for m in _CMD_PATH_RE.finditer(command):
        yield next((g for g in m.groups() if g), "")


def _match_p1_desc(command: str) -> str:
    """返回命令命中的第一条 P1 系统级操作描述（用于确认弹窗说明）。"""
    for pattern, desc in _SYSTEM_LEVEL_P1_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return desc
    return "系统级操作"


def _check_sensitive_read_command(command: str):
    """命令同时满足「读取语义 + 命中敏感读取路径」时返回展示描述，否则 None。

    - 读取语义：包含 Get-Content / type / cat / Get-ChildItem / dir 等只读命令；
    - 敏感读取路径：仅 C:\\Windows（读取语义已放宽，Program Files / ProgramData 允许读取）；
    - 仅命中敏感路径但为写操作（如 Remove-Item）不在此拦截；
    - 命中敏感路径但路径在用户目录（如 C:\\Users\\...）不在拦截范围。
    """
    if not command or not _READ_COMMAND_RE.search(command):
        return None
    from core.safety_policy import match_sensitive_read_root
    for p in _cmd_path_matches(command):
        hit = match_sensitive_read_root(p)
        if hit:
            return hit
    return None


# 复制语义命令识别：命令中包含这些复制命令即视为"复制"。
_COPY_COMMAND_RE = re.compile(
    r"\b(copy-item|copy|cp|xcopy|robocopy|shutil\.copy2?|shutil\.copyfile)\b",
    re.IGNORECASE,
)


def _check_sensitive_copy_command(command: str):
    """命令同时满足「复制语义 + 命中复制保护路径」时返回展示描述，否则 None。

    复制语义：Copy-Item / copy / xcopy / robocopy / cp / shutil.copy 等；
    源或目标任一命中 C:\\Windows / C:\\Program Files / C:\\Program Files (x86) /
    C:\\ProgramData 四目录即拒绝——系统核心路径不允许复制（含"复制副本再读"
    绕过路径），也不允许作为复制目标被写入。复制语义保持四目录保护。
    """
    if not command or not _COPY_COMMAND_RE.search(command):
        return None
    from core.safety_policy import match_sensitive_copy_root
    for p in _cmd_path_matches(command):
        hit = match_sensitive_copy_root(p)
        if hit:
            return hit
    return None


# ============================================================
# 卸载路径缓存 — 防止 LLM 捏造路径
# ============================================================
_uninstall_path_cache: dict = {}   # {program_name: set of valid_paths}
_uninstall_path_lock = threading.Lock()
_PATH_CACHE_TTL_SECONDS = 600


_AUDIO_COMMAND_PATTERNS = [
    # pycaw / ctypes 音量设置
    r'\bpycaw\b',
    r'\bIAudioEndpointVolume\b',
    r'\bSetMasterVolumeLevel\b',
    r'\bAudioUtilities\.GetSpeakers\(\)\.EndpointVolume\b',
    r'\bGetMasterVolumeLevelScalar\b',
    # SendKeys 音量键
    r'\bSendKeys\(\[char\]175\)',
    r'\bSendKeys\(\[char\]173\)',
    r'\bSendKeys\(\[char\]174\)',
    r'\bSendKeys\(\[char\]\d+\)',
    # WScript.Shell 音量
    r'\bWScript\.Shell\b.*\bSendKeys\b.*(?:174|175|173)',
    # nircmd / setvol
    r'\bnircmd\b.*\bsetvolume\b',
    r'\bsetvol\b',
    r'\bSetVol\b',
]


def _is_audio_command(command: str) -> bool:
    """判断是否为音频/音量相关命令。匹配则重定向到 system_audio。"""
    if not command:
        return False
    cmd_lower = command.lower()
    # 关键词匹配：音量/音频相关操作（使用正则单词边界避免误匹配）
    audio_keywords = [
        '音量', 'volume', r'\bvol\b', r'\baudio\b', r'\bsound\b', '喇叭',
        '静音', 'mute', r'\bmic\b', 'microphone',
        '扬声器', 'speaker',
    ]
    kw_hit = any(re.search(kw, cmd_lower) for kw in audio_keywords)
    pattern_hit = any(re.search(p, command, re.IGNORECASE) for p in _AUDIO_COMMAND_PATTERNS)
    return kw_hit or pattern_hit


def _win_split_command(cmdline: str) -> list:
    """Windows CommandLineToArgvW 风格解析命令行（保留带空格参数为单 token）。

    与 shlex.split(posix=False) 不同，引号内的空格不会被拆开（如
    /LOG="a b.txt" 应解析为 '/LOG=a b.txt' 而非 '/LOG="a' 与 'b.txt"'），
    供卸载器 Popen 列表形式使用，避免 cmd.exe 引号地狱。
    """
    args = []
    cur = ""
    quote_char = None
    i = 0
    n = len(cmdline)
    while i < n:
        c = cmdline[i]
        if c in ('"', "'"):
            if quote_char is None:
                quote_char = c
            elif quote_char == c:
                quote_char = None
            i += 1
            continue
        if c == '\\' and i + 1 < n and cmdline[i + 1] == '"':
            cur += '"'
            i += 2
            continue
        if c.isspace() and quote_char is None:
            if cur:
                args.append(cur)
                cur = ""
            i += 1
            continue
        cur += c
        i += 1
    if cur:
        args.append(cur)
    return args


def _parse_first_token(command: str) -> str:
    """解析命令的第一个token（shlex.split失败时的兜底）"""
    try:
        tokens = shlex.split(command, posix=False)
        return tokens[0] if tokens else ""
    except Exception:
        pass
    
    command = command.lstrip()
    
    if command.startswith('"'):
        end = command.find('"', 1)
        if end != -1:
            return command[1:end]
        return command[1:]
    
    if command.startswith("'"):
        end = command.find("'", 1)
        if end != -1:
            return command[1:end]
        return command[1:]
    
    first_space = command.find(' ')
    if first_space != -1:
        return command[:first_space]
    
    return command


# ============================================================
# 临时文件夹判定 —— 删除临时文件夹内文件免确认
# ============================================================
_TEMP_DIR_CACHE = None
_TEMP_DIR_LOCK = threading.Lock()

def _get_temp_roots() -> tuple:
    """获取临时文件夹根目录集合（系统 TEMP + workspace\\temp）。"""
    global _TEMP_DIR_CACHE
    if _TEMP_DIR_CACHE is not None:
        return _TEMP_DIR_CACHE
    with _TEMP_DIR_LOCK:
        if _TEMP_DIR_CACHE is not None:
            return _TEMP_DIR_CACHE
        roots = set()
        try:
            roots.add(os.path.normcase(os.path.abspath(tempfile.gettempdir())))
        except Exception:
            pass
        try:
            from config.settings import DATA_DIR
        except Exception:
            DATA_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'smartDesktop')
        workspace_temp = os.path.join(DATA_DIR, "workspace", "temp")
        roots.add(os.path.normcase(os.path.abspath(workspace_temp)))
        _TEMP_DIR_CACHE = tuple(sorted(roots))
        return _TEMP_DIR_CACHE


def _extract_delete_targets(command: str) -> list:
    """提取删除命令中引号包裹或常见形式的路径目标（含 $env:TEMP / %TEMP% 展开）。"""
    targets = []
    # 引号包裹的路径（单/双引号）
    targets += re.findall(r'"([^"]+)"', command)
    targets += re.findall(r"'([^']+)'", command)
    # 未加引号但以盘符\开头的路径
    targets += re.findall(r'[A-Za-z]:\\[^\s;|&]+', command)
    # $env:TEMP\... / %TEMP%\... 形式
    targets += re.findall(r'(?i)\$env:TEMP\\[^\s;|&]+', command)
    targets += re.findall(r'(?i)%TEMP%\\[^\s;|&]+', command)

    expanded = []
    for t in targets:
        t = t.strip().strip('"').strip("'")
        if not t:
            continue
        t = re.sub(r'(?i)\$env:TEMP', lambda m: tempfile.gettempdir().rstrip('\\'), t)
        t = re.sub(r'(?i)%TEMP%', lambda m: tempfile.gettempdir().rstrip('\\'), t)
        if re.search(r'[*?]', t):
            # 含通配符时取通配符前的目录前缀进行判定
            cut = re.split(r'[*?]', t, maxsplit=1)[0]
            t = cut
        expanded.append(t)

    # 去重（同一路径可能同时命中引号正则与盘符正则），避免单文件被误判为批量
    seen = set()
    unique = []
    for t in expanded:
        try:
            key = os.path.normcase(os.path.normpath(t))
        except Exception:
            key = t
        if key not in seen:
            seen.add(key)
            unique.append(t)
    return unique


def _is_temp_dir_delete(command: str) -> bool:
    """判断删除命令的目标是否全部位于临时文件夹内（是则免确认）。"""
    if not any(re.search(pattern, command, re.IGNORECASE) for pattern in _get_cfg().delete_command_patterns):
        return False
    targets = _extract_delete_targets(command)
    if not targets:
        return False
    roots = _get_temp_roots()
    for t in targets:
        try:
            norm = os.path.normcase(os.path.normpath(os.path.abspath(t)))
        except Exception:
            return False
        if not any(norm == root or norm.startswith(root + os.sep) for root in roots):
            return False
    return True


def _is_c_drive_delete(command: str) -> bool:
    """判断删除命令的目标是否位于 C 盘。

    仅作"目标在 C 盘"的初步判定，最终是否拒绝取决于调用处
    （C 盘仅当前用户目录 C:\\Users\\<当前用户> 允许删除，其余一律拒绝）。

    注意：卸载命令（uninstaller exe 等）不在本函数判定范围内，卸载走
    execute_command 步骤 4 的卸载专用分支（C 盘内 P1、非 C 盘 P2）。
    """
    targets = _extract_delete_targets(command)
    if not targets:
        return False
    for t in targets:
        try:
            norm = os.path.normpath(os.path.abspath(t))
            drive = os.path.splitdrive(norm)[0]
        except Exception:
            continue
        if drive.upper() == "C:":
            return True
    return False


def _is_current_user_dir_delete(command: str) -> bool:
    """判断删除命令的目标是否全部位于当前用户目录（C:\\Users\\<当前用户>）内。

    该目录属于用户文件区，允许 Agent 删除（单文件 P2、批量 P1 分级确认），
    不适用「C 盘禁止删除」规则。其余 C 盘位置（含 C:\\Users 下其他用户目录、
    C:\\Windows、C:\\Program Files 等）一律拒绝。
    """
    targets = _extract_delete_targets(command)
    if not targets:
        return False
    user_profile = os.environ.get("USERPROFILE", "")
    if not user_profile:
        return False
    try:
        user_profile_norm = os.path.normcase(os.path.normpath(os.path.abspath(user_profile)))
    except Exception:
        return False
    for t in targets:
        try:
            norm = os.path.normcase(os.path.normpath(os.path.abspath(t)))
        except Exception:
            return False
        if not (norm == user_profile_norm or norm.startswith(user_profile_norm + os.sep)):
            return False
    return True


def _is_batch_delete(command: str) -> bool:
    """判断删除命令是否为批量删除（多目标 / 通配符 / 递归删除目录）。"""
    if re.search(r'[*?]', command):
        return True
    targets = _extract_delete_targets(command)
    if len(targets) > 1:
        return True
    # 递归删除目录视为批量（rmdir /s、rd /s、Remove-Item -Recurse、rm -r、rmtree 等）
    # 注意：-Recurse 前不能用 \b（连字符是非单词字符，\b 无法在空格与 - 之间成立），
    # 统一用 \s 或 - 直接衔接
    if re.search(
        r'(?i)(?:rmdir|rd)\s+/\s*s\b|\bRemove-Item\b[^;|&]*\s-Recurse\b|\brm\s+-r\b|\bshutil\.rmtree\b',
        command,
    ):
        return True
    return False


def _get_uninstall_exe_path(command: str) -> str:
    """提取卸载命令中的卸载器 exe 路径（用于判断卸载目标是否位于 C 盘）。"""
    try:
        tokens = shlex.split(command.strip(), posix=False)
        if not tokens:
            return ""
        _idx = 0
        _leading = tokens[0].strip().lower().strip('"').strip("'")
        if _leading in ("&", "start-process", "start-process.exe", "cmd", "cmd.exe", "start", "call"):
            _idx = 1
        return tokens[_idx].strip('"').strip("'") if len(tokens) > _idx else ""
    except Exception:
        return ""


def _try_redirect_audio_command(command: str) -> str | None:
    """检测音频命令并重定向到 system_audio。返回 JSON 结果或 None（不匹配）。"""
    if not _is_audio_command(command):
        return None

    cmd_lower = command.lower()

    # 解析目标值
    value = None
    # 匹配中文：音量改成45、音量设置到50、音量80等
    value_match = re.search(r'(?:改成|设置为?|调整到|set\s*(?:to|as)?|设为)\s*(\d+)', cmd_lower)
    if not value_match:
        value_match = re.search(r'(?:音量|volume).{0,10}(\d{1,3})', cmd_lower)
    if value_match:
        try:
            v = int(value_match.group(1))
            if 0 <= v <= 100:
                value = v
        except ValueError:
            pass

    # 检测操作类型
    if 'get' in cmd_lower or '查询' in cmd_lower or '多少' in cmd_lower:
        op = 'get_volume'
    elif 'mute' in cmd_lower or '静音' in cmd_lower:
        op = 'toggle_mute'
    elif 'mic' in cmd_lower or '麦克风' in cmd_lower or 'microphone' in cmd_lower:
        if 'set' in cmd_lower or '改' in cmd_lower or '设置' in cmd_lower:
            op = 'set_mic_volume'
        elif 'mute' in cmd_lower or '静音' in cmd_lower:
            op = 'toggle_mic_mute'
        else:
            op = 'get_mic_volume'
    else:
        op = 'set_volume'

    try:
        result = system_audio(operation=op, value=value)
        parsed = json.loads(result)
        parsed['routed_from_execute_command'] = True
        parsed['note'] = '此命令已自动路由到 system_audio 工具执行（零风险，免确认）'
        return json.dumps(parsed, ensure_ascii=False)
    except Exception as e:
        return json.dumps({
            'success': False,
            'message': f'路由到 system_audio 失败: {str(e)}',
            'routed_from_execute_command': True
        }, ensure_ascii=False)


def _get_cfg():
    """获取安全规则配置（便捷封装）。"""
    return get_safety_config()


def _cache_install_paths(program_name: str, results: list):
    """缓存 get_uninstall_string 返回的有效卸载路径，供 execute_command 硬性校验。"""
    paths = set()
    for prog in results:
        for key in ("UninstallString", "QuietUninstallString"):
            val = prog.get(key, "")
            if val:
                # 提取 .exe 路径（去除参数）
                try:
                    tokens = shlex.split(val.strip(), posix=False)
                except Exception:
                    tokens = val.strip().split()
                if tokens:
                    exe = tokens[0].strip('"')
                    if exe.lower().endswith(".exe"):
                        paths.add(os.path.normpath(exe))
                    else:
                        # 处理无引号含空格路径（如 C:\Program Files\App\uninst.exe /S）
                        # shlex.split 会把空格路径拆成多个 token，需逐步拼接直到找到 .exe
                        reconstructed = tokens[0].strip('"')
                        for extra in tokens[1:]:
                            reconstructed += " " + extra.strip('"')
                            if reconstructed.lower().endswith(".exe"):
                                paths.add(os.path.normpath(reconstructed))
                                break
        # 也把 InstallLocation 目录下的 uninstall*.exe 加入
        loc = prog.get("InstallLocation", "")
        if loc and os.path.isdir(loc):
            try:
                import glob as _glob
                for ue in _glob.iglob(os.path.join(loc, "*uninstall*.exe")):
                    paths.add(os.path.normpath(ue))
            except Exception:
                pass
    if paths:
        with _uninstall_path_lock:
            _uninstall_path_cache[program_name.lower()] = (time.time(), paths)


def _get_cached_uninstall_paths(program_name: str) -> set:
    """获取指定程序的缓存卸载路径（已过期则返回空）。"""
    with _uninstall_path_lock:
        entry = _uninstall_path_cache.get(program_name.lower())
        if entry:
            ts, paths = entry
            if time.time() - ts < _PATH_CACHE_TTL_SECONDS:
                return paths
            else:
                del _uninstall_path_cache[program_name.lower()]
    return set()


def _get_confirm_log_path() -> str:
    """获取确认操作日志路径，目录不存在时自动创建。"""
    try:
        from config.settings import DATA_DIR
    except ImportError:
        DATA_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'smartDesktop')
    log_dir = os.path.join(DATA_DIR, "logs", "confirm")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, "confirm_operations.log")


def _log_confirmation(command: str, status: str, detail: str = ""):
    """记录一次确认操作日志（命令、状态、详情）。"""
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


_com_ref_count = {}
_com_ref_lock = threading.Lock()


def _validate_uninstall_path(command: str, prevalidated_paths: list = None) -> dict:
    """提取命令中的 .exe 路径，检查是否存在于缓存的有效卸载路径中。

    prevalidated_paths: 由主进程预注入的当前会话已知的有效卸载路径列表（解决
    multiprocessing 子进程无法共享主进程 in-memory cache 的问题）。
    匹配规则: Windows 路径大小写不敏感 + 路径归一化。
    """
    try:
        tokens = shlex.split(command.strip(), posix=False)
        if not tokens:
            return {"valid": False, "message": "无法解析命令"}
        # 否则 tokens[0]="&" 导致 os.path.exists("&") 始终 False 且注册表匹配永远失败
        _idx = 0
        _leading = tokens[0].strip().lower().strip('"').strip("'")
        if _leading in ("&", "start-process", "start-process.exe", "cmd", "cmd.exe", "start", "call"):
            _idx = 1
        exe_path = tokens[_idx].strip('"').strip("'")
        exe_norm = os.path.normpath(exe_path).lower()
    except Exception:
        return {"valid": False, "message": "命令格式错误"}

    def _exe_basename(p: str) -> str:
        """提取 exe 文件的纯名（msiexec.exe → msiexec），用于别名匹配。"""
        return os.path.splitext(os.path.basename(p))[0].lower()

    def _match_exe(candidate_set: set, target_norm: str) -> bool:
        """判断 target 是否在候选集中，支持 msiexec / MsiExec 别名等价。"""
        if target_norm in candidate_set:
            return True
        target_bn = _exe_basename(target_norm)
        if target_bn in ("msiexec", "msiexec"):
            return any(_exe_basename(p) in ("msiexec", "msiexec") for p in candidate_set)
        return False

    exe_basename = _exe_basename(exe_norm)

    # ① 优先使用主进程预注入的路径列表（跨进程安全）
    if prevalidated_paths:
        normalized = {os.path.normpath(p).lower() for p in prevalidated_paths}
        if _match_exe(normalized, exe_norm):
            return {"valid": True, "message": "路径已由主进程预验证"}

    # ② 降级到本进程 cache（仅在同进程内有效）
    with _uninstall_path_lock:
        for prog_name, (ts, paths) in list(_uninstall_path_cache.items()):
            if time.time() - ts >= _PATH_CACHE_TTL_SECONDS:
                del _uninstall_path_cache[prog_name]
                continue
            normalized_paths = {os.path.normpath(p).lower() for p in paths}
            if _match_exe(normalized_paths, exe_norm):
                return {"valid": True, "message": "路径已缓存"}

    # ③ 快速检查：文件是否存在（比注册表扫描快）
    try:
        if os.path.exists(exe_norm):
            return {"valid": True, "message": "路径文件存在"}
    except Exception:
        pass

    # ④ 兜底：注册表扫描（限制最大条目数，避免性能问题）
    try:
        import winreg
        _REG_ROOTS = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        max_entries = 200
        entries_scanned = 0
        for root, reg_path in _REG_ROOTS:
            if entries_scanned >= max_entries:
                break
            try:
                with winreg.OpenKey(root, reg_path) as key:
                    num_subkeys = winreg.QueryInfoKey(key)[0]
                    for i in range(num_subkeys):
                        if entries_scanned >= max_entries:
                            break
                        entries_scanned += 1
                        try:
                            sub = winreg.EnumKey(key, i)
                            with winreg.OpenKey(key, sub) as sub_key:
                                for valname in ("UninstallString", "QuietUninstallString"):
                                    try:
                                        val, _ = winreg.QueryValueEx(sub_key, valname)
                                        val_tokens = shlex.split(val.strip(), posix=False)
                                        if val_tokens:
                                            reg_exe = val_tokens[0].strip('"')
                                            reg_norm = os.path.normpath(reg_exe).lower()
                                            if _match_exe({reg_norm}, exe_norm):
                                                return {"valid": True, "message": "路径已在注册表中找到"}
                                    except (FileNotFoundError, OSError):
                                        pass
                        except (FileNotFoundError, OSError):
                            pass
            except (FileNotFoundError, OSError):
                pass
    except Exception:
        pass

    return {"valid": False, "message": "卸载路径未预先缓存且无法验证，已拒绝放行"}


def _run_command_threaded(command: str, working_dir: str = None, timeout: int = 120,
                          **safe_exec_kwargs) -> dict:
    """在线程中执行非破坏性命令，共享主进程 Python 状态。

    与 subprocess 子进程不同，线程内存与主进程共享：TOOL_REGISTRY、
    confirmation_manager、_uninstall_path_cache 等模块级状态均可直接访问。

    破坏性命令（卸载/删除/格式化等）应继续走 subprocess 路径以保持隔离。
    """
    result_container: list = [None]
    exception_container: list = [None]

    def _target():
        """子进程线程目标：执行 PowerShell 命令并将结果回写容器。"""
        try:
            result_container[0] = safe_exec.run_powershell(
                command, working_dir=working_dir, timeout=timeout,
                _skip_validation=True, **safe_exec_kwargs
            )
        except Exception as e:
            exception_container[0] = e

    t = threading.Thread(target=_target, daemon=False)
    t.start()
    t.join(timeout=timeout + 10)

    if t.is_alive():
        # 超时但线程未结束，不强行 join 避免卡死主线程
        return {"success": False, "output": "", "message": "命令执行超时（线程未返回）"}

    if exception_container[0] is not None:
        return {"success": False, "output": "",
                "message": f"命令执行异常: {str(exception_container[0])}"}

    return result_container[0] or {"success": False, "output": "", "message": "命令未产生结果"}


@register_tool(timeout=120)
def execute_command(command: str, working_dir: str = None, **kwargs) -> str:
    """在用户电脑上执行 shell/PowerShell 命令并返回输出。
    
    适用场景：
    - 修改系统设置（音量、亮度、分辨率等）
    - 安装/卸载软件（pip install, winget, choco 等）
    - 运行脚本（python xxx.py, node xxx.js）
    - 系统信息查询（systeminfo, ipconfig 等）
    - 进程管理（tasklist, taskkill 等）
    
    Args:
        command: 要执行的命令字符串（Windows 下默认使用 PowerShell）
        working_dir: 工作目录，默认为 workspace
        timeout: 超时秒数，默认30秒
    """
    # 内部确认标志：由 react_engine Path B 确认通过后带 token 重调注入，
    # 不出现在 LLM 可见的 JSON Schema 中（schema 已过滤这些参数名）。
    confirmed = bool(kwargs.get("confirmed"))
    challenge_response = kwargs.get("challenge_response")
    challenge_token = kwargs.get("challenge_token")
    confirmation_token = kwargs.get("confirmation_token")
    domain_confirmed = bool(kwargs.get("domain_confirmed"))
    prevalidated_uninstall_paths = kwargs.get("prevalidated_uninstall_paths")
    stripped = command.lstrip()
    if not stripped:
        return json.dumps({"success": False, "message": "空命令"})

    _confirmed_note = ""
    _is_verified = False
    _command_is_readonly = False  
    # ── 1. 提前验证可能传入的确认令牌（如果有效则直接标记为已验证）──
    if challenge_response and challenge_token:
        pending = _get_confirmation_manager().pop(challenge_token)
        if pending:
            stored_cmd = pending["command"]
            expected_phrase = _get_cfg().danger_challenge_phrase
            if challenge_response == expected_phrase:
                if command.strip() != stored_cmd.strip():
                    return json.dumps({
                        "success": False,
                        "message": "命令不匹配：令牌已绑定其他命令"
                    })
                _is_verified = True
                _confirmed_note = f"[已授权] {challenge_token[:8]}"
        if not _is_verified:
            return json.dumps({
                "success": False,
                "message": "挑战验证失败：令牌无效、已过期或短语不匹配"
            })
    elif confirmation_token:
        pending = _get_confirmation_manager().pop_confirmation(confirmation_token)
        if pending:
            stored_cmd = pending["command"]
            if command.strip() != stored_cmd.strip():
                return json.dumps({
                    "success": False,
                    "message": "命令不匹配：令牌已绑定其他命令"
                })
            _is_verified = True
            _confirmed_note = f"[已确认] {confirmation_token[:8]}"
        if not _is_verified:
            return json.dumps({
                "success": False,
                "message": "确认令牌已过期或不匹配"
            })

    # ── 2. 强制黑名单检查（始终执行，无论是否已验证）──
    if any(re.search(pattern, command, re.IGNORECASE) for pattern in _get_cfg().always_blocked):
        return json.dumps({"success": False, "message": f"命令被阻止：包含危险模式"})

    # ── 2.5 敏感系统路径读取拦截（P0/BLOCK：读取 C:\Windows 等系统目录直接拒绝，不弹确认框）──
    # 读取语义命令（Get-Content/type/cat/Get-ChildItem/dir 等）命中 C 盘稳定系统文件夹即拒绝；
    # 写入命令（Remove-Item 等）不在此拦截，继续走既有删除/危险模式通道。
    _sensitive_read_hit = _check_sensitive_read_command(command)
    if _sensitive_read_hit:
        return json.dumps({
            "success": False,
            "message": "内容不允许操作，如需操作请用户自行手动操作",
            "details": {
                "命令": command[:200],
                "说明": "C 盘稳定系统文件夹（Windows / Program Files / ProgramData）下的文件不允许读取，命中即系统级拒绝"
            }
        }, ensure_ascii=False)

    # ── 2.6 敏感系统路径复制拦截（P0/BLOCK：复制命令命中系统目录直接拒绝，不弹确认框）──
    # 复制语义命令（Copy-Item/copy/xcopy/robocopy/cp/shutil.copy 等）源或目标命中
    # C 盘稳定系统文件夹即拒绝，杜绝"复制系统文件到工作区再读取"的绕过路径。
    _sensitive_copy_hit = _check_sensitive_copy_command(command)
    if _sensitive_copy_hit:
        return json.dumps({
            "success": False,
            "message": "内容不允许操作，如需操作请用户自行手动操作",
            "details": {
                "命令": command[:200],
                "说明": "C 盘稳定系统文件夹（Windows / Program Files / ProgramData）下的文件不允许复制，命中即系统级拒绝"
            }
        }, ensure_ascii=False)

    # ── 3. 音频命令重定向到 system_audio（零风险命令，免确认）──
    audio_result = _try_redirect_audio_command(stripped)
    if audio_result is not None:
        return audio_result

    # ── 4. 卸载命令前置拦截（始终执行路径验证，无论是否已验证）──
    if _is_uninstall_command(command):
        path_check = _validate_uninstall_path(command, prevalidated_uninstall_paths)
        if not path_check["valid"]:
            return json.dumps(path_check, ensure_ascii=False)

        if not _is_verified and not confirmed:
            # 卸载软件分级确认：C 盘内 P1（挑战短语），非 C 盘 P2（普通确认）
            uninstall_exe = _get_uninstall_exe_path(command)
            try:
                _on_c_drive = bool(uninstall_exe) and os.path.splitdrive(os.path.abspath(uninstall_exe))[0].upper() == "C:"
            except Exception:
                _on_c_drive = True  # 无法解析时按最严格处理（C 盘内）
            if _on_c_drive:
                # P1 风险：需要挑战短语确认，返回 confirmation_required 触发系统弹窗
                challenge_phrase = _get_cfg().danger_challenge_phrase
                token = _get_confirmation_manager().create_challenge(
                    command=command,
                    desc="卸载软件（C 盘内）",
                    source="system_tools",
                    level="high"
                )
                return json.dumps({
                    "success": False,
                    "confirmation_required": True,
                    "challenge_phrase": challenge_phrase,
                    "challenge_token": token,
                    "message": f"卸载 C 盘内软件属于 P1 风险操作，需要确认。请回复以下完整短语以确认执行：\\n```\\n{challenge_phrase}\\n```",
                    "details": {
                        "操作": "卸载软件（C 盘内）",
                        "风险等级": "P1",
                        "说明": "此操作将删除软件及其相关文件，不可恢复"
                    }
                }, ensure_ascii=False)
            else:
                # P2 风险：普通确认（确认/取消按钮）
                token = _get_confirmation_manager().create_confirmation(command=command)
                return json.dumps({
                    "success": False,
                    "confirmation_required": True,
                    "confirmation_token": token,
                    "message": "卸载非 C 盘软件属于 P2 风险操作，需要确认",
                    "details": {
                        "操作": "卸载软件（非 C 盘）",
                        "风险等级": "P2",
                        "说明": "此操作将删除软件及其相关文件，不可恢复"
                    }
                }, ensure_ascii=False)
        # confirmed 或已验证 → 继续执行

    # ── 4. 原有的命令分类与风险判断（未经验证时执行）──
    try:
        tokens = shlex.split(stripped, posix=False)
        first_token = tokens[0] if tokens else ""
    except Exception:
        first_token = _parse_first_token(stripped)

    if not _is_verified:
        prog = os.path.basename(first_token).lower()
        prog_no_ext = os.path.splitext(prog)[0]

        if prog and prog in _get_cfg().readonly_patterns:
            if not _HAS_COMBINED_SEGMENTS_RE.search(command):
                _is_verified = True
                _command_is_readonly = True
        elif prog_no_ext and prog_no_ext in _get_cfg().readonly_patterns:
            if not _HAS_COMBINED_SEGMENTS_RE.search(command):
                _is_verified = True
                _command_is_readonly = True
        elif first_token.lower() in _get_cfg().readonly_patterns:
            if not _HAS_COMBINED_SEGMENTS_RE.search(command):
                _is_verified = True
                _command_is_readonly = True
        elif (any(kw in command for kw in _get_cfg().low_risk_registry_patterns)
                and _is_hkcu_only_low_risk_command(command, first_token)):
            # P2-10：仅放行纯 HKCU 低风险注册表写（无分号/管道/其他根键夹带）
            _is_verified = True
            _command_is_readonly = True
        elif _NET_READONLY_RE.search(command) or _WMIC_READONLY_RE.search(command):
            # P1-5：net/wmic 已从 readonly 白名单移除，仅放行明确只读子命令（net view/user
            # 查询、wmic list/get 等）；带修改参数的 net/wmic 由下方 _SYSTEM_LEVEL_P1_RE 拦截。
            _is_verified = True
            _command_is_readonly = True

        if not _is_verified:
            if confirmed or domain_confirmed:
                _is_verified = True
                _confirmed_note = "[已确认]" if confirmed else "[域已确认]"
            elif _SYSTEM_LEVEL_P1_RE.search(command):
                # P1-6：系统级操作（服务/账户/进程/启动配置/网络/计划任务/ACL/WMI 修改）→ P1 挑战确认
                _p1_desc = _match_p1_desc(command)
                challenge_phrase = _get_cfg().danger_challenge_phrase
                token = _get_confirmation_manager().create_challenge(
                    command=command,
                    desc=_p1_desc,
                    source="system_tools",
                    level="challenge"
                )
                _log_confirmation(command, "CHALLENGE", f"系统级操作: {_p1_desc}")
                return json.dumps({
                    "success": False,
                    "confirmation_required": True,
                    "challenge_phrase": challenge_phrase,
                    "challenge_token": token,
                    "message": f"检测到系统级操作: {_p1_desc}，属于 P1 风险操作。请回复以下完整短语以确认执行：\n```\n{challenge_phrase}\n```",
                    "details": {
                        "命令": command[:200],
                        "风险等级": "P1",
                        "说明": _p1_desc
                    }
                }, ensure_ascii=False)
            elif _INLINE_INTERPRETER_RE.search(command):
                # P1-7：解释器入口带内联代码载荷（python -c / node -e / powershell -Command /
                # cmd /c / bash -c 等），载荷无法静态解析确认无危险 → P1 挑战确认。
                # 例外：解释器包装的 C 盘删除不因包装而放宽（保持既有 C 盘删除 REJECT 优先）。
                if (any(re.search(pattern, command, re.IGNORECASE)
                        for pattern in _get_cfg().delete_command_patterns)
                        and _is_c_drive_delete(command)
                        and not _is_current_user_dir_delete(command)):
                    return json.dumps({
                        "success": False,
                        "message": "操作被拒绝：C 盘除当前用户文件夹外禁止 Agent 删除文件",
                        "details": {
                            "命令": command[:200],
                            "说明": "C 盘中仅当前用户文件夹（C:\\Users\\<当前用户>）允许删除，其余位置（含其他用户目录、Windows、Program Files 等）不允许 Agent 执行，请用户手动操作"
                        }
                    }, ensure_ascii=False)
                challenge_phrase = _get_cfg().danger_challenge_phrase
                token = _get_confirmation_manager().create_challenge(
                    command=command,
                    desc="解释器内联代码执行",
                    source="system_tools",
                    level="challenge"
                )
                _log_confirmation(command, "CHALLENGE", "解释器内联代码执行")
                return json.dumps({
                    "success": False,
                    "confirmation_required": True,
                    "challenge_phrase": challenge_phrase,
                    "challenge_token": token,
                    "message": f"检测到解释器内联代码执行（无法静态解析载荷），属于 P1 风险操作。请回复以下完整短语以确认执行：\n```\n{challenge_phrase}\n```",
                    "details": {
                        "命令": command[:200],
                        "风险等级": "P1",
                        "说明": "python -c / node -e / powershell -Command / cmd /c / bash -c 等内联载荷无法静态确认无危险"
                    }
                }, ensure_ascii=False)
            elif any(re.search(p[0], command, re.IGNORECASE) for p in _get_cfg().danger_patterns):
                # 危险模式：无预先授权时直接拒绝，不返回 chat 级确认
                return json.dumps({
                    "success": False,
                    "message": "操作被拒绝：命令匹配危险模式，需要预先授权"
                })
            elif any(re.search(pattern, command, re.IGNORECASE) for pattern in _get_cfg().delete_command_patterns):
                if _is_temp_dir_delete(command):
                    # 临时文件夹豁免：删除临时文件夹内文件无需确认
                    _is_verified = True
                    _confirmed_note = "[临时文件夹豁免]"
                    _log_confirmation(command, "TEMP_EXEMPT", "删除临时文件夹内文件，豁免确认")
                elif _is_c_drive_delete(command) and not _is_current_user_dir_delete(command):
                    # C 盘除当前用户目录（C:\Users\<当前用户>，含子目录）外禁止 Agent 删除：
                    # 含 C:\Users 下其他用户目录、C:\Windows、C:\Program Files 等一律拒绝。
                    # 当前用户目录内删除走下方单文件 P2 / 批量 P1 分级；卸载走步骤 4 专用分支。
                    return json.dumps({
                        "success": False,
                        "message": "操作被拒绝：C 盘除当前用户文件夹外禁止 Agent 删除文件",
                        "details": {
                            "命令": command[:200],
                            "说明": "C 盘中仅当前用户文件夹（C:\\Users\\<当前用户>）允许删除，其余位置（含其他用户目录、Windows、Program Files 等）不允许 Agent 执行，请用户手动操作"
                        }
                    }, ensure_ascii=False)
                elif not _is_batch_delete(command):
                    # 单文件删除（非 C 盘，或 C 盘当前用户目录内）：P2 普通确认（确认/取消按钮）
                    token = _get_confirmation_manager().create_confirmation(command=command)
                    return json.dumps({
                        "success": False,
                        "confirmation_required": True,
                        "confirmation_token": token,
                        "message": "删除文件属于 P2 风险操作，需要确认",
                        "details": {
                            "命令": command[:200],
                            "风险等级": "P2",
                            "说明": "删除单个文件（非 C 盘或 C 盘当前用户目录内）"
                        }
                    }, ensure_ascii=False)
                else:
                    # 批量删除（非 C 盘，或 C 盘当前用户目录内）：P1 挑战短语确认
                    challenge_phrase = _get_cfg().danger_challenge_phrase
                    token = _get_confirmation_manager().create_challenge(
                        command=command,
                        desc="批量删除（非 C 盘或当前用户目录内）",
                        source="system_tools",
                        level="high"
                    )
                    return json.dumps({
                        "success": False,
                        "confirmation_required": True,
                        "challenge_phrase": challenge_phrase,
                        "challenge_token": token,
                        "message": f"批量删除属于 P1 风险操作，需要确认。请回复以下完整短语以确认执行：\\n```\\n{challenge_phrase}\\n```",
                        "details": {
                            "命令": command[:200],
                            "风险等级": "P1",
                            "说明": "批量删除文件/目录（非 C 盘或当前用户目录内）"
                        }
                    }, ensure_ascii=False)
            elif _REG_IMPORT_RE.search(command):
                # reg import：.reg 文件内容不可静态解析，一律拒绝（registry_tools 已禁止 import）
                return json.dumps({
                    "success": False,
                    "message": "操作被拒绝：reg import 导入的注册表文件内容无法静态解析，禁止 Agent 直接执行",
                    "details": {
                        "命令": command[:200],
                        "说明": "请由用户手动运行 reg import，或明确拆分导入键后再授权"
                    }
                }, ensure_ascii=False)
            elif _REGISTRY_WRITE_HKCU_RE.search(command):
                # HKCU 注册表写操作（非 low_risk 白名单项）：P2 普通确认，不得直接放行
                token = _get_confirmation_manager().create_confirmation(command=command)
                return json.dumps({
                    "success": False,
                    "confirmation_required": True,
                    "confirmation_token": token,
                    "message": "修改 HKCU 注册表属于 P2 风险操作，需要确认",
                    "details": {
                        "命令": command[:200],
                        "风险等级": "P2",
                        "说明": "修改用户级注册表（HKCU），需用户确认后执行"
                    }
                }, ensure_ascii=False)
            elif _REGISTRY_WRITE_OTHER_RE.search(command):
                # P2-9：其他注册表根键（HKCR/HKU/HKCC 等）写 → P2 普通确认
                token = _get_confirmation_manager().create_confirmation(command=command)
                return json.dumps({
                    "success": False,
                    "confirmation_required": True,
                    "confirmation_token": token,
                    "message": "修改注册表（非 HKCU 根键）属于 P2 风险操作，需要确认",
                    "details": {
                        "命令": command[:200],
                        "风险等级": "P2",
                        "说明": "修改 HKCR/HKU/HKCC 等注册表根键，需用户确认后执行"
                    }
                }, ensure_ascii=False)
            else:
                # 1) protected_path_patterns（L0 硬拦截）：敏感凭据目录（.ssh/.aws/.kube/.env/.git/
                #    微信/QQ/钉钉）与系统目录（C:\Windows、C:\ProgramData）的修改语义命令 → 直接拒绝；
                #    danger_path_patterns（L2 挑战）：Program Files(x86) 修改 → P1 挑战确认
                #    （保留挑战而非硬拒，避免破坏 winget/pip 等正常安装流程）。
                # 2) _check_danger：rm/del/iex/Start-Process/管道组合等危险规则 → P1 挑战确认。
                _block_hit, _challenge_hit = safe_exec._check_protected_paths(command, cwd=working_dir)
                if _block_hit:
                    _log_confirmation(command, "BLOCK", f"路径保护拦截: {_block_hit}")
                    return json.dumps({
                        "success": False,
                        "message": f"安全策略：禁止修改 {_block_hit}。此路径受硬保护。",
                        "stop": True,
                        "details": {
                            "命令": command[:200],
                            "说明": "目标路径受 protected_path_patterns 硬保护（敏感凭据目录/系统目录），请用户手动完成该操作"
                        }
                    }, ensure_ascii=False)
                if _challenge_hit:
                    challenge_phrase = _get_cfg().danger_challenge_phrase
                    token = _get_confirmation_manager().create_challenge(
                        command=command,
                        desc=_challenge_hit,
                        source="system_tools",
                        level="challenge"
                    )
                    _log_confirmation(command, "CHALLENGE", f"挑战路径: {_challenge_hit}")
                    return json.dumps({
                        "success": False,
                        "confirmation_required": True,
                        "challenge_phrase": challenge_phrase,
                        "challenge_token": token,
                        "message": f"检测到目标路径涉及 {_challenge_hit}，属于 P1 风险操作。请回复以下完整短语以确认执行：\n```\n{challenge_phrase}\n```",
                        "details": {
                            "命令": command[:200],
                            "风险等级": "P1",
                            "说明": _challenge_hit
                        }
                    }, ensure_ascii=False)

                _is_danger, _danger_desc, _danger_level = safe_exec._check_danger(command)
                if _is_danger:
                    challenge_phrase = _get_cfg().danger_challenge_phrase
                    token = _get_confirmation_manager().create_challenge(
                        command=command,
                        desc=_danger_desc,
                        source="system_tools",
                        level="challenge"
                    )
                    _log_confirmation(command, "CHALLENGE", f"危险操作: {_danger_desc}")
                    return json.dumps({
                        "success": False,
                        "confirmation_required": True,
                        "challenge_phrase": challenge_phrase,
                        "challenge_token": token,
                        "message": f"检测到危险操作: {_danger_desc}，属于 P1 风险操作。请回复以下完整短语以确认执行：\n```\n{challenge_phrase}\n```",
                        "details": {
                            "命令": command[:200],
                            "风险等级": "P1",
                            "说明": _danger_desc
                        }
                    }, ensure_ascii=False)
                # 未匹配任何分类的命令：直接执行（UI 确认已在 react_engine 层完成）
                _is_verified = True


    # ── 5. 命令执行 ──
    # 统一兜底为 None（继承当前进程工作目录），同时保证 os.makedirs 逻辑不受影响
    working_dir = working_dir or None
    if working_dir:
        os.makedirs(working_dir, exist_ok=True)

    actual_command = command
    is_uninstall = _is_uninstall_command(command)
    if is_uninstall:
        silent_command = _get_silent_uninstall_command(command)
        if silent_command != command and _is_uninstall_command(silent_command):
            actual_command = silent_command
            _log_confirmation(command, "SILENT", f"自动转换为静默卸载: {actual_command}")
        is_uninstall = _is_uninstall_command(actual_command)

    safe_exec_kwargs = {}
    try:
        if challenge_response and challenge_token and _is_verified:
            # 必须使用新建的令牌传给 safe_exec：原 challenge_token 已在 L442 pop 消耗，
            # 若继续透传旧 token，safe_exec.verify_challenge 再次 pop 必然失败（令牌无效或已过期）。
            _reissue_token = _get_confirmation_manager().create_challenge(
                command=actual_command,
                desc="execute_command 已验证",
                source="powershell",
                level="challenge",
            )
            safe_exec_kwargs = {"challenge_response": challenge_response, "challenge_token": _reissue_token}

        if is_uninstall:
            import subprocess
            try:
                tokens = _win_split_command(actual_command.strip())
                if tokens and tokens[0].strip().lower() in ("&", "cmd", "cmd.exe", "start", "call"):
                    tokens = tokens[1:]
                exe_path = tokens[0].strip('"').strip("'") if tokens else ""
                rest_args = tokens[1:] if len(tokens) > 1 else []
                if not exe_path or not os.path.exists(exe_path):
                    result = {"success": False,
                              "message": f"可执行文件不存在: {exe_path or '未知路径'}",
                              "output": f"可执行文件不存在: {exe_path or '未知路径'}"}
                else:
                    # 设置工作目录为卸载器所在目录（InnoSetup 需要 unins000.dat 等文件在同一目录）
                    uninstall_dir = os.path.dirname(exe_path)
                    # 静默/交互式判定：带 /S、/silent、/verysilent、/qn 等参数为静默卸载，
                    # 不应判定为 GUI 操作（不会弹窗，也无需用户手动操作）
                    is_silent = bool(_SILENT_FLAG_RE.search(actual_command))
                    # 统一使用 Popen 列表形式异步启动（shell=False），避免 cmd.exe 引号地狱
                    # 导致 WinError 123（如 exe 路径或参数含嵌套引号时）
                    proc = subprocess.Popen(
                        [exe_path] + rest_args,
                        shell=False, cwd=uninstall_dir,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                    )
                    if is_silent:
                        # 静默卸载：等待进程结束并返回真实退出码，避免"假启动"误报 GUI 操作
                        try:
                            _exit_code = proc.wait(timeout=120)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            result = {"success": False,
                                      "message": "静默卸载超时（120秒），已终止卸载进程",
                                      "output": "静默卸载超时（120秒），已终止卸载进程"}
                        else:
                            result = {"success": _exit_code == 0,
                                      "output": f"静默卸载进程已结束，退出码 {_exit_code}"}
                            if _exit_code != 0:
                                result["message"] = f"静默卸载退出码 {_exit_code}，可能未成功卸载"
                    else:
                        # 交互式卸载：短等待确认进程存活后移交用户手动操作
                        time.sleep(1.5)
                        if proc.poll() is not None:
                            result = {"success": False,
                                      "message": f"卸载程序启动后立即退出，退出码 {proc.returncode}，请检查权限或卸载器完整性",
                                      "output": f"卸载程序启动后立即退出，退出码 {proc.returncode}"}
                        else:
                            result = {"success": True,
                                      "output": "卸载程序已启动，需要用户手动完成后续操作",
                                      "is_gui_operation": True}
            except FileNotFoundError:
                result = {"success": False, "message": f"可执行文件不存在: {exe_path}", "output": f"可执行文件不存在: {exe_path}"}
            except Exception as e:
                result = {"success": False, "message": f"启动卸载程序失败: {str(e)}", "output": f"启动卸载程序失败: {str(e)}"}
        elif _command_is_readonly and not is_uninstall:
            result = _run_command_threaded(actual_command, working_dir=working_dir, timeout=120,
                                          domain_confirmed=(confirmed or bool(_confirmed_note)),
                                          **safe_exec_kwargs)
        else:
            result = safe_exec.run_powershell(actual_command, working_dir=working_dir, timeout=120, domain_confirmed=(confirmed or bool(_confirmed_note)), _skip_validation=True, **safe_exec_kwargs)

        if result.get("challenge_required"):
            return json.dumps({
                "success": False,
                "message": "操作被拒绝：safe_exec 模块验证失败，需要预先授权"
            })

        success = result.get("success")
        if success is None or success is False:
            error_output = result.get("output", "") or result.get("message", "未知错误")
            return json.dumps({
                "success": False,
                "output": error_output,
                "error": result.get("message", "")
            }, ensure_ascii=False)

        output = result.get("output", "")
        is_gui = result.get("is_gui_operation", False)

        if is_gui:
            return json.dumps({
                "success": True,
                "output": output,
                "is_gui_operation": True,
                "polling": False,
                "warning": "⚠️ GUI 程序已启动，需要用户手动操作完成。"
            }, ensure_ascii=False)

        if len(output) > MAX_OUTPUT_CHARS:
            half = MAX_OUTPUT_CHARS // 2
            output = output[:half] + f"\n[...截断：共 {len(output)} 字符...]\n" + output[-half:]

        # 检测是否为轮询命令（re 已在模块级 import，函数内重复 import 会导致闭包作用域错误）
        _poll_re = re.compile(r"(tasklist|findstr|ping\s+-n|Start-Sleep|timeout|while|until|--watch)", re.I)
        is_polling = bool(_poll_re.search(command))

        return json.dumps({
            "success": True,
            "output": output,
            "output_total_chars": len(result.get("output", "")),
            "truncated": len(result.get("output", "")) > MAX_OUTPUT_CHARS,
            "polling": is_polling
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({
            "success": False,
            "message": f"命令执行异常: {str(e)}",
            "output": str(e)
        }, ensure_ascii=False)

def _classify_uninstall_type(uninstall_string: str) -> str:
    """分类卸载命令类型"""
    if not uninstall_string:
        return "unknown"
    us = uninstall_string.lower()
    if "msiexec" in us and "/x" in us:
        return "msi"
    if us.startswith("unins") or us.startswith("\"unins"):
        return "inno"
    if "setup.exe" in us and (" /uninstall" in us or " /remove" in us or " /repair" in us):
        return "setup"
    if "wmic" in us and "product" in us and "call uninstall" in us:
        return "wmic"
    if "powershell" in us and "uninstall-package" in us:
        return "powershell"
    if "installshield" in us:
        return "installshield"
    return "exe"


def _is_uninstall_command(command: str) -> bool:
    """判断是否为卸载命令"""
    if not command:
        return False
    
    try:
        tokens = shlex.split(command.strip(), posix=False)
        if not tokens:
            return False
        first_token = tokens[0]
        # 找到实际卸载器 token 作为识别与执行目标，避免 tokens[0]="&" 导致识别失败走错分支
        _leading = first_token.strip().strip('"').strip("'").lower()
        if _leading in ("&", "start-process", "start-process.exe", "cmd", "cmd.exe", "start", "call"):
            for tok in tokens[1:]:
                t = tok.strip().strip('"').strip("'")
                if not t:
                    continue
                base = os.path.basename(t).lower()
                if base.startswith("unins") or "uninstall" in base or base in ("msiexec.exe", "msiexec"):
                    first_token = t
                    break
            else:
                for tok in tokens[1:]:
                    t = tok.strip().strip('"').strip("'")
                    if t and not t.startswith("-") and not t.startswith("/"):
                        first_token = t
                        break
    except Exception:
        first_token = _parse_first_token(command)
    
    prog_name = os.path.basename(first_token.strip('"').strip("'")).lower()
    
    cmd_lower = command.lower()
    
    if prog_name == "msiexec" and "/x" in cmd_lower:
        return True
    
    # InnoSetup 卸载器命名规范：unins000.exe / unins001.exe / unins.exe 等，
    # 严格匹配避免误伤 uninstall_test.exe 之类业务文件（原 startswith 存在误匹配）
    if re.match(r"^unins\d*\.exe$", prog_name):
        return True
    
    if prog_name == "setup.exe" and (" /uninstall" in cmd_lower or " /remove" in cmd_lower):
        return True
    
    if prog_name == "wmic" and "product" in cmd_lower and "call uninstall" in cmd_lower:
        return True
    
    if prog_name in ("powershell", "powershell.exe") and "uninstall-package" in cmd_lower:
        return True
    
    if prog_name == "installshield.exe" and "uninstall" in cmd_lower:
        return True
    
    # 只匹配标准卸载程序名，避免误匹配 uninstall_test.exe 等文件
    STANDARD_UNINSTALL_NAMES = {"uninst.exe", "unins000.exe", "uninstall.exe", "uninstall_helper.exe", "uninstaller.exe"}
    if prog_name in STANDARD_UNINSTALL_NAMES:
        return True

    # Start-Process 包装的卸载调用（如 Start-Process -FilePath "...Uninstall.exe"）
    if prog_name in ("start-process", "start-process.exe"):
        import re as _uninstall_re
        if _uninstall_re.search(r'(?:["\'])?[^"\'\\s]*unins[^"\'\\s]*\.exe', cmd_lower, _uninstall_re.IGNORECASE):
            return True

    return False


# 统一的静默卸载参数识别正则（含 /S、/silent、/verysilent、/qn、/qb、/passive 等），
# 用于判断命令是否已是静默模式，避免重复追加静默参数。
# 允许参数被单/双引号包裹（如 Start-Process -ArgumentList '/S'）。
_SILENT_FLAG_RE = re.compile(
    r"(?i)(?:^|[\s'\"])(?:['\"]*)/(?:s|silent|verysilent|quiet|qn|qn-|qb|qb!|qb-|passive)(?:['\"]*)(?=$|[\s'\";])"
)


def _get_silent_uninstall_command(command: str) -> str:
    """尝试获取静默卸载命令"""
    if not command:
        return command
    
    cmd = command.lower()
    
    if "msiexec" in cmd and "/x" in cmd:
        msiexec_silent_patterns = ["/qn", "/qn-", "/qb", "/qb!", "/qb-", "/passive"]
        if not any(pattern in cmd for pattern in msiexec_silent_patterns):
            return command + " /qn"
        return command
    
    if "unins" in cmd:
        # 兼容 NSIS(/S) 与 InnoSetup(/silent、/verysilent)：只要已含任一静默参数就不再追加
        if _SILENT_FLAG_RE.search(command):
            return command
        return command + " /verysilent"
    
    if "setup.exe" in cmd and (" /uninstall" in cmd or " /remove" in cmd):
        setup_silent_patterns = ["/silent", "/quiet", "/qn", "/qn-", "/qb", "/passive"]
        if not any(pattern in cmd for pattern in setup_silent_patterns):
            return command + " /quiet"
        return command
    
    # 扩展：非标准卸载程序（如 ThunderUninstall.exe、Uninstall.exe 等）
    try:
        tokens = shlex.split(command.strip(), posix=False)
        if tokens:
            prog_name = os.path.basename(tokens[0]).lower()
            if "uninstall" in prog_name and prog_name.endswith(".exe"):
                if _SILENT_FLAG_RE.search(command):
                    return command
                return command + " /silent"
    except Exception:
        pass
    
    return command


@register_tool(timeout=30)
def get_uninstall_string(program_name: str) -> str:
    """获取软件的卸载命令路径。

    program_name: 软件名称（支持模糊匹配）

    返回：JSON 格式，包含匹配到的软件列表。
    """
    import winreg

    registry_paths = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall")
    ]
    results = []

    for hkey, reg_path in registry_paths:
        try:
            with winreg.OpenKey(hkey, reg_path) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    subkey_name = winreg.EnumKey(key, i)
                    with winreg.OpenKey(key, subkey_name) as subkey:
                        try:
                            display_name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                            if program_name.lower() in display_name.lower():
                                _matched = True
                            else:
                                _matched = False
                                # 模糊匹配：拼写差1-2个字母也能找到
                                try:
                                    import difflib
                                    pn_lower = program_name.lower()
                                    dn_lower = display_name.lower()
                                    # 先用括号/空格切分，取软件名主体部分对比
                                    dn_short = dn_lower.split('(')[0].split('（')[0].split(' - ')[0].split(',')[0].strip().rstrip()
                                    # 主体匹配（如 qoder vs qoder）
                                    if pn_lower == dn_short:
                                        _matched = True
                                    # 编辑距离相似度 > 0.5
                                    elif len(pn_lower) > 2 and difflib.SequenceMatcher(None, pn_lower, dn_short).ratio() > 0.5:
                                        _matched = True
                                    # 尝试逐个删字符匹配（qorder → qoder 删一个r就匹配）
                                    elif len(pn_lower) > 2:
                                        _found = False
                                        for _i in range(len(pn_lower)):
                                            _truncated = pn_lower[:_i] + pn_lower[_i+1:]
                                            if _truncated == dn_short or _truncated in dn_short or dn_short in _truncated:
                                                _found = True
                                                break
                                        _matched = _found
                                except Exception:
                                    pass
                            if _matched:
                                def _get_value(k, name, default=""):
                                    """读取注册表键值，缺失时返回默认值。"""
                                    try:
                                        return str(winreg.QueryValueEx(k, name)[0])
                                    except FileNotFoundError:
                                        return default
                                    except Exception:
                                        return default
                                uninstall_string = _get_value(subkey, "UninstallString")
                                install_location = _get_value(subkey, "InstallLocation")
                                display_version = _get_value(subkey, "DisplayVersion")
                                publisher = _get_value(subkey, "Publisher")
                                quiet_uninstall = _get_value(subkey, "QuietUninstallString")

                                parsed_args = []
                                if uninstall_string:
                                    try:
                                        parsed_args = shlex.split(uninstall_string.strip(), posix=False)
                                    except Exception:
                                        parsed_args = uninstall_string.strip().split()

                                uninstall_type = _classify_uninstall_type(uninstall_string)

                                results.append({
                                    "DisplayName": display_name,
                                    "RegistryPath": f"HKLM\\{reg_path}\\{subkey_name}" if hkey == winreg.HKEY_LOCAL_MACHINE else f"HKCU\\{reg_path}\\{subkey_name}",
                                    "UninstallString": uninstall_string,
                                    "QuietUninstallString": quiet_uninstall,
                                    "ParsedArgs": parsed_args,
                                    "UninstallType": uninstall_type,
                                    "InstallLocation": install_location,
                                    "DisplayVersion": display_version,
                                    "Publisher": publisher
                                })
                        except Exception:
                            continue
        except Exception:
            continue

    if results:
        # 缓存有效卸载路径，用于 execute_command 硬性校验
        _cache_install_paths(program_name, results)
        return json.dumps({
            "success": True,
            "message": f"找到 {len(results)} 个匹配的软件",
            "programs": results
        }, ensure_ascii=False, indent=2)
    else:
        # 注册表未找到，兜底搜索文件系统：常见安装目录下的 unins*.exe + 程序目录
        fs_results = []
        import glob as _glob
        home = os.path.expanduser("~")
        search_dirs = [
            os.path.join(os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local")), "Programs"),
            os.path.join(os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming"))),
            os.environ.get("PROGRAMFILES", "C:\\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)"),
            home,
        ]
        searched = []
        # 1) 先直接搜程序名目录下的 unins*.exe（最快命中）
        for base_dir in search_dirs:
            if not os.path.isdir(base_dir):
                continue
            try:
                for entry in os.listdir(base_dir):
                    if program_name.lower() in entry.lower():
                        prog_dir = os.path.join(base_dir, entry)
                        if os.path.isdir(prog_dir):
                            searched.append(prog_dir)
                            for fname in os.listdir(prog_dir):
                                if fname.lower().startswith("unins") and fname.lower().endswith(".exe"):
                                    fpath = os.path.join(prog_dir, fname)
                                    fs_results.append({
                                        "DisplayName": entry,
                                        "UninstallString": fpath,
                                        "InstallLocation": prog_dir,
                                        "Source": "filesystem",
                                        "UninstallType": _classify_uninstall_type(fpath),
                                    })
            except PermissionError:
                continue
        # 2) 如果上面没找到，递归搜索 unins*.exe（限制深度防卡死）
        if not fs_results:
            for base_dir in search_dirs[:3]:  # 只在前3个目录深层搜索
                if not os.path.isdir(base_dir):
                    continue
                try:
                    for root, dirs, files in os.walk(base_dir, topdown=True):
                        rel_depth = root[len(base_dir):].count(os.sep)
                        if rel_depth > 4:  # 限制4层
                            dirs.clear()
                            continue
                        for f in files:
                            if f.lower().startswith("unins") and f.lower().endswith(".exe"):
                                fpath = os.path.join(root, f)
                                parent_name = os.path.basename(root)
                                if program_name.lower() in parent_name.lower():
                                    fs_results.append({
                                        "DisplayName": parent_name,
                                        "UninstallString": fpath,
                                        "InstallLocation": root,
                                        "Source": "filesystem_deep",
                                        "UninstallType": _classify_uninstall_type(fpath),
                                    })
                except PermissionError:
                    continue

        if fs_results:
            return json.dumps({
                "success": True,
                "message": f"注册表未找到，但在文件系统发现 {len(fs_results)} 个匹配的卸载程序",
                "programs": fs_results,
                "registry_fallback": "filesystem"
            }, ensure_ascii=False, indent=2)

        return json.dumps({
            "success": False,
            "message": f"未找到名称包含 '{program_name}' 的已安装软件，请尝试其他关键词"
        }, ensure_ascii=False)


@register_tool(timeout=30)
def verify_uninstall(program_name: str, registry_path: str = None) -> str:
    """验证软件是否已卸载成功。

    program_name: 软件名称（支持模糊匹配）
    registry_path: 可选的注册表路径，用于精确验证

    返回：JSON 格式，包含验证结果。
    """
    import winreg

    registry_paths = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall")
    ]

    found = []
    install_locations = []
    
    if registry_path:
        try:
            hkey = winreg.HKEY_LOCAL_MACHINE if registry_path.startswith("HKLM") else winreg.HKEY_CURRENT_USER
            path = registry_path.replace("HKLM\\", "").replace("HKCU\\", "")
            with winreg.OpenKey(hkey, path, 0, winreg.KEY_READ) as subkey:
                found.append({"RegistryPath": registry_path, "exists": True})
                try:
                    install_loc = winreg.QueryValueEx(subkey, "InstallLocation")[0]
                    if install_loc:
                        install_locations.append(str(install_loc))
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            found.append({"RegistryPath": registry_path, "exists": False})
        except Exception:
            found.append({"RegistryPath": registry_path, "exists": False, "error": "访问失败"})
    else:
        for hkey, reg_path in registry_paths:
            try:
                with winreg.OpenKey(hkey, reg_path) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        subkey_name = winreg.EnumKey(key, i)
                        with winreg.OpenKey(key, subkey_name) as subkey:
                            try:
                                display_name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                                if program_name.lower() in display_name.lower():
                                    full_path = f"HKLM\\{reg_path}\\{subkey_name}" if hkey == winreg.HKEY_LOCAL_MACHINE else f"HKCU\\{reg_path}\\{subkey_name}"
                                    found.append({"DisplayName": display_name, "RegistryPath": full_path, "exists": True})
                                    try:
                                        install_loc = winreg.QueryValueEx(subkey, "InstallLocation")[0]
                                        if install_loc:
                                            install_locations.append(str(install_loc))
                                    except FileNotFoundError:
                                        pass
                            except FileNotFoundError:
                                continue
            except Exception:
                continue

    remaining_entries = [item for item in found if item.get("exists", True)]
    
    remaining_files = []
    for loc in install_locations:
        if os.path.exists(loc):
            remaining_files.append({"path": loc, "type": "install_location"})
    
    shortcut_paths = [
        os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
        os.path.join(os.environ.get("ALLUSERSPROFILE", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
        os.path.join(os.environ.get("PUBLIC", ""), "Desktop"),
        os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")
    ]
    
    for shortcut_dir in shortcut_paths:
        if os.path.exists(shortcut_dir):
            try:
                for root, dirs, files in os.walk(shortcut_dir):
                    for f in files:
                        if f.endswith(".lnk") and program_name.lower() in f.lower():
                            remaining_files.append({"path": os.path.join(root, f), "type": "shortcut"})
            except Exception:
                continue

    any_exists = any(item.get("exists", True) for item in found) or len(remaining_files) > 0
    
    message = "验证完成："
    if not any_exists:
        message += "软件已卸载成功，未发现残留"
    else:
        issues = []
        if len(remaining_entries) > 0:
            issues.append(f"注册表残留 {len(remaining_entries)} 项")
        if len(remaining_files) > 0:
            issues.append(f"文件/快捷方式残留 {len(remaining_files)} 个")
        message += "，".join(issues)

    return json.dumps({
        "success": True,
        "uninstalled": not any_exists,
        "message": message,
        "remaining_entries": remaining_entries,
        "remaining_files": remaining_files
    }, ensure_ascii=False, indent=2)




# ═══════════════════════════════════════════════════════════════
# P0-1 / P0-2: 注册表写后读回验证与补偿快照辅助
# ═══════════════════════════════════════════════════════════════

def _read_registry_value_snapshot(hkey, key_path, value_name):
    """读注册表值快照（P0-2 补偿用）。

    Returns:
        (key_exists, value_exists, value, vt)；键不存在返回 None
    """
    import winreg
    try:
        with winreg.OpenKey(hkey, key_path, 0, winreg.KEY_READ) as k:
            try:
                value, vt = winreg.QueryValueEx(k, value_name)
                return (True, True, value, vt)
            except FileNotFoundError:
                return (True, False, None, None)
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _restore_registry_value(hkey, key_path, value_name, old_exists, old_value, old_vt):
    """补偿动作：恢复注册表值（P0-2）。old_exists=False 表示原值不存在→删除当前值。"""
    import winreg
    try:
        if old_exists:
            if old_vt is None:
                old_vt = winreg.REG_SZ
            with winreg.CreateKey(hkey, key_path) as k:
                winreg.SetValueEx(k, value_name, 0, old_vt, old_value)
            return f"已恢复 {key_path}\\{value_name if value_name is not None else '(默认值)'}"
        try:
            with winreg.OpenKey(hkey, key_path, 0, winreg.KEY_WRITE) as k:
                winreg.DeleteValue(k, value_name)
            return f"已删除新增值 {key_path}\\{value_name if value_name is not None else '(默认值)'}"
        except FileNotFoundError:
            return f"值 {key_path}\\{value_name if value_name is not None else '(默认值)'} 已不存在，无需恢复"
    except Exception as e:
        raise RuntimeError(f"补偿失败: {str(e)[:200]}") from e


def _snapshot_registry_key_tree(hkey, key_path, depth=0, max_depth=10):
    """递归导出注册表键树快照（P0-2 删除键前备份用）。

    Returns:
        {"values": [(name, value, vt), ...], "subkeys": {sub_name: snapshot}}；失败返回 None
    """
    import winreg
    if depth > max_depth:
        return None
    try:
        with winreg.OpenKey(hkey, key_path, 0, winreg.KEY_READ) as k:
            values = []
            for i in range(winreg.QueryInfoKey(k)[1]):
                try:
                    name, value, vt = winreg.EnumValue(k, i)
                    values.append((name, value, vt))
                except OSError:
                    continue
            subkeys = {}
            for i in range(winreg.QueryInfoKey(k)[0]):
                try:
                    sub_name = winreg.EnumKey(k, i)
                except OSError:
                    continue
                sub_snapshot = _snapshot_registry_key_tree(hkey, os.path.join(key_path, sub_name), depth + 1, max_depth)
                if sub_snapshot is not None:
                    subkeys[sub_name] = sub_snapshot
            return {"values": values, "subkeys": subkeys}
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _restore_registry_key_tree(hkey, key_path, snapshot):
    """补偿动作：重建注册表键树（P0-2）。"""
    import winreg
    if snapshot is None:
        return f"无快照，跳过恢复 {key_path}"
    try:
        with winreg.CreateKey(hkey, key_path) as k:
            for name, value, vt in snapshot.get("values", []):
                try:
                    winreg.SetValueEx(k, name, 0, vt, value)
                except (OSError, TypeError):
                    try:
                        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, str(value))
                    except OSError:
                        pass
            for sub_name, sub_snapshot in snapshot.get("subkeys", {}).items():
                _restore_registry_key_tree(hkey, os.path.join(key_path, sub_name), sub_snapshot)
        return f"已重建注册表键 {key_path}"
    except Exception as e:
        raise RuntimeError(f"重建注册表键 {key_path} 失败: {str(e)[:200]}") from e


@register_tool(timeout=30)
def modify_registry(operation: str, key: str, value_name: str = None, value_data: str = None, value_type: str = "REG_SZ", **kwargs) -> str:
    """修改 Windows 注册表。

    operation: 操作类型，支持 get / set / delete
    key: 注册表路径（如 HKLM\\SOFTWARE\\Microsoft）
    value_name: 值名称
    value_data: 值数据
    value_type: 值类型（REG_SZ / REG_DWORD / REG_BINARY）
    """
    import winreg

    try:
        if operation == "get":
            hkey = None
            if key.startswith("HKLM\\"):
                hkey = winreg.HKEY_LOCAL_MACHINE
                key_path = key[5:]
            elif key.startswith("HKCU\\"):
                hkey = winreg.HKEY_CURRENT_USER
                key_path = key[5:]
            else:
                return json.dumps({"success": False, "message": "不支持的注册表根键"})

            def _try_open_read(hk, kp):
                """尝试读取注册表项，成功返回 (True, key_handle)，失败返回 (False, None)"""
                try:
                    return (True, winreg.OpenKey(hk, kp, 0, winreg.KEY_READ))
                except FileNotFoundError:
                    return (False, None)
                except OSError:
                    return (False, None)

            success, reg_key = _try_open_read(hkey, key_path)
            wow_used = False
            if not success and hkey == winreg.HKEY_LOCAL_MACHINE and key_path.startswith("SOFTWARE\\"):
                wow_key_path = "SOFTWARE\\Wow6432Node\\" + key_path[len("SOFTWARE\\"):]
                success, reg_key = _try_open_read(hkey, wow_key_path)
                if success:
                    wow_used = True

            if not success:
                return json.dumps({"success": False, "message": "注册表项未找到"}, ensure_ascii=False)

            with reg_key:
                if value_name:
                    value, vt = winreg.QueryValueEx(reg_key, value_name)
                    return json.dumps({
                        "success": True,
                        "message": f"获取成功",
                        "value": str(value),
                        "type": str(vt)
                    }, ensure_ascii=False)
                else:
                    values = []
                    for i in range(winreg.QueryInfoKey(reg_key)[1]):
                        name, value, vt = winreg.EnumValue(reg_key, i)
                        values.append({"name": name, "value": str(value), "type": str(vt)})
                    return json.dumps({
                        "success": True,
                        "message": f"找到 {len(values)} 个值",
                        "values": values
                    }, ensure_ascii=False)

        elif operation == "set":
            hkey = None
            if key.startswith("HKLM\\"):
                hkey = winreg.HKEY_LOCAL_MACHINE
                key_path = key[5:]
            elif key.startswith("HKCU\\"):
                hkey = winreg.HKEY_CURRENT_USER
                key_path = key[5:]
            else:
                return json.dumps({"success": False, "message": "不支持的注册表根键"})

            vt = winreg.REG_SZ
            if value_type == "REG_DWORD":
                vt = winreg.REG_DWORD
                value_data = int(value_data)
            elif value_type == "REG_BINARY":
                vt = winreg.REG_BINARY
                value_data = bytes.fromhex(value_data)

            # P0-2: 写前快照（补偿用，必须在 CreateKey/SetValueEx 之前抓取旧值）
            _old = _read_registry_value_snapshot(hkey, key_path, value_name)
            _old_exists, _old_value, _old_vt = (_old[1], _old[2], _old[3]) if _old else (False, None, None)

            with winreg.CreateKey(hkey, key_path) as reg_key:
                if value_name is None:
                    winreg.SetValueEx(reg_key, None, 0, vt, value_data)
                    # P0-1: 写后读回验证，确认实际生效（SetValueEx 不抛错 ≠ 真正写入）
                    _readback = _read_registry_value_snapshot(hkey, key_path, None)
                    if not _readback or not _readback[1] or _readback[2] != value_data:
                        return json.dumps({
                            "success": False,
                            "message": f"写后验证失败：默认值写入未生效（期望 {value_data!r}，实际 {_readback[2] if _readback else None!r}）"
                        }, ensure_ascii=False)
                    # P0-2: 记录补偿动作（恢复旧值/删除新增值）
                    side_effect_journal.record(
                        "modify_registry",
                        {"operation": "set", "key": key, "value_name": value_name, "value_data": value_data, "value_type": value_type},
                        f"设置注册表默认值 {key}",
                        lambda: _restore_registry_value(hkey, key_path, None, _old_exists, _old_value, _old_vt),
                    )
                    return json.dumps({
                        "success": True,
                        "message": f"注册表项默认值已设置，且读回验证通过"
                    }, ensure_ascii=False)
                else:
                    winreg.SetValueEx(reg_key, value_name, 0, vt, value_data)
                    # P0-1: 写后读回验证，确认实际生效
                    _readback = _read_registry_value_snapshot(hkey, key_path, value_name)
                    if not _readback or not _readback[1] or _readback[2] != value_data:
                        return json.dumps({
                            "success": False,
                            "message": f"写后验证失败：注册表项 '{value_name}' 写入未生效（期望 {value_data!r}，实际 {_readback[2] if _readback else None!r}）"
                        }, ensure_ascii=False)
                    # P0-2: 记录补偿动作（恢复旧值/删除新增值）
                    side_effect_journal.record(
                        "modify_registry",
                        {"operation": "set", "key": key, "value_name": value_name, "value_data": value_data, "value_type": value_type},
                        f"设置注册表项 {key}\\{value_name}",
                        lambda: _restore_registry_value(hkey, key_path, value_name, _old_exists, _old_value, _old_vt),
                    )
                    return json.dumps({
                        "success": True,
                        "message": f"注册表项 '{value_name}' 已设置，且读回验证通过"
                    }, ensure_ascii=False)

        elif operation == "delete":
            hkey = None
            if key.startswith("HKLM\\"):
                hkey = winreg.HKEY_LOCAL_MACHINE
                key_path = key[5:]
            elif key.startswith("HKCU\\"):
                hkey = winreg.HKEY_CURRENT_USER
                key_path = key[5:]
            else:
                return json.dumps({"success": False, "message": "不支持的注册表根键"})

            if value_name:
                # P0-2: 写前快照（补偿用）
                _old = _read_registry_value_snapshot(hkey, key_path, value_name)
                _old_exists, _old_value, _old_vt = (_old[1], _old[2], _old[3]) if _old else (False, None, None)
                with winreg.OpenKey(hkey, key_path, 0, winreg.KEY_WRITE) as reg_key:
                    winreg.DeleteValue(reg_key, value_name)
                # P0-1: 删除后读回验证，确认值确实不存在
                _readback = _read_registry_value_snapshot(hkey, key_path, value_name)
                if _readback is not None and _readback[0] and _readback[1]:
                    return json.dumps({
                        "success": False,
                        "message": f"删除后验证失败：值 '{value_name}' 仍存在"
                    }, ensure_ascii=False)
                # P0-2: 记录补偿动作（恢复原值）
                side_effect_journal.record(
                    "modify_registry",
                    {"operation": "delete", "key": key, "value_name": value_name},
                    f"删除注册表值 {key}\\{value_name}",
                    lambda: _restore_registry_value(hkey, key_path, value_name, _old_exists, _old_value, _old_vt),
                )
                return json.dumps({
                    "success": True,
                    "message": f"值已删除，且读回验证通过"
                }, ensure_ascii=False)
            else:
                # P0-2: 删除前递归导出整棵键树快照（补偿用）
                _old_tree = _snapshot_registry_key_tree(hkey, key_path)

                def _delete_key_recursive(base_hkey, base_path, depth=0):
                    """递归删除注册表键，深度超过 10 层时中止。"""
                    if depth > 10:
                        return False
                    try:
                        with winreg.OpenKey(base_hkey, base_path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
                            num_subkeys = winreg.QueryInfoKey(key)[0]
                            for i in range(num_subkeys - 1, -1, -1):
                                subkey_name = winreg.EnumKey(key, i)
                                subkey_path = os.path.join(base_path, subkey_name)
                                _delete_key_recursive(base_hkey, subkey_path, depth + 1)
                        winreg.DeleteKey(base_hkey, base_path)
                        return True
                    except FileNotFoundError:
                        return True
                    except Exception as e:
                        return False

                success = _delete_key_recursive(hkey, key_path)
                if success:
                    # P0-1: 删除后读回验证，确认键确实不存在
                    _readback = _read_registry_value_snapshot(hkey, key_path, None)
                    if _readback is not None:
                        return json.dumps({
                            "success": False,
                            "message": f"删除后验证失败：键仍存在 {key}"
                        }, ensure_ascii=False)
                    # P0-2: 记录补偿动作（重建整棵键树）
                    side_effect_journal.record(
                        "modify_registry",
                        {"operation": "delete", "key": key, "value_name": value_name},
                        f"递归删除注册表键 {key}",
                        lambda: _restore_registry_key_tree(hkey, key_path, _old_tree),
                    )
                    return json.dumps({
                        "success": True,
                        "message": f"键已删除，且读回验证通过"
                    }, ensure_ascii=False)
                else:
                    return json.dumps({
                        "success": False,
                        "message": f"删除键失败，可能存在权限问题"
                    }, ensure_ascii=False)

        else:
            return json.dumps({"success": False, "message": f"不支持的操作类型: {operation}"})

    except PermissionError:
        return json.dumps({
            "success": False,
            "message": f"权限不足：无法访问注册表项。请以管理员身份运行程序。"
        }, ensure_ascii=False)
    except Exception as e:
        error_msg = str(e)
        error_lower = error_msg.lower()
        permission_keywords = [
            "access denied", "拒绝访问", "permission denied", 
            "权限不足", "privilege", "没有权限", "无法访问",
            "error 5", "error 13", "error 50", "error 1008"
        ]
        if any(kw in error_lower for kw in permission_keywords):
            return json.dumps({
                "success": False,
                "message": f"权限不足：无法访问注册表项。请以管理员身份运行程序。"
            }, ensure_ascii=False)
        return json.dumps({
            "success": False,
            "message": f"注册表操作失败: {error_msg}"
        }, ensure_ascii=False)


@register_tool(timeout=30)
def system_audio(operation: str, value: int = None, device: str = None) -> str:
    """音频控制工具（音量/静音/麦克风/设备切换）。

    operation: 操作类型，支持 get_volume / set_volume / toggle_mute / get_mic_volume / set_mic_volume / toggle_mic_mute / list_devices
    value: 音量值 0-100（仅 set_volume / set_mic_volume 需要）
    device: 设备名关键词（可选，用于指定设备）

    返回：JSON 格式的操作结果
    """
    try:
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        from comtypes import CLSCTX_ALL, CoInitialize, CoUninitialize
        from ctypes import POINTER, cast
    except ImportError as e:
        missing_lib = str(e).split("'")[1] if "'" in str(e) else "未知"
        return json.dumps({
            "success": False,
            "message": f"缺少依赖库 {missing_lib}: {str(e)}，请安装: pip install pycaw comtypes"
        }, ensure_ascii=False)

    tid = threading.get_ident()
    with _com_ref_lock:
        if tid not in _com_ref_count:
            _com_ref_count[tid] = {"value": 0, "initialized": False}
        entry = _com_ref_count[tid]

    if entry["value"] == 0:
        # comtypes 1.4.16：CoInitializeEx(flags) 仅接受一个可选位置参数且无返回值，
        # 初始化失败时抛 COMError；CoInitialize() 等价于 CoInitializeEx(COINIT_APARTMENTTHREADED)。
        # 旧版 comtypes(<1.4) 签名为 CoInitializeEx(None, flags)，需做兼容。
        # RPC_E_CHANGED_MODE(0x80010106) 表示当前线程 COM 已以其他模式初始化，可安全复用。
        from comtypes import CoInitializeEx, COINIT_APARTMENTTHREADED, COMError
        init_ok = False
        init_error = None
        try:
            CoInitializeEx(COINIT_APARTMENTTHREADED)  # comtypes >= 1.4 新签名
            init_ok = True
        except TypeError:
            try:
                CoInitializeEx(None, COINIT_APARTMENTTHREADED)  # comtypes < 1.4 旧签名
                init_ok = True
            except Exception as e:
                init_error = e
        except Exception as e:
            init_error = e
        if init_ok:
            with _com_ref_lock:
                _com_ref_count[tid]["value"] = 1
                _com_ref_count[tid]["initialized"] = True
        else:
            # 线程 COM 已以其他模式初始化（RPC_E_CHANGED_MODE）时视为可用，不重复卸载
            if isinstance(init_error, COMError) and getattr(init_error, "hresult", None) == -2147417850:
                with _com_ref_lock:
                    _com_ref_count[tid]["value"] = 1
                    _com_ref_count[tid]["initialized"] = False
            else:
                return json.dumps({
                    "success": False,
                    "message": f"COM 初始化失败: {type(init_error).__name__}: {init_error}"
                }, ensure_ascii=False)
    else:
        with _com_ref_lock:
            _com_ref_count[tid]["value"] += 1

    try:
        def _get_speakers():
            """获取系统扬声器音量控制对象。"""
            return AudioUtilities.GetSpeakers().EndpointVolume
        
        def _get_microphone():
            """获取系统麦克风音量控制对象，不存在时返回 None。"""
            mic = AudioUtilities.GetMicrophone()
            if mic is None:
                return None
            return mic.EndpointVolume

        def _get_all_devices():
            """获取系统全部音频设备列表。"""
            return AudioUtilities.GetAllDevices()

        if operation == "get_volume":
            volume = _get_speakers()
            current = int(volume.GetMasterVolumeLevelScalar() * 100)
            muted = volume.GetMute()
            return json.dumps({
                "success": True,
                "message": f"当前音量: {current}%",
                "volume": current,
                "muted": bool(muted)
            }, ensure_ascii=False)

        elif operation == "set_volume":
            if value is None:
                return json.dumps({
                    "success": False,
                    "message": "set_volume 需要 value 参数（0-100）"
                }, ensure_ascii=False)
            if value < 0 or value > 100:
                return json.dumps({
                    "success": False,
                    "message": "value 必须在 0-100 之间"
                }, ensure_ascii=False)
            volume = _get_speakers()
            volume.SetMasterVolumeLevelScalar(value / 100.0, None)
            if value > 0:
                volume.SetMute(False, None)
            time.sleep(0.1)
            actual = int(volume.GetMasterVolumeLevelScalar() * 100)
            muted = volume.GetMute()
            if abs(actual - value) <= 5 and (value == 0 or not muted):
                return json.dumps({
                    "success": True,
                    "message": f"音量已设置为 {value}%"
                }, ensure_ascii=False)
            else:
                return json.dumps({
                    "success": False,
                    "message": f"设置失败：期望 {value}%，实际 {actual}%，静音状态: {muted}"
                }, ensure_ascii=False)

        elif operation == "toggle_mute":
            volume = _get_speakers()
            current_muted = volume.GetMute()
            volume.SetMute(not current_muted, None)
            return json.dumps({
                "success": True,
                "message": "已静音" if not current_muted else "已取消静音",
                "muted": not current_muted
            }, ensure_ascii=False)

        elif operation == "get_mic_volume":
            volume = _get_microphone()
            if volume is None:
                return json.dumps({
                    "success": False,
                    "message": "未找到麦克风设备"
                }, ensure_ascii=False)
            current = int(volume.GetMasterVolumeLevelScalar() * 100)
            muted = volume.GetMute()
            return json.dumps({
                "success": True,
                "message": f"当前麦克风音量: {current}%",
                "volume": current,
                "muted": bool(muted)
            }, ensure_ascii=False)

        elif operation == "set_mic_volume":
            if value is None:
                return json.dumps({
                    "success": False,
                    "message": "set_mic_volume 需要 value 参数（0-100）"
                }, ensure_ascii=False)
            if value < 0 or value > 100:
                return json.dumps({
                    "success": False,
                    "message": "value 必须在 0-100 之间"
                }, ensure_ascii=False)
            volume = _get_microphone()
            if volume is None:
                return json.dumps({
                    "success": False,
                    "message": "未找到麦克风设备"
                }, ensure_ascii=False)
            volume.SetMasterVolumeLevelScalar(value / 100.0, None)
            return json.dumps({
                "success": True,
                "message": f"麦克风音量已设置为 {value}%"
            }, ensure_ascii=False)

        elif operation == "toggle_mic_mute":
            volume = _get_microphone()
            if volume is None:
                return json.dumps({
                    "success": False,
                    "message": "未找到麦克风设备"
                }, ensure_ascii=False)
            current_muted = volume.GetMute()
            volume.SetMute(not current_muted, None)
            return json.dumps({
                "success": True,
                "message": "麦克风已静音" if not current_muted else "麦克风已取消静音",
                "muted": not current_muted
            }, ensure_ascii=False)

        elif operation == "list_devices":
            devices = AudioUtilities.GetAllDevices()
            result = []
            for dev in devices:
                try:
                    props = dev.properties
                    friendly_name = ""
                    for i in range(1, props.Count):
                        prop = props.Item(i)
                        if prop.Key == "DeviceFriendlyName":
                            friendly_name = prop.Value
                            break
                    result.append({
                        "name": str(friendly_name),
                        "id": str(dev.id),
                        "type": "speaker" if dev.FriendlyName and "Speaker" in dev.FriendlyName else "microphone"
                    })
                except Exception:
                    continue
            return json.dumps({
                "success": True,
                "message": f"找到 {len(result)} 个音频设备",
                "devices": result
            }, ensure_ascii=False)

        else:
            return json.dumps({
                "success": False,
                "message": f"不支持的操作类型: {operation}。支持的操作: get_volume, set_volume, toggle_mute, get_mic_volume, set_mic_volume, toggle_mic_mute, list_devices"
            }, ensure_ascii=False)

    except Exception as e:
        error_msg = str(e)
        if "access denied" in error_msg.lower() or "权限" in error_msg:
            return json.dumps({
                "success": False,
                "message": "权限不足：无法访问音频设备。请以管理员身份运行程序。"
            }, ensure_ascii=False)
        elif "No such interface" in error_msg or "IID" in error_msg:
            return json.dumps({
                "success": False,
                "message": "COM 接口初始化失败：请确保安装了正确版本的 pycaw 和 comtypes。"
            }, ensure_ascii=False)
        else:
            return json.dumps({
                "success": False,
                "message": f"音频操作失败: {error_msg}"
            }, ensure_ascii=False)
    finally:
        tid = threading.get_ident()
        need_uninit = False
        with _com_ref_lock:
            if tid in _com_ref_count:
                if _com_ref_count[tid]["value"] > 0:
                    _com_ref_count[tid]["value"] -= 1
                    if _com_ref_count[tid]["value"] == 0 and _com_ref_count[tid]["initialized"]:
                        _com_ref_count[tid]["initialized"] = False
                        need_uninit = True
                    if _com_ref_count[tid]["value"] == 0:
                        del _com_ref_count[tid]
        if need_uninit:
            try:
                CoUninitialize()
            except Exception:
                pass

@register_tool(timeout=15)
def find_program(program_name: str) -> str:
    """查找已安装程序的路径，支持通过 PATH、注册表 App Paths、常见安装目录查找，返回多个匹配。
    program_name: 程序名关键词（如 chrome、edge、notepad）。
    来源标记：PATH（环境变量）、registry（注册表 App Paths）、filesystem（安装目录/开始菜单扫描）。"""
    try:
        if not program_name or not program_name.strip():
            return json.dumps({"success": False, "message": "program_name 不能为空"}, ensure_ascii=False)
        raw_name = program_name.strip()
        name_key = raw_name[:-4] if raw_name.lower().endswith(".exe") else raw_name
        name_key = name_key.lower()
        results: list = []
        seen: set = set()

        def _add(path: str, source: str) -> None:
            """将路径加入指定集合，去除重复并规范化。"""
            if not path:
                return
            p = os.path.normpath(path)
            if not os.path.exists(p):
                return
            pk = p.lower()
            if pk in seen:
                return
            seen.add(pk)
            results.append({"name": name_key, "path": p, "source": source})

        # ① PATH：依次试 name / name.exe / name.com / name.bat
        for candidate in (raw_name, name_key + ".exe", name_key + ".com", name_key + ".bat"):
            try:
                found = shutil.which(candidate)
            except Exception:
                found = None
            if found:
                _add(found, "PATH")

        # ② Windows 注册表 App Paths（HKLM + HKCU）
        try:
            import winreg
        except Exception:
            winreg = None
        if winreg is not None:
            _app_paths_key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
            for hive, hive_name in ((winreg.HKEY_LOCAL_MACHINE, "HKLM"), (winreg.HKEY_CURRENT_USER, "HKCU")):
                try:
                    with winreg.OpenKey(hive, _app_paths_key) as key:
                        for i in range(winreg.QueryInfoKey(key)[0]):
                            try:
                                sub = winreg.EnumKey(key, i)
                                if name_key not in sub.lower():
                                    continue
                                try:
                                    with winreg.OpenKey(key, sub) as sk:
                                        try:
                                            val, _ = winreg.QueryValueEx(sk, None)
                                        except OSError:
                                            val = None
                                except OSError:
                                    val = None
                                if val:
                                    _add(val, "registry")
                            except OSError:
                                continue
                except OSError:
                    continue

        # ③ 常见安装目录一层扫描（PROGRAMFILES / PROGRAMFILES(X86) / LOCALAPPDATA\Programs / 开始菜单 Programs）
        base_dirs: list = []
        for env_var in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
            v = os.environ.get(env_var)
            if v and os.path.isdir(v):
                base_dirs.append(v)
        local = os.environ.get("LOCALAPPDATA")
        if local:
            base_dirs.append(os.path.join(local, "Programs"))
        for env_var in ("APPDATA", "PROGRAMDATA"):
            v = os.environ.get(env_var)
            if v:
                base_dirs.append(os.path.join(v, "Microsoft", "Windows", "Start Menu", "Programs"))
        for base in base_dirs:
            if not os.path.isdir(base):
                continue
            try:
                entries = os.listdir(base)
            except (PermissionError, OSError):
                continue
            for entry in entries:
                el = entry.lower()
                if name_key not in el:
                    continue
                full = os.path.join(base, entry)
                try:
                    if os.path.isfile(full) and (el.endswith(".exe") or el.endswith(".lnk")):
                        _add(full, "filesystem")
                    elif os.path.isdir(full):
                        # 目录名命中关键词：在该目录一层内找含关键词的 .exe/.lnk
                        try:
                            for fname in os.listdir(full):
                                fl = fname.lower()
                                if (fl.startswith(name_key) or name_key in fl) and (fl.endswith(".exe") or fl.endswith(".lnk")):
                                    _add(os.path.join(full, fname), "filesystem")
                        except (PermissionError, OSError):
                            continue
                except (PermissionError, OSError):
                    continue

        if not results:
            return json.dumps({"success": False, "message": f"未找到程序: {program_name}", "programs": []}, ensure_ascii=False)
        return json.dumps({
            "success": True,
            "message": f"找到 {len(results)} 个匹配程序",
            "programs": results
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "message": f"查找程序失败: {str(e)}", "programs": []}, ensure_ascii=False)


@register_tool(timeout=30)
def undo_side_effect(operation: str = "undo_last") -> str:
    """撤销/回滚副作用工具操作（P0-2 显式回滚入口）。

    副作用工具（如 modify_registry 修改注册表）执行成功后，会记录
    "操作日志+补偿动作"（含原始值快照）。本工具提供两种回滚方式：

    operation:
      - "undo_last": 撤销最近一次有副作用的操作（推荐，精确回滚单步）
      - "rollback_all": 回滚当前会话已记录的全部副作用操作（按逆序，后发生先撤销）

    返回：JSON 格式的操作结果
    """
    try:
        if operation == "rollback_all":
            results = side_effect_journal.rollback_all()
            return json.dumps({
                "success": True,
                "message": f"已回滚 {len(results)} 条副作用操作",
                "results": results
            }, ensure_ascii=False, default=str)
        result = side_effect_journal.undo_last()
        if result is None:
            return json.dumps({
                "success": False,
                "message": "没有可撤销的副作用操作记录"
            }, ensure_ascii=False)
        return json.dumps({
            "success": True,
            "message": f"已撤销: {result.get('description', '')}",
            "result": result
        }, ensure_ascii=False, default=str)
    except Exception as e:
        return json.dumps({
            "success": False,
            "message": f"撤销失败: {str(e)[:200]}"
        }, ensure_ascii=False)
