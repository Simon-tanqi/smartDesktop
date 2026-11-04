# -*- coding: utf-8 -*-
r"""路径保护回归测试：设备前缀 / 组件尾点不得绕过敏感路径判定。

背景：`\\?\C:\Windows\...`、`C:\Windows.\...` 这类 Windows 等价写法
曾使前缀匹配失效，导致 P0 写入/删除拒绝与读取保护被绕过。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.safety_policy import (  # noqa: E402
    canonicalize_win_path,
    match_sensitive_read_root,
    match_sensitive_copy_root,
)
from core.tool_data_provider import (  # noqa: E402
    _check_single_file_edit_p0,
    _check_delete_p0,
)

READ_TARGETS = [
    r"\\?\C:\Windows\System32\config\SAM",
    r"\\.\C:\Windows\System32\config\SAM",
    r"C:\Windows.\System32\config\SAM",
    r"C:\Windows \System32\config\SAM",
    r"//?/C:/Windows/System32/config/SAM",
]

WRITE_TARGETS = [
    r"\\?\C:\Windows\System32\drivers\etc\hosts",
    r"\\.\C:\Windows\System32\drivers\etc\hosts",
    r"C:\Windows.\System32\drivers\etc\hosts",
]


class TestCanonicalize:
    def test_device_prefix_stripped(self):
        assert canonicalize_win_path(r"\\?\C:\Windows\x") == r"C:\Windows\x"
        assert canonicalize_win_path(r"\\.\C:\Windows\x") == r"C:\Windows\x"

    def test_unc_device_prefix(self):
        assert canonicalize_win_path(r"\\?\UNC\srv\share\x") == r"\\srv\share\x"

    def test_trailing_dot_and_space_stripped(self):
        assert canonicalize_win_path(r"C:\Windows.\System32 ") == r"C:\Windows\System32"

    def test_dot_segments_preserved(self):
        # ".." 不做解析，交由后续 normpath 处理，避免改变语义
        assert canonicalize_win_path(r"C:\Windows\..\Windows") == r"C:\Windows\..\Windows"


class TestReadProtection:
    def test_variants_blocked(self):
        for p in READ_TARGETS:
            assert match_sensitive_read_root(p) == r"C:\Windows", f"未拦截: {p}"

    def test_normal_path_not_blocked(self):
        assert match_sensitive_read_root(r"D:\work\a.txt") is None
        assert match_sensitive_read_root(r"C:\Users\me\a.txt") is None

    def test_copy_root_still_protected(self):
        assert match_sensitive_copy_root(r"\\?\C:\Program Files\app\a.txt") == r"C:\Program Files"


class TestWriteDeleteProtection:
    def test_long_path_prefix_blocks_write(self):
        for p in WRITE_TARGETS:
            assert _check_single_file_edit_p0("write_file", {"file_path": p}), f"未拦截: {p}"

    def test_long_path_prefix_blocks_delete(self):
        for p in WRITE_TARGETS:
            assert _check_delete_p0({"file_path": p}), f"未拦截: {p}"

    def test_user_dir_still_allowed(self):
        import os
        profile = os.environ.get("USERPROFILE", r"C:\Users\UnknownUser")
        assert _check_single_file_edit_p0("write_file", {"file_path": profile + r"\a.txt"}) is None
