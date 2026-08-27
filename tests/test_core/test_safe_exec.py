# -*- coding: utf-8 -*-
"""tools/safe_exec.py 命令预检拦截规则纯逻辑单元测试。

只测试规则判定函数（黑名单 / 路径保护 / 危险管道 / 编码命令解码等），
不执行任何真实命令；run_powershell 等含真实执行副作用的路径不在本文件覆盖范围。
"""
import base64
import os

import pytest

from tools.safe_exec import (
    _check_danger,
    _check_dangerous_pipeline,
    _check_protected_paths,
    _decode_powershell_script,
    _expand_env_vars,
    _extract_paths_from_args,
    _extract_paths_from_command,
    _is_modify_command,
    _normalize_command,
    _path_matches_protected,
    _powershell_quote,
    _strip_string_literals,
    is_dangerous,
)


def _ps_encoded(cmd: str) -> str:
    """将 PowerShell 命令编码为 -EncodedCommand 参数（utf-16-le + base64）。"""
    return base64.b64encode(cmd.encode("utf-16-le")).decode("ascii")


# ── 命令规范化 ───────────────────────────────────────────────────────

class TestNormalizeCommand:
    def test_taskkill_force_mapping(self):
        out = _normalize_command("taskkill /F /m notepad.exe")
        assert "/IM" in out and "/m" not in out

    def test_plain_command_unchanged(self):
        cmd = "Get-Process | Select-Object Name"
        assert _normalize_command(cmd) == cmd


# ── 编码命令解码 ─────────────────────────────────────────────────────

class TestDecodePowerShellScript:
    def test_decodes_encoded_command(self):
        plain = "Get-ChildItem C:\\Users"
        encoded = _ps_encoded(plain)
        script = f"powershell -EncodedCommand {encoded}"
        decoded = _decode_powershell_script(script)
        assert plain in decoded

    def test_no_match_returns_original(self):
        script = "Get-ChildItem C:\\Users"
        assert _decode_powershell_script(script) == script

    def test_short_enc_alias(self):
        plain = "dir"
        encoded = _ps_encoded(plain)
        script = f"powershell -Enc {encoded}"
        assert plain in _decode_powershell_script(script)


# ── 字符串与路径辅助 ─────────────────────────────────────────────────

class TestStringHelpers:
    def test_strip_string_literals(self):
        assert _strip_string_literals('echo "hello world"') == 'echo ""'
        assert _strip_string_literals("echo 'hello'") == 'echo ""'

    def test_powershell_quote(self):
        assert _powershell_quote("it's") == "'it''s'"
        assert _powershell_quote("abc") == "'abc'"

    def test_expand_env_vars_percent(self):
        expected = os.environ.get("USERPROFILE", "%USERPROFILE%")
        assert _expand_env_vars("%USERPROFILE%") == expected

    def test_expand_env_vars_dollar_env(self):
        expected = os.environ.get("USERPROFILE", "$env:USERPROFILE")
        assert _expand_env_vars("$env:USERPROFILE") == expected

    def test_expand_env_vars_home(self):
        expected = os.environ.get("USERPROFILE", os.environ.get("HOME", "$HOME"))
        assert _expand_env_vars("$HOME") == expected

    def test_expand_undefined_env_kept(self):
        token = "%NO_SUCH_VAR_12345%"
        assert _expand_env_vars(token) == token


class TestExtractPaths:
    def test_absolute_path(self):
        paths = _extract_paths_from_args([r"C:\Windows\System32"], cwd=r"C:\work")
        assert any(p == os.path.normpath(r"C:\Windows\System32") for p in paths)

    def test_relative_path_joined_with_cwd(self):
        paths = _extract_paths_from_args([r"sub\file.txt"], cwd=r"C:\work")
        assert any(os.path.normpath(r"C:\work\sub\file.txt") == p for p in paths)

    def test_skip_pure_options(self):
        paths = _extract_paths_from_args(["-f", "/r", r"C:\real\path"], cwd=r"C:\work")
        assert all("real" not in p or p.endswith("path") for p in paths)

    def test_from_command_skips_first_token(self):
        paths = _extract_paths_from_command("del C:\\tmp\\a.txt", cwd=r"C:\work")
        assert any(p.endswith("a.txt") for p in paths)


# ── 修改类命令判定 ───────────────────────────────────────────────────

class TestIsModifyCommand:
    @pytest.mark.parametrize("cmd", [
        "del C:\\tmp\\a.txt",
        "rm -rf /tmp/a",
        "Remove-Item C:\\tmp\\a",
        "format C:",
        "Format-Volume -DriveLetter C",
        "reg add HKLM\\SOFTWARE\\x /v y /d z",
    ])
    def test_modify_commands_true(self, cmd):
        assert _is_modify_command(cmd) is True

    @pytest.mark.parametrize("cmd", [
        "Get-ChildItem C:\\Users",
        "ipconfig",
        "ping 127.0.0.1",
        "Get-Content C:\\tmp\\a.txt",
    ])
    def test_readonly_commands_false(self, cmd):
        assert _is_modify_command(cmd) is False


# ── 受保护路径匹配 ───────────────────────────────────────────────────

class TestPathMatchesProtected:
    def test_exact_match(self):
        patterns = [("C:\\Windows\\", "Windows 目录")]
        assert _path_matches_protected("C:\\Windows\\", patterns) == "Windows 目录"

    def test_subpath_match(self):
        patterns = [("C:\\Windows\\", "Windows 目录")]
        assert _path_matches_protected("C:\\Windows\\System32\\a.dll", patterns) == "Windows 目录"

    def test_parent_path_match(self):
        patterns = [("C:\\Windows\\", "Windows 目录")]
        assert _path_matches_protected("C:\\Windows", patterns) == "Windows 目录"

    def test_case_insensitive(self):
        patterns = [("c:\\windows\\", "Windows 目录")]
        assert _path_matches_protected("C:\\WINDOWS\\System32", patterns) == "Windows 目录"

    def test_no_match(self):
        patterns = [("C:\\Windows\\", "Windows 目录")]
        assert _path_matches_protected("D:\\data\\file.txt", patterns) is None


class TestCheckProtectedPaths:
    def test_block_hit_windows_modify(self):
        block_hit, challenge_hit = _check_protected_paths(
            "Remove-Item C:\\Windows\\Temp\\x -Force", cwd=r"C:\work"
        )
        assert block_hit is not None
        assert challenge_hit is None

    def test_challenge_hit_program_files(self):
        block_hit, challenge_hit = _check_protected_paths(
            "Set-Content C:\\Program Files\\app\\x.txt 'v'", cwd=r"C:\work"
        )
        assert block_hit is None
        assert challenge_hit is not None

    def test_readonly_safe(self):
        block_hit, challenge_hit = _check_protected_paths(
            "Get-ChildItem C:\\Windows", cwd=r"C:\work"
        )
        assert block_hit is None and challenge_hit is None

    def test_safe_modify_outside_protected(self):
        block_hit, challenge_hit = _check_protected_paths(
            "Set-Content D:\\data\\a.txt 'v'", cwd=r"C:\work"
        )
        assert block_hit is None and challenge_hit is None


# ── 危险管道 / 危险命令 ──────────────────────────────────────────────

class TestDangerousPipeline:
    def test_pipe_to_delete_is_dangerous(self):
        danger, desc, _level = _check_dangerous_pipeline("Get-ChildItem | Remove-Item -Force")
        assert danger is True
        assert "删除" in desc

    def test_safe_pipeline_not_dangerous(self):
        danger, _desc, _level = _check_dangerous_pipeline("Get-Process | Select-Object Name")
        assert danger is False

    def test_semicolon_delete_is_dangerous(self):
        cmd = "Get-ChildItem; Remove-Item C:\\tmp\\a"
        danger, desc, _level = _check_dangerous_pipeline(cmd)
        assert danger is True
        assert "删除" in desc


class TestCheckDanger:
    def test_rm_alias(self):
        assert _check_danger("rm -rf /tmp/x")[0] is True

    def test_del_alias(self):
        assert _check_danger("del C:\\tmp\\a.txt")[0] is True

    def test_format_disk(self):
        assert _check_danger("format C: /q")[0] is True

    def test_invoke_expression(self):
        assert _check_danger("iex (Get-Content a.ps1)")[0] is True

    def test_variable_assignment_then_delete(self):
        cmd = '$p = "C:\\tmp\\a"; Remove-Item $p -Force'
        assert _check_danger(cmd)[0] is True

    def test_encoded_delete_decoded(self):
        plain = "Remove-Item C:\\tmp\\a -Force"
        encoded = _ps_encoded(plain)
        cmd = f"powershell -EncodedCommand {encoded}"
        decoded = _decode_powershell_script(cmd)
        assert plain in decoded
        # 解码后的明文命令应命中危险规则
        assert _check_danger(decoded)[0] is True

    def test_safe_command_not_dangerous(self):
        assert _check_danger("Get-ChildItem C:\\Users")[0] is False
        assert _check_danger("notepad")[0] is False

    def test_string_literal_low_false_positive(self):
        # 引号内的 "rm" 不应误报（字符串字面量被剥离）
        assert _check_danger('echo "rm not a command"')[0] is False


class TestIsDangerousWrapper:
    def test_true_for_dangerous(self):
        assert is_dangerous("rm -rf /tmp/x") is True

    def test_false_for_safe(self):
        assert is_dangerous("Get-ChildItem C:\\Users") is False
