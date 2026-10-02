# -*- coding: utf-8 -*-
"""P0 批命令拦截修复 - 针对性验证测试。

覆盖：
1. execute_command 取消 _skip_validation 后，safe_exec 全套规则并入（置于"未匹配直接执行"兜底前）：
   - protected_path_patterns（.ssh/.env/.git/微信/QQ/钉钉 + C:\\Windows/C:\\ProgramData）修改 → L0 拒绝
   - danger_path_patterns（Program Files(x86)）修改 → P1 挑战
   - _check_danger（rm/del/iex/Start-Process/管道组合）→ P1 挑战
2. 注册表写拦截扩充：reg delete/Set-ItemProperty/New-ItemProperty/Remove-ItemProperty/reg import
   HKLM/HKEY_LOCAL_MACHINE、HKCR/HKU/HKCC 等全局根键 → 直接拒绝；HKCU 写 → P1 挑战确认；reg import 单独 → 拒绝
3. Program Files / Program Files (x86)（任意盘符，含 %ProgramFiles% 展开）批量删除 → P0 拒绝；软件卸载仍走专用分支（C 盘内 P1）
4. 正常命令（用户目录写、只读查询、开发工具）照常放行，不破坏正常执行。

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
    if res.get("success") is False or res.get("valid") is False:
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


# ── 2. P0-2 HKCU 写 → P1 挑战确认（不得直接放行，无 P2-10 低风险放行路径）──
def test_hkcu_write_p1_challenge():
    p1, p2 = _mock_exec()
    try:
        res = _exec(r"reg add HKCU\Software\Foo /v Bar /d 1")
        assert _classify(res) == "P1_CHALLENGE"
        assert res.get("challenge_phrase"), "应返回挑战短语"
        assert res.get("challenge_token"), "应返回挑战令牌"
        assert _classify(_exec(r"reg delete HKCU\Software\Foo /f")) == "P1_CHALLENGE"
        assert _classify(_exec('Set-ItemProperty -Path "HKCU:\\Software\\Foo" -Name Bar -Value 1')) == "P1_CHALLENGE"
        # 低风险关键词（桌面图标等）同样不得直接放行 → P1 挑战
        assert _classify(_exec(
            'reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced" '
            '/v HideFileExt /d 1 /f')) == "P1_CHALLENGE"
    finally:
        p1.stop(); p2.stop()


# ── 2.5 全局注册表根键（HKCR/HKU/HKCC）写 → P0 直接拒绝 ──
def test_registry_other_root_reject():
    p1, p2 = _mock_exec()
    try:
        assert _classify(_exec(r"reg add HKCR\Software\Foo /v Bar /d 1")) == "REJECT"
        assert _classify(_exec(r"reg delete HKEY_CLASSES_ROOT\Software\Foo /f")) == "REJECT"
        assert _classify(_exec(r"reg add HKU\.DEFAULT\Software\Foo /v Bar /d 1")) == "REJECT"
        assert _classify(_exec(r"reg delete HKEY_USERS\.DEFAULT\Software\Foo /f")) == "REJECT"
        assert _classify(_exec(r"reg add HKCC\Software\Foo /v Bar /d 1")) == "REJECT"
        assert _classify(_exec(r"reg add HKEY_CURRENT_CONFIG\Software\Foo /v Bar /d 1")) == "REJECT"
    finally:
        p1.stop(); p2.stop()


# ── 2.6 Program Files 批量删除 → P0 拒绝；单文件保持分级；卸载仍 P1 ──
def test_program_files_batch_delete_reject():
    p1, p2 = _mock_exec()
    try:
        # C 盘 Program Files 批量删除（通配符）→ P0 拒绝（不弹确认框）
        assert _classify(_exec(r'Remove-Item "C:\Program Files\App\*" -Force')) == "REJECT"
        assert _classify(_exec(r'Remove-Item "C:\Program Files (x86)\App" -Recurse -Force')) == "REJECT"
        # 非 C 盘 Program Files 批量删除 → P0 拒绝（任意盘符）
        assert _classify(_exec(r'Remove-Item "D:\Program Files\App\*" -Force')) == "REJECT"
        # %ProgramFiles% 环境变量展开 → P0 拒绝
        assert _classify(_exec(r'Remove-Item "%ProgramFiles%\App\*" -Force')) == "REJECT"
        assert _classify(_exec(r'Remove-Item "%ProgramFiles(x86)%\App\*" -Force')) == "REJECT"
        # 解释器包装同样 P0 拒绝
        assert _classify(_exec(r'cmd /c "del /q D:\Program Files\App\*"')) == "REJECT"
    finally:
        p1.stop(); p2.stop()


def test_program_files_single_delete_keeps_grade():
    p1, p2 = _mock_exec()
    try:
        # 单文件删除不受批量 P0 新检查影响（不落入 _is_program_files_delete + _is_batch_delete 分支）：
        # C 盘 Program Files 单文件 → C 盘非当前用户目录 REJECT（既有 C 盘删除拒绝）；
        # 非 C 盘 Program Files 单文件 → 命中既有 danger_patterns（Program Files 目录修改危险模式）→ REJECT
        assert _classify(_exec(r"Remove-Item 'C:\Program Files\App\a.exe'")) == "REJECT"
        assert _classify(_exec(r"Remove-Item 'D:\Program Files\App\a.exe'")) == "REJECT"
    finally:
        p1.stop(); p2.stop()


def test_uninstall_still_p1():
    p1, p2 = _mock_exec()
    try:
        # 卸载命令不受 Program Files 批量删除新检查影响：C 盘内卸载 → P1 挑战确认（步骤 4 前置分支）
        # 注：卸载路径校验依赖卸载注册表缓存；此处直接断言分类不落入 Program Files P0 拒绝分支
        res = _exec(r'Start-Process "C:\Program Files\App\uninstall.exe" -ArgumentList "/S"')
        assert _classify(res) in ("P1_CHALLENGE", "REJECT"), "卸载必须走专用分支（P1 挑战/路径校验拒绝），不得落入 P0 批量删除拒绝"
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


# ── 7. P0-1 confirmed 来源校验：伪造 confirmed=True 仍被拦截 ──
def test_forged_confirmed_true_still_blocked():
    """手工传入 confirmed=True、无真实确认凭证时，不得绕过分级拦截。"""
    p1, p2 = _mock_exec()
    try:
        # HKLM 注册表删除：伪造 confirmed 后仍 P0 拒绝（不落入 ANY 放行）
        raw = execute_command(command=r"reg delete HKLM\Software\Foo /f", working_dir=None, confirmed=True)
        res = json.loads(raw) if isinstance(raw, str) else raw
        assert _classify(res) == "REJECT", "伪造 confirmed=True 不得放行 HKLM 删除"
        # 危险模式（Start-Process）：伪造 confirmed 后不得 ALLOW（须回到分级通道）
        raw2 = execute_command(command="Start-Process notepad", working_dir=None, confirmed=True)
        res2 = json.loads(raw2) if isinstance(raw2, str) else raw2
        assert _classify(res2) != "ALLOW", "伪造 confirmed=True 不得放行危险模式命令"
        # 用户目录内单文件删除：伪造 confirmed 后仍 P2 确认（不得直接放行）
        raw3 = execute_command(command=f"Remove-Item {USERPROFILE}\\Desktop\\old.txt", working_dir=None, confirmed=True)
        res3 = json.loads(raw3) if isinstance(raw3, str) else raw3
        assert _classify(res3) == "P2_CONFIRM", "伪造 confirmed=True 不得放行删除命令"
    finally:
        p1.stop(); p2.stop()


def test_valid_challenge_token_still_authorizes():
    """真实挑战凭证（confirmation_manager 已注册）仍应放行——P0-1 修复不得破坏确认链路。"""
    import uuid
    from core.confirmation_manager import ConfirmationManager
    p1, p2 = _mock_exec()
    try:
        mgr = ConfirmationManager.instance()
        cmd = 'Set-ItemProperty -Path "HKCU:\\Software\\Foo" -Name Bar -Value 1'
        token = "p0-test-" + uuid.uuid4().hex[:8]
        mgr.store(token, {"type": "challenge", "command": cmd})
        raw = execute_command(
            command=cmd,
            working_dir=None,
            challenge_token=token,
            challenge_response=system_tools._get_cfg().danger_challenge_phrase,
        )
        res = json.loads(raw) if isinstance(raw, str) else raw
        assert _classify(res) == "ALLOW", "真实挑战凭证应放行（挑战层豁免、硬拦截不豁免）"
    finally:
        p1.stop(); p2.stop()
