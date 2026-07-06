"""
GUI自动化工具 - 提供完整的桌面操作能力
基于pyautogui、pygetwindow等库实现鼠标点击、键盘输入、窗口管理等
"""

import os
import time
import json
import base64
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from config.settings import SCREENSHOTS_DIR

# 延迟导入，避免依赖问题
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

def _check_deps() -> tuple[bool, str]:
    """检查GUI依赖是否可用"""
    if not HAS_GUI_DEPS:
        return False, "GUI功能需要安装额外依赖: pip install pyautogui pygetwindow pillow"
    return True, ""

def _safe_gui_operation(func):
    """GUI操作的安全装饰器"""
    def wrapper(*args, **kwargs):
        try:
            # 检查依赖
            has_deps, error_msg = _check_deps()
            if not has_deps:
                return {"success": False, "message": error_msg}
            
            # 执行操作
            result = func(*args, **kwargs)
            return result
        except Exception as e:
            return {"success": False, "message": f"GUI操作失败: {str(e)}"}
    return wrapper

# ==================== 基础GUI操作 ====================

@_safe_gui_operation
def gui_mouse_click(x: int, y: int, button: str = "left", clicks: int = 1) -> Dict[str, Any]:
    """
    鼠标点击操作
    
    Args:
        x: X坐标
        y: Y坐标  
        button: 鼠标按钮 (left/right/middle)
        clicks: 点击次数 (1=单击, 2=双击)
    """
    try:
        pyautogui.click(x=x, y=y, button=button, clicks=clicks)
        return {
            "success": True, 
            "message": f"已在位置({x}, {y})执行{button}鼠标{'双击' if clicks==2 else '单击'}"
        }
    except Exception as e:
        return {"success": False, "message": f"鼠标点击失败: {str(e)}"}

@_safe_gui_operation
def gui_mouse_move(x: int, y: int, duration: float = 0.5) -> Dict[str, Any]:
    """
    移动鼠标到指定位置
    
    Args:
        x: 目标X坐标
        y: 目标Y坐标
        duration: 移动持续时间(秒)
    """
    try:
        pyautogui.moveTo(x, y, duration=duration)
        return {"success": True, "message": f"鼠标已移动到位置({x}, {y})"}
    except Exception as e:
        return {"success": False, "message": f"鼠标移动失败: {str(e)}"}

@_safe_gui_operation  
def gui_type_text(text: str, interval: float = 0.1) -> Dict[str, Any]:
    """
    模拟键盘输入文本
    
    Args:
        text: 要输入的文本
        interval: 字符间延迟(秒)
    """
    try:
        pyautogui.write(text, interval=interval)
        return {"success": True, "message": f"已输入文本: {text[:50]}{'...' if len(text)>50 else ''}"}
    except Exception as e:
        return {"success": False, "message": f"文本输入失败: {str(e)}"}

@_safe_gui_operation
def gui_press_keys(keys: List[str]) -> Dict[str, Any]:
    """
    按下键盘按键
    
    Args:
        keys: 按键列表，如["ctrl", "c"] 或 ["enter"]
    """
    try:
        if len(keys) == 1:
            pyautogui.press(keys[0])
        else:
            pyautogui.hotkey(*keys)
        return {"success": True, "message": f"已按下按键: {'+'.join(keys)}"}
    except Exception as e:
        return {"success": False, "message": f"按键操作失败: {str(e)}"}

# ==================== 窗口管理 ====================

@_safe_gui_operation
def gui_get_active_window() -> Dict[str, Any]:
    """获取当前活动窗口信息"""
    try:
        active_window = gw.getActiveWindow()
        if active_window:
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
        else:
            return {"success": False, "message": "没有检测到活动窗口"}
    except Exception as e:
        return {"success": False, "message": f"获取窗口信息失败: {str(e)}"}

@_safe_gui_operation
def gui_find_window_by_title(title_keyword: str) -> Dict[str, Any]:
    """
    根据标题关键词查找窗口
    
    Args:
        title_keyword: 窗口标题关键词
    """
    try:
        windows = gw.getWindowsWithTitle(title_keyword)
        if windows:
            window_list = []
            for win in windows:
                window_list.append({
                    "title": win.title,
                    "left": win.left,
                    "top": win.top,
                    "width": win.width,
                    "height": win.height
                })
            return {
                "success": True,
                "message": f"找到{len(windows)}个匹配窗口",
                "windows": window_list
            }
        else:
            return {"success": False, "message": f"未找到标题包含'{title_keyword}'的窗口"}
    except Exception as e:
        return {"success": False, "message": f"查找窗口失败: {str(e)}"}

@_safe_gui_operation
def gui_activate_window(title_keyword: str) -> Dict[str, Any]:
    """
    激活指定窗口
    
    Args:
        title_keyword: 窗口标题关键词
    """
    try:
        windows = gw.getWindowsWithTitle(title_keyword)
        if windows:
            window = windows[0]  # 激活第一个匹配的窗口
            window.activate()
            time.sleep(0.5)  # 等待窗口激活
            return {"success": True, "message": f"已激活窗口: {window.title}"}
        else:
            return {"success": False, "message": f"未找到标题包含'{title_keyword}'的窗口"}
    except Exception as e:
        return {"success": False, "message": f"激活窗口失败: {str(e)}"}

# ==================== 屏幕操作 ====================

@_safe_gui_operation
def gui_take_screenshot(region: Optional[Tuple[int, int, int, int]] = None, 
                       save_path: Optional[str] = None) -> Dict[str, Any]:
    """
    屏幕截图
    
    Args:
        region: 截图区域 (x, y, width, height)，None表示全屏
        save_path: 保存路径，None则自动生成
    """
    try:
        # 确保截图目录存在
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        
        if save_path is None:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            save_path = os.path.join(SCREENSHOTS_DIR, f"screenshot_{timestamp}.png")
        
        if region:
            # 区域截图
            screenshot = pyautogui.screenshot(region=region)
        else:
            # 全屏截图
            screenshot = pyautogui.screenshot()
        
        screenshot.save(save_path)
        
        return {
            "success": True,
            "message": "截图已保存",
            "file_path": save_path,
            "img_md": f"![截图]({save_path})",
            "region": region or "全屏"
        }
    except Exception as e:
        return {"success": False, "message": f"截图失败: {str(e)}"}

@_safe_gui_operation
def gui_locate_on_screen(image_path: str, confidence: float = 0.8) -> Dict[str, Any]:
    """
    在屏幕上定位图像
    
    Args:
        image_path: 模板图像路径
        confidence: 匹配置信度 (0-1)
    """
    try:
        if not os.path.exists(image_path):
            return {"success": False, "message": f"模板图像不存在: {image_path}"}
        
        location = pyautogui.locateOnScreen(image_path, confidence=confidence)
        
        if location:
            return {
                "success": True,
                "message": "找到匹配图像",
                "location": {
                    "left": location.left,
                    "top": location.top,
                    "width": location.width,
                    "height": location.height,
                    "center_x": location.left + location.width // 2,
                    "center_y": location.top + location.height // 2
                }
            }
        else:
            return {"success": False, "message": "屏幕上未找到匹配图像"}
    except Exception as e:
        return {"success": False, "message": f"图像定位失败: {str(e)}"}

# ==================== 高级操作 ====================

@_safe_gui_operation
def gui_open_application(application_path: str, wait_time: int = 3) -> Dict[str, Any]:
    """
    打开应用程序
    
    Args:
        application_path: 应用程序路径
        wait_time: 等待应用程序启动时间(秒)
    """
    try:
        if not os.path.exists(application_path):
            return {"success": False, "message": f"应用程序不存在: {application_path}"}
        
        # 使用系统命令启动应用
        os.startfile(application_path)
        time.sleep(wait_time)
        
        return {"success": True, "message": f"已启动应用程序，等待{wait_time}秒"}
    except Exception as e:
        return {"success": False, "message": f"启动应用程序失败: {str(e)}"}

@_safe_gui_operation
def gui_automate_workflow(steps: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    执行GUI自动化工作流
    
    Args:
        steps: 步骤列表，每个步骤包含操作类型和参数
    """
    try:
        results = []
        
        for i, step in enumerate(steps, 1):
            action = step.get("action")
            params = step.get("params", {})
            
            if action == "click":
                result = gui_mouse_click(**params)
            elif action == "type":
                result = gui_type_text(**params)
            elif action == "press_keys":
                result = gui_press_keys(**params)
            elif action == "move_mouse":
                result = gui_mouse_move(**params)
            elif action == "wait":
                time.sleep(params.get("seconds", 1))
                result = {"success": True, "message": f"等待{params.get('seconds', 1)}秒"}
            elif action == "screenshot":
                result = gui_take_screenshot(**params)
            else:
                result = {"success": False, "message": f"未知操作: {action}"}
            
            results.append({"step": i, "action": action, "result": result})
            
            # 如果某一步失败，停止执行
            if not result["success"]:
                return {
                    "success": False, 
                    "message": f"工作流在第{i}步失败: {action}",
                    "failed_step": i,
                    "step_result": result,
                    "completed_steps": results[:i]
                }
        
        return {
            "success": True, 
            "message": f"工作流执行完成，共{len(steps)}步",
            "results": results
        }
    except Exception as e:
        return {"success": False, "message": f"工作流执行失败: {str(e)}"}

# ==================== 注册工具 ====================

# 基础操作工具
from tools import register_tool, path_params

@register_tool(timeout=10)
@path_params("save_path", "image_path")
def gui_click(x: int, y: int, button: str = "left", clicks: int = 1) -> str:
    """执行鼠标点击操作"""
    return json.dumps(gui_mouse_click(x, y, button, clicks), ensure_ascii=False)

@register_tool(timeout=10)
def gui_move(x: int, y: int, duration: float = 0.5) -> str:
    """移动鼠标到指定位置"""
    return json.dumps(gui_mouse_move(x, y, duration), ensure_ascii=False)

@register_tool(timeout=15)
def gui_type(text: str, interval: float = 0.1) -> str:
    """模拟键盘输入文本"""
    return json.dumps(gui_type_text(text, interval), ensure_ascii=False)

@register_tool(timeout=10)
def gui_keys(keys: List[str]) -> str:
    """按下键盘按键"""
    return json.dumps(gui_press_keys(keys), ensure_ascii=False)

# 窗口管理工具
@register_tool(timeout=10)
def gui_active_window() -> str:
    """获取当前活动窗口信息"""
    return json.dumps(gui_get_active_window(), ensure_ascii=False)

@register_tool(timeout=10)
def gui_find_window(title_keyword: str) -> str:
    """根据标题查找窗口"""
    return json.dumps(gui_find_window_by_title(title_keyword), ensure_ascii=False)

@register_tool(timeout=10)
def gui_activate(title_keyword: str) -> str:
    """激活指定窗口"""
    return json.dumps(gui_activate_window(title_keyword), ensure_ascii=False)

# 屏幕操作工具
@register_tool(timeout=15)
@path_params("save_path")
def gui_screenshot(region_x: int = None, region_y: int = None, 
                   region_w: int = None, region_h: int = None, 
                   save_path: str = None) -> str:
    """屏幕截图"""
    region = None
    if all(v is not None for v in [region_x, region_y, region_w, region_h]):
        region = (region_x, region_y, region_w, region_h)
    return json.dumps(gui_take_screenshot(region=region, save_path=save_path), ensure_ascii=False)

@register_tool(timeout=30)
@path_params("image_path")
def gui_locate_image(image_path: str, confidence: float = 0.8) -> str:
    """在屏幕上定位图像"""
    return json.dumps(gui_locate_on_screen(image_path, confidence), ensure_ascii=False)

# 高级操作工具
@register_tool(timeout=60)
@path_params("application_path")
def gui_open_app(application_path: str, wait_time: int = 3) -> str:
    """打开应用程序"""
    return json.dumps(gui_open_application(application_path, wait_time), ensure_ascii=False)

@register_tool(timeout=120)
def gui_workflow(steps: List[Dict[str, Any]]) -> str:
    """执行GUI自动化工作流"""
    return json.dumps(gui_automate_workflow(steps), ensure_ascii=False)

# 兼容性别名
@register_tool(timeout=15)
@path_params("save_path")
def take_screenshot(save_path: str = None) -> str:
    """截图功能（兼容性别名）"""
    return gui_screenshot(save_path=save_path)