# -*- coding: utf-8 -*-
"""P1-1 校验跳过原因（validation_skip_reason）上下文化 + P1-3 副作用日志落盘 + P1-2 路径相对化。

覆盖：
1. P1-1 上下文化：裸布尔 _skip_validation 已替换为显式来源枚举 validation_skip_reason。
   - 未传原因：默认不跳过，L0 路径保护仍硬拦截（回归）。
   - 未知原因（如手工伪造 "attacker_flag"）：忽略并回退正常校验，仍被拦截。
   - 白名单原因 "upper_layer_verified"：跳过 L0/L2（路径保护、危险模式），命令放行执行。
   - system_tools.execute_command 内部调用 safe_exec.run_powershell 时
     显式携带 validation_skip_reason="upper_layer_verified"（两处调用点均已落地）。
   - P0-1 凭证绑定：手工传入 confirmed=True / domain_confirmed=True 且无有效确认凭证
     （challenge_token / confirmation_token）时，两者均被忽略并回退未确认分级通道；
     伪造 domain_confirmed 不得豁免 P1 分级、不得绕过 P0 硬拦截。
2. P1-3 副作用日志 JSONL 持久化：
   - record 同步追加写 JSONL；新实例启动加载合并去重；序号跨进程序号单调；clear 清空磁盘。
3. P1-2 测试路径相对化：tests/run_tests.py 的 BASE_DIR/PROJECT_ROOT 由 __file__ 推导，
   不残留本机绝对路径硬编码。

安全设计：应拦截命令在真实执行前即 return；应放行命令通过 mock 替换执行通道，不产生真实副作用。
"""
import json
import os
import pathlib
import re
import sys
import tempfile
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# 先初始化 core 包再导入 tools，规避 tools/__init__ 循环导入（与项目既有入口一致）
import core.tool_data_provider  # noqa: F401
from tools import safe_exec, system_tools
from tools.safe_exec import run_powershell
from tools.side_effect_journal import SideEffectJournal
from tools.system_tools import execute_command

USERPROFILE = os.environ.get("USERPROFILE", r"C:\Users\UnknownUser")
SSH_KEY = os.path.join(USERPROFILE, ".ssh", "id_rsa")


def _mock_subprocess_run():
    """mock run_powershell 的底层 subprocess.run：不产生真实命令副作用。"""
    fake = mock.Mock(returncode=0, stdout="ok", stderr="")
    p = mock.patch.object(safe_exec.subprocess, "run", return_value=fake)
    p.start()
    return p


def _mock_exec():
    """mock execute_command 的执行通道。"""
    patcher1 = mock.patch.object(system_tools, "_run_command_threaded",
                                 return_value={"success": True, "output": "MOCKED_THREAD"})
    patcher2 = mock.patch.object(system_tools.safe_exec, "run_powershell",
                                 return_value={"success": True, "output": "MOCKED_PS"})
    patcher1.start()
    patcher2.start()
    return patcher1, patcher2


# ── P1-1：validation_skip_reason 上下文化 ──

def test_no_reason_still_blocks_protected_path():
    """未传 validation_skip_reason：默认不跳过，.ssh 等受保护路径修改仍被 L0 硬拦截。"""
    res = run_powershell(f'Remove-Item "{SSH_KEY}"', working_dir=None)
    assert res.get("success") is False, f"受保护路径应被拦截，实际 {res}"
    assert res.get("stop") is True, "L0 路径保护应为硬拒绝（stop）"


def test_unknown_reason_ignored_and_still_blocked():
    """未知/伪造 skip 原因：忽略并回退正常校验，受保护路径修改仍被拦截。"""
    res = run_powershell(f'Remove-Item "{SSH_KEY}"', working_dir=None,
                         validation_skip_reason="attacker_flag")
    assert res.get("success") is False, "未知原因不得放行受保护路径"
    assert res.get("stop") is True


def test_upper_layer_verified_skips_l0_l2():
    """白名单原因 upper_layer_verified：跳过 L0/L2（路径保护/危险模式），命令放行执行。"""
    p = _mock_subprocess_run()
    try:
        res = run_powershell(f'Remove-Item "{SSH_KEY}"', working_dir=None,
                             validation_skip_reason="upper_layer_verified")
        assert res.get("success") is True, f"白名单原因应放行，实际 {res}"
        safe_exec.subprocess.run.assert_called()
    finally:
        p.stop()


def test_execute_command_forwards_skip_reason():
    """system_tools.execute_command 内部调用 run_powershell 须显式携带白名单原因。"""
    p1, p2 = _mock_exec()
    try:
        raw = execute_command(command=f'Set-Content {USERPROFILE}\\Desktop\\p1_test.txt "x"',
                              working_dir=None)
        res = json.loads(raw) if isinstance(raw, str) else raw
        assert res.get("success") is True
        call = system_tools.safe_exec.run_powershell.call_args
        assert call is not None, "execute_command 应调用 safe_exec.run_powershell"
        assert call.kwargs.get("validation_skip_reason") == "upper_layer_verified", \
            f"应显式传白名单原因，实际 {call.kwargs.get('validation_skip_reason')}"
        assert call.kwargs.get("domain_confirmed") is False, "无确认凭证时不得声称域确认"
    finally:
        p1.stop(); p2.stop()


def _exec_raw(cmd, **extra):
    raw = execute_command(command=cmd, working_dir=None, **extra)
    return json.loads(raw) if isinstance(raw, str) else raw


def _classify(res):
    if res.get("success") is False or res.get("valid") is False:
        if res.get("confirmation_required"):
            return "P1_CHALLENGE" if res.get("challenge_phrase") else "P2_CONFIRM"
        return "BLOCK" if res.get("stop") else "REJECT"
    return "ALLOW"


def test_forged_domain_confirmed_no_credential_still_challenges_p1():
    """伪造 domain_confirmed=True 且无有效确认凭证：P1 系统级命令不得被裸布尔豁免。

    P0-1 修复：domain_confirmed 与 confirmed 相同，必须绑定 challenge_token /
    confirmation_token 才能生效；手工传入的裸布尔被忽略并按未确认分级处理。
    taskkill /F（P1 挑战）应仍返回挑战短语，而非直接放行。
    """
    p1, p2 = _mock_exec()
    try:
        res = _exec_raw("taskkill /F /IM notepad.exe", domain_confirmed=True)
        assert _classify(res) == "P1_CHALLENGE", f"伪造 domain_confirmed 不得豁免 P1，实际 {res}"
        assert res.get("challenge_token"), "应返回挑战令牌"
    finally:
        p1.stop(); p2.stop()


def test_forged_domain_confirmed_no_credential_still_blocks_p0():
    """伪造 domain_confirmed=True 且无有效确认凭证：P0 硬拦截（危险模式）不受影响。"""
    p1, p2 = _mock_exec()
    try:
        res = _exec_raw(r"reg add HKLM\Software\Foo /v Bar /d 1", domain_confirmed=True)
        assert _classify(res) == "REJECT", f"伪造 domain_confirmed 不得绕过 P0 硬拦截，实际 {res}"
        assert "拒绝" in res.get("message", ""), "P0 危险模式拒绝应返回明确拒绝文案"
    finally:
        p1.stop(); p2.stop()


# ── P1-3：副作用日志 JSONL 持久化 ──

def test_journal_jsonl_persistence_and_reload():
    """record 追加写 JSONL；新实例加载合并去重；序号跨进程序号单调；clear 清空磁盘。"""
    tmp = tempfile.mkdtemp(prefix="journal_p1_")
    journal_path = os.path.join(tmp, "side_effect_journal.jsonl")
    try:
        j = SideEffectJournal(max_records=50, journal_path=journal_path)
        seq1 = j.record("modify_registry", {"key": r"HKCU\Software\Foo", "value": 1},
                        "测试注册表写入", compensate=lambda: "ok")
        seq2 = j.record("delete_file", {"path": r"C:\Users\Foo\Desktop\old.txt"},
                        "测试文件删除", compensate=lambda: "ok")
        assert seq1 == 1 and seq2 == 2

        # 磁盘 JSONL 存在且为追加写（两行）
        assert os.path.exists(journal_path), "record 后 JSONL 应落盘"
        with open(journal_path, "r", encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
        assert len(lines) == 2, f"应追加写 2 行，实际 {len(lines)}"
        first = json.loads(lines[0])
        assert first["tool_name"] == "modify_registry" and first["sequence"] == 1
        assert "created_at" in first and "arguments" in first

        # 新实例加载：合并去重、按序号升序
        j2 = SideEffectJournal(max_records=50, journal_path=journal_path)
        assert j2.count() == 2
        seqs = [r["sequence"] for r in j2.list_records()]
        assert seqs == [1, 2]

        # 序号跨进程序号单调：新实例首次 record 续接
        seq3 = j2.record("edit_file", {"path": r"C:\Users\Foo\Desktop\a.txt"},
                         "测试文件编辑", compensate=lambda: "ok")
        assert seq3 == 3

        # clear：内存 + 磁盘审计文件一并清空
        assert j2.clear() >= 3
        assert not os.path.exists(journal_path) or os.path.getsize(journal_path) == 0
    finally:
        for path in (journal_path, tmp):
            try:
                if os.path.isfile(path):
                    os.remove(path)
                elif os.path.isdir(path):
                    os.rmdir(path)
            except OSError:
                pass


def test_journal_persist_failure_degrades_to_memory():
    """磁盘写入失败（目标路径被普通文件占据，目录无法创建）时降级为纯内存，不阻断主流程。"""
    tmp = tempfile.mkdtemp(prefix="journal_degrade_")
    blocker = os.path.join(tmp, "blocker.txt")
    with open(blocker, "w", encoding="utf-8") as f:
        f.write("x")
    try:
        j = SideEffectJournal(max_records=10, journal_path=os.path.join(blocker, "journal.jsonl"))
        seq = j.record("modify_registry", {"key": r"HKCU\Foo"}, "降级测试", compensate=lambda: "ok")
        assert seq == 1
        assert j.count() == 1
    finally:
        for path in (blocker, tmp):
            try:
                if os.path.isfile(path):
                    os.remove(path)
                elif os.path.isdir(path):
                    os.rmdir(path)
            except OSError:
                pass


# ── P1-2：测试路径相对化 ──

def test_run_tests_paths_are_relative():
    """tests/run_tests.py 路径常量须由 __file__ 推导，不残留本机绝对路径硬编码。"""
    src_path = pathlib.Path(__file__).resolve().parents[1] / "tests" / "run_tests.py"
    text = src_path.read_text(encoding="utf-8")
    assert "BASE_DIR = os.path.dirname(os.path.abspath(__file__))" in text
    assert "PROJECT_ROOT = os.path.dirname(BASE_DIR)" in text
    assert "D:\\PyCharm" not in text and "D:/PyCharm" not in text, "残留本机绝对路径硬编码"
    # 路径常量定义不得直接引用盘符绝对路径
    for m in re.finditer(r"^([A-Z_]+)\s*=\s*['\"][A-Za-z]:\\\\", text, re.MULTILINE):
        raise AssertionError(f"路径常量 {m.group(1)} 仍为盘符绝对路径硬编码: {m.group(0)}")
