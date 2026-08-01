"""
安全命令执行模块（全面修复版）

防护层：
  L0: 路径硬拦截 — 修改类命令触碰受保护路径时拒绝（支持环境变量、通配符、相对路径）
  L1: 挑战短语验证 — 必须完整输入授权文本
  L2: 危险模式检测 — 增强版黑名单（含别名、拼接、间接执行、编码命令等）
  L3: subprocess.run(list, shell=False) — 切断 OS shell（但 cmd /c 仍解析元字符，已做额外检测）

所有配置从 config/safety_rules.yaml 热加载。
"""
import subprocess
import re
import secrets
import os
import base64
import shlex
import threading
from datetime import datetime

from config.config_loader import get_safety_config
from config.settings import COMMAND_TIMEOUT

from . import register_tool


# =============================================================================
# 命令规范化
# =============================================================================
_COMMAND_NORMALIZATIONS = [
    (r'\btaskkill\s+(/F\s+)/m\b', r'taskkill \1/IM'),
]

def _normalize_command(script: str) -> str:
    for pattern, replacement in _COMMAND_NORMALIZATIONS:
        script = re.sub(pattern, replacement, script, flags=re.IGNORECASE)
    return script

def _decode_powershell_script(script: str) -> str:
    """解码 -EncodedCommand (及简写 -Enc, -e) 并附加明文"""
    match = re.search(r'-(?:EncodedCommand|Enc|e)\s+([A-Za-z0-9+/=]+)', script, re.IGNORECASE)
    if match:
        try:
            encoded = match.group(1)
            padding = 4 - (len(encoded) % 4)
            if padding != 4:
                encoded += '=' * padding
            decoded = base64.b64decode(encoded).decode('utf-16-le', errors='ignore')
            return script + " " + decoded
        except Exception:
            pass
    return script

def _strip_string_literals(text: str) -> str:
    """移除单双引号包裹的字符串字面量，用于降低误报"""
    # 匹配单引号或双引号字符串，支持转义
    pattern = r"'[^'\\]*(?:\\.[^'\\]*)*'|\"[^\"\\]*(?:\\.[^\"\\]*)*\""
    return re.sub(pattern, '""', text)

def _powershell_quote(s: str) -> str:
    """用单引号包裹字符串，内部单引号转义为 ''"""
    return "'" + s.replace("'", "''") + "'"

def _expand_env_vars(token: str) -> str:
    """展开环境变量：%VAR%、$env:VAR、${env:VAR}、$HOME、$PSHOME 等"""
    def repl_percent(m):
        var = m.group(1)
        return os.environ.get(var, m.group(0))
    token = re.sub(r'%([^%]+)%', repl_percent, token)
    
    def repl_env(m):
        var = m.group(1)
        return os.environ.get(var, m.group(0))
    token = re.sub(r'\$env:([A-Za-z_][A-Za-z0-9_]*)', repl_env, token, flags=re.IGNORECASE)
    
    def repl_env_braces(m):
        var = m.group(1)
        return os.environ.get(var, m.group(0))
    token = re.sub(r'\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}', repl_env_braces, token, flags=re.IGNORECASE)
    
    token = token.replace('$HOME', os.environ.get('USERPROFILE', os.environ.get('HOME', '$HOME')))
    token = token.replace('$PSHOME', os.environ.get('PSHOME', '$PSHOME'))
    token = token.replace('$pwd', os.getcwd())
    
    return token


# =============================================================================
# 内部状态（统一确认管理器）
# 惰性导入：避免 tools → core → tools 循环导入
# =============================================================================
_confirmation_manager = None

def _get_confirmation_manager():
    global _confirmation_manager
    if _confirmation_manager is None:
        from core.confirmation_manager import confirmation_manager as _cm
        _confirmation_manager = _cm
    return _confirmation_manager

_config_cache = None
_config_cache_mtime = 0
_config_cache_lock = threading.Lock()

def _get_safety_config_cached():
    global _config_cache, _config_cache_mtime
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "safety_rules.yaml")
    with _config_cache_lock:
        try:
            if _config_cache is not None and os.path.exists(config_path):
                current_mtime = os.path.getmtime(config_path)
                if current_mtime <= _config_cache_mtime:
                    return _config_cache
        except Exception:
            pass
        _config_cache = get_safety_config()
        try:
            if os.path.exists(config_path):
                _config_cache_mtime = os.path.getmtime(config_path)
        except Exception:
            _config_cache_mtime = 0
        return _config_cache

@register_tool
def invalidate_config_cache():
    global _config_cache, _config_cache_mtime
    with _config_cache_lock:
        _config_cache = None
        _config_cache_mtime = 0

def _evict_stale_challenges():
    """清理过期挑战令牌（委托给统一确认管理器）。"""
    return _get_confirmation_manager().cleanup_expired()

def _get_log_path():
    try:
        from config.settings import DATA_DIR
    except ImportError:
        DATA_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'MyAgent')
    log_dir = os.path.join(DATA_DIR, "logs", "safe_exec")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, "safe_exec.log")

_log_lock = threading.Lock()

def _log(level, message):
    try:
        safe_msg = message.replace('\n', '\\n').replace('\r', '\\r')
        log_path = _get_log_path()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with _log_lock:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] [{level}] {safe_msg}\n")
    except Exception:
        pass


# =============================================================================
# 路径提取与受保护路径检查（支持环境变量、通配符、相对路径、编码命令）
# =============================================================================
def _extract_paths_from_args(args: list, cwd: str = None) -> list:
    """
    从参数列表中提取可能的路径，展开环境变量，并与 cwd 拼接得到绝对路径。
    返回规范化绝对路径列表。
    """
    if cwd is None:
        cwd = os.getcwd()
    paths = []
    for token in args:
        # 跳过纯选项：-f 或 /f 形式（单个字母或短选项名）
        if token.startswith('-'):
            if len(token) == 2 or (len(token) > 2 and token[1].isalpha() and not any(ch in token[1:] for ch in ['\\', '/', '.', ':'])):
                continue
        if token.startswith('/'):
            if len(token) == 1:
                continue
            if token[1].isalpha() and len(token) <= 3 and not any(ch in token[1:] for ch in ['\\', '/', '.', ':']):
                continue
            if re.match(r'^/[A-Za-z_][A-Za-z0-9_]*$', token):
                continue
        
        # 如果包含路径分隔符或点，或者已经像绝对路径
        if '\\' in token or '/' in token or '.' in token or ':' in token:
            expanded = _expand_env_vars(token)
            # 判断是否为绝对路径 (UNC、盘符、或正斜杠开头的Unix风格绝对路径)
            if expanded.startswith('\\\\') or re.match(r'[A-Za-z]:[\\/]', expanded):
                abs_path = expanded
            elif expanded.startswith('/') and len(expanded) > 1:
                abs_path = expanded
            else:
                abs_path = os.path.join(cwd, expanded)
            try:
                norm = os.path.normpath(abs_path)
                paths.append(norm)
            except Exception:
                continue
    return list(set(paths))

def _extract_paths_from_command(command: str, cwd: str = None) -> list:
    """从命令字符串解析参数，并提取路径"""
    try:
        args = shlex.split(command.strip(), posix=False)
    except Exception:
        args = command.strip().split()
    if args:
        # 第一个是命令，只取后续参数
        return _extract_paths_from_args(args[1:], cwd)
    return []

def _is_modify_command(command: str) -> bool:
    config = _get_safety_config_cached()
    modify_patterns = list(config.delete_command_patterns) + [
        r'\b(?:del|erase|rm|rd|ri|Remove-Item|Clear-Content|Set-Content|Add-Content|Out-File)\b',
        r'\b(?:format|diskpart|chkdsk|defrag|sfc)\b',
        r'\b(?:reg\s+(?:add|delete|import|export|save|unload))\b',
        r'\b(?:wmic\s+product\s+call\s+uninstall)\b',
        r'\b(?:sc\s+(?:delete|config|stop))\b',
    ]
    for pattern in modify_patterns:
        if re.search(pattern, command, re.IGNORECASE):
            return True
    return False

def _path_matches_protected(path: str, protected_patterns: list) -> str | None:
    """
    检查单个路径是否匹配受保护模式，支持通配符。
    正确逻辑：用户路径是否会覆盖/匹配受保护目录。
    """
    def _wildcard_to_regex(pattern_str):
        escaped = re.escape(pattern_str)
        escaped = escaped.replace('\\*', '.*').replace('\\?', '.')
        return '^' + escaped + '$'
    
    for protected_pattern, desc in protected_patterns:
        try:
            if '*' in path or '?' in path:
                user_regex = _wildcard_to_regex(path)
                if re.match(user_regex, protected_pattern, re.IGNORECASE):
                    return desc
                if re.match(user_regex.replace(r'\.', r'\.'), protected_pattern, re.IGNORECASE):
                    return desc
            else:
                protected_regex = _wildcard_to_regex(protected_pattern)
                if re.match(protected_regex, path, re.IGNORECASE):
                    return desc
                if re.match(protected_regex.replace(r'\.', r'\.'), path, re.IGNORECASE):
                    return desc
            
            if '*' not in path and '*' not in protected_pattern:
                path_lower = path.lower()
                pattern_lower = protected_pattern.lower()
                if path_lower == pattern_lower:
                    return desc
                if path_lower.startswith(pattern_lower + '\\') or path_lower.startswith(pattern_lower + '/'):
                    return desc
                if pattern_lower.startswith(path_lower + '\\') or pattern_lower.startswith(path_lower + '/'):
                    return desc
        except re.error:
            continue
    return None

def _check_protected_paths(command: str, cwd: str = None, args_list: list = None):
    """
    检查修改类命令是否触碰受保护路径。
    先解码命令，从原命令和解码后的命令中提取路径，合并检查。
    返回 (block_hit, challenge_hit) 元组：
    - block_hit: L0 硬拦截命中描述，无则 None
    - challenge_hit: L2 挑战路径命中描述，无则 None
    两者互斥，最多一个非 None。
    """
    if not _is_modify_command(command):
        return (None, None)

    config = _get_safety_config_cached()
    protected_patterns = config.protected_path_patterns          # L0 硬拦截
    challenge_patterns = getattr(config, 'danger_path_patterns', [])  # L2 挑战层

    # 收集路径：从原始命令提取，从解码命令提取，若提供了args_list则直接使用
    all_paths = set()
    # 从原始命令提取
    if args_list is not None:
        all_paths.update(_extract_paths_from_args(args_list, cwd))
    else:
        all_paths.update(_extract_paths_from_command(command, cwd))
    # 解码后命令提取
    decoded = _decode_powershell_script(command)
    if decoded != command:
        all_paths.update(_extract_paths_from_command(decoded, cwd))

    for path in all_paths:
        desc = _path_matches_protected(path, protected_patterns)
        if desc:
            return (desc, None)   # 硬拦截命中
        desc = _path_matches_protected(path, challenge_patterns)
        if desc:
            return (None, desc)   # 挑战层命中

    # 兜底：对整条命令中可能漏提取的路径做匹配
    # 仅在路径上下文中匹配，避免误报
    expanded = _expand_env_vars(command)
    expanded_decoded = _expand_env_vars(decoded)
    for text in (expanded, expanded_decoded):
        # 先检查 L0 硬拦截
        for pattern, desc in protected_patterns:
            try:
                matches = list(re.finditer(pattern, text, re.IGNORECASE))
                for match in matches:
                    start = match.start()
                    end = match.end()
                    
                    is_path_context = False
                    
                    if start > 0:
                        prev_char = text[start - 1]
                        if prev_char in [' ', '\t', '"', "'", '=', ':', '\\', '/', '|', ';', '>']:
                            is_path_context = True
                    
                    if end < len(text):
                        next_char = text[end]
                        if next_char in [' ', '\t', '"', "'", '\\', '/', '|', ';', '>', ',', ')', ']']:
                            is_path_context = True
                    
                    if start == 0:
                        is_path_context = True
                    
                    if is_path_context:
                        return (desc, None)   # 硬拦截
            except re.error:
                continue
        # 再检查 L2 挑战层
        for pattern, desc in challenge_patterns:
            try:
                matches = list(re.finditer(pattern, text, re.IGNORECASE))
                for match in matches:
                    start = match.start()
                    end = match.end()
                    
                    is_path_context = False
                    
                    if start > 0:
                        prev_char = text[start - 1]
                        if prev_char in [' ', '\t', '"', "'", '=', ':', '\\', '/', '|', ';', '>']:
                            is_path_context = True
                    
                    if end < len(text):
                        next_char = text[end]
                        if next_char in [' ', '\t', '"', "'", '\\', '/', '|', ';', '>', ',', ')', ']']:
                            is_path_context = True
                    
                    if start == 0:
                        is_path_context = True
                    
                    if is_path_context:
                        return (None, desc)   # 挑战层
            except re.error:
                continue
    return (None, None)


# =============================================================================
# 危险检测（增强版，拼接、间接执行、变量赋值等）
# =============================================================================
def _check_dangerous_pipeline(command: str) -> tuple:
    safe_pipeline_prefixes = [
        r'\bGet-', r'\bSelect-', r'\bWhere-', r'\bForEach-', r'\bFormat-',
        r'\bSort-', r'\bGroup-', r'\bMeasure-', r'\bCompare-', r'\bConvert-',
        r'\bExport-', r'\bImport-', r'\bTest-', r'\bWrite-', r'\bOut-',
        r'\bSelect-Object', r'\bWhere-Object', r'\bForEach-Object',
        r'\bFormat-Table', r'\bFormat-List', r'\bSort-Object',
        r'\bMeasure-Object', r'\bCompare-Object', r'\bGet-ChildItem',
        r'\bGet-Item', r'\bGet-Content', r'\bGet-ItemProperty',
        r'\bSelect-String', r'\bWrite-Host', r'\bWrite-Output',
        r'\bOut-Null', r'\bOut-File', r'\bOut-String',
        r'\bConvertTo-Json', r'\bConvertFrom-Json',
        r'\bExport-Csv', r'\bImport-Csv',
    ]
    dangerous_pipeline_targets = [
        (r'\|\s*(?:bash|sh|cmd|powershell|pwsh)\b', '跨进程命令注入', 'challenge'),
        (r'\|\s*(?:rm|del|erase|rd|ri|Remove-Item)\b', '管道后执行删除', 'challenge'),
        (r'\|\s*(?:format|diskpart|chkdsk|sfc)\b', '管道后执行系统操作', 'challenge'),
        (r'\|\s*(?:reg\s+add|reg\s+delete|reg\s+import)\b', '管道后执行注册表修改', 'challenge'),
        (r'\|\s*(?:wmic\s+product\s+call\s+uninstall)\b', '管道后执行卸载', 'challenge'),
        (r'\|\s*(?:sc\s+delete|sc\s+config)\b', '管道后执行服务操作', 'challenge'),
    ]
    dangerous_semicolon_patterns = [
        (r';\s*(?:rm|del|erase|rd|ri|Remove-Item)\b', '分号后执行删除', 'challenge'),
        (r';\s*(?:format|diskpart)\b', '分号后执行系统操作', 'challenge'),
        (r';\s*(?:reg\s+add|reg\s+delete)\b', '分号后执行注册表修改', 'challenge'),
    ]

    for pattern, desc, level in dangerous_pipeline_targets:
        if re.search(pattern, command, re.IGNORECASE):
            return True, desc, level

    for pattern, desc, level in dangerous_semicolon_patterns:
        if re.search(pattern, command, re.IGNORECASE):
            return True, desc, level

    pipe_segments = re.split(r'\|', command)
    for i in range(1, len(pipe_segments)):
        segment = pipe_segments[i].strip()
        if not segment:
            continue
        is_safe = False
        for safe_prefix in safe_pipeline_prefixes:
            if re.match(safe_prefix, segment, re.IGNORECASE):
                is_safe = True
                break
        if not is_safe:
            if re.match(r'\b[A-Za-z]', segment):
                pass
            elif re.match(r'\s*\(', segment):
                pass
            elif re.match(r'\s*\[', segment):
                pass
            elif re.match(r'\s*\$', segment):
                pass
            else:
                pass

    return False, "", ""


def _check_danger(command: str):
    config = _get_safety_config_cached()
    # 先去除字符串字面量，降低误报
    stripped = _strip_string_literals(command)

    builtin_rules = [
        # 别名与简写
        (r'(?<![A-Za-z])rm\s+', '删除文件/目录 (rm 别名)', 'challenge'),
        (r'(?<![A-Za-z])del\s+', '删除文件/目录 (del 别名)', 'challenge'),
        (r'(?<![A-Za-z])ri\s+', '删除文件/目录 (ri 别名)', 'challenge'),
        (r'(?<![A-Za-z])rd\s+', '删除目录 (rd 别名)', 'challenge'),
        (r'\b(?:erase|format|diskpart)\b', '文件系统破坏操作', 'challenge'),
        # 间接执行（& 后可跟空格或引号）
        (r'\b(?:iex|Invoke-Expression|Start-Job)\s+', '间接执行 (调用操作符)', 'challenge'),
        (r'(?<![A-Za-z])&\s*(?:["\']|[\w/\\:])', '& 调用运算符', 'challenge'),
        (r'\.\s*[\\/]?\s*[A-Za-z0-9_\-]+\s*\.ps1', '点源执行脚本', 'challenge'),
        (r'\b(?:Invoke-Command|Start-Process)\s+', '间接执行 (Invoke-Command/Start-Process)', 'challenge'),
        (r'\[scriptblock\]::Create\s*\(', 'ScriptBlock动态创建', 'challenge'),
        (r'\.Invoke\s*\(', '.Invoke() 调用', 'challenge'),
        # 变量赋值后使用（如 $p = ...; Remove-Item $p）
        (r'\$[A-Za-z_][A-Za-z0-9_]*\s*=\s*[^;]+;\s*(?:rm|del|ri|rd|Remove-Item)', '变量赋值后删除', 'challenge'),
    ]
    config_rules = list(config.danger_patterns) if config.danger_patterns else []
    all_rules = builtin_rules + config_rules

    for cmd in (command, _decode_powershell_script(command)):
        stripped_cmd = _strip_string_literals(cmd)
        
        danger, desc, level = _check_dangerous_pipeline(stripped_cmd)
        if danger:
            return True, desc, level

        for i, (pattern, desc, level) in enumerate(all_rules):
            try:
                if re.search(pattern, stripped_cmd, re.IGNORECASE):
                    return True, desc, level
            except re.error as e:
                _log("ERROR", f"Regex error in danger rule[{i}] '{pattern}': {e}")
    return False, "", ""

@register_tool
def is_dangerous(command):
    danger, _, _ = _check_danger(command)
    return danger


# =============================================================================
# 安全执行核心函数
# =============================================================================
@register_tool(timeout=120)
def run_powershell(script, working_dir=None, timeout=COMMAND_TIMEOUT, encoding="gbk", **kwargs):
    """
    安全执行 PowerShell 命令。

    _skip_validation=True: 跳过 L0/L1/L2 校验，由上层（execute_command）统一完成权限验证。
    此参数解除 system_tools.py 与 safe_exec.py 的双重拦截问题。
    内部确认标志（challenge_response/challenge_token/domain_confirmed/_skip_validation）
    由上层经 kwargs 透传，不出现在 LLM 可见的 JSON Schema 中。
    """
    # 内部标志：由 execute_command 透传（kwargs 吸收，schema 不暴露）
    challenge_response = kwargs.get("challenge_response")
    challenge_token = kwargs.get("challenge_token")
    domain_confirmed = bool(kwargs.get("domain_confirmed"))
    _skip_validation = bool(kwargs.get("_skip_validation"))
    _evict_stale_challenges()

    script = _normalize_command(script)
    challenge_phrase = _get_safety_config_cached().danger_challenge_phrase
    challenge_verified = False

    # ---- 挑战令牌透传（上层已验证场景） ----
    if challenge_response is not None and challenge_token:
        pending = _get_confirmation_manager().verify_challenge(
            challenge_token, challenge_response.strip(), challenge_phrase
        )
        if not pending:
            _log("WARN", f"无效挑战令牌或短语不匹配: {challenge_token[:8]}...")
            return {"success": False, "message": "挑战令牌无效或已过期，请重新发起操作"}
        if pending.get("source") != "powershell":
            _log("WARN", f"挑战令牌来源不匹配: {pending.get('source', 'unknown')}")
            return {"success": False, "message": "挑战令牌来源不匹配，请重新发起操作"}
        if "command" not in pending:
            _log("WARN", f"挑战令牌缺少 command 字段")
            return {"success": False, "message": "挑战令牌格式无效，请重新发起操作"}
        script = pending["command"]
        challenge_verified = True
        _log("AUTH", f"用户授权执行: {pending['desc']} | cmd={script[:120]}")

    # ---- 解码命令用于检测 ----
    decoded_script = _decode_powershell_script(script)

    # ---- 永久拦截（始终执行，不受 _skip_validation 影响） ----
    config = _get_safety_config_cached()
    for i, pattern in enumerate(config.always_blocked):
        try:
            if re.search(pattern, decoded_script, re.IGNORECASE):
                _log("BLOCK", f"永久拦截 (匹配: {pattern}): {script[:120]}")
                return {"success": False, "message": "安全策略：此命令被永久禁止执行。如需操作请手动完成。"}
        except re.error:
            continue

    # ---- L0/L2 校验：_skip_validation=True 时跳过，由上层统一管控 ----
    if not _skip_validation and not challenge_verified:
        block_hit, challenge_hit = _check_protected_paths(script, cwd=working_dir)
        if block_hit:
            _log("BLOCK", f"路径保护拦截 ({block_hit}): {script[:120]}")
            return {"success": False, "message": f"安全策略：禁止修改 {block_hit}。此路径受硬保护。", "stop": True}
        if challenge_hit:
            _log("CHALLENGE_PATH", f"挑战路径 ({challenge_hit}): {script[:120]}")
            token = _get_confirmation_manager().create_challenge(
                command=script,
                desc=challenge_hit,
                source="powershell",
                level="challenge",
            )
            return {
                "challenge_required": True,
                "challenge_token": token,
                "danger_type": challenge_hit,
                "challenge_phrase": challenge_phrase,
                "message": (
                    f"检测到目标路径涉及 {challenge_hit}。\n\n"
                    f"请完整输入以下授权文字以继续:\n"
                    f"「{challenge_phrase}」"
                ),
            }

        is_danger, danger_desc, danger_level = _check_danger(script)
        if is_danger:
            token = _get_confirmation_manager().create_challenge(
                command=script,
                desc=danger_desc,
                source="powershell",
                level="challenge",
            )
            _log("CHALLENGE", f"发起挑战: {danger_desc} | token={token[:8]}...")
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

    # ---- 执行 ----
    try:
        tokens = shlex.split(script.strip(), posix=False)
        first_token = tokens[0] if tokens else ""
    except Exception:
        first_token = script.strip().split()[0] if script.strip() else ""
    first_prog = os.path.basename(first_token).lower().replace('"', '').replace("'", '')

    cmd_exclusive_programs = {
        "wmic", "reg", "msiexec", "sc", "net", "taskkill", "tasklist",
        "ipconfig", "ping", "tracert", "nslookup", "systeminfo", "where",
        "findstr", "fc", "comp", "certutil", "shutdown", "format", "diskpart",
        "assoc", "ftype", "chkdsk", "defrag", "sfc", "chcp", "mode", "title",
        "color", "prompt", "cls", "ver", "vol", "label", "date", "time",
        "path", "set", "copy", "move", "del", "rd", "md", "ren", "type",
        "more", "find", "sort", "tree", "attrib", "xcopy", "robocopy",
        "for", "if", "goto", "call", "start", "exit", "rem"
    }

    if first_prog in cmd_exclusive_programs:
        # 对 cmd /c 分支额外检测拼接（防止绕过）
        if not challenge_verified:
            # 再次检测拼接符号（已增强）
            danger, _, _ = _check_danger(script)
            if danger:
                token = _get_confirmation_manager().create_challenge(
                    command=script,
                    desc="CMD 拼接或危险操作",
                    source="powershell",
                    level="challenge",
                )
                _log("CHALLENGE", f"cmd /c 分支触发挑战: {script[:120]}")
                return {
                    "challenge_required": True,
                    "challenge_token": token,
                    "danger_type": "CMD 分支危险操作",
                    "challenge_phrase": challenge_phrase,
                    "message": (
                        f"检测到 CMD 分支可能的危险操作。\n\n"
                        f"请完整输入授权文字以继续:\n"
                        f"「{challenge_phrase}」"
                    ),
                }
        cmd_list = ["cmd", "/c", script]
    else:
        cmd_list = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]

    is_gui = False
    script_lower = script.lower()
    
    console_exe_programs = {
        "cmd.exe", "powershell.exe", "pwsh.exe", "python.exe", "pythonw.exe",
        "node.exe", "npm.cmd", "npx.cmd", "git.exe", "bash.exe", "wsl.exe",
        "wmic.exe", "reg.exe", "msiexec.exe", "sc.exe", "net.exe", "taskkill.exe",
        "tasklist.exe", "ipconfig.exe", "ping.exe", "tracert.exe", "nslookup.exe",
        "systeminfo.exe", "where.exe", "findstr.exe", "fc.exe", "comp.exe",
        "certutil.exe", "shutdown.exe", "format.com", "diskpart.exe", "chkdsk.exe",
        "defrag.exe", "sfc.exe", "chcp.com", "mode.com", "title.exe", "color.exe",
        "prompt.exe", "cls.exe", "ver.exe", "vol.exe", "label.exe", "date.exe",
        "time.exe", "path.exe", "set.exe", "copy.exe", "move.exe", "del.exe",
        "rd.exe", "md.exe", "ren.exe", "type.exe", "more.com", "find.exe",
        "sort.exe", "tree.com", "attrib.exe", "xcopy.exe", "robocopy.exe",
        "for.exe", "if.exe", "goto.exe", "call.exe", "start.exe", "exit.exe",
        "rem.exe", "cscript.exe", "wscript.exe", "mshta.exe", "schtasks.exe",
        "at.exe", "arp.exe", "netsh.exe", "route.exe", "print.exe", "subst.exe",
        "assoc.exe", "ftype.exe", "expand.exe", "extract.exe", "makecab.exe",
        "logman.exe", "wevtutil.exe", "eventcreate.exe", "query.exe",
        "bitsadmin.exe", "regsvr32.exe", "regedit.exe", "gpupdate.exe",
        "gpresult.exe", "whoami.exe", "quser.exe", "query.exe", "runas.exe",
        "cmdkey.exe", "klist.exe", "nbtstat.exe", "netstat.exe", "tasklist.exe",
        "icacls.exe", "cacls.exe", "takeown.exe", "shutdown.exe", "hiberfil.exe",
        "powercfg.exe", "bdehdcfg.exe", "manage-bde.exe", "sfc.exe",
        "dism.exe", "pkgmgr.exe", "ocsetup.exe", "dcomcnfg.exe", "compmgmt.msc",
        "gpedit.msc", "secpol.msc", "services.msc", "eventvwr.msc", "mmc.exe",
    }
    
    gui_indicators = [
        "Start-Process", "start-process",
        "Show-Command", "show-command",
        "Out-GridView", "out-gridview",
        "notepad.exe", "notepad++.exe",
        "word.exe", "excel.exe", "powerpoint.exe",
        "chrome.exe", "firefox.exe", "edge.exe", "iexplore.exe",
        "explorer.exe", "calc.exe", "mspaint.exe",
        "winword.exe", "excel.exe", "powerpnt.exe",
        "outlook.exe", "access.exe", "publisher.exe",
        "visio.exe", "project.exe", "onenote.exe",
        "acrobat.exe", "reader.exe",
        "vlc.exe", "wmplayer.exe",
        "steam.exe", "epicgameslauncher.exe",
        "teams.exe", "skype.exe", "zoom.exe",
    ]
    
    if "Start-Process" in script or "start-process" in script_lower:
        is_gui = True
    else:
        for indicator in gui_indicators:
            if indicator.lower() in script_lower:
                is_gui = True
                break
        
        if not is_gui and first_prog.endswith(".exe"):
            if first_prog.lower() not in console_exe_programs:
                is_gui = True
        # 静默/无窗口参数的程序走非 GUI 路径（等待执行完成）
        if "/verysilent" in script_lower or "/silent" in script_lower or "/S" in script_lower or "/quiet" in script_lower or "/qn" in script_lower:
            is_gui = False

    if is_gui and not ("Start-Process" in script or "start-process" in script.lower()):
        if first_prog.lower() in console_exe_programs:
            pass
        else:
            try:
                all_args = shlex.split(script.strip(), posix=False)
            except Exception:
                all_args = script.strip().split()
            if not all_args:
                return {"success": False, "message": "无法解析命令"}
            exe_path = all_args[0]
            rest_args = all_args[1:] if len(all_args) > 1 else []
            try:
                subprocess.Popen(
                    [exe_path] + rest_args,
                    shell=False,
                    cwd=working_dir,
                    creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0,
                )
                return {"success": True, "output": "GUI程序已启动，请手动完成后续操作", "is_gui_operation": True}
            except FileNotFoundError:
                _log("ERROR", f"文件不存在: {exe_path}")
                return {"success": False, "message": f"可执行文件不存在: {exe_path}", "output": f"可执行文件不存在: {exe_path}"}
            except Exception as e:
                _log("ERROR", str(e)[:200])
                return {"success": False, "message": f"启动GUI程序失败: {str(e)}", "output": f"启动GUI程序失败: {str(e)}"}

    # 非 GUI 常规执行
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
        if result.returncode != 0:
            error = result.stderr.strip() or "未知错误"
            return {"success": False, "message": f"命令执行失败 (code {result.returncode}): {error}", "output": f"命令执行失败 (code {result.returncode}): {error}"}
        return {"success": True, "output": result.stdout.strip()}
    except subprocess.TimeoutExpired:
        return {"success": False, "message": f"命令执行超时 ({timeout}s)", "output": f"命令执行超时 ({timeout}s)"}
    except Exception as e:
        _log("ERROR", str(e)[:200])
        return {"success": False, "message": str(e), "output": str(e)}


@register_tool(timeout=120)
def run_cmd(command_list, working_dir=None, timeout=COMMAND_TIMEOUT, encoding="gbk", **kwargs):
    """
    安全执行 CMD 命令（list 形式，shell=False）。
    直接使用参数列表进行检测，避免拼接丢失空格。
    内部确认标志（challenge_response/challenge_token）由上层经 kwargs 透传，
    不出现在 LLM 可见的 JSON Schema 中。
    """
    # 内部标志：由 execute_command 透传（kwargs 吸收，schema 不暴露）
    challenge_response = kwargs.get("challenge_response")
    challenge_token = kwargs.get("challenge_token")
    _evict_stale_challenges()

    challenge_phrase = _get_safety_config_cached().danger_challenge_phrase
    challenge_verified = False

    # ---- 挑战验证 ----
    if challenge_response is not None and challenge_token:
        pending = _get_confirmation_manager().pop(challenge_token)
        if not pending:
            _log("WARN", f"无效挑战令牌: {challenge_token[:8]}...")
            return {"success": False, "message": "挑战令牌无效或已过期"}
        if pending.get("source") != "cmd":
            _log("WARN", f"挑战令牌来源不匹配: {pending.get('source', 'unknown')}")
            return {"success": False, "message": "挑战令牌来源不匹配，请重新发起操作"}
        if "cmd_list" not in pending:
            _log("WARN", f"挑战令牌缺少 cmd_list 字段")
            return {"success": False, "message": "挑战令牌格式无效，请重新发起操作"}
        if not challenge_response or not challenge_response.strip():
            return {"success": False, "message": "授权文本为空，操作已取消"}
        if challenge_response.strip() != challenge_phrase:
            return {"success": False, "message": "授权文本不匹配，操作已取消"}
        command_list = pending["cmd_list"]
        challenge_verified = True
        _log("AUTH", f"用户授权执行: {pending['desc']}")

    # 构建命令字符串用于部分检测
    cmd_str = " ".join(command_list)
    decoded_cmd = _decode_powershell_script(cmd_str)

    # ---- 永久拦截 ----
    config = _get_safety_config_cached()
    for i, pattern in enumerate(config.always_blocked):
        try:
            if re.search(pattern, decoded_cmd, re.IGNORECASE):
                _log("BLOCK", f"永久拦截: {cmd_str[:120]}")
                return {"success": False, "message": "安全策略：此命令被永久禁止执行"}
        except re.error:
            continue

    # ---- L0 路径硬拦截 + L2 挑战路径（传递参数列表） ----
    if not challenge_verified:
        block_hit, challenge_hit = _check_protected_paths(cmd_str, cwd=working_dir, args_list=command_list[1:])
        if block_hit:
            _log("BLOCK", f"路径保护拦截 ({block_hit}): {cmd_str[:120]}")
            return {"success": False, "message": f"安全策略：禁止修改 {block_hit}。此路径受硬保护。", "stop": True}
        if challenge_hit:
            _log("CHALLENGE_PATH", f"挑战路径 ({challenge_hit}): {cmd_str[:120]}")
            token = _get_confirmation_manager().create_challenge(
                command=cmd_str,
                desc=challenge_hit,
                source="cmd",
                level="challenge",
                cmd_list=command_list,
            )
            return {
                "challenge_required": True,
                "challenge_token": token,
                "danger_type": challenge_hit,
                "challenge_phrase": challenge_phrase,
                "message": (
                    f"检测到目标路径涉及 {challenge_hit}。\n\n"
                    f"请完整输入以下授权文字以继续:\n"
                    f"「{challenge_phrase}」"
                ),
            }

    # ---- L2 危险检测 ----
    if not challenge_verified:
        is_danger, danger_desc, danger_level = _check_danger(cmd_str)
    else:
        is_danger = False

    if is_danger:
        token = _get_confirmation_manager().create_challenge(
            command=cmd_str,
            desc=danger_desc,
            source="cmd",
            level="challenge",
            cmd_list=command_list,
        )
        _log("CHALLENGE", f"发起挑战: {danger_desc}")
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

    # ---- 执行 ----
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
        if result.returncode != 0:
            error = result.stderr.strip() or "未知错误"
            return {
                "success": False,
                "message": f"命令执行失败 (code {result.returncode}): {error}",
                "output": f"命令执行失败 (code {result.returncode}): {error}"   # 新增
            }
        return {"success": True, "output": result.stdout.strip()}
    except subprocess.TimeoutExpired:
        return {"success": False, "message": f"命令执行超时 ({timeout}s)", "output": f"命令执行超时 ({timeout}s)"}
    except Exception as e:
        return {"success": False, "message": str(e), "output": str(e)}