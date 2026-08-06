"""
错误信息翻译层：将技术异常转换为用户可理解的提示。
用于 app.py 所有 API 路由的 except 块，避免直接暴露 str(e) 给前端。
"""

import re

# 关键词 → 用户友好提示
_ERROR_USER_MAP = [
    # 文件/路径类
    ("No such file or directory", "文件不存在，可能已被移动或删除"),
    ("FileNotFoundError", "找不到指定文件，请确认路径是否正确"),
    ("FileExistsError", "目标文件已存在，无法覆盖"),
    ("Permission denied", "没有权限执行此操作，请以管理员身份运行或检查文件权限"),
    ("PermissionError", "没有权限执行此操作，请以管理员身份运行或检查文件权限"),
    ("Access is denied", "访问被拒绝，请检查权限设置"),
    ("Disk full", "磁盘空间不足，请清理后重试"),
    ("No space left", "磁盘空间不足，请清理后重试"),
    ("Not a directory", "路径不是有效的目录"),
    ("Is a directory", "目标是目录而非文件，请指定文件路径"),

    # 网络/连接类
    ("Connection refused", "无法连接到服务，请检查网络或稍后重试"),
    ("Connection reset", "连接被重置，请检查网络稳定性后重试"),
    ("ConnectionError", "网络连接失败，请检查网络设置"),
    ("timed out", "操作超时，请稍后重试或减少单次数据量"),
    ("TimeoutError", "操作超时，请稍后重试或减少单次数据量"),
    ("Name or service not known", "无法解析域名，请检查网络或 DNS 设置"),
    ("Network is unreachable", "网络不可达，请检查网络连接"),
    ("SSL", "安全连接失败，请检查证书或网络代理设置"),

    # 数据库/存储类
    ("database is locked", "数据库被占用，请稍后重试"),
    ("no such table", "数据表不存在，可能需要重建数据库"),
    ("malformed", "数据格式异常，文件可能已损坏"),
    ("corrupt", "数据文件损坏，无法读取"),

    # 资源/系统类
    ("out of memory", "内存不足，请关闭其他程序后重试"),
    ("MemoryError", "内存不足，请关闭其他程序后重试"),
    ("Broken pipe", "连接已断开，请重新操作"),
    ("Interrupted", "操作被中断，请重试"),

    # 编码/格式类
    ("UnicodeDecodeError", "文件编码无法识别，请确认文件格式"),
    ("UnicodeEncodeError", "包含无法编码的字符"),
    ("JSONDecodeError", "数据格式错误，不是有效的 JSON"),
]


def translate_error(exception_str: str) -> str:
    """将技术异常信息翻译为用户可理解的提示。

    Args:
        exception_str: 原始异常字符串（str(e) 或 repr(e)）

    Returns:
        用户友好的错误提示文本
    """
    try:
        s = str(exception_str) if exception_str else "未知错误"

        for keyword, user_msg in _ERROR_USER_MAP:
            if keyword in s:
                # 附加简短技术上下文（截断至 60 字符，方便排查但不吓人）
                short_tech = s[:60].replace("\n", " ").replace("`", "")
                return f"{user_msg}。（技术详情：{short_tech}）"

        # 未命中映射，返回通用提示 + 截断技术信息
        short_tech = s[:80].replace("\n", " ").replace("`", "")
        return f"操作失败，请稍后重试。（技术详情：{short_tech}）"
    except Exception:
        return f"操作失败，请稍后重试。（技术详情：{exception_str[:80] if exception_str else '未知错误'}）"
