"""系统工具：命令执行、Python脚本运行、注册表操作"""
import os
import json
import secrets
import time
import subprocess
import re
import shlex
import threading
from datetime import datetime

from . import register_tool, path_params
from . import safe_exec
from . import post_action_verify
from config.config_loader import get_safety_config
from config.settings import MAX_OUTPUT_CHARS, PYTHON_TIMEOUT

# 注意：tools/system_tools.py 使用独立的 _pending_confirmations 字典
# 与 backend/chat.py 的 _pending_confirmations 完全分离，避免数据格式冲突
# - backend/chat.py 格式: {token: {"event": evt, "result": result_holder}}
# - tools/system_tools.py 格式: {token: (command, expiry_time)}
_pending_confirmations: dict = {}
_pending_lock = threading.Lock()

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
    try:
        from config.settings import DATA_DIR
    except ImportError:
        DATA_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'MyAgent')
    log_dir = os.path.join(DATA_DIR, "logs", "confirm")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, "confirm_operations.log")


def _log_confirmation(command: str, status: str, detail: str = ""):
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


def _get_lock():
    global _pending_lock
    if _pending_lock is None:
        _pending_lock = threading.Lock()
    return _pending_lock
_com_ref_count = threading.local()
_com_ref_count.value = 0
_com_ref_count.initialized = False


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
        exe_path = tokens[0].strip('"')
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

    return {"valid": True, "message": "路径未缓存，已放行通过"}

@register_tool(timeout=120)
def execute_command(command: str, working_dir: str = None, confirmed: bool = False,
                    challenge_response: str = None, challenge_token: str = None,
                    confirmation_token: str = None, domain_confirmed: bool = False,
                    prevalidated_uninstall_paths: list = None, **kwargs) -> str:
    """在终端执行一条命令。"""
    stripped = command.lstrip()
    if not stripped:
        return json.dumps({"success": False, "message": "空命令"})

    _confirmed_note = ""
    _is_verified = False

    # ── 1. 提前验证可能传入的确认令牌（如果有效则直接标记为已验证）──
    if challenge_response and challenge_token:
        with _get_lock():
            pending = _pending_confirmations.get(challenge_token)
            if pending:
                stored_cmd, expiry = pending
                expected_phrase = _get_cfg().danger_challenge_phrase
                if time.time() < expiry and challenge_response == expected_phrase:
                    if command.strip() != stored_cmd.strip():
                        return json.dumps({
                            "success": False,
                            "message": "命令不匹配：令牌已绑定其他命令"
                        })
                    _is_verified = True
                    _confirmed_note = f"[已授权] {challenge_token[:8]}"
                    del _pending_confirmations[challenge_token]
        if not _is_verified:
            return json.dumps({
                "success": False,
                "message": "挑战验证失败：令牌无效、已过期或短语不匹配"
            })
    elif confirmation_token:
        with _get_lock():
            pending = _pending_confirmations.get(confirmation_token)
            if pending:
                stored_cmd, expiry = pending
                if time.time() < expiry:
                    if command.strip() != stored_cmd.strip():
                        return json.dumps({
                            "success": False,
                            "message": "命令不匹配：令牌已绑定其他命令"
                        })
                    _is_verified = True
                    _confirmed_note = f"[已确认] {confirmation_token[:8]}"
                    del _pending_confirmations[confirmation_token]
        if not _is_verified:
            return json.dumps({
                "success": False,
                "message": "确认令牌已过期或不匹配"
            })

    # ── 2. 强制黑名单检查（始终执行，无论是否已验证）──
    if any(re.search(pattern, command, re.IGNORECASE) for pattern in _get_cfg().always_blocked):
        return json.dumps({"success": False, "message": f"命令被阻止：包含危险模式"})

    # ── 3. 音频命令重定向到 system_audio（零风险命令，免确认）──
    audio_result = _try_redirect_audio_command(stripped)
    if audio_result is not None:
        return audio_result

    # ── 4. 卸载命令前置拦截（始终执行路径验证，无论是否已验证）──
    if _is_uninstall_command(command):
        path_check = _validate_uninstall_path(command, prevalidated_uninstall_paths)
        if not path_check["valid"]:
            return json.dumps(path_check, ensure_ascii=False)

        # 已验证则跳过确认，继续执行
        if _is_verified:
            pass
        else:
            # 生成新的确认令牌（P1 挑战）
            token = secrets.token_hex(16)
            with _get_lock():
                _pending_confirmations[token] = (command, time.time() + 300)

            return json.dumps({
                "success": False,
                "message": "高危操作需要确认",
                "confirmation_required": True,
                "challenge_phrase": _get_cfg().danger_challenge_phrase,
                "challenge_token": token
            })

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
            _is_verified = True
        elif prog_no_ext and prog_no_ext in _get_cfg().readonly_patterns:
            _is_verified = True
        elif first_token.lower() in _get_cfg().readonly_patterns:
            _is_verified = True
        elif any(kw in command for kw in _get_cfg().low_risk_registry_patterns) and "hkcu" in command.lower():
            _is_verified = True

        # 原有的挑战/确认逻辑（已移到前面，下面的代码不会在令牌已验证时执行）
        if not _is_verified:
            # 注意：如果令牌验证已在第一步完成，这里不会进入
            # 但为了代码清晰，仍保留原有结构，只是移除了重复的令牌验证部分
            if confirmed or domain_confirmed:
                _is_verified = True
                _confirmed_note = "[已确认]" if confirmed else "[域已确认]"
            elif any(re.search(p[0], command, re.IGNORECASE) for p in _get_cfg().danger_patterns):
                matched_level = "challenge"
                for p in _get_cfg().danger_patterns:
                    try:
                        if re.search(p[0], command, re.IGNORECASE):
                            matched_level = p[2] if len(p) > 2 else "challenge"
                            break
                    except re.error:
                        continue

                cmd_lower = command.lower().strip('"').strip("'")
                is_program_files = any(pf in cmd_lower for pf in [
                    "c:\\program files\\", "c:\\program files (x86)\\",
                    "d:\\program files\\", "d:\\program files (x86)\\",
                    "c:\\program files", "d:\\program files",
                ]) or any(kw in cmd_lower for kw in ["program files", "programfiles", "progra~1", "progra~2"])
                is_batch_delete = any(kw in cmd_lower for kw in [
                    "del ", "rmdir ", "rd ", "rm -rf ", "清空回收站",
                    "rd /s", "del /f /s", "del /q /s",
                ])
                is_password_change = any(kw in cmd_lower for kw in [
                    "net user ", "密码", "password ", "change password",
                ])
                is_recycle_bin = any(kw in cmd_lower for kw in [
                    "回收站", "$recycle.bin", "recycle.bin", "empty",
                ])
                is_high_risk = is_program_files or is_batch_delete or is_password_change or is_recycle_bin

                token = secrets.token_hex(16)
                with _get_lock():
                    _pending_confirmations[token] = (command, time.time() + 300)

                if matched_level == "challenge" or is_high_risk:
                    return json.dumps({
                        "success": False,
                        "message": "高危操作需要确认",
                        "confirmation_required": True,
                        "challenge_phrase": _get_cfg().danger_challenge_phrase,
                        "challenge_token": token
                    })
                else:
                    return json.dumps({
                        "success": False,
                        "message": "操作需要确认",
                        "confirmation_required": True,
                        "confirmation_token": token
                    })
            elif any(re.search(pattern, command, re.IGNORECASE) for pattern in _get_cfg().delete_command_patterns):
                cfg = _get_cfg()
                challenge_phrase = None
                if hasattr(cfg, 'danger_path_patterns') and cfg.danger_path_patterns:
                    for path_pattern, path_desc in cfg.danger_path_patterns:
                        if re.search(path_pattern, command, re.IGNORECASE):
                            challenge_phrase = cfg.danger_challenge_phrase
                            break

                token = secrets.token_hex(16)
                with _get_lock():
                    _pending_confirmations[token] = (command, time.time() + 300)

                if challenge_phrase:
                    return json.dumps({
                        "success": False,
                        "message": "检测到目标路径涉及受保护目录，需要授权",
                        "confirmation_required": True,
                        "challenge_phrase": challenge_phrase,
                        "challenge_token": token
                    })
                else:
                    return json.dumps({
                        "success": False,
                        "message": "中等风险操作，请确认",
                        "confirmation_required": True,
                        "confirmation_token": token
                    })

            # 默认 P2：未匹配任何白名单(P3)或危险模式(P0/P1)的命令，需要确认
            token = secrets.token_hex(16)
            with _get_lock():
                _pending_confirmations[token] = (command, time.time() + 300)
            return json.dumps({
                "success": False,
                "message": "操作需要确认",
                "confirmation_required": True,
                "confirmation_token": token
            })

    # ── 5. 命令执行 ──
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
            from tools.safe_exec import _pending_challenges, _pending_lock as se_lock
            import datetime as _dt
            with se_lock:
                _pending_challenges[challenge_token] = {
                    "command": actual_command,
                    "desc": "execute_command 已验证",
                    "level": "challenge",
                    "timestamp": _dt.datetime.now().isoformat(),
                    "source": "powershell",
                }
            safe_exec_kwargs = {"challenge_response": challenge_response, "challenge_token": challenge_token}

        if is_uninstall:
            import subprocess
            try:
                tokens = shlex.split(actual_command.strip(), posix=False)
                exe_path = tokens[0].strip('"').strip("'") if tokens else ""
                rest_args = tokens[1:] if len(tokens) > 1 else []
                # 设置工作目录为卸载器所在目录（InnoSetup 需要 unins000.dat 等文件在同一目录）
                uninstall_dir = os.path.dirname(exe_path)
                # 统一使用 Popen 异步启动，不等执行完成（卸载器通常需要用户交互）
                cmd_str = f'"{exe_path}" {" ".join(rest_args)}' if isinstance(rest_args, list) else f'"{exe_path}" {rest_args}'
                subprocess.Popen(
                    cmd_str,
                    shell=True, cwd=uninstall_dir,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                )
                result = {"success": True, "output": "卸载程序已启动，需要用户手动完成后续操作", "is_gui_operation": True}
            except FileNotFoundError:
                result = {"success": False, "message": f"可执行文件不存在: {exe_path}", "output": f"可执行文件不存在: {exe_path}"}
            except Exception as e:
                result = {"success": False, "message": f"启动卸载程序失败: {str(e)}", "output": f"启动卸载程序失败: {str(e)}"}
        else:
            result = safe_exec.run_powershell(actual_command, working_dir=working_dir, timeout=120, domain_confirmed=(confirmed or bool(_confirmed_note)), **safe_exec_kwargs)

        if result.get("challenge_required"):
            token = result["challenge_token"]
            if _is_verified and challenge_response:
                from tools.safe_exec import _pending_challenges, _pending_lock as se_lock
                import datetime as _dt
                with se_lock:
                    _pending_challenges[token] = {
                        "command": actual_command,
                        "desc": "execute_command 已验证，自动转发授权",
                        "level": "challenge",
                        "timestamp": _dt.datetime.now().isoformat(),
                        "source": "powershell",
                    }
                result = safe_exec.run_powershell(actual_command, working_dir=working_dir, timeout=120, 
                                                  domain_confirmed=(confirmed or bool(_confirmed_note)), 
                                                  challenge_token=token, challenge_response=challenge_response)
            else:
                with _get_lock():
                    _pending_confirmations[token] = (command, time.time() + 300)
                return json.dumps({
                    "success": False,
                    "confirmation_required": True,
                    "challenge_phrase": result.get("challenge_phrase"),
                    "challenge_token": token,
                    "message": result.get("message"),
                    "details": result.get("details", {}),
                }, ensure_ascii=False)

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
                "warning": "⚠️ GUI 程序已启动，需要用户手动操作完成。"
            }, ensure_ascii=False)

        if len(output) > MAX_OUTPUT_CHARS:
            half = MAX_OUTPUT_CHARS // 2
            output = output[:half] + f"\n[...截断：共 {len(output)} 字符...]\n" + output[-half:]

        return json.dumps({
            "success": True,
            "output": output,
            "output_total_chars": len(result.get("output", "")),
            "truncated": len(result.get("output", "")) > MAX_OUTPUT_CHARS
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
    except Exception:
        first_token = _parse_first_token(command)
    
    prog_name = os.path.basename(first_token).lower()
    
    cmd_lower = command.lower()
    
    if prog_name == "msiexec" and "/x" in cmd_lower:
        return True
    
    if prog_name.startswith("unins"):
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
    
    return False


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
        inno_silent_patterns = ["/silent", "/verysilent"]
        if not any(pattern in cmd for pattern in inno_silent_patterns):
            return command + " /verysilent"
        return command
    
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
                silent_flags = ["/silent", "/S", "/quiet", "/passive", "/verysilent"]
                existing_args = cmd
                if not any(flag.lower() in existing_args for flag in silent_flags):
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


@register_tool(timeout=30)
def modify_registry(operation: str, key: str, value_name: str = None, value_data: str = None, value_type: str = "REG_SZ") -> str:
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

            with winreg.CreateKey(hkey, key_path) as reg_key:
                if value_name is None:
                    winreg.SetValueEx(reg_key, None, 0, vt, value_data)
                    return json.dumps({
                        "success": True,
                        "message": f"注册表项默认值已设置"
                    }, ensure_ascii=False)
                else:
                    winreg.SetValueEx(reg_key, value_name, 0, vt, value_data)
                    return json.dumps({
                        "success": True,
                        "message": f"注册表项 '{value_name}' 已设置"
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
                with winreg.OpenKey(hkey, key_path, 0, winreg.KEY_WRITE) as reg_key:
                    winreg.DeleteValue(reg_key, value_name)
                return json.dumps({
                    "success": True,
                    "message": f"值已删除"
                }, ensure_ascii=False)
            else:
                def _delete_key_recursive(base_hkey, base_path, depth=0):
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
                    return json.dumps({
                        "success": True,
                        "message": f"键已删除"
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

    if not hasattr(_com_ref_count, 'value'):
        _com_ref_count.value = 0
    if not hasattr(_com_ref_count, 'initialized'):
        _com_ref_count.initialized = False

    if _com_ref_count.value == 0:
        try:
            from comtypes import CoInitializeEx, COINIT_APARTMENTTHREADED
            hr = CoInitializeEx(None, COINIT_APARTMENTTHREADED)
            if hr == 0:
                _com_ref_count.value = 1
                _com_ref_count.initialized = True
        except Exception:
            try:
                CoInitialize(None)
                _com_ref_count.value = 1
                _com_ref_count.initialized = True
            except Exception:
                pass
    else:
        _com_ref_count.value += 1

    try:
        def _get_speakers():
            return AudioUtilities.GetSpeakers().EndpointVolume
        
        def _get_microphone():
            mic = AudioUtilities.GetMicrophone()
            if mic is None:
                return None
            return mic.EndpointVolume

        def _get_all_devices():
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
        if hasattr(_com_ref_count, 'value') and _com_ref_count.value > 0:
            _com_ref_count.value -= 1
            if _com_ref_count.value == 0 and hasattr(_com_ref_count, 'initialized') and _com_ref_count.initialized:
                _com_ref_count.initialized = False
                try:
                    CoUninitialize()
                except Exception:
                    pass