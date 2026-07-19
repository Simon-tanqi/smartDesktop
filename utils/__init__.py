import os
import re


def normalize_path_component(path_str):
    path_str = re.sub(r'([A-Za-z])\s*盘', r'\1:', path_str)
    path_str = path_str.replace('/', '\\')
    path_str = re.sub(r'^([A-Za-z]:)(?!\\)', r'\1\\', path_str)
    return os.path.normpath(path_str)
