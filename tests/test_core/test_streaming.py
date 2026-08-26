# -*- coding: utf-8 -*-
"""streaming 单测：SSE 输出 / 代码块剥离 / 推理轨迹解析 / 工具失败分类。

覆盖目标（core/streaming.py）：
- SSEStreamer：emit 序列化、不可序列化字段跳过、emit_final 时间戳兜底与回调
- strip_code_blocks：剥离 reasoning/action/json 围栏与指定 key 内联 JSON
- parse_reasoning_trace：代码块 JSON / 纯文本兜底 / 无匹配返回 None
- classify_tool_failure：empty / fatal / retryable / file_not_found / unknown
- strip_temp_system_messages：移除 [格式纠错]/[进展控制器] 临时 system 消息
- inline_compress：超限逐对合并摘要
"""
import json

import pytest

from core.streaming import (
    SSEStreamer,
    classify_tool_failure,
    inline_compress,
    is_file_not_found,
    parse_reasoning_trace,
    strip_code_blocks,
    strip_temp_system_messages,
)


def _parse_sse(sse_text: str) -> dict:
    """解析 SSE 文本为 dict：'data: {...}\n\n' → {...}"""
    assert sse_text.startswith("data: ")
    payload = sse_text[len("data: "):].strip()
    return json.loads(payload)


# ─────────────────────── SSEStreamer ───────────────────────

class TestSSEStreamer:
    def test_emit_structure(self):
        sse = SSEStreamer().emit("delta", content="hello")
        payload = _parse_sse(sse)
        assert payload["type"] == "delta"
        assert payload["content"] == "hello"

    def test_emit_unserializable_skipped(self):
        sse = SSEStreamer().emit("tool_result", bad=object(), ok=1)
        payload = _parse_sse(sse)
        assert "bad" not in payload
        assert payload["ok"] == 1

    def test_emit_final_has_complete_flag(self):
        sse = SSEStreamer().emit_final("完成", timestamp="2026-08-26 10:00:00")
        payload = _parse_sse(sse)
        assert payload["type"] == "final"
        assert payload["complete"] is True
        assert payload["text"] == "完成"

    def test_emit_final_timestamp_fallback(self):
        """未传时间戳时兜底为本地当前时间（非 None）。"""
        sse = SSEStreamer().emit_final("完成")
        payload = _parse_sse(sse)
        assert payload["timestamp"]
        assert len(payload["timestamp"]) == 19  # %Y-%m-%d %H:%M:%S

    def test_emit_final_triggers_callback(self):
        calls = []
        streamer = SSEStreamer()
        streamer.set_on_final(lambda text, ts: calls.append((text, ts)))
        streamer.emit_final("完成", timestamp="2026-08-26 10:00:00")
        assert calls == [("完成", "2026-08-26 10:00:00")]

    def test_emit_step_and_error_types(self):
        streamer = SSEStreamer()
        assert _parse_sse(streamer.emit_step("进行中"))["type"] == "step"
        assert _parse_sse(streamer.emit_error("出错"))["type"] == "error"


# ─────────────────────── strip_code_blocks ───────────────────────

class TestStripCodeBlocks:
    def test_strip_reasoning_block(self):
        text = "```reasoning\n内部思考\n```\n最终答案"
        assert strip_code_blocks(text) == "最终答案"

    def test_strip_action_block(self):
        text = "```action\n{\"tool\": \"x\"}\n```\n结果"
        assert strip_code_blocks(text) == "结果"

    def test_strip_json_block(self):
        text = "```json\n{\"a\": 1}\n```\n说明"
        assert strip_code_blocks(text) == "说明"

    def test_no_block_unchanged(self):
        text = "普通文本内容"
        assert strip_code_blocks(text) == text

    def test_inline_json_with_known_key_removed(self):
        text = "结果如下：{\"tool_name\": \"web_search\"} 完"
        result = strip_code_blocks(text)
        assert "tool_name" not in result

    def test_inline_json_nested_braces_removed(self):
        text = "{\"final_message\": {\"a\": {\"b\": 1}}} 结束"
        result = strip_code_blocks(text)
        assert "final_message" not in result


# ─────────────────────── parse_reasoning_trace ───────────────────────

class TestParseReasoningTrace:
    def test_empty_returns_none(self):
        assert parse_reasoning_trace("") is None

    def test_code_block_json(self):
        text = ("```reasoning\n"
                '{"current_intent": "搜索资料", "decision_rationale": "用户要最新数据"}\n'
                "```\n正文")
        result = parse_reasoning_trace(text)
        assert result is not None
        assert result.get("current_intent") == "搜索资料"

    def test_plain_text_fallback(self):
        text = "```reasoning\n这是一段纯文本推理\n```"
        result = parse_reasoning_trace(text)
        assert result is not None
        assert "纯文本推理" in result.get("current_intent", "")

    def test_no_trace_returns_none(self):
        assert parse_reasoning_trace("没有推理轨迹的普通输出") is None


# ─────────────────────── classify_tool_failure ───────────────────────

class TestClassifyToolFailure:
    def test_empty_status(self):
        assert classify_tool_failure({"status": "empty"}) == "empty_result"

    def test_fatal_codes(self):
        for code in ("AUTH_FAILED", "PERMISSION_DENIED", "TOOL_NOT_FOUND"):
            assert classify_tool_failure({"error_code": code}) == "fatal"

    def test_retryable_codes(self):
        for code in ("TIMEOUT", "RATE_LIMITED", "NETWORK_ERROR"):
            assert classify_tool_failure({"error_code": code}) == "retryable"

    def test_file_not_found(self):
        result = classify_tool_failure({"error": "文件不存在: D:\\a.txt"})
        assert result == "file_not_found"
        result = classify_tool_failure({"message": "No such file or directory"})
        assert result == "file_not_found"

    def test_unknown(self):
        assert classify_tool_failure({"error": "其他错误"}) == "unknown"

    def test_is_file_not_found_helpers(self):
        assert is_file_not_found("找不到文件") is True
        assert is_file_not_found("一切正常") is False
        assert is_file_not_found("") is False


# ─────────────────────── strip_temp_system_messages ───────────────────────

class TestStripTempSystemMessages:
    def test_removes_temp_markers(self):
        messages = [
            {"role": "system", "content": "你是助手"},
            {"role": "system", "content": "[格式纠错] 上轮输出格式有误"},
            {"role": "system", "content": "[进展控制器] 进度推进"},
            {"role": "user", "content": "你好"},
        ]
        result = strip_temp_system_messages(messages)
        assert len(result) == 2
        assert result[0]["content"] == "你是助手"
        assert result[1]["role"] == "user"

    def test_no_temp_unchanged(self):
        messages = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "你好"},
        ]
        assert strip_temp_system_messages(messages) == messages


# ─────────────────────── inline_compress ───────────────────────

class TestInlineCompress:
    def test_empty(self):
        assert inline_compress([]) == []

    def test_under_limit_unchanged(self):
        summaries = [
            {"current_intent": "短", "decision_rationale": "短", "active_hypothesis": ""},
            {"current_intent": "短2", "decision_rationale": "短2", "active_hypothesis": ""},
        ]
        assert inline_compress(summaries, max_chars=1000) == summaries

    def test_over_limit_merged(self):
        summaries = [
            {"current_intent": "很长的意图" * 100, "decision_rationale": "很长的理由" * 100,
             "active_hypothesis": ""},
            {"current_intent": "另一个长意图" * 100, "decision_rationale": "另一个长理由" * 100,
             "active_hypothesis": ""},
            {"current_intent": "第三个", "decision_rationale": "第三个", "active_hypothesis": ""},
        ]
        result = inline_compress(summaries, max_chars=100)
        assert len(result) < len(summaries)
        # 合并后仍保留全部信息（以合并块 + 尾部形式存在）
        merged_chars = sum(
            len(str(s.get("current_intent", ""))) + len(str(s.get("decision_rationale", "")))
            for s in result
        )
        assert merged_chars > 0
