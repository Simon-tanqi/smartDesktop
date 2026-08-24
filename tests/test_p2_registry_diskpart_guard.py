# -*- coding: utf-8 -*-
"""P2 批针对性验证：diskpart 整体 P1、low_risk_registry+HKCU 收紧、声明对齐冒烟。"""
import json
import sys
from unittest import mock

import pytest

sys.path.insert(0, r"D:\PyCharm Project\smartDesktop")

import core.tool_data_provider  # noqa
from tools import system_tools  # noqa


def _mock_exec():
    p1 = mock.patch.object(system_tools, "_run_command_threaded", return_value={"success": True, "output": "M"})
    p2 = mock.patch.object(system_tools.safe_exec, "run_powershell", return_value={"success": True, "output": "M"})
    p1.start(); p2.start()
    return p1, p2


def _exec(cmd):
    raw = system_tools.execute_command(command=cmd, working_dir=None)
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {"raw": raw}
    return raw


def _classify(res):
    if res.get("success") is False or res.get("valid") is False:
        if res.get("confirmation_required"):
            if res.get("challenge_phrase"):
                return "P1_CHALLENGE"
            return "P2_CONFIRM"
        return "BLOCK" if res.get("stop") else "REJECT"
    return "ALLOW"


# ── P2-8：diskpart 整体挑战级 ──
def test_diskpart_clean_still_blocked():
    p1, p2 = _mock_exec()
    try:
        assert _classify(_exec("diskpart clean")) == "REJECT"
        assert _classify(_exec('echo select disk 0 & diskpart clean all')) == "REJECT"
    finally:
        p1.stop(); p2.stop()


def test_diskpart_other_commands_challenge():
    p1, p2 = _mock_exec()
    try:
        assert _classify(_exec("diskpart list disk")) == "P1_CHALLENGE"
        assert _classify(_exec("diskpart select disk 0")) == "P1_CHALLENGE"
        assert _classify(_exec("diskpart /s script.txt")) == "P1_CHALLENGE"
        assert _classify(_exec("diskpart")) == "P1_CHALLENGE"
    finally:
        p1.stop(); p2.stop()


# ── P2-10：low_risk_registry + HKCU 放行收紧 ──
def test_hkcu_low_risk_plain_allowed():
    p1, p2 = _mock_exec()
    try:
        assert _classify(_exec(
            'reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced" '
            '/v HideFileExt /t REG_DWORD /d 1 /f')) == "ALLOW"
        assert _classify(_exec(
            'Set-ItemProperty -Path "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced" '
            '-Name HideFileExt -Value 1')) == "ALLOW"
    finally:
        p1.stop(); p2.stop()


def test_hkcu_low_risk_with_semicolon_blocked():
    p1, p2 = _mock_exec()
    try:
        # 分号夹带 HKLM 写 → 不放行（danger_patterns 命中 → REJECT）
        assert _classify(_exec(
            'reg add "HKCU\\Software\\Test" /v HideFileExt /d 1 /f ; reg add "HKLM\\Software\\Test" /v x /d 1 /f')) == "REJECT"
        # 分号夹带其他命令 → 不放行（解释器/组合段规则生效）
        res = _classify(_exec(
            'reg add "HKCU\\Software\\Test" /v HideFileExt /d 1 /f ; whoami'))
        assert res in ("P1_CHALLENGE", "P2_CONFIRM", "REJECT")
    finally:
        p1.stop(); p2.stop()


def test_hkcu_low_risk_with_pipe_blocked():
    p1, p2 = _mock_exec()
    try:
        res = _classify(_exec(
            'reg add "HKCU\\Software\\Test" /v HideFileExt /d 1 /f | Remove-Item -Recurse -Force C:\\Users\\Tankq\\x'))
        assert res in ("P1_CHALLENGE", "REJECT")
    finally:
        p1.stop(); p2.stop()


def test_hkcu_low_risk_with_other_root_blocked():
    p1, p2 = _mock_exec()
    try:
        # 夹带 HKLM 根键 → 不放行（danger_patterns 命中）
        assert _classify(_exec(
            'reg add "HKLM\\Software\\Test" /v HideDesktopIcons /d 1 /f')) == "REJECT"
        # 夹带 HKCR 根键 → 不放行（其他根键 → HKCU P2 或拒绝）
        res = _classify(_exec(
            'reg add "HKCR\\Test" /v HideDesktopIcons /d 1 /f'))
        assert res in ("P2_CONFIRM", "REJECT")
    finally:
        p1.stop(); p2.stop()


def test_hkcu_low_risk_non_reg_first_token_blocked():
    p1, p2 = _mock_exec()
    try:
        # echo 仅打印文本（无注册表写副作用、无组合段）→ 放行是安全的
        assert _classify(_exec(
            'echo reg add "HKCU\\Software\\Test" /v HideDesktopIcons /d 1 /f')) == "ALLOW"
        # echo + 管道夹带删除 → 不放行
        res = _classify(_exec(
            'echo reg add "HKCU\\Software\\Test" /v HideDesktopIcons /d 1 /f | Remove-Item -Force C:\\Users\\Tankq\\a.txt'))
        assert res in ("P1_CHALLENGE", "P2_CONFIRM", "REJECT")
    finally:
        p1.stop(); p2.stop()


def test_hkcu_write_non_lowrisk_still_p2():
    p1, p2 = _mock_exec()
    try:
        # 非 low_risk 关键词的 HKCU 写 → P2 确认（既有机制不回退）
        assert _classify(_exec(
            'reg add "HKCU\\Software\\Test" /v CustomValue /d 1 /f')) == "P2_CONFIRM"
    finally:
        p1.stop(); p2.stop()


def test_reg_query_still_allowed():
    p1, p2 = _mock_exec()
    try:
        assert _classify(_exec("reg query HKCU\\Software\\Microsoft")) == "ALLOW"
        assert _classify(_exec("reg query HKLM\\Software\\Microsoft")) == "ALLOW"
    finally:
        p1.stop(); p2.stop()


# ── 正常命令不回退 ──
def test_normal_commands_still_work():
    p1, p2 = _mock_exec()
    try:
        assert _classify(_exec("dir C:\\Users\\Tankq\\Desktop")) == "ALLOW"
        assert _classify(_exec("echo hello")) == "ALLOW"
        assert _classify(_exec("systeminfo")) == "ALLOW"
        assert _classify(_exec("net view")) == "ALLOW"
        assert _classify(_exec("wmic process list brief")) == "ALLOW"
    finally:
        p1.stop(); p2.stop()
