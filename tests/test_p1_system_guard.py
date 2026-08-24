# -*- coding: utf-8 -*-
"""P1 批命令拦截修复 - 针对性验证测试。

覆盖：
5. readonly 白名单移除 net/wmic：仅放行明确只读子命令（net view/net user 查询、
   wmic list/get 等）；net stop/start、net user /add、wmic terminate/call 等修改 → P1 挑战；
   首 token 命中 readonly 时携带管道/分号组合段 → 不得放行（沿用 P0 _HAS_COMBINED_SEGMENTS_RE）。
6. 系统级操作分级确认 → P1 挑战：
   sc config/delete/create/stop/start、Set-Service/New-Service、taskkill /F、
   bcdedit 写、netsh 修改、schtasks create/delete/change、icacls/takeown、wmic 修改。
7. 解释器入口带内联代码载荷（python -c / node -e / powershell -Command / cmd /c /
   bash -c 等）→ P1 挑战确认；python -m 模块运行（pip 等）不拦截。

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


# ── P1-5：net / wmic 只读子命令放行，修改语义拦截 ──
def test_net_readonly_query_allowed():
    p1, p2 = _mock_exec()
    try:
        assert _classify(_exec("net view")) == "ALLOW"
        assert _classify(_exec("net user")) == "ALLOW"
        assert _classify(_exec("net user username")) == "ALLOW"
        assert _classify(_exec("net accounts")) == "ALLOW"
        assert _classify(_exec("net statistics workstation")) == "ALLOW"
        assert _classify(_exec("net session")) == "ALLOW"
        assert _classify(_exec("net share")) == "ALLOW"
        assert _classify(_exec("net share C$")) == "ALLOW"  # 查询指定共享信息
    finally:
        p1.stop(); p2.stop()


def test_net_write_operations_challenge():
    p1, p2 = _mock_exec()
    try:
        assert _classify(_exec("net stop spooler")) == "P1_CHALLENGE"
        assert _classify(_exec("net start WSearch")) == "P1_CHALLENGE"
        assert _classify(_exec("net use Z: \\\\server\\share")) == "P1_CHALLENGE"
        assert _classify(_exec("net user username P@ss /add")) == "P1_CHALLENGE"
        assert _classify(_exec("net user username /delete")) == "P1_CHALLENGE"
        assert _classify(_exec("net share temp=C:\\tmp")) == "P1_CHALLENGE"
        assert _classify(_exec("net share temp /delete")) == "P1_CHALLENGE"
        assert _classify(_exec("net localgroup Administrators username /add")) == "P1_CHALLENGE"
    finally:
        p1.stop(); p2.stop()


def test_wmic_readonly_query_allowed():
    p1, p2 = _mock_exec()
    try:
        assert _classify(_exec("wmic process list brief")) == "ALLOW"
        assert _classify(_exec("wmic product get name,version")) == "ALLOW"
        assert _classify(_exec("wmic cpu get name")) == "ALLOW"
    finally:
        p1.stop(); p2.stop()


def test_wmic_modify_operations_challenge():
    p1, p2 = _mock_exec()
    try:
        assert _classify(_exec('wmic process where name="notepad.exe" call terminate')) == "P1_CHALLENGE"
        assert _classify(_exec("wmic process delete")) == "P1_CHALLENGE"
        # uninstall 载荷命中既有卸载前置检查（路径未缓存直接拒绝，比 P1 更严格）
        assert _classify(_exec("wmic product call uninstall")) == "REJECT"
    finally:
        p1.stop(); p2.stop()


# ── P1-6：系统级操作 → P1 挑战确认 ──
def test_sc_service_operations_challenge():
    p1, p2 = _mock_exec()
    try:
        assert _classify(_exec("sc config X start= disabled")) == "P1_CHALLENGE"
        assert _classify(_exec("sc delete X")) == "P1_CHALLENGE"
        assert _classify(_exec("sc create X binPath= C:\\x.exe")) == "P1_CHALLENGE"
        assert _classify(_exec("sc stop X")) == "P1_CHALLENGE"
        assert _classify(_exec("sc start X")) == "P1_CHALLENGE"
    finally:
        p1.stop(); p2.stop()


def test_taskkill_force_challenge():
    p1, p2 = _mock_exec()
    try:
        assert _classify(_exec("taskkill /F /IM notepad.exe")) == "P1_CHALLENGE"
        assert _classify(_exec("taskkill /F /PID 1234")) == "P1_CHALLENGE"
    finally:
        p1.stop(); p2.stop()


def test_powershell_service_cmdlets_challenge():
    p1, p2 = _mock_exec()
    try:
        assert _classify(_exec("Set-Service X -Status Stopped")) == "P1_CHALLENGE"
        assert _classify(_exec("New-Service X -BinaryPathName C:\\x.exe")) == "P1_CHALLENGE"
        assert _classify(_exec("Stop-Service X")) == "P1_CHALLENGE"
        assert _classify(_exec("Remove-Service X")) == "P1_CHALLENGE"
    finally:
        p1.stop(); p2.stop()


def test_bcdedit_write_challenge_enum_allowed():
    p1, p2 = _mock_exec()
    try:
        assert _classify(_exec("bcdedit /set {current} recoveryenabled no")) == "P1_CHALLENGE"
        assert _classify(_exec("bcdedit /delete {current} /f")) == "P1_CHALLENGE"
        assert _classify(_exec("bcdedit /enum")) == "ALLOW"  # 查询放行
        assert _classify(_exec("bcdedit /enum all")) == "ALLOW"
    finally:
        p1.stop(); p2.stop()


def test_netsh_modify_challenge_show_allowed():
    p1, p2 = _mock_exec()
    try:
        assert _classify(_exec("netsh interface set interface Ethernet disabled")) == "P1_CHALLENGE"
        assert _classify(_exec("netsh advfirewall set allprofiles state off")) == "P1_CHALLENGE"
        assert _classify(_exec("netsh interface ip add dns Ethernet 8.8.8.8")) == "P1_CHALLENGE"
        assert _classify(_exec("netsh interface show interface")) == "ALLOW"  # 查询放行
        assert _classify(_exec("netsh advfirewall show allprofiles")) == "ALLOW"
    finally:
        p1.stop(); p2.stop()


def test_schtasks_operations_challenge():
    p1, p2 = _mock_exec()
    try:
        assert _classify(_exec("schtasks /create /tn X /tr C:\\x.exe /sc daily")) == "P1_CHALLENGE"
        assert _classify(_exec("schtasks /delete /tn X /f")) == "P1_CHALLENGE"
        assert _classify(_exec("schtasks /change /tn X /ru SYSTEM")) == "P1_CHALLENGE"
        assert _classify(_exec("schtasks /query")) == "ALLOW"  # 查询放行
        assert _classify(_exec("schtasks /query /tn X")) == "ALLOW"
    finally:
        p1.stop(); p2.stop()


def test_acl_modify_challenge():
    p1, p2 = _mock_exec()
    try:
        assert _classify(_exec(r"icacls C:\Windows\foo /grant Users:F")) == "P1_CHALLENGE"
        assert _classify(_exec("takeown /f C:\\Windows\\foo")) == "P1_CHALLENGE"
        assert _classify(_exec("takeown /f C:\\foo /r /d y")) == "P1_CHALLENGE"
    finally:
        p1.stop(); p2.stop()


# ── P1-7：解释器入口内联载荷 → P1 挑战；python -m 模块运行不拦 ──
def test_inline_interpreter_challenge():
    p1, p2 = _mock_exec()
    try:
        assert _classify(_exec('python -c "import os; os.remove(\'D:\\\\a.txt\')"')) == "P1_CHALLENGE"
        assert _classify(_exec("node -e \"require('fs').unlinkSync('x')\"")) == "P1_CHALLENGE"
        assert _classify(_exec('powershell -Command "Remove-Item D:\\a.txt"')) == "P1_CHALLENGE"
        assert _classify(_exec('pwsh -c "Get-Process | Stop-Process"')) == "P1_CHALLENGE"
        assert _classify(_exec('cmd /c "del D:\\a.txt"')) == "P1_CHALLENGE"
        # 内联载荷含系统目录读取 → 命中既有敏感读拦截（REJECT，比 P1 更严格）
        assert _classify(_exec('cmd /c "dir C:\\Windows"')) == "REJECT"
        # rm -rf / 变体 → 命中 always_blocked 永久拦截（L0，比 P1 更严格）
        assert _classify(_exec("bash -c \"rm -rf /tmp/x\"")) == "REJECT"
        # 纯内联载荷（无更高优先级命中）→ P1 挑战确认
        assert _classify(_exec("bash -c \"echo hi && whoami\"")) == "P1_CHALLENGE"
        assert _classify(_exec("python -c \"print(1)\"")) == "P1_CHALLENGE"
    finally:
        p1.stop(); p2.stop()


def test_interpreter_module_run_allowed():
    p1, p2 = _mock_exec()
    try:
        # python -m 属模块运行（pip 安装等正常开发流程），不拦截
        assert _classify(_exec("python -m pip install flask")) == "ALLOW"
        assert _classify(_exec("python D:\\scripts\\run.py")) == "ALLOW"  # 脚本文件，非内联载荷
        assert _classify(_exec("node D:\\scripts\\server.js")) == "ALLOW"
    finally:
        p1.stop(); p2.stop()


# ── 正常命令不破坏：只读查询、普通文件操作照常 ──
def test_normal_commands_still_work():
    p1, p2 = _mock_exec()
    try:
        assert _classify(_exec("whoami")) == "ALLOW"
        assert _classify(_exec("dir C:\\Users")) == "ALLOW"
        assert _classify(_exec("Get-Process")) == "ALLOW"
        assert _classify(_exec("systeminfo")) == "ALLOW"
        assert _classify(_exec("echo hello")) == "ALLOW"
    finally:
        p1.stop(); p2.stop()
