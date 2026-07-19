"""
PPT 生成引擎：HTML → Playwright 截图 → python-pptx
双轨制：截图做背景（视觉） + 透明文本框覆盖（可编辑文本）
"""

from pathlib import Path
import tempfile, os

SLIDE_W = 1920
SLIDE_H = 1080

DATA_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'MyAgent')
OUTPUT_DIR = Path(os.environ.get("MYAGENT_OUTPUT", os.path.join(DATA_DIR, "workspace")))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
