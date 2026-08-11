"""通用工具函数包：提供路径等基础字符串规范化能力。"""

import os
import re


def normalize_path_component(path_str):
    """规范化用户输入的盘符与路径分隔符。

    将「C 盘/D盘」口语化写法转为标准盘符（C:），统一分隔符为反斜杠，
    并确保盘符后带有根目录斜杠，最终经 os.path.normpath 规整。
    """
    path_str = re.sub(r'([A-Za-z])\s*盘', r'\1:', path_str)
    path_str = path_str.replace('/', '\\')
    path_str = re.sub(r'^([A-Za-z]:)(?!\\)', r'\1\\', path_str)
    return os.path.normpath(path_str)
