# -*- coding: utf-8 -*-
"""敏感系统路径复制/读取拦截 + C 盘删除规则细化 单元测试。

覆盖：
- 引擎层 _check_sensitive_path_read / _check_sensitive_path_copy（C:\\Windows 等命中、用户目录放行）
- 工具层 _check_sensitive_read_command / _check_sensitive_copy_command（读/复制命令命中系统路径拒绝）
- 删除规则 _is_current_user_dir_delete（当前用户目录允许、其他用户目录/系统目录拒绝）
纯逻辑测试，不依赖 LLM / 网络；路径用 USERPROFILE 动态获取，避免残留个人路径。
"""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# 先初始化 core 包再导入 tools，规避 tools/__init__ 循环导入（与项目既有入口一致）
import core.tool_data_provider  # noqa: F401
from core.tool_data_provider import (
    _check_sensitive_path_read,
    _check_sensitive_path_copy,
    _READ_TOOLS,
    _COPY_TOOLS,
    SENSITIVE_BLOCK_FINAL_REPLY,
)
from tools.system_tools import (
    _check_sensitive_read_command,
    _check_sensitive_copy_command,
    _is_current_user_dir_delete,
    _is_c_drive_delete,
)

USERPROFILE = os.environ.get("USERPROFILE", r"C:\Users\UnknownUser")


# ── 引擎层：复制类工具集合与判定 ──
def test_copy_tools_include_copy_file_to_workspace():
    assert "copy_file_to_workspace" in _COPY_TOOLS
    assert "copy_file_to_workspace" not in _READ_TOOLS


def test_block_final_reply_text():
    assert SENSITIVE_BLOCK_FINAL_REPLY == "内容不允许操作，如需操作请用户自行手动操作"


def test_engine_copy_block_windows_hosts():
    assert _check_sensitive_path_copy(
        "copy_file_to_workspace", {"source_path": r"C:\Windows\System32\drivers\etc\hosts"}) is not None


def test_engine_copy_block_program_files_and_data():
    assert _check_sensitive_path_copy(
        "copy_file_to_workspace", {"source_path": r"C:\Program Files\App\a.dll"}) is not None
    assert _check_sensitive_path_copy(
        "copy_file_to_workspace", {"source_path": r"C:\ProgramData\App\c.ini"}) is not None
    assert _check_sensitive_path_copy(
        "copy_file_to_workspace", {"source_path": r"C:\Program Files (x86)\App\b.dll"}) is not None


def test_engine_copy_allow_user_dir():
    assert _check_sensitive_path_copy(
        "copy_file_to_workspace", {"source_path": os.path.join(USERPROFILE, "Desktop", "t.txt")}) is None


def test_engine_copy_other_tool_ignored():
    assert _check_sensitive_path_copy("write_file", {"path": r"C:\Windows\x.txt"}) is None


# ── 引擎层：读取拦截回归 ──
def test_engine_read_block_windows_still_works():
    assert _check_sensitive_path_read(
        "read_file", {"file_path": r"C:\Windows\System32\drivers\etc\hosts"}) is not None
    assert _check_sensitive_path_read("read_file", {"file_path": USERPROFILE}) is None


# ── 工具层：命令拦截 ──
def test_cmd_read_block_windows():
    assert _check_sensitive_read_command("Get-Content C:\\Windows\\System32\\drivers\\etc\\hosts") is not None
    assert _check_sensitive_read_command("type C:\\Windows\\win.ini") is not None


def test_cmd_read_allow_user_dir():
    assert _check_sensitive_read_command(
        f"Get-Content '{os.path.join(USERPROFILE, 'Desktop', 'a.txt')}'") is None


def test_cmd_copy_block_windows():
    assert _check_sensitive_copy_command("Copy-Item C:\\Windows\\System32\\drivers\\etc\\hosts D:\\tmp\\h") is not None
    assert _check_sensitive_copy_command("copy C:\\Windows\\win.ini D:\\tmp\\w") is not None
    assert _check_sensitive_copy_command("robocopy C:\\Windows\\temp D:\\backup") is not None
    assert _check_sensitive_copy_command("cp C:\\Windows\\a.txt D:\\b.txt") is not None


def test_cmd_copy_allow_user_dir():
    assert _check_sensitive_copy_command(
        f"Copy-Item '{os.path.join(USERPROFILE, 'Desktop', 'a.txt')}' D:\\tmp\\a.txt") is None


def test_cmd_copy_block_target_in_system_dir():
    # 复制到系统目录同样拒绝（系统核心路径不允许任何操作）
    assert _check_sensitive_copy_command(f"Copy-Item '{os.path.join(USERPROFILE, 'a.txt')}' C:\\Windows\\Temp\\a.txt") is not None


# ── 删除规则：C 盘仅当前用户目录允许 ──
def test_delete_current_user_dir_allowed():
    assert _is_current_user_dir_delete(
        f"Remove-Item '{os.path.join(USERPROFILE, 'Desktop', 't.txt')}'") is True
    assert _is_current_user_dir_delete(
        rf"Remove-Item '{USERPROFILE}\AppData\Roaming\App\x'") is True


def test_delete_other_user_dir_denied():
    assert _is_current_user_dir_delete(r"Remove-Item 'C:\Users\OtherUser\Desktop\t.txt'") is False


def test_delete_windows_or_program_files_denied():
    assert _is_current_user_dir_delete(r"Remove-Item 'C:\Windows\temp\a.txt'") is False
    assert _is_current_user_dir_delete(r"Remove-Item 'C:\Program Files\App\a.exe'") is False
    assert _is_current_user_dir_delete(r"Remove-Item 'C:\pagefile.sys'") is False


def test_delete_d_drive_not_c_drive():
    cmd = r"Remove-Item 'D:\data\a.txt'"
    assert _is_c_drive_delete(cmd) is False
    assert _is_current_user_dir_delete(cmd) is False


def test_delete_case_insensitive():
    assert _is_current_user_dir_delete(
        f"Remove-Item '{os.path.join(USERPROFILE, 'DESKTOP', 't.txt')}'") is True
