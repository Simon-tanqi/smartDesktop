# -*- coding: utf-8 -*-
"""core/react_engine.py 冒烟测试 + 熔断/循环检测纯逻辑测试。

ReAct 主循环通过 mock 掉 llm_client 与工具执行层，用注入式 fake 驱动
「reasoning → action → final」完整一轮与多轮工具调用，验证事件流；
不依赖真实 LLM API，也不依赖 Windows 专属第三方包。
"""
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.tool_data_provider import (
    SearchCircuitBreaker,
    ToolLoopDetector,
    ToolLoopError,
    ToolLoopPolicy,
    compute_call_signature,
)
from core.react_engine import ReActEngine


# ═══════════════════════════════════════════════════════════════════
# SearchCircuitBreaker（core/tool_data_provider.py）
# ═══════════════════════════════════════════════════════════════════

class TestSearchCircuitBreaker:
    def test_closed_by_default(self):
        breaker = SearchCircuitBreaker(failure_threshold=3, cooldown_seconds=60)
        assert breaker.should_skip_search() is False

    def test_opens_after_threshold_failures(self):
        breaker = SearchCircuitBreaker(failure_threshold=3, cooldown_seconds=60)
        breaker.record_result(False, False)
        breaker.record_result(False, False)
        assert breaker.should_skip_search() is False  # 未达阈值
        breaker.record_result(False, False)
        assert breaker.should_skip_search() is True  # 已熔断且未过冷却期

    def test_success_closes_breaker(self):
        breaker = SearchCircuitBreaker(failure_threshold=3, cooldown_seconds=60)
        breaker.record_result(False, False)
        breaker.record_result(False, False)
        breaker.record_result(True, True)
        assert breaker.should_skip_search() is False

    def test_low_relevance_counts_as_failure(self):
        breaker = SearchCircuitBreaker(failure_threshold=2, cooldown_seconds=60)
        breaker.record_result(True, False)  # 成功但相关度低 → 仍计失败
        breaker.record_result(True, False)
        assert breaker.should_skip_search() is True

    def test_cooldown_opens_half_open(self):
        breaker = SearchCircuitBreaker(failure_threshold=1, cooldown_seconds=0.2)
        breaker.record_result(False, False)
        assert breaker.should_skip_search() is True  # 熔断刚开启，冷却未结束
        time.sleep(0.3)
        assert breaker.should_skip_search() is False  # 冷却结束 → half_open 放行

    def test_degradation_message(self):
        breaker = SearchCircuitBreaker()
        assert "降级" in breaker.get_degradation_message()


# ═══════════════════════════════════════════════════════════════════
# ToolLoopDetector（core/tool_data_provider.py）
# ═══════════════════════════════════════════════════════════════════

class TestToolLoopDetector:
    def test_exact_repeat_raises(self):
        detector = ToolLoopDetector(policy=ToolLoopPolicy(exact_repeat_limit=2))
        detector.check("scan_directory", {"path": "C:/"}, step_num=1)
        detector.check("scan_directory", {"path": "C:/"}, step_num=2)
        with pytest.raises(ToolLoopError):
            detector.check("scan_directory", {"path": "C:/"}, step_num=3)

    def test_same_tool_hard_limit_raises(self):
        detector = ToolLoopDetector(policy=ToolLoopPolicy(
            exact_repeat_limit=99, same_tool_hard_limit=2, exempt_tools=set()
        ))
        detector.check("write_file", {"a": 1}, step_num=1)
        detector.check("write_file", {"a": 2}, step_num=2)
        with pytest.raises(ToolLoopError):
            detector.check("write_file", {"a": 3}, step_num=3)

    def test_exempt_tool_not_hard_limited(self):
        detector = ToolLoopDetector(policy=ToolLoopPolicy(
            exact_repeat_limit=99,
            same_tool_hard_limit=2,
            exempt_tools={"web_search"},
        ))
        for i in range(5):
            detector.check("web_search", {"q": f"query-{i}"}, step_num=i + 1)
        # 豁免工具只记录，不抛异常

    def test_reset_clears_state(self):
        detector = ToolLoopDetector(policy=ToolLoopPolicy(exact_repeat_limit=2))
        detector.check("scan_directory", {"path": "C:/"}, step_num=1)
        detector.reset()
        # reset 后再次连续调用不会立即触发精确重复
        detector.check("scan_directory", {"path": "C:/"}, step_num=1)
        detector.check("scan_directory", {"path": "C:/"}, step_num=2)
        with pytest.raises(ToolLoopError):
            detector.check("scan_directory", {"path": "C:/"}, step_num=3)

    def test_warning_hint_for_repeated_tool(self):
        detector = ToolLoopDetector(policy=ToolLoopPolicy(exempt_tools=set()))
        for i in range(5):
            detector.check("write_file", {"n": i}, step_num=i + 1)
        hint = detector.get_warning_hint()
        assert hint is not None and "write_file" in hint

    def test_no_warning_hint_for_new_detector(self):
        detector = ToolLoopDetector()
        assert detector.get_warning_hint() is None

    def test_compute_call_signature_stability(self):
        s1 = compute_call_signature("scan_directory", {"path": "C:/", "depth": 2})
        s2 = compute_call_signature("scan_directory", {"depth": 2, "path": "C:/"})  # key 顺序无关
        s3 = compute_call_signature("scan_directory", {"path": "C:/", "depth": 3})
        assert s1 == s2
        assert s1 != s3


# ═══════════════════════════════════════════════════════════════════
# ReActEngine.run 冒烟测试（mock llm_client / 工具执行层）
# ═══════════════════════════════════════════════════════════════════

def _make_result(**overrides):
    """构造 PrepareResult 等价命名空间。"""
    base = dict(
        flow_control="proceed",
        state=SimpleNamespace(conversation_messages=[], final_reply=""),
        model_id="test-model",
        base_url="http://localhost:9999/v1",
        default_model="test-model",
        default_provider="openai",
        temperature=0.0,
        stream=False,
        call_seq=0,
        tools_param=[{"type": "function", "function": {"name": "scan_directory"}}],
        tank_logger=None,
        session_logger=None,
        remaining_steps=ReActEngine.MAX_TURNS,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_prepare_run_state(result):
    """生成 prepare_run_state 的 generator 替身：yield 一次事件后返回 result。"""
    def _fake(engine, rs, user_message, model_choice, resumed_checkpoint=None):
        yield "prepare-event"
        return result
    return _fake


def _make_call_llm_sequence(actions):
    """生成 call_llm_and_parse 的 generator 替身：按轮次顺序返回 action。

    actions 为 [(visible, action, native, flow), ...]，耗尽后重复最后一个。
    """
    state = {"i": 0}

    def _fake(engine, rs, state_ns, user_message, step, cancel_event, call_seq,
              tools_param, base_url, default_provider, default_model,
              model_id, temperature, stream, remaining_steps):
        idx = min(state["i"], len(actions) - 1)
        state["i"] += 1
        visible, action, native, flow = actions[idx]
        rs.full_content = visible
        rs.full_reasoning = ""
        yield "llm-event"
        return (flow, visible, action, native)
    return _fake


def _make_execute_one_step(captured=None):
    """生成 execute_one_step 的 generator 替身：记录 tool_call 后返回 proceed。"""
    def _fake(engine, rs, state, tc, step, full_content, full_reasoning,
              tl, sl, cancel_event, confirmation_handler, user_message,
              remaining_steps):
        if captured is not None:
            captured.append(tc)
        yield "exec-event"
        return "proceed"
    return _fake


def _make_safe_final(captured=None):
    def _fake(engine, state, text, **kwargs):
        if captured is not None:
            captured.append((text, kwargs))
        yield f"SAFE_FINAL|{text}"
    return _fake


def _make_do_final_cleanup(captured=None):
    def _fake(engine, state, user_message, remaining_steps, cancel_event,
              default_model, default_provider, model_id, temperature,
              final_reply_saved=False):
        if captured is not None:
            captured.append("cleanup-called")
        yield "cleanup-event"
    return _fake


def _make_save_checkpoint():
    def _fake(*args, **kwargs):
        yield "checkpoint-event"
    return _fake


def _run_engine(user_message, action, final_visible, captured=None,
                result_extra=None, cancel_event=None, actions_seq=None):
    """驱动一次 mock 后的 run()，返回事件列表。"""
    result = _make_result(**(result_extra or {}))
    engine = ReActEngine(trace_id="ut", session_id="ut-session")
    events = []
    # 默认单轮 final；actions_seq 可传入多轮 (visible, action, native, flow) 序列
    actions = actions_seq if actions_seq is not None else [(final_visible, action, None, "proceed")]
    with patch("core.react_engine.prepare_run_state", _make_prepare_run_state(result)), \
         patch("core.react_engine.call_llm_and_parse",
               _make_call_llm_sequence(actions)), \
         patch("core.react_engine.execute_one_step", _make_execute_one_step(captured)), \
         patch("core.react_engine.safe_final", _make_safe_final()), \
         patch("core.react_engine.do_final_cleanup", _make_do_final_cleanup(captured)), \
         patch("core.react_engine.save_checkpoint", _make_save_checkpoint()):
        gen = engine.run(user_message, model_choice={}, cancel_event=cancel_event)
        for event in gen:
            events.append(event)
    return events


class TestReActEngineRunSmoke:
    def test_single_final_reply(self):
        events = _run_engine(
            "你好",
            {"final_message": "你好，有什么可以帮你？"},
            "你好，有什么可以帮你？",
        )
        assert any(e.startswith("SAFE_FINAL|你好") for e in events)
        assert not any(e == "cleanup-event" for e in events)

    def test_reasoning_then_action_then_final(self):
        captured = []
        events = _run_engine(
            "扫描C盘",
            {"final_message": "任务完成"},
            "任务完成",
            captured=captured,
            actions_seq=[
                ("开始调用工具", {"tool_name": "scan_directory", "arguments": {"path": "C:/"}}, None, "proceed"),
                ("任务完成", {"final_message": "任务完成"}, None, "proceed"),
            ],
        )
        # 工具调用发生一次：scan_directory
        assert len(captured) >= 1
        assert captured[0]["function"]["name"] == "scan_directory"
        assert any(e.startswith("SAFE_FINAL|任务完成") for e in events)

    def test_cancel_signal_triggers_immediate_final(self):
        captured = []
        cancel_event = threading.Event()
        cancel_event.set()
        result = _make_result()
        engine = ReActEngine(trace_id="ut", session_id="ut-session")
        events = []
        with patch("core.react_engine.prepare_run_state", _make_prepare_run_state(result)), \
             patch("core.react_engine.safe_final", _make_safe_final()), \
             patch("core.react_engine.do_final_cleanup", _make_do_final_cleanup(captured)), \
             patch("core.react_engine.save_checkpoint", _make_save_checkpoint()):
            for event in engine.run("随便聊聊", model_choice={}, cancel_event=cancel_event):
                events.append(event)
        assert any(e.startswith("SAFE_FINAL|") for e in events)
        assert "cleanup-called" not in captured  # 取消路径不进入收尾

    def test_return_flow_control_stops(self):
        result = _make_result(flow_control="return")
        engine = ReActEngine(trace_id="ut", session_id="ut-session")
        events = []
        with patch("core.react_engine.prepare_run_state", _make_prepare_run_state(result)):
            for event in engine.run("恢复检查点", model_choice={}):
                events.append(event)
        # 仅透传 prepare 事件后静默返回，不进入主循环
        assert events == ["prepare-event"]

    def test_risk_levels_mapping_sane(self):
        # P0 永远拒绝（硬 BLOCK，绝不弹确认框）；静态表值仅作动态定级兜底：
        # - delete_file 实际按目标路径/数量动态定级（C 盘非用户目录→P0、批量→P1、单文件→P2）
        # - modify_registry 实际按根键动态定级（HKLM 等全局根键→P0、HKCU→P1）
        assert ReActEngine.RISK_LEVELS["delete_file"] == "P2"
        assert ReActEngine.RISK_LEVELS["modify_registry"] == "P1"
        assert ReActEngine.RISK_LEVELS["write_file"] == "P2"
        assert "web_search" not in ReActEngine.RISK_LEVELS  # 默认 P3
        # P0 不再由任何工具静态声明（P0 只能由动态定级命中，且一律直接拒绝）
        assert all(level != "P0" for level in ReActEngine.RISK_LEVELS.values())
