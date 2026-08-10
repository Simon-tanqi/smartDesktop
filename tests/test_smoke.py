# -*- coding: utf-8 -*-
"""CI 冒烟测试：不依赖 Windows 专属第三方包，仅验证仓库结构与关键文件。

CI 运行于 ubuntu-latest，requirements-ci.txt 只装跨平台核心依赖，
因此本模块禁止导入 pywin32/comtypes/pycaw 等 Windows 专属包。
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]

_SKIP_PARTS = {"temp", ".git", "__pycache__", ".venv", "venv", "node_modules"}


def test_core_modules_exist():
    required = [
        "core/react_engine.py",
        "core/tool_data_provider.py",
        "core/logger.py",
        "memory/session.py",
        "memory/long_term.py",
        "tools/file_tools.py",
        "tools/__init__.py",
        "backend/chat.py",
        "backend/routing.py",
        "prompts/core.md",
        "prompts/tools.yaml",
        "requirements.txt",
        "requirements-ci.txt",
    ]
    for rel in required:
        assert (ROOT / rel).is_file(), f"缺少关键模块: {rel}"


def test_no_absolute_user_path():
    """仓库内不得残留开发者个人绝对路径。"""
    hits = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or p.suffix not in {".py", ".md", ".yaml", ".yml", ".json", ".txt"}:
            continue
        if any(part in _SKIP_PARTS for part in p.parts):
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        if "C:\\Users\\username" in text:
            hits.append(str(p.relative_to(ROOT)))
    assert not hits, f"残留个人绝对路径: {hits}"


def test_readme_no_aigc_header():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert not readme.startswith("---\nAIGC:"), "README 残留 AIGC 元数据头"


def test_requirements_has_pandas():
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "pandas" in req, "requirements.txt 缺少 pandas"


def test_gitignore_models_json():
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "models.json" in gi, ".gitignore 未忽略 models.json"


def test_ci_uses_ci_requirements():
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "requirements-ci.txt" in ci, "ci.yml 未使用 requirements-ci.txt"
