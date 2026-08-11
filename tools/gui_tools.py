"""
GUI自动化工具 - 提供完整的桌面操作能力
基于pyautogui、pygetwindow等库实现鼠标点击、键盘输入、窗口管理等
"""
import os
import time
import json
import logging
import shutil
from typing import Dict, List, Optional, Tuple, Any

from config.settings import SCREENSHOTS_DIR, DATA_DIR

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════
#  常量配置
# ═══════════════════════════════════════════

WORKSPACE_ROOT = os.path.join(DATA_DIR, "workspace")
MAX_RETURN_CHARS = 4000           # 返回JSON最大字符数
DEFAULT_ACTIVATE_DELAY = 0.3     # 窗口激活等待时间
DEFAULT_APP_WAIT = 3             # 应用启动默认等待
WORKFLOW_STEP_TIMEOUT = 30       # 工作流单步超时秒数

# ═══════════════════════════════════════════
#  依赖检测
# ═══════════════════════════════════════════

try:
    import pyautogui
    import pygetwindow as gw
    from PIL import Image
    HAS_GUI_DEPS = True
except ImportError:
    HAS_GUI_DEPS = False
    pyautogui = None
    gw = None
    Image = None

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False
    cv2 = None

try:
    import pyperclip
    HAS_PYPERCLIP = True
except ImportError:
    HAS_PYPERCLIP = False
    pyperclip = None

if HAS_GUI_DEPS:
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.1


def _check_deps(require_cv2: bool = False, require_clip: bool = False) -> tuple[bool, str]:
    """检查GUI依赖是否可用"""
    if not HAS_GUI_DEPS:
        return False, "GUI功能需要安装额外依赖: pip install pyautogui pygetwindow pillow"
    if require_cv2 and not HAS_CV2:
        return False, "图像定位功能需要opencv: pip install opencv-python"
    if require_clip and not HAS_PYPERCLIP:
        return False, "中文输入需要pyperclip: pip install pyperclip"
    return True, ""


# ═══════════════════════════════════════════
#  路径安全
# ═══════════════════════════════════════════

def _resolve_path(file_path: str) -> str | None:
    """解析路径，确保在workspace范围内。"""
    if not file_path:
        return None
    file_path = os.path.normpath(file_path)
    if not os.path.isabs(file_path):
        resolved = os.path.abspath(os.path.join(WORKSPACE_ROOT, file_path))
    else:
        resolved = file_path
    workspace_abs = os.path.abspath(WORKSPACE_ROOT)
    if not resolved.startswith(workspace_abs + os.sep) and resolved != workspace_abs:
        logger.warning("路径穿越检测：%s 不在workspace内", resolved)
        return None
    return resolved


# ═══════════════════════════════════════════
#  环境状态快照（新增：为LLM提供文本化环境感知）
# ═══════════════════════════════════════════

  
def _get_environment_snapshot(wait: float = 0.3) -> dict:
    """获取当前GUI环境的轻量级文本快照。"""
    snapshot = {}
    if wait > 0:
        time.sleep(wait)
    try:
        win = gw.getActiveWindow()
        if win and win.title:
            snapshot["active_window"] = {
                "title": win.title[:100],
                "rect": {"left": win.left, "top": win.top, "width": win.width, "height": win.height}
            }
        else:
            snapshot["active_window"] = None
    except Exception:
        snapshot["active_window"] = "unknown"
    try:
        pos = pyautogui.position()
        snapshot["mouse_position"] = {"x": pos.x, "y": pos.y}
    except Exception:
        pass
    return snapshot


# ═══════════════════════════════════════════
#  装饰器
# ═══════════════════════════════════════════

def _safe_gui_operation(require_cv2: bool = False, require_clip: bool = False):
    """GUI操作的安全装饰器，统一异常处理。
    操作后自动等待0.5s确保UI响应，并记录操作日志。
    """
    def decorator(func):
        """装饰器：包装函数加入安全检查与日志"""
        def wrapper(*args, **kwargs):
            """包装后的执行体：依赖检查、异常处理与结果截断"""
            has_deps, error_msg = _check_deps(require_cv2, require_clip)
            if not has_deps:
                return {"success": False, "message": error_msg}
            try:
                result = func(*args, **kwargs)
                time.sleep(0.2)
                logger.info("GUI 操作完成 %s, 结果: %s",
                            func.__name__,
                            str(result.get("message", ""))[:120] if isinstance(result, dict) else "ok")
                return _truncate_result(result)
            except pyautogui.FailSafeException:
                return {"success": False, "message": "⚠️ 触发了pyautogui安全熔断（鼠标移到左上角），操作已终止"}
            except Exception as e:
                logger.warning("GUI操作失败 %s: %s", func.__name__, str(e)[:200])
                return {"success": False, "message": f"GUI操作失败: {str(e)}"}
        return wrapper
    return decorator


def _truncate_result(result: dict) -> dict:
    """截断返回结果，防止超长JSON撑爆上下文。"""
    if not isinstance(result, dict):
        return result
    try:
        json_str = json.dumps(result, ensure_ascii=False)
        if len(json_str) <= MAX_RETURN_CHARS:
            return result
        # 截断长字段
        for key in ["windows", "results", "step_result", "completed_steps"]:
            if key in result and isinstance(result[key], (list, str)):
                val_str = json.dumps(result[key], ensure_ascii=False)
                if len(val_str) > MAX_RETURN_CHARS // 2:
                    if isinstance(result[key], list):
                        result[key] = result[key][:3]
                        result[f"_{key}_truncated"] = True
                    else:
                        result[key] = result[key][:MAX_RETURN_CHARS // 2] + "…[截断]"
        # 仍超长时，丢弃environment快照字段
        if len(json.dumps(result, ensure_ascii=False)) > MAX_RETURN_CHARS:
            for env_key in ("environment", "final_environment", "current_environment"):
                result.pop(env_key, None)
        # 最终兜底：只保留核心字段，保证绝不超限
        if len(json.dumps(result, ensure_ascii=False)) > MAX_RETURN_CHARS:
            result = {
                "success": result.get("success", False),
                "message": str(result.get("message", ""))[:MAX_RETURN_CHARS // 2] + "…[截断]",
            }
        return result
    except Exception:
        return result


# ═══════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════

def _validate_coordinates(x: int, y: int) -> tuple[bool, str]:
    """校验坐标是否在屏幕范围内。"""
    if not HAS_GUI_DEPS:
        return True, ""
    try:
        sw, sh = pyautogui.size()
        if x < 0 or y < 0:
            return False, f"坐标({x},{y})超出屏幕范围(不能为负数)，屏幕尺寸:{sw}x{sh}"
        if x > sw or y > sh:
            return False, f"坐标({x},{y})超出屏幕范围，屏幕尺寸:{sw}x{sh}"
        return True, ""
    except Exception:
        return True, ""


def _find_windows_fuzzy(title_keyword: str) -> list:
    """模糊匹配窗口标题（大小写不敏感）。"""
    if not HAS_GUI_DEPS:
        return []
    keyword_lower = title_keyword.lower()
    all_windows = gw.getAllWindows()
    matched = []
    for win in all_windows:
        if win.title and keyword_lower in win.title.lower():
            matched.append(win)
    return matched


def _normalize_keys(keys) -> list:
    """规范化按键输入，拆分组合键字符串。"""
    if isinstance(keys, str):
        keys = [keys]
    if not isinstance(keys, list):
        return []
    normalized = []
    for k in keys:
        if "+" in k and len(k) > 1:
            parts = [p.strip().lower() for p in k.split("+")]
            normalized.extend(parts)
        else:
            normalized.append(k.strip().lower())
    return normalized


def _type_text_robust(text: str, interval: float = 0.1):
    """输入文本，中文使用剪贴板，ASCII使用pyautogui.write。"""
    has_ascii = all(ord(c) < 128 for c in text)
    if has_ascii:
        pyautogui.write(text, interval=interval)
        return
    if not HAS_PYPERCLIP:
        ascii_part = "".join(c for c in text if ord(c) < 128)
        if ascii_part:
            pyautogui.write(ascii_part, interval=interval)
        logger.warning("文本包含非ASCII字符但pyperclip未安装，中文部分已跳过")
        return
    pyperclip.copy(text)
    time.sleep(0.05)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.05)


# ═══════════════════════════════════════════
#  基础GUI操作（内部函数，不注册）
# ═══════════════════════════════════════════

@_safe_gui_operation()
def _gui_mouse_click(x: int, y: int, button: str = "left", clicks: int = 1) -> Dict[str, Any]:
    """模拟鼠标点击"""
    ok, msg = _validate_coordinates(x, y)
    if not ok:
        return {"success": False, "message": msg, "suggestion": "请先调用gui_screenshot或gui_locate_image获取正确坐标"}
    pyautogui.click(x=x, y=y, button=button, clicks=clicks)
    click_desc = f"{button}鼠标{'双击' if clicks == 2 else '单击'}"
    return {
        "success": True,
        "message": f"已在({x},{y})执行{click_desc}",
        "clicked_position": {"x": x, "y": y},
        "environment": _get_environment_snapshot()
    }


@_safe_gui_operation()
def _gui_mouse_move(x: int, y: int, duration: float = 0.3) -> Dict[str, Any]:
    """移动鼠标到指定坐标"""
    ok, msg = _validate_coordinates(x, y)
    if not ok:
        return {"success": False, "message": msg}
    pyautogui.moveTo(x, y, duration=duration)
    return {
        "success": True,
        "message": f"鼠标已移动到({x},{y})",
        "environment": _get_environment_snapshot()
    }


@_safe_gui_operation(require_clip=False)
def _gui_type_text(text: str, interval: float = 0.1) -> Dict[str, Any]:
    """输入文本"""
    _type_text_robust(text, interval)
    display_text = text[:50] + ("..." if len(text) > 50 else "")
    return {
        "success": True,
        "message": f"已输入文本: {display_text}",
        "input_length": len(text),
        "environment": _get_environment_snapshot()
    }


@_safe_gui_operation()
def _gui_press_keys(keys: List[str]) -> Dict[str, Any]:
    """按下按键或组合键"""
    normalized = _normalize_keys(keys)
    if not normalized:
        return {"success": False, "message": "无效的按键参数", "suggestion": "请传入非空列表，如['ctrl','c']或['enter']"}
    if len(normalized) == 1:
        pyautogui.press(normalized[0])
    else:
        pyautogui.hotkey(*normalized)
    return {
        "success": True,
        "message": f"已按下按键: {'+'.join(normalized)}",
        "pressed_keys": normalized,
        "environment": _get_environment_snapshot()
    }


# ═══════════════════════════════════════════
#  窗口管理
# ═══════════════════════════════════════════

@_safe_gui_operation()
def _gui_get_active_window() -> Dict[str, Any]:
    """获取当前活动窗口信息"""
    active_window = gw.getActiveWindow()
    if active_window and active_window.title:
        return {
            "success": True,
            "message": "获取活动窗口成功",
            "window_info": {
                "title": active_window.title,
                "left": active_window.left,
                "top": active_window.top,
                "width": active_window.width,
                "height": active_window.height,
                "is_maximized": active_window.isMaximized,
                "is_minimized": active_window.isMinimized
            }
        }
    return {
        "success": False,
        "message": "没有检测到活动窗口",
        "suggestion": "可能桌面处于焦点状态，请先调用gui_activate激活目标窗口"
    }


@_safe_gui_operation()
def _gui_find_window_by_title(title_keyword: str) -> Dict[str, Any]:
    """按标题关键词模糊查找窗口"""
    windows = _find_windows_fuzzy(title_keyword)
    if windows:
        window_list = [{
            "title": win.title,
            "left": win.left,
            "top": win.top,
            "width": win.width,
            "height": win.height
        } for win in windows[:10]]
        return {
            "success": True,
            "message": f"找到{len(windows)}个匹配窗口",
            "windows": window_list,
            "suggestion": "使用gui_activate传入标题关键词来激活目标窗口"
        }
    return {
        "success": False,
        "message": f"未找到标题包含'{title_keyword}'的窗口",
        "suggestion": "请尝试更短的关键词，或使用gui_open_app先启动程序"
    }


@_safe_gui_operation()
def _gui_activate_window(title_keyword: str, delay: float = DEFAULT_ACTIVATE_DELAY) -> Dict[str, Any]:
    """激活指定窗口并等待响应"""
    windows = _find_windows_fuzzy(title_keyword)
    if windows:
        window = windows[0]
        if window.isMinimized:
            window.restore()
        window.activate()
        time.sleep(delay)
        return {
            "success": True,
            "message": f"已激活窗口: {window.title}",
            "window_info": {
                "title": window.title,
                "left": window.left,
                "top": window.top,
                "width": window.width,
                "height": window.height
            },
            "environment": _get_environment_snapshot()
        }
    return {
        "success": False,
        "message": f"未找到标题包含'{title_keyword}'的窗口",
        "suggestion": "请先调用gui_find_window确认窗口标题，或gui_open_app启动程序"
    }


# ═══════════════════════════════════════════
#  屏幕操作
# ═══════════════════════════════════════════

@_safe_gui_operation()
def _gui_take_screenshot(region: Optional[Tuple[int, int, int, int]] = None,
                          save_path: Optional[str] = None) -> Dict[str, Any]:
    """截取全屏或指定区域并保存"""
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

    if save_path is None:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        save_path = os.path.join(SCREENSHOTS_DIR, f"screenshot_{timestamp}.png")
    else:
        save_path = _resolve_path(save_path)
        if not save_path:
            return {"success": False, "message": "非法保存路径，只能在workspace内操作"}
        os.makedirs(os.path.dirname(save_path), exist_ok=True)

    if region:
        screenshot = pyautogui.screenshot(region=region)
    else:
        screenshot = pyautogui.screenshot()

    screenshot.save(save_path)
    return {
        "success": True,
        "message": "截图已保存",
        "file_path": save_path,
        "img_md": f"![截图]({save_path})",
        "region": region or "全屏",
        "image_size": {"width": screenshot.width, "height": screenshot.height}
    }


@_safe_gui_operation(require_cv2=True)
def _gui_locate_on_screen(image_path: str, confidence: float = 0.8) -> Dict[str, Any]:
    """在屏幕上定位模板图像坐标"""
    resolved = _resolve_path(image_path)
    if not resolved:
        return {"success": False, "message": "非法图片路径，只能在workspace内操作"}
    if not os.path.exists(resolved):
        return {"success": False, "message": f"模板图像不存在: {resolved}", "suggestion": "请检查文件路径或使用gui_screenshot截取新模板"}

    location = pyautogui.locateOnScreen(resolved, confidence=confidence)
    if location:
        center_x = location.left + location.width // 2
        center_y = location.top + location.height // 2
        return {
            "success": True,
            "message": "找到匹配图像",
            "location": {
                "left": location.left,
                "top": location.top,
                "width": location.width,
                "height": location.height,
                "center_x": center_x,
                "center_y": center_y
            },
            "suggestion": f"可使用gui_click({center_x},{center_y})点击该位置"
        }
    return {
        "success": False,
        "message": "屏幕上未找到匹配图像",
        "suggestion": "尝试降低confidence参数，或重新截取模板图像"
    }


# ═══════════════════════════════════════════
#  高级操作
# ═══════════════════════════════════════════

_KNOWN_PROGRAMS: Dict[str, List[str]] = {
    "msedge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
    ],
    "firefox": [
        r"C:\Program Files\Mozilla Firefox\firefox.exe",
        r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
    ],
    "notepad": [r"C:\Windows\System32\notepad.exe", r"C:\Windows\notepad.exe"],
    "calc": [r"C:\Windows\System32\calc.exe", r"C:\Windows\calc.exe"],
    "explorer": [r"C:\Windows\explorer.exe"],
    "cmd": [r"C:\Windows\System32\cmd.exe"],
    "powershell": [r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"],
    "taskmgr": [r"C:\Windows\System32\Taskmgr.exe"],
    "control": [r"C:\Windows\System32\control.exe"],
    "mspaint": [r"C:\Windows\System32\mspaint.exe", r"C:\Program Files\Windows NT\Accessories\mspaint.exe"],
    "wordpad": [r"C:\Program Files\Windows NT\Accessories\wordpad.exe"],
}


def _resolve_program_path(name_or_path: str) -> Optional[str]:
    """将裸程序名解析为可执行文件绝对路径。"""
    if not name_or_path or not name_or_path.strip():
        return None
    name = name_or_path.strip()

    candidates = _KNOWN_PROGRAMS.get(name.lower())
    if candidates:
        for cand in candidates:
            if cand and os.path.isfile(cand):
                return cand

    try:
        found = shutil.which(name)
        if found:
            return found
        found = shutil.which(name + ".exe")
        if found:
            return found
    except Exception:
        pass

    base_dirs: List[str] = []
    for env_var in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
        val = os.environ.get(env_var)
        if val and os.path.isdir(val):
            base_dirs.append(val)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        base_dirs.append(os.path.join(local, "Programs"))
    name_key = name.lower()
    for base in base_dirs:
        try:
            entries = os.listdir(base)
        except (PermissionError, OSError):
            continue
        for entry in entries:
            try:
                if name_key not in entry.lower():
                    continue
                dir_path = os.path.join(base, entry)
                if not os.path.isdir(dir_path):
                    continue
                try:
                    for fname in os.listdir(dir_path):
                        fl = fname.lower()
                        if fl.startswith(name_key) and fl.endswith(".exe"):
                            full = os.path.join(dir_path, fname)
                            if os.path.isfile(full):
                                return full
                except (PermissionError, OSError):
                    continue
            except (PermissionError, OSError):
                continue
    return None


@_safe_gui_operation()
def _gui_open_application(application_path: str, wait_time: int = DEFAULT_APP_WAIT) -> Dict[str, Any]:
    """启动应用：URI 协议、程序名解析或路径"""
    _uri_schemes = (
        "ms-settings:", "ms-clock:", "ms-calculator:", "ms-paint:", "ms-screensketch:",
        "ms-sttoverlay:", "ms-gaming:", "ms-phone:", "ms-windows-store:",
        "mailto:", "https:", "http:", "ftp:", "tel:", "callto:",
    )
    if application_path.lower().startswith(_uri_schemes):
        try:
            os.startfile(application_path)
            time.sleep(wait_time)
            return {
                "success": True,
                "message": f"已通过URI协议启动: {application_path}",
                "app_path": application_path,
                "environment": _get_environment_snapshot()
            }
        except Exception as e:
            return {"success": False, "message": f"URI协议启动失败: {str(e)}"}

    if not os.path.isabs(application_path) and "/" not in application_path and "\\" not in application_path:
        resolved_prog = _resolve_program_path(application_path)
        if resolved_prog:
            try:
                os.startfile(resolved_prog)
                time.sleep(wait_time)
                return {
                    "success": True,
                    "message": f"已启动应用程序: {application_path} → {resolved_prog}",
                    "app_path": resolved_prog,
                    "environment": _get_environment_snapshot()
                }
            except Exception as e:
                return {"success": False, "message": f"启动应用程序失败: {application_path} → {resolved_prog}: {str(e)}"}

    resolved = _resolve_path(application_path)
    if resolved:
        target = resolved
    elif os.path.isabs(application_path) and os.path.exists(application_path):
        target = application_path
    else:
        return {
            "success": False,
            "message": f"非法应用路径或文件不存在: {application_path}",
            "suggestion": "请使用绝对路径、workspace相对路径或已知程序名(如notepad/chrome)"
        }

    if not os.path.exists(target):
        return {"success": False, "message": f"应用程序不存在: {target}"}

    _ALLOWED_EXTENSIONS = {".exe", ".lnk", ".bat", ".cmd", ".msi", ".com", ".ps1", ".vbs", ".wsf", ".jar", ".appref-ms"}
    _ext = os.path.splitext(target)[1].lower()
    if _ext and _ext not in _ALLOWED_EXTENSIONS:
        return {
            "success": False,
            "message": f"拒绝启动：文件扩展名'{_ext}'不在白名单中。仅允许: {', '.join(sorted(_ALLOWED_EXTENSIONS))}"
        }

    os.startfile(target)
    time.sleep(wait_time)
    return {
        "success": True,
        "message": f"已启动应用程序，等待{wait_time}秒",
        "app_path": target,
        "environment": _get_environment_snapshot()
    }


@_safe_gui_operation()
def _gui_automate_workflow(steps: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按步骤列表执行 GUI 自动化工作流"""
    results = []
    for i, step in enumerate(steps, 1):
        action = step.get("action")
        params = step.get("params", {})
        step_start = time.time()

        if action == "click":
            result = _gui_mouse_click(**params)
        elif action == "type":
            result = _gui_type_text(**params)
        elif action == "press_keys":
            result = _gui_press_keys(**params)
        elif action == "move_mouse":
            result = _gui_mouse_move(**params)
        elif action == "wait":
            seconds = min(params.get("seconds", 1), WORKFLOW_STEP_TIMEOUT)
            time.sleep(seconds)
            result = {"success": True, "message": f"等待{seconds}秒"}
        elif action == "screenshot":
            result = _gui_take_screenshot(**params)
        else:
            result = {"success": False, "message": f"未知操作: {action}", "suggestion": f"支持的action: click/type/press_keys/move_mouse/wait/screenshot"}

        elapsed = time.time() - step_start
        results.append({"step": i, "action": action, "result": result, "elapsed": round(elapsed, 2)})

        if not result.get("success"):
            return {
                "success": False,
                "message": f"工作流在第{i}步失败: {action}",
                "failed_step": i,
                "step_result": result,
                "completed_steps": results[:i],
                "current_environment": _get_environment_snapshot(),
                "suggestion": "请根据current_environment和step_result分析失败原因，修正后可调整steps重新调用"
            }

    return {
        "success": True,
        "message": f"工作流执行完成，共{len(steps)}步",
        "results": results,
        "final_environment": _get_environment_snapshot()
    }


# ═══════════════════════════════════════════
#  注册工具（对外接口 - 签名与返回格式完全不变）
# ═══════════════════════════════════════════

from tools import register_tool, path_params


@register_tool(timeout=10)
@path_params("save_path", "image_path")
def gui_click(x: int, y: int, button: str = "left", clicks: int = 1) -> str:
    """执行鼠标点击操作。x、y为屏幕坐标，button可选left/right/middle，clicks可选1单击/2双击。"""
    return json.dumps(_gui_mouse_click(x, y, button, clicks), ensure_ascii=False)


@register_tool(timeout=10)
def gui_move(x: int, y: int, duration: float = 0.3) -> str:
    """移动鼠标到指定位置。x、y为屏幕坐标，duration为移动持续时间(秒)。"""
    return json.dumps(_gui_mouse_move(x, y, duration), ensure_ascii=False)


@register_tool(timeout=15)
def gui_type(text: str, interval: float = 0.1) -> str:
    """模拟键盘输入文本。支持中文（需安装pyperclip），interval为字符间延迟(秒)。"""
    return json.dumps(_gui_type_text(text, interval), ensure_ascii=False)


@register_tool(timeout=10)
def gui_keys(keys: List[str]) -> str:
    """按下键盘按键。keys为按键列表如["ctrl","c"]或["ctrl+c"]或["enter"]。"""
    return json.dumps(_gui_press_keys(keys), ensure_ascii=False)


@register_tool(timeout=10)
def gui_active_window() -> str:
    """获取当前活动窗口信息。"""
    return json.dumps(_gui_get_active_window(), ensure_ascii=False)


@register_tool(timeout=10)
def gui_find_window(title_keyword: str) -> str:
    """根据标题关键词查找窗口，大小写不敏感模糊匹配。"""
    return json.dumps(_gui_find_window_by_title(title_keyword), ensure_ascii=False)


@register_tool(timeout=10)
def gui_activate(title_keyword: str, delay: float = 0.3) -> str:
    """激活指定窗口，大小写不敏感模糊匹配。delay为激活后等待时间(秒)。"""
    return json.dumps(_gui_activate_window(title_keyword, delay), ensure_ascii=False)


@register_tool(timeout=15)
@path_params("save_path")
def gui_screenshot(region_x: int = None, region_y: int = None,
                   region_w: int = None, region_h: int = None,
                   save_path: str = None) -> str:
    """屏幕截图。region_x/y/w/h全部传入时截取指定区域，否则全屏截图。save_path须在workspace内。"""
    region = None
    if all(v is not None for v in [region_x, region_y, region_w, region_h]):
        region = (region_x, region_y, region_w, region_h)
    return json.dumps(_gui_take_screenshot(region=region, save_path=save_path), ensure_ascii=False)


@register_tool(timeout=30)
@path_params("image_path")
def gui_locate_image(image_path: str, confidence: float = 0.8) -> str:
    """在屏幕上定位图像。image_path须在workspace内。confidence为匹配置信度(0-1)，需安装opencv-python。"""
    return json.dumps(_gui_locate_on_screen(image_path, confidence), ensure_ascii=False)


@register_tool(timeout=60)
def gui_open_app(application_path: str, wait_time: int = 3) -> str:
    """打开应用程序。application_path须在workspace内或为系统已安装程序路径。wait_time为启动等待秒数。"""
    return json.dumps(_gui_open_application(application_path, wait_time), ensure_ascii=False)


@register_tool(timeout=120)
def gui_workflow(steps: List[Dict[str, Any]]) -> str:
    """执行GUI自动化工作流。steps为步骤列表，每个步骤含action和params。单步超时30秒。"""
    return json.dumps(_gui_automate_workflow(steps), ensure_ascii=False)