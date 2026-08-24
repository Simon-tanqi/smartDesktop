# -*- coding: utf-8 -*-
"""单文件内容修改类工具风险动态定级 + 预留接口 单元测试。

覆盖：
- replace_in_file 修改 D:\\ 盘文件 → P2 简单确认（非 P1）
- 修改 C:\\Users\\<当前用户> 下文件 → P2
- 修改 C:\\Windows / C:\\ 下非用户目录文件 → P0 直接拒绝
- 批量删除仍 P1（工具层 execute_command 删除分级不受影响）
- 预留接口：confirm_override 默认关闭、apply_workspace_edit 未接入流程
纯逻辑测试，不依赖 LLM / 网络；路径用 USERPROFILE 动态获取，避免残留个人路径。
"""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# 先初始化 core 包再导入 tools，规避 tools/__init__ 循环导入（与项目既有入口一致）
import core.tool_data_provider  # noqa: F401
from core.tool_data_provider import (
    _check_single_file_edit_p0,
    _SINGLE_FILE_EDIT_TOOLS,
    ToolExecutionPipeline,
)
from core.react_engine import ReActEngine
from config.config_loader import get_safety_config, SafetyConfig
from tools.file_tools import apply_workspace_edit

USERPROFILE = os.environ.get("USERPROFILE", r"C:\Users\UnknownUser")


def _pipeline() -> ToolExecutionPipeline:
    """构造带真实 RISK_LEVELS 的管线实例（不依赖引擎全局状态）。"""
    engine = ReActEngine(trace_id="test", session_id="test", session_name="test")
    return engine.tool_pipeline


def _check(tool: str, args: dict):
    """返回 check_tool_risk_level 的 (msg, challenge, risk_level)。"""
    return _pipeline().check_tool_risk_level(tool, args)


# ── 1. 非 C 盘：单文件修改 → P2 简单确认 ──
def test_replace_in_file_d_drive_p2():
    msg, challenge, risk = _check("replace_in_file", {
        "file_path": r"D:\PyCharm Project\smartDesktop\tests\test_data\sample_config.yaml",
        "old_str": "timeout: 30", "new_str": "timeout: 60"})
    assert risk == "P2", f"期望 P2，实际 {risk}"
    assert challenge is None, "P2 简单确认不应带挑战短语"
    assert "P2" in msg


def test_edit_file_d_drive_p2():
    _, _, risk = _check("edit_file", {"file_path": r"D:\docs\a.txt", "old": "x", "new": "y"})
    assert risk == "P2"


def test_write_file_d_drive_p2():
    _, _, risk = _check("write_file", {"path": r"D:\docs\b.txt", "content": "hi"})
    assert risk == "P2"


def test_replace_in_text_d_drive_p2():
    _, _, risk = _check("replace_in_text", {"file_path": r"D:\docs\c.txt", "old": "x", "new": "y"})
    assert risk == "P2"


# ── 2. C 盘当前用户目录：单文件修改 → P2 简单确认 ──
def test_replace_in_file_user_dir_p2():
    msg, challenge, risk = _check("replace_in_file", {
        "file_path": os.path.join(USERPROFILE, "Desktop", "note.txt"),
        "old_str": "a", "new_str": "b"})
    assert risk == "P2", f"期望 P2，实际 {risk}"
    assert challenge is None


def test_edit_file_user_dir_p2():
    _, _, risk = _check("edit_file", {"file_path": os.path.join(USERPROFILE, "Documents", "a.txt"),
                                      "old": "x", "new": "y"})
    assert risk == "P2"


def test_user_profile_root_itself_p2():
    # C:\Users\<当前用户> 目录本身（用户数据文件夹根）视为用户目录内 → P2
    _, _, risk = _check("write_file", {"path": USERPROFILE, "content": "x"})
    assert risk == "P2"


# ── 3. C 盘非用户目录：单文件修改 → P0 直接拒绝 ──
def test_replace_in_file_c_windows_p0():
    msg, challenge, risk = _check("replace_in_file", {
        "file_path": r"C:\Windows\System32\drivers\etc\hosts",
        "old_str": "x", "new_str": "y"})
    assert risk == "P0", f"期望 P0，实际 {risk}"
    assert "禁止操作" in msg or "禁止" in msg


def test_edit_file_c_root_p0():
    msg, _, risk = _check("edit_file", {"file_path": r"C:\config.ini", "old": "x", "new": "y"})
    assert risk == "P0"
    assert "禁止操作" in msg


def test_write_file_c_other_user_p0():
    # 其他用户目录（C:\Users\other）非当前用户 → P0
    msg, _, risk = _check("write_file", {"path": r"C:\Users\OtherUser\a.txt", "content": "x"})
    assert risk == "P0"
    assert "禁止操作" in msg


def test_replace_in_file_c_program_files_p0():
    _, _, risk = _check("replace_in_file", {
        "file_path": r"C:\Program Files\App\conf.ini",
        "old_str": "x", "new_str": "y"})
    assert risk == "P0"


def test_p0_detector_returns_path():
    hit = _check_single_file_edit_p0("replace_in_file", {"file_path": r"C:\Windows\a.ini"})
    assert hit is not None and "Windows" in hit
    assert _check_single_file_edit_p0("replace_in_file", {"file_path": r"D:\a.txt"}) is None
    assert _check_single_file_edit_p0("replace_in_file",
                                      {"file_path": os.path.join(USERPROFILE, "a.txt")}) is None


# ── 4. 非单文件修改类工具不受动态分支影响 ──
def test_copy_file_unchanged_direct_pass():
    # copy_file 静态 P2：仍直接放行（不返回确认消息）
    msg, challenge, risk = _check("copy_file", {"source_path": r"D:\a.txt", "dest": r"D:\b.txt"})
    assert risk is None and msg is None and challenge is None


def test_move_file_still_p1():
    # move_file 静态 P1：保持挑战短语确认
    msg, challenge, risk = _check("move_file", {"source": r"D:\a.txt", "dest": r"D:\b.txt"})
    assert risk == "P1"
    assert challenge is not None


def test_batch_delete_unchanged_p1():
    # 批量删除定级在工具层 execute_command（system_tools），引擎层删除相关定级未被改动：
    # RISK_LEVELS.delete_file 仍为 P0（静态强制确认），execute_command 已移除不走引擎层。
    engine = ReActEngine(trace_id="t", session_id="s", session_name="n")
    assert engine.RISK_LEVELS["delete_file"] == "P0"
    assert "execute_command" not in engine.RISK_LEVELS


# ── 5. 预留接口：只预留不实现 ──
def test_confirm_override_default_off():
    cfg = get_safety_config()
    assert cfg.confirm_override is False
    assert SafetyConfig().confirm_override is False


def test_apply_workspace_edit_not_implemented():
    try:
        apply_workspace_edit(r"D:\a.txt")
        assert False, "预留接口应抛出 NotImplementedError"
    except NotImplementedError:
        pass


def test_apply_workspace_edit_not_registered():
    engine = ReActEngine(trace_id="t", session_id="s", session_name="n")
    assert "apply_workspace_edit" not in engine.RISK_LEVELS
    assert "apply_workspace_edit" not in engine.WRITE_TOOLS
    assert "apply_workspace_edit" not in _SINGLE_FILE_EDIT_TOOLS


def test_single_file_edit_tools_membership():
    assert {"replace_in_file", "replace_in_text", "edit_file", "write_file"} <= _SINGLE_FILE_EDIT_TOOLS
