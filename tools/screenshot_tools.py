"""截图工具：全屏或区域截图，基于 PIL.ImageGrab"""

import os
import json
from datetime import datetime
from pathlib import Path

from . import register_tool


def _get_screenshot_dir():
    """截图保存目录"""
    project_dir = Path(__file__).parent.parent
    ss_dir = project_dir / "workspace" / "screenshots"
    os.makedirs(ss_dir, exist_ok=True)
    return str(ss_dir)


@register_tool
def take_screenshot(left: int = None, top: int = None,
                    right: int = None, bottom: int = None) -> str:
    """截取屏幕并保存为 PNG 文件，返回文件路径。
    不传区域参数时截取全屏；传 left,top,right,bottom 时截取指定区域（像素坐标）。

    left: 区域左边界（像素，可选）
    top: 区域上边界（像素，可选）
    right: 区域右边界（像素，可选）
    bottom: 区域下边界（像素，可选）
    """
    try:
        from PIL import ImageGrab
    except ImportError:
        return json.dumps({
            "success": False,
            "message": "Pillow 库未安装，无法截图。请运行: pip install Pillow"
        }, ensure_ascii=False)

    try:
        # 区域截图或全屏截图
        region_given = all(v is not None for v in (left, top, right, bottom))
        if region_given:
            img = ImageGrab.grab(bbox=(left, top, right, bottom))
        else:
            img = ImageGrab.grab()

        # 生成文件名：screenshot_YYYYMMDD_HHMMSS.png
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"screenshot_{timestamp}.png"
        filepath = os.path.join(_get_screenshot_dir(), filename)
        img.save(filepath, "PNG")

        file_size_kb = os.path.getsize(filepath) / 1024
        img_md = f"![截图](http://localhost:8765/screenshots/{filename})"
        return json.dumps({
            "success": True,
            "file_path": filepath,
            "width": img.size[0],
            "height": img.size[1],
            "file_size_kb": round(file_size_kb, 1),
            "img_md": img_md,
            "message": f"截图已保存：{img.size[0]}x{img.size[1]}, {file_size_kb:.1f}KB。图片链接和文件路径将由系统自动附加到最终回复中。"
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({
            "success": False,
            "message": f"截图失败: {str(e)}"
        }, ensure_ascii=False)
