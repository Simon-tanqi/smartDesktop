"""
配置模块 — 全局配置入口

导入顺序：
  settings.py  → 全局常量/魔法数字
  config_loader.py → safety_rules.yaml 热加载工具类
"""
from .settings import *
from .config_loader import get_safety_config
