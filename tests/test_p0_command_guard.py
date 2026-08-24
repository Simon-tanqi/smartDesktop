# -*- coding: utf-8 -*-
"""P0 批命令拦截修复 - 针对性验证测试。

覆盖：
1. execute_command 取消 _skip_validation 后，safe_exec 全套规则并入（置于"未匹配直接执行"兜底前）：
   - protected_path_patterns（.ssh/.env/.git/微信/QQ/钉钉 + C:\Windows/C:\ProgramData）修改 → L0 拒绝
   - danger_path_patterns（Program Files(x86)）修改 → P1 挑战
   - _check_danger（rm/del/iex/Start-Process/管道组合）→ P1 挑战
2. 注册表写拦截扩充：reg delete/Set-ItemProperty/New-ItemProperty/Remove-ItemProperty/reg import
   HKLM/HKEY_LOCAL_MACHINE → 直接拒绝；HKCU 写 → P2 确认；reg import 单独 → 拒绝
3. 正常命令（用户目录写、只读查询、开发工具）照常放行，不破坏正常执行。

安全设计：应拦截命令在真实执行前即 return；应放行命令通过 mock 替换执行通道，不产生真实副作用。
"""
import json
import os
import pathlib
import sys
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# 先初始化 core 包再导入 tools，规避 tools/__init__ 循环导入（与项目既有入口一致）
import core.tool_data_provider  # noqa: F401
from tools import system_tools
from tools.system_tools import execute_command

USERPROFILE = os.environ.get("USERPROFILE", r"C:\Users\UnknownUser")


def _mock_exec():
    """mock 执行通道：放行命令不会真正执行。"""
    patcher1 = mock.patch.object(system_tools, "_run_command_threaded",
                                 return_value={"success": True, "output": "MOCKED_THREAD"})
    patcher2 = mock.patch.object(system_tools.safe_exec, "run_powershell",
                                 return_value={"success": True, "output": "MOCKED_PS"})
    patcher1.start()
    patcher2.start()
    return patcher1, patcher2


def _exec(cmd):
    """执行命令并返回解析后的 dict。"""
    raw = execute_command(command=cmd, working_dir=None)
    return json.loads(raw) if isinstance(raw, str) else raw


def _classify(res):
    if res.get("success") is False:
        if res.get("confirmation_required"):
            if res.get("challenge_phrase"):
                return "P1_CHALLENGE"
            return "P2_CONFIRM"
        return "BLOCK" if res.get("stop") else "REJECT"
    return "ALLOW"


# ── 1. P0-2 注册表 HKLM 写 → 直接拒绝 ──
def test_reg_delete_hklm_reject():
    p1, p2 = _mock_exec()
    try:
        assert _classify(_exec(r"reg delete HKLM\Software\Foo /f")) == "REJECT"
        assert _classify(_exec(r"reg delete HKEY_LOCAL_MACHINE\Software\Foo /f")) == "REJECT"
    finally:
        p1.stop(); p2.stop()


def test_registry_item_property_hklm_reject():
    p1, p2 = _mock_exec()
    try:
        assert _classify(_exec('Set-ItemProperty -Path "HKLM:\\Software\\Foo" -Name Bar -Value 1')) == "REJECT"
        assert _classify(_exec("New-ItemProperty HKLM:\\Software\\Foo -Name Bar -Value 1")) == "REJECT"
        assert _classify(_exec("Remove-ItemProperty HKLM:\\Software\\Foo -Name Bar")) == "REJECT"
    finally:
        p1.stop(); p2.stop()


def test_reg_import_reject():
    p1, p2 = _mock_exec()
    try:
        # 单独出现（无 HKLM 字样）也拒绝：.reg 内容不可静态解析
        assert _classify(_exec(r"reg import C:\temp\x.reg")) == "REJECT"
        assert _classify(_exec(r"reg import C:\temp\x.reg")) == "REJECT"
    finally:
        p1.stop(); p2.stop()


# ── 2. P0-2 HKCU 写 → P2 确认（不得直接放行）──
def test_hkcu_write_p2_confirm():
    p1, p2 = _mock_exec()
    try:
        res = _exec(r"reg add HKCU\Software\Foo /v Bar /d 1")
        assert _classify(res) == "P2_CONFIRM"
        assert res.get("confirmation_token"), "应返回确认令牌"
        assert _classify(_exec(r"reg delete HKCU\Software\Foo /f")) == "P2_CONFIRM"
        assert _classify(_exec('Set-ItemProperty -Path "HKCU:\\Software\\Foo" -Name Bar -Value 1')) == "P2_CONFIRM"
    finally:
        p1.stop(); p2.stop()


# ── 3. P0-3 敏感凭据目录修改 → L0 拒绝 ──
def test_sensitive_dir_write_block():
    p1, p2 = _mock_exec()
    try:
        res = _exec(f'Set-Content "{USERPROFILE}\\.ssh\\config" "x"')
        assert _classify(res) == "BLOCK"
        assert res.get("stop") is True
        assert _classify(_exec(f'Add-Content {USERPROFILE}\\.env "x"')) == "BLOCK"
        assert _classify(_exec(f'Out-File -FilePath "{USERPROFILE}\\.git\\config" -InputObject "x"')) == "BLOCK"
    finally:
        p1.stop(); p2.stop()


# ── 4. P0-4 系统目录写 → L0 拒绝 / Program Files → P1 挑战 ──
def test_system_dir_write_block():
    p1, p2 = _mock_exec()
    try:
        assert _classify(_exec('Set-Content C:\\Windows\\Temp\\a.txt "x"')) == "BLOCK"
        assert _classify(_exec('Add-Content C:\\ProgramData\\a.txt "x"')) == "BLOCK"
        assert _classify(_exec('Set-Content C:\\Program Files\\Test\\a.txt "x"')) == "P1_CHALLENGE"
        assert _classify(_exec('Set-Content "C:\\Program Files (x86)\\Test\\a.txt" "x"')) == "P1_CHALLENGE"
    finally:
        p1.stop(); p2.stop()


# ── 5. P0-1 safe_exec 危险规则并入 → P1 挑战 ──
def test_check_danger_merged_challenge():
    p1, p2 = _mock_exec()
    try:
        assert _classify(_exec("Start-Process notepad")) == "P1_CHALLENGE"
        # cmd /c del 用户文件：既有 C 盘删除规则优先（引号路径未识别为当前用户目录）→ REJECT，命令仍被拦截
        assert _classify(_exec(f'cmd /c "del {USERPROFILE}\\a.txt"')) == "REJECT"
        # readonly 白名单（echo）携带管道组合不得整体放行：删除分类分支先命中 →
        # 单文件当前用户目录删除 P2 确认（命令被拦截需用户确认，不再 ALLOW）
        assert _classify(_exec(f"echo hello | Remove-Item {USERPROFILE}\\a.txt")) == "P2_CONFIRM"
        assert _classify(_exec("echo x; reg add HKLM\\Software\\Foo /v Bar /d 1")) == "REJECT"
    finally:
        p1.stop(); p2.stop()


# ── 6. 正常命令照常放行（不得破坏正常执行）──
def test_normal_commands_allow():
    p1, p2 = _mock_exec()
    try:
        assert _classify(_exec(f'Set-Content {USERPROFILE}\\Desktop\\p0_test.txt "x"')) == "ALLOW"
        assert _classify(_exec("dir C:\\")) == "ALLOW"
        assert _classify(_exec("git status")) == "ALLOW"
        assert _classify(_exec("pip install requests")) == "ALLOW"
        assert _classify(_exec(f"python {USERPROFILE}\\script.py")) == "ALLOW"
        # 用户目录内删除仍走既有 P2（不受新增检查影响）
        assert _classify(_exec(f"Remove-Item {USERPROFILE}\\Desktop\\old.txt")) == "P2_CONFIRM"
    finally:
        p1.stop(); p2.stop()
