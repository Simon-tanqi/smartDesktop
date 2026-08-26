# -*- coding: utf-8 -*-
"""agent_state 单测：检查点序列化 / 状态机 / 记忆注入 / 确认信号。

覆盖目标（core/agent_state.py）：
- TaskCheckpoint：to_dict / from_dict 往返（plan 为 None 与 dict 两种形态）
- AgentState：合法/非法状态迁移、status 禁止直接修改、deepcopy 隔离、超时判定
- GlobalStateManager：单例、create_turn_state、状态委托
- SessionDomainState / AgentExecutionState：域集合与执行状态重置
- 记忆链路：inject_memory_if_needed（幂等）、_build_lightweight_memory、
  _infer_current_focus、_extract_key_findings、_truncate_str、_truncate_keep_tail
- 确认信号：_is_user_confirmation、_detect_confirmation_signal
- build_user_content：预算截断与确认信号注入
"""
import copy
import json

import pytest

import core.agent_state as agent_state
from core.agent_state import (
    AgentExecutionState,
    AgentState,
    AgentTurnState,
    GlobalStateManager,
    SessionDomainState,
    TaskCheckpoint,
    _build_lightweight_memory,
    _detect_confirmation_signal,
    _extract_key_findings,
    _infer_current_focus,
    _is_user_confirmation,
    _truncate_keep_tail,
    _truncate_str,
    build_user_content,
    inject_memory_if_needed,
)


# ─────────────────────── TaskCheckpoint ───────────────────────

class TestTaskCheckpoint:
    def test_to_dict_json_encodes_complex_fields(self):
        cp = TaskCheckpoint(
            session_id="sess-1",
            user_task="任务",
            messages=[{"role": "user", "content": "你好"}],
            plan=None,
        )
        data = cp.to_dict()
        assert json.loads(data["messages"]) == [{"role": "user", "content": "你好"}]
        assert data["plan"] == "null"

    def test_from_dict_roundtrip(self):
        cp = TaskCheckpoint(
            session_id="sess-1",
            trace_id="trace-1",
            user_task="生成报告",
            turn=3,
            current_step=2,
            messages=[{"role": "user", "content": "继续"}],
            completed_steps=[{"step": 1}],
            remaining_steps=["step2", "step3"],
            current_tool_name="write_file",
            current_tool_args={"path": "D:\\a.md"},
            progress_summary="完成一半",
            created_at="2026-08-26 10:00:00",
            plan={"name": "planA"},
        )
        restored = TaskCheckpoint.from_dict(cp.to_dict())
        assert restored.session_id == "sess-1"
        assert restored.user_task == "生成报告"
        assert restored.turn == 3
        assert restored.messages == [{"role": "user", "content": "继续"}]
        assert restored.remaining_steps == ["step2", "step3"]
        assert restored.current_tool_args == {"path": "D:\\a.md"}

    def test_plan_none_roundtrip(self):
        cp = TaskCheckpoint(session_id="s", user_task="t", plan=None)
        restored = TaskCheckpoint.from_dict(cp.to_dict())
        assert restored.plan is None


# ─────────────────────── AgentState 状态机 ───────────────────────

class TestAgentState:
    def test_initial_idle(self):
        state = AgentState()
        assert state.get_state()["status"] == "IDLE"

    def test_valid_transition(self):
        state = AgentState()
        state.transition_to("RUNNING")
        assert state.get_state()["status"] == "RUNNING"
        state.transition_to("COMPLETED")
        assert state.get_state()["status"] == "COMPLETED"

    def test_invalid_transition_raises(self):
        state = AgentState()
        state.transition_to("RUNNING")
        with pytest.raises(ValueError):
            state.transition_to("IDLE")  # RUNNING 不可直接回 IDLE

    def test_update_state_blocks_status(self):
        state = AgentState()
        with pytest.raises(ValueError):
            state.update_state(status="RUNNING")

    def test_update_state_other_fields(self):
        state = AgentState()
        state.update_state(request={"task": "x"})
        assert state.get_request() == {"task": "x"}

    def test_get_state_deepcopy_isolation(self):
        state = AgentState()
        snapshot = state.get_state()
        snapshot["status"] = "MUTATED"
        assert state.get_state()["status"] == "IDLE"

    def test_get_request_deepcopy_isolation(self):
        state = AgentState()
        state.update_state(request={"nested": {"a": 1}})
        req = state.get_request()
        req["nested"]["a"] = 999
        assert state.get_request()["nested"]["a"] == 1

    def test_timeout_check(self):
        state = AgentState()
        assert state._check_timeout() is False
        # 模拟已过期
        state.created_at = 0
        assert state._check_timeout() is True


# ─────────────────────── GlobalStateManager ───────────────────────

class TestGlobalStateManager:
    def test_singleton(self):
        assert GlobalStateManager() is GlobalStateManager()

    def test_create_turn_state(self):
        gsm = GlobalStateManager()
        ts = gsm.create_turn_state("任务")
        assert isinstance(ts, AgentTurnState)
        assert ts.turn == -1
        assert ts.user_task == "任务"

    def test_transition_delegation(self):
        gsm = GlobalStateManager()
        gsm.create_state()
        gsm.transition_to("RUNNING")
        assert gsm.get_state()["status"] == "RUNNING"

    def test_get_active_sessions(self):
        gsm = GlobalStateManager()
        gsm.create_state()
        sessions = gsm.get_active_sessions()
        assert isinstance(sessions, list)
        assert sessions[0]["status"] == "IDLE"


# ─────────────────── SessionDomainState / AgentExecutionState ───────────────────

class TestSessionDomainState:
    def test_add_get_clear(self):
        sds = SessionDomainState()
        sds.add_domain("s1", "file")
        sds.add_domain("s1", "file")  # 去重
        sds.add_domain("s1", "web")
        assert sds.get_domains("s1") == {"file", "web"}
        assert sds.get_domains("unknown") == set()
        sds.clear_domains("s1")
        assert sds.get_domains("s1") == set()


class TestAgentExecutionState:
    def test_reset(self):
        aes = AgentExecutionState()
        aes.retry_count = 5
        aes.consecutive_progress = 2
        aes.skills_injected.add("skill-a")
        aes.collected_steps.append({"s": 1})
        aes.reset()
        assert aes.retry_count == 0
        assert aes.consecutive_progress == 0
        assert aes.skills_injected == set()
        assert aes.collected_steps == []

    def test_history_deepcopy_protection(self):
        aes = AgentExecutionState()
        messages = [{"role": "user", "content": "你好"}]
        aes.set_history(messages)
        got = aes.get_history()
        got[0]["content"] = "被修改"
        assert aes.get_history()[0]["content"] == "你好"


# ─────────────────────── 记忆链路纯函数 ───────────────────────

class TestInjectMemoryIfNeeded:
    def test_injects_system_memory(self):
        messages = [{"role": "system", "content": "你是助手"},
                    {"role": "user", "content": "你好"}]
        result = inject_memory_if_needed(messages, {"long_term": "长期记忆"})
        assert len(result) == 3
        assert result[1]["role"] == "system"
        assert "[长期记忆]" in result[1]["content"]

    def test_idempotent_when_already_injected(self):
        messages = [
            {"role": "system", "content": "你是助手"},
            {"role": "system", "content": "[长期记忆]\n记忆内容"},
            {"role": "user", "content": "你好"},
        ]
        result = inject_memory_if_needed(messages, {"long_term": "新的记忆"})
        assert result is messages  # 幂等：原列表直接返回

    def test_no_memory_unchanged(self):
        messages = [{"role": "user", "content": "你好"}]
        assert inject_memory_if_needed(messages, {}) == messages


class TestLightweightMemory:
    def test_empty_history(self):
        result = _build_lightweight_memory([])
        assert result["recent_history"] == []
        assert result["current_task_focus"] == "继续执行当前任务"

    def test_recent_rounds_trimmed(self):
        history = [
            {"round_index": i, "user_task": f"任务{i}", "ai_reply": f"回复{i}"}
            for i in range(6)
        ]
        result = _build_lightweight_memory(history, max_rounds=3)
        assert len(result["recent_history"]) == 3
        assert result["recent_history"][-1]["round"] == 5

    def test_truncate_long_fields(self):
        history = [{"round_index": 1, "user_task": "长" * 500, "ai_reply": "长" * 500}]
        result = _build_lightweight_memory(history)
        assert len(result["recent_history"][0]["user_task"]) <= 200
        assert len(result["recent_history"][0]["ai_reply"]) <= 200

    def test_infer_current_focus_waiting(self):
        rounds = [{"status": "waiting_confirmation",
                   "artifacts": {"pending_action": "请确认删除"}}]
        assert _infer_current_focus(rounds) == "请确认删除"

    def test_infer_current_focus_in_progress(self):
        rounds = [{"status": "in_progress", "subtask_status": "正在写文件"}]
        assert _infer_current_focus(rounds) == "正在写文件"

    def test_infer_current_focus_default(self):
        assert _infer_current_focus([]) == "继续执行当前任务"
        assert _infer_current_focus([{"status": "completed", "user_task": ""}]) == "继续执行当前任务"


class TestExtractKeyFindings:
    def test_extract_web_search_findings(self):
        entry = {
            "tool_calls": [{
                "tool_name": "web_search",
                "tool_result": json.dumps({
                    "results": [
                        {"title": "标题A", "snippet": "摘要A"},
                        {"title": "标题B", "snippet": "摘要B"},
                    ]
                }),
            }]
        }
        findings = _extract_key_findings(entry)
        assert len(findings) == 2
        assert findings[0] == "标题A"

    def test_non_web_search_ignored(self):
        entry = {"tool_calls": [{"tool_name": "write_file", "tool_result": "{}"}]}
        assert _extract_key_findings(entry) == []

    def test_invalid_json_returns_empty(self):
        entry = {"tool_calls": [{"tool_name": "web_search", "tool_result": "not-json"}]}
        assert _extract_key_findings(entry) == []


class TestTruncateHelpers:
    def test_truncate_str(self):
        assert _truncate_str("abcde", 3) == "abc"
        assert _truncate_str("abc", 10) == "abc"
        # 非字符串输入转为字符串处理（str(None)="None" 截前 3 位）
        assert _truncate_str(None, 3) == "Non"
        assert _truncate_str(12345, 3) == "123"

    def test_truncate_keep_tail(self):
        assert _truncate_keep_tail("abc", 10) == "abc"
        result = _truncate_keep_tail("a" * 100, 20)
        assert len(result) < 100
        assert result.endswith("a" * 10)
        assert result.startswith("a" * 10)
        assert "内容过长已截断" in result


# ─────────────────────── 确认信号 ───────────────────────

class TestUserConfirmation:
    @pytest.mark.parametrize("text", [
        "确认", "可以", "好的", "同意", "没问题", "OK", "ok", "是的",
        "确认，采用方案一", "可以，继续", "就按这个来",
    ])
    def test_positive(self, text):
        assert _is_user_confirmation(text) is True

    @pytest.mark.parametrize("text", [
        "", "   ", "请确认一下方案", "帮我确认下时间", "确认书在哪里",
    ])
    def test_negative(self, text):
        assert _is_user_confirmation(text) is False


class TestDetectConfirmationSignal:
    def test_non_confirmation_empty(self):
        assert _detect_confirmation_signal({}, "普通消息") == ""

    def test_confirmation_with_waiting_prev(self):
        layered = {
            "recent_structured": json.dumps([
                {"task_status": "waiting_confirmation", "user_task": "前一轮"},
                {"task_status": "running", "user_task": "当前轮"},
            ])
        }
        signal = _detect_confirmation_signal(layered, "确认")
        assert "已确认" in signal

    def test_confirmation_without_waiting_prev(self):
        layered = {
            "recent_structured": json.dumps([
                {"task_status": "completed", "user_task": "前一轮"},
                {"task_status": "running", "user_task": "当前轮"},
            ])
        }
        assert _detect_confirmation_signal(layered, "确认") == ""

    def test_invalid_json_empty(self):
        assert _detect_confirmation_signal({"recent_structured": "not-json"}, "确认") == ""


# ─────────────────────── build_user_content ───────────────────────

class TestBuildUserContent:
    def test_basic(self):
        content = build_user_content(None, "生成周报", [])
        assert "【任务】" in content
        assert "生成周报" in content

    def test_confirmation_signal_prepended(self):
        content = build_user_content(None, "生成周报", [], confirmation_signal="[系统] 已确认")
        assert content.startswith("[系统] 已确认")

    def test_thinking_summaries_included(self):
        summaries = [{"current_intent": "搜索资料", "decision_rationale": "需要数据"}]
        content = build_user_content(None, "生成周报", summaries)
        assert "【思考轨迹】" in content

    def test_long_user_message_truncated(self):
        content = build_user_content(None, "长" * 20000, [])
        assert "该段已截断" in content
        assert len(content) <= 12000 + 100
