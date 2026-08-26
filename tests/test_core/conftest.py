# -*- coding: utf-8 -*-
"""test_core 公共配置：将项目根注入 sys.path，保证 CI 上模块可被干净导入。

CI（ubuntu-latest + requirements-ci.txt）只安装跨平台核心依赖，
本目录测试必须避免 import pywin32/comtypes 等 Windows 专属包。
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
