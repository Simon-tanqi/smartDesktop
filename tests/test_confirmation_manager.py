# -*- coding: utf-8 -*-
"""ConfirmationManager 回归测试：挑战短语必须逐字一致，泛化词一律拒绝。

背景：verify_challenge 曾用 `any(kw in response for kw in APPROVE_KEYWORDS)` 兜底，
导致用户只需回复 "ok"/"确认" 即可放行 P1 高危操作，挑战确认形同虚设。
本文件锁定修复后的语义，防止回归。
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.confirmation_manager import ConfirmationManager  # noqa: E402

PHRASE = "我知晓此操作为危险操作，确认执行。"


def _manager_with_token(command="Remove-Item C:\\tmp\\demo.txt"):
    """新建独立实例并签发一个挑战令牌，避免单例状态串扰。"""
    mgr = ConfirmationManager()
    token = mgr.create_challenge(command=command, desc="删除文件", source="powershell")
    return mgr, token


def test_exact_phrase_passes():
    """逐字输入挑战短语应通过。"""
    mgr, token = _manager_with_token()
    entry = mgr.verify_challenge(token, PHRASE, PHRASE)
    assert entry is not None
    assert entry["command"] == "Remove-Item C:\\tmp\\demo.txt"


def test_surrounding_whitespace_and_case_ignored():
    """仅忽略首尾空白与大小写，中间内容不得省略。"""
    mgr, token = _manager_with_token()
    assert mgr.verify_challenge(token, f"  {PHRASE}  ", PHRASE) is not None


def test_generic_approval_words_rejected():
    """ok / yes / 确认 / 同意 等泛化词必须拒绝（历史漏洞）。"""
    for bad in ("ok", "OK", "yes", "confirm", "确认", "同意", "允许", "是"):
        mgr, token = _manager_with_token()
        assert mgr.verify_challenge(token, bad, PHRASE) is None, f"泛化词 {bad!r} 不应通过"


def test_wrong_phrase_rejected():
    """错误短语应拒绝。"""
    mgr, token = _manager_with_token()
    assert mgr.verify_challenge(token, "我确认执行", PHRASE) is None
    assert mgr.verify_challenge(token, PHRASE[:-1], PHRASE) is None


def test_token_is_single_use():
    """令牌一次性：成功使用后再次校验必须失败。"""
    mgr, token = _manager_with_token()
    assert mgr.verify_challenge(token, PHRASE, PHRASE) is not None
    assert mgr.verify_challenge(token, PHRASE, PHRASE) is None


def test_token_burned_after_failed_attempt():
    """校验失败同样消耗令牌，防止暴力重试。"""
    mgr, token = _manager_with_token()
    assert mgr.verify_challenge(token, "ok", PHRASE) is None
    assert mgr.verify_challenge(token, PHRASE, PHRASE) is None


def test_expired_token_rejected():
    """超过 TTL 的令牌应在 pop 阶段失效。"""
    mgr, token = _manager_with_token()
    mgr._pending[token]["_expiry"] = time.time() - 1
    assert mgr.verify_challenge(token, PHRASE, PHRASE) is None


def test_confirm_token_cannot_be_used_as_challenge():
    """普通确认令牌不能冒充挑战令牌。"""
    mgr = ConfirmationManager()
    confirm_token = mgr.create_confirmation("dir")
    assert mgr.verify_challenge(confirm_token, PHRASE, PHRASE) is None


def test_pending_requests_snapshot_is_copy():
    """查询接口返回副本，外部修改不影响内部状态。"""
    mgr, token = _manager_with_token()
    pending = mgr.get_pending_requests()
    assert len(pending) == 1
    pending[0]["command"] = "tampered"
    assert mgr.get_pending_requests()[0]["command"] == "Remove-Item C:\\tmp\\demo.txt"
