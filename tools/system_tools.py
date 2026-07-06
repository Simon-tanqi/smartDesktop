"""系统工具：命令执行、Python脚本运行、注册表操作"""
import os
import json
import secrets
import time
import subprocess
import re
from datetime import datetime

from . import register_tool, path_params
from . import safe_exec
from . import post_action_verify
from config.config_loader import get_safety_config
from config.settings import MAX_OUTPUT_CHARS, PYTHON_TIMEOUT

_pending_confirmations: dict = {}


def _get_cfg():
    return get_safety_config()


def _get_confirm_log_path() -> str:
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_dir = os.path.join(project_dir, "logs", "confirm")
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


@register_tool(timeout=120)
def execute_command(command: str, working_dir: str = None, confirmed: bool = False,
                    challenge_response: str = None, challenge_token: str = None,
                    confirmation_token: str = None, domain_confirmed: bool = False, **kwargs) -> str:
    """在终端执行一条命令。

    command: 完整命令字符串。
    working_dir: 可选工作目录。
    confirmed: 收到 confirmation_required 后，用户回复"确认"时设为 true 重新调用。
    challenge_response: 危险操作授权文本。
    challenge_token: 挑战令牌。
    """
    stripped = command.lstrip()
    if not stripped:
        return json.dumps({"success": False, "message": "空命令"})

    _confirmed_note = ""

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

    if prog and prog in _get_cfg().readonly_patterns:
        pass
    elif first_token.lower() in _get_cfg().readonly_patterns:
        pass
    elif any(kw in command for kw in _get_cfg().low_risk_registry_patterns) and "hkcu" in command.lower():
        pass
    elif challenge_response or challenge_token:
        pass
    elif domain_confirmed:
        pass
    elif confirmation_token and confirmation_token in _pending_confirmations:
        stored_cmd, expiry = _pending_confirmations[confirmation_token]
        if time.time() < expiry and stored_cmd == command:
            _confirmed_note = f"[已确认] {confirmation_token[:8]}"
            del _pending_confirmations[confirmation_token]
        else:
            return json.dumps({"success": False, "message": "确认令牌已过期或不匹配"})
    elif confirmed:
        if any(p[0] in command.lower() for p in _get_cfg().danger_patterns):
            return json.dumps({"success": False, "message": "高危命令需要完整挑战短语确认"})
        _confirmed_note = "[用户已确认]"
    elif any(pattern in command.lower() for pattern in _get_cfg().always_blocked):
        return json.dumps({"success": False, "message": f"命令被阻止：包含危险模式"})
    elif any(p[0] in command.lower() for p in _get_cfg().danger_patterns):
        return json.dumps({
            "success": False,
            "message": "高危操作需要确认",
            "confirmation_required": True,
            "challenge_phrase": "我确认执行此危险操作"
        })
    elif any(pattern in command.lower() for pattern in _get_cfg().delete_command_patterns):
        token = secrets.token_hex(16)
        _pending_confirmations[token] = (command, time.time() + 300)
        return json.dumps({
            "success": False,
            "message": "中等风险操作，请确认",
            "confirmation_required": True,
            "confirmation_token": token
        })

    if working_dir:
        os.makedirs(working_dir, exist_ok=True)
        old_cwd = os.getcwd()
        os.chdir(working_dir)
    else:
        old_cwd = None

    try:
        result = safe_exec.run_powershell(command, timeout=120)
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
            output = output[:MAX_OUTPUT_CHARS] + f"\n[截断：共 {len(output)} 字符]"

        return json.dumps({
            "success": result.get("success", True),
            "output": output,
            "output_total_chars": len(result.get("output", "")),
            "truncated": len(result.get("output", "")) > MAX_OUTPUT_CHARS
        }, ensure_ascii=False)
    finally:
        if old_cwd:
            os.chdir(old_cwd)


@register_tool(timeout=30)
def get_uninstall_string(program_name: str) -> str:
    """获取软件的卸载命令路径。

    program_name: 软件名称（支持模糊匹配）

    返回：JSON 格式，包含匹配到的软件列表。
    """
    import winreg

    registry_paths = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
    ]
    results = []

    for path in registry_paths:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    subkey_name = winreg.EnumKey(key, i)
                    with winreg.OpenKey(key, subkey_name) as subkey:
                        try:
                            display_name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                            if program_name.lower() in display_name.lower():
                                uninstall_string = winreg.QueryValueEx(subkey, "UninstallString")[0] if "UninstallString" in [winreg.EnumValue(subkey, j)[0] for j in range(winreg.QueryInfoKey(subkey)[1])] else ""
                                install_location = winreg.QueryValueEx(subkey, "InstallLocation")[0] if "InstallLocation" in [winreg.EnumValue(subkey, j)[0] for j in range(winreg.QueryInfoKey(subkey)[1])] else ""
                                display_version = winreg.QueryValueEx(subkey, "DisplayVersion")[0] if "DisplayVersion" in [winreg.EnumValue(subkey, j)[0] for j in range(winreg.QueryInfoKey(subkey)[1])] else ""
                                publisher = winreg.QueryValueEx(subkey, "Publisher")[0] if "Publisher" in [winreg.EnumValue(subkey, j)[0] for j in range(winreg.QueryInfoKey(subkey)[1])] else ""
                                results.append({
                                    "DisplayName": display_name,
                                    "RegistryPath": f"HKLM\\{path}\\{subkey_name}",
                                    "UninstallString": uninstall_string,
                                    "InstallLocation": install_location,
                                    "DisplayVersion": display_version,
                                    "Publisher": publisher
                                })
                        except Exception:
                            continue
        except Exception:
            continue

    if results:
        return json.dumps({
            "success": True,
            "message": f"找到 {len(results)} 个匹配的软件",
            "programs": results
        }, ensure_ascii=False, indent=2)
    else:
        return json.dumps({
            "success": False,
            "message": f"未找到名称包含 '{program_name}' 的已安装软件，请尝试其他关键词"
        }, ensure_ascii=False)


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

            with winreg.OpenKey(hkey, key_path, 0, winreg.KEY_READ) as reg_key:
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

            key_path_parent, key_path_name = os.path.split(key_path)
            with winreg.CreateKey(hkey, key_path_parent) as parent_key:
                winreg.SetValueEx(parent_key, key_path_name if key_path_name else None, 0, vt, value_data)

            return json.dumps({
                "success": True,
                "message": f"注册表项已设置"
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
                key_path_parent, key_path_name = os.path.split(key_path)
                winreg.DeleteKey(hkey, key_path)
                return json.dumps({
                    "success": True,
                    "message": f"键已删除"
                }, ensure_ascii=False)

        else:
            return json.dumps({"success": False, "message": f"不支持的操作类型: {operation}"})

    except Exception as e:
        return json.dumps({
            "success": False,
            "message": f"注册表操作失败: {str(e)}"
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

    _com_initialized = False
    try:
        CoInitialize(None)
        _com_initialized = True
    except:
        pass

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
        if _com_initialized:
            try:
                CoUninitialize()
            except:
                pass