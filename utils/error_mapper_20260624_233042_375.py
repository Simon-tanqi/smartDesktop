"""
错误信息翻译层 — 将技术异常转为用户可读的中文提示。
"""

_USER_ERROR_MAP = [
    ("No such file or directory", "文件或目录不存在，可能已被移动或删除。请确认路径是否正确。"),
    ("Permission denied", "没有权限执行该操作。请检查文件权限或以管理员身份运行。"),
    ("PermissionError", "没有权限执行该操作。请检查文件权限或以管理员身份运行。"),
    ("Access is denied", "访问被拒绝。请以管理员身份运行或检查安全策略。"),
    ("Connection refused", "无法连接到服务，请检查网络或等待服务启动后重试。"),
    ("ConnectionError", "网络连接失败，请检查网络状态后重试。"),
    ("timed out", "操作超时，请稍后重试或减少单次数据量。"),
    ("TimeoutError", "操作超时，请稍后重试或减少单次数据量。"),
    ("disk full", "磁盘空间不足，请清理磁盘后重试。"),
    ("No space left", "磁盘空间不足，请清理磁盘后重试。"),
    ("address already in use", "端口被占用，请关闭占用程序后重试。"),
    ("cannot find the file", "找不到指定文件，请确认文件名和路径。"),
    ("does not exist", "目标不存在，可能已被删除或移动。"),
    ("out of memory", "内存不足，请关闭其他应用后重试。"),
    ("SQLITE_BUSY", "数据库忙碌中，请稍后重试。"),
    ("database is locked", "数据库被锁定，请等待其他操作完成后重试。"),
]


def translate_error(exception_str: str) -> str:
    """将技术异常字符串翻译为用户友好的中文提示。

    匹配策略：遍历 _USER_ERROR_MAP，命中第一条匹配的即返回。
    未匹配时截断至 80 字符，保留技术细节但限制长度。
    """
    for keyword, user_msg in _USER_ERROR_MAP:
        if keyword in exception_str:
            return user_msg
    # 未匹配：截断保留，兼顾可排查性
    truncated = exception_str[:80]
    if len(exception_str) > 80:
        truncated += "..."
    return f"操作失败（{truncated}）"
