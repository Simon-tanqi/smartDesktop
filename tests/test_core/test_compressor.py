# -*- coding: utf-8 -*-
"""compressor 单测：Token 估算 / 切割点 / 消息压缩 / 结构化摘要。

覆盖目标（memory/compressor.py）：
- estimate_tokens：CJK ~1.5 token/字、ASCII ~0.25 token/字、空输入为 0
- _find_cut_point：保留最近轮次、反向累加、硬上限约束
- compress_messages / check_and_compress_messages：阈值触发、system 保留、tool 配对不切断
- 结构化摘要链路：_extract_structured_fields / _parse_structured_summary /
  _legacy_summary_to_struct / _merge_with_fallback / increment_merge_summary（LLM 失败回退）
"""
import json

import pytest

from memory import compressor
from memory.compressor import (
    RECENT_TURNS_KEEP,
    TOKEN_TARGET,
    _extract_structured_fields,
    _find_cut_point,
    _legacy_summary_to_struct,
    _merge_with_fallback,
    _parse_structured_summary,
    check_and_compress_messages,
    compress_messages,
    estimate_tokens,
    increment_merge_summary,
)


def _mk(role, content, **extra):
    m = {"role": role, "content": content}
    m.update(extra)
    return m


def _mk_many(n, prefix="第"):
    """构造 n 轮 user/assistant 交替消息。"""
    messages = []
    for i in range(n):
        messages.append(_mk("user", f"{prefix}{i}轮用户消息内容"))
        messages.append(_mk("assistant", f"{prefix}{i}轮助手回复内容"))
    return messages


def _mk_long(n, prefix="很长的内容" * 200):
    """构造 n 轮长消息，确保 token 超限。"""
    messages = []
    for i in range(n):
        messages.append(_mk("user", f"{prefix}用户{i}"))
        messages.append(_mk("assistant", f"{prefix}助手{i}"))
    return messages


# ─────────────────────── estimate_tokens ───────────────────────

class TestEstimateTokens:
    def test_empty(self):
        assert estimate_tokens("") == 0
        assert estimate_tokens(None) == 0

    def test_ascii_quarter_per_char(self):
        # 11 个 ASCII 字符 → int(11 * 0.25) = 2
        assert estimate_tokens("hello world") == 2

    def test_chinese_one_and_half_per_char(self):
        # 4 个汉字 → int(4 * 1.5) = 6
        assert estimate_tokens("你好世界") == 6

    def test_floor_at_one(self):
        # 1 个 ASCII 字符 → int(0.25) = 0 → floor 到 1
        assert estimate_tokens("a") == 1

    def test_monotonic_with_length(self):
        assert estimate_tokens("a" * 200) > estimate_tokens("a" * 100)


# ─────────────────────── _find_cut_point ───────────────────────

class TestFindCutPoint:
    def test_empty_messages(self):
        assert _find_cut_point([], TOKEN_TARGET) == 0

    def test_no_user_messages(self):
        messages = [_mk("assistant", "只有助手消息")]
        assert _find_cut_point(messages, TOKEN_TARGET) == len(messages)

    def test_short_conversation_no_cut(self):
        messages = _mk_many(2)
        # 2 轮不足 RECENT_TURNS_KEEP，不应切割
        assert _find_cut_point(messages, TOKEN_TARGET) == len(messages)

    def test_long_conversation_cuts(self):
        messages = _mk_long(12)
        cut = _find_cut_point(messages, TOKEN_TARGET)
        assert 0 < cut < len(messages)

    def test_cut_preserves_recent_rounds(self):
        messages = _mk_long(12)
        cut = _find_cut_point(messages, TOKEN_TARGET)
        # 最近几轮消息必须保留在切割点之后
        recent_msgs = messages[cut:]
        assert messages[-1] in recent_msgs
        assert messages[-2] in recent_msgs

    def test_cut_never_exceeds_len(self):
        messages = _mk_long(8)
        cut = _find_cut_point(messages, TOKEN_TARGET)
        assert 0 <= cut <= len(messages)


# ─────────────────────── compress_messages ───────────────────────

class TestCompressMessages:
    def test_short_list_unchanged(self):
        messages = _mk_many(2)
        assert compress_messages(messages, target_tokens=100000) == messages

    def test_all_system_unchanged(self):
        messages = [_mk("system", "你是助手")]
        assert compress_messages(messages, target_tokens=100) == messages

    def test_compress_keeps_system_and_recent(self):
        messages = (
            [_mk("system", "你是助手")]
            + _mk_long(10)
        )
        result = compress_messages(messages, target_tokens=2000, session_id="")
        assert result != messages
        # system 消息保留在头部
        assert result[0]["role"] == "system"
        # 最近一条消息必须保留
        assert messages[-1] in result

    def test_compress_marks_summary(self):
        messages = _mk_long(10)
        result = compress_messages(messages, target_tokens=1000, session_id="")
        contents = [m.get("content", "") for m in result]
        assert any("[上下文压缩]" in c or "[历史压缩]" in c for c in contents)


# ─────────────────── check_and_compress_messages ───────────────────

class TestCheckAndCompress:
    def test_below_threshold_unchanged(self):
        messages = _mk_many(3)
        result = check_and_compress_messages(
            messages, max_context_tokens=100000, safety_margin=5000
        )
        assert result == messages

    def test_above_threshold_compressed(self):
        messages = _mk_long(10)
        result = check_and_compress_messages(
            messages, max_context_tokens=2000, safety_margin=100
        )
        contents = [m.get("content", "") for m in result]
        assert any("[上下文压缩]" in c or "[历史压缩]" in c for c in contents)


# ─────────────────── 结构化摘要：确定性提取 ───────────────────

class TestExtractStructuredFields:
    def test_workdir_and_paths_extracted(self):
        new_round = {
            "user_task": "把文件移到 D:\\Docs\\out",
            "ai_reply": "好的，已移动",
            "status": "completed",
            "tool_calls": [{
                "tool_name": "file_move",
                "tool_arguments": {
                    "source_path": "C:\\Temp\\a.txt",
                    "working_dir": "D:\\Docs\\out",
                },
                "tool_result": "ok",
            }],
        }
        result = _extract_structured_fields(new_round)
        kc = result["key_context"]
        assert kc["work_dir"] == "D:\\Docs\\out"
        assert "C:\\Temp\\a.txt" in kc["key_paths"]
        assert "file_move" in kc["key_params"]

    def test_user_task_kept_verbatim(self):
        task = "帮我修改配置文件 config.yaml 并把端口改为 8080"
        result = _extract_structured_fields({"user_task": task})
        assert result["tasks"][0]["user_task"] == task

    def test_empty_tool_calls_safe(self):
        result = _extract_structured_fields({"user_task": "任务"})
        assert result["tasks"][0]["user_task"] == "任务"
        assert result["tool_trace_brief"] == ""


# ─────────────────── 结构化摘要：解析与兼容 ───────────────────

class TestParseStructuredSummary:
    def test_plain_json(self):
        assert _parse_structured_summary('{"a": 1}') == {"a": 1}

    def test_fenced_json(self):
        text = "```json\n{\"a\": 1}\n```"
        assert _parse_structured_summary(text) == {"a": 1}

    def test_with_surrounding_text(self):
        text = "以下是结果：{\"a\": 1} 结束"
        assert _parse_structured_summary(text) == {"a": 1}

    def test_invalid_returns_none(self):
        assert _parse_structured_summary("not json") is None
        assert _parse_structured_summary("") is None
        assert _parse_structured_summary(None) is None


class TestLegacySummaryToStruct:
    def test_structured_passthrough(self):
        data = '{"tasks": [{"user_task": "x"}]}'
        result = _legacy_summary_to_struct(data)
        assert result["tasks"][0]["user_task"] == "x"

    def test_natural_language_to_shell(self):
        result = _legacy_summary_to_struct("旧的自然语言摘要")
        assert result["tool_trace_brief"] == "旧的自然语言摘要"
        assert result["key_context"] == {"work_dir": "", "key_paths": [], "key_params": {}}


class TestMergeWithFallback:
    def test_key_paths_union(self):
        old = {
            "key_context": {"work_dir": "D:\\a", "key_paths": ["C:\\1.txt"], "key_params": {}},
            "tasks": [], "unfinished_tasks": [], "user_forbidden": [],
            "decisions": [], "tool_trace_brief": "",
        }
        new = {
            "key_context": {"work_dir": "D:\\b", "key_paths": ["C:\\2.txt"], "key_params": {}},
            "tasks": [], "unfinished_tasks": [], "user_forbidden": [],
            "decisions": [], "tool_trace_brief": "",
        }
        merged = _merge_with_fallback(old, new, None)
        # work_dir 被新值覆盖，key_paths 做并集不丢失旧路径
        assert merged["key_context"]["work_dir"] == "D:\\b"
        assert "C:\\1.txt" in merged["key_context"]["key_paths"]
        assert "C:\\2.txt" in merged["key_context"]["key_paths"]

    def test_llm_parsed_supplements(self):
        old = {"key_context": {}, "tasks": [], "unfinished_tasks": [],
               "user_forbidden": [], "decisions": [], "tool_trace_brief": ""}
        new = {"key_context": {}, "tasks": [], "unfinished_tasks": ["A"],
               "user_forbidden": [], "decisions": [], "tool_trace_brief": "t"}
        parsed = {"unfinished_tasks": ["B"], "tool_trace_brief": "压缩版"}
        merged = _merge_with_fallback(old, new, parsed)
        assert "A" in merged["unfinished_tasks"]
        assert "B" in merged["unfinished_tasks"]
        assert merged["tool_trace_brief"] == "压缩版"


# ─────────────────── increment_merge_summary ───────────────────

class TestIncrementMergeSummary:
    def test_llm_failure_fallback(self, monkeypatch):
        """LLM 调用失败时应回退为代码拼接，关键字段不丢。"""
        monkeypatch.setattr(compressor, "get_latest_summary", lambda sid: "")
        monkeypatch.setattr(compressor, "call_llm_with_config", lambda *a, **k: None)
        new_round = {
            "user_task": "生成周报",
            "ai_reply": "周报已生成",
            "tool_calls": [{
                "tool_name": "write_file",
                "tool_arguments": {"file_path": "D:\\report.md"},
                "tool_result": "ok",
            }],
        }
        result = increment_merge_summary("sess-test", new_round)
        data = json.loads(result)
        assert data["tasks"][0]["user_task"] == "生成周报"
        assert "D:\\report.md" in data["key_context"]["key_paths"]

    def test_llm_success_uses_parsed(self, monkeypatch):
        """LLM 输出合法 JSON 时按其合并，但必要字段（tasks）仍以确定性数据为准。"""
        monkeypatch.setattr(compressor, "get_latest_summary", lambda sid: "")
        monkeypatch.setattr(
            compressor, "call_llm_with_config",
            lambda *a, **k: {"message": {"content": (
                '{"key_context": {"work_dir": "", "key_paths": [], "key_params": {}},'
                ' "tasks": [{"user_task": "旧任务"}], "unfinished_tasks": ["补充事项"],'
                ' "user_forbidden": [], "decisions": [], "tool_trace_brief": "LLM"'
                "}"
            )}},
        )
        new_round = {"user_task": "新任务", "ai_reply": "ok", "tool_calls": []}
        result = increment_merge_summary("sess-test", new_round)
        data = json.loads(result)
        # 确定性数据：新任务必须保留（LLM 的 tasks 不覆盖）
        assert any(t["user_task"] == "新任务" for t in data["tasks"])
        # LLM 输出仅用于补充非必要字段
        assert "补充事项" in data["unfinished_tasks"]
        assert data["tool_trace_brief"] == "LLM"

    def test_old_natural_summary_absorbed(self, monkeypatch):
        """旧自然语言摘要应被吸收进 tool_trace_brief，不丢失。"""
        monkeypatch.setattr(compressor, "get_latest_summary",
                            lambda sid: "旧的摘要文本")
        monkeypatch.setattr(compressor, "call_llm_with_config", lambda *a, **k: None)
        new_round = {"user_task": "新任务", "ai_reply": "ok", "tool_calls": []}
        result = increment_merge_summary("sess-test", new_round)
        data = json.loads(result)
        assert "旧的摘要文本" in data["tool_trace_brief"]

    def test_user_task_capped_at_3000(self, monkeypatch):
        """user_task 原文上限 3000 字符，超出部分被截断，结果受控。"""
        monkeypatch.setattr(compressor, "get_latest_summary", lambda sid: "")
        monkeypatch.setattr(compressor, "call_llm_with_config", lambda *a, **k: None)
        new_round = {"user_task": "任务" * 10000, "ai_reply": "ok", "tool_calls": []}
        result = increment_merge_summary("sess-test", new_round)
        data = json.loads(result)
        task_text = data["tasks"][0]["user_task"]
        assert len(task_text) <= 3000
        assert len(result) < 3500


# ─────────────────────── 模块级常量一致性 ───────────────────────

class TestConstantsConsistency:
    def test_importable(self):
        assert compressor is not None
        assert RECENT_TURNS_KEEP >= 1
        assert TOKEN_TARGET > 0
