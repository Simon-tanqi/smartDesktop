# -*- coding: utf-8 -*-
"""memory/extractor.py 纯函数单元测试。

只测结果解析的纯函数部分（_clean_llm_output / _safe_json_parse /
_build_extraction_prompt / register_custom_categories），
不触发真实 LLM 调用，也不依赖 Windows 专属包。
"""
import pytest

from memory.extractor import (
    FACT_EXTRACTION_PROMPT_BASE,
    _build_extraction_prompt,
    _clean_llm_output,
    _safe_json_parse,
    _custom_skill_categories,
    register_custom_categories,
)


# ── _clean_llm_output ────────────────────────────────────────────────

class TestCleanLlmOutput:
    def test_none_returns_empty(self):
        assert _clean_llm_output(None) == ""

    def test_empty_returns_empty(self):
        assert _clean_llm_output("") == ""
        assert _clean_llm_output("   \n  ") == ""

    def test_strips_thinking_markers(self):
        raw = "<|begin_of_thinking|>先分析再输出<|end_of_thinking|>{\"fact\": \"x\"}"
        cleaned = _clean_llm_output(raw)
        assert "<|" not in cleaned and "|>" not in cleaned
        assert '"fact": "x"' in cleaned

    def test_strips_code_block(self):
        raw = '```json\n{"project": "P"}\n```'
        cleaned = _clean_llm_output(raw)
        assert cleaned.startswith("{")
        assert "```" not in cleaned

    def test_preserves_json_content(self):
        raw = '<|begin_of_thinking|>思考中<|end_of_thinking|>```json\n{"fact": "你好"}\n```'
        cleaned = _clean_llm_output(raw)
        assert "你好" in cleaned
        assert "```" not in cleaned and "<|" not in cleaned


# ── _safe_json_parse ─────────────────────────────────────────────────

class TestSafeJsonParse:
    def test_plain_json(self):
        assert _safe_json_parse('{"fact": "x"}') == {"fact": "x"}

    def test_with_thinking_markers(self):
        raw = '<|begin_of_thinking|>分析<|end_of_thinking|>{"project": "P"}'
        assert _safe_json_parse(raw) == {"project": "P"}

    def test_with_code_block(self):
        raw = '```json\n{"knowledge": "K"}\n```'
        assert _safe_json_parse(raw) == {"knowledge": "K"}

    def test_embedded_json_in_text(self):
        raw = '好的，结果如下：{"fact": "喜欢咖啡"}，请查收。'
        assert _safe_json_parse(raw) == {"fact": "喜欢咖啡"}

    def test_invalid_json_returns_none(self):
        assert _safe_json_parse("这不是 JSON") is None

    def test_empty_returns_none(self):
        assert _safe_json_parse("") is None
        assert _safe_json_parse("<|begin_of_thinking|>只思考<|end_of_thinking|>") is None

    def test_none_object_parsed(self):
        assert _safe_json_parse('{"none": true}') == {"none": True}


# ── 提取提示词构建 ───────────────────────────────────────────────────

class TestExtractionPrompt:
    def test_no_custom_categories_uses_base(self):
        prompt = _build_extraction_prompt([])
        assert prompt == FACT_EXTRACTION_PROMPT_BASE.format(
            extra_categories="", extra_fields=""
        )
        assert "股票" not in prompt

    def test_with_explicit_categories(self):
        cats = [{"key": "stock", "label": "股票关注", "description": "用户关注的股票"}]
        prompt = _build_extraction_prompt(cats)
        assert "股票关注" in prompt
        assert '"stock": "股票关注相关"' in prompt
        # 自定义类别从第 5 项开始编号
        assert "5. 【股票关注】用户关注的股票" in prompt

    def test_register_custom_categories_updates_global(self):
        try:
            register_custom_categories([{"key": "music", "label": "音乐偏好"}])
            prompt = _build_extraction_prompt()  # 使用全局注册类别
            assert "音乐偏好" in prompt
            assert '"music": "音乐偏好相关"' in prompt
        finally:
            register_custom_categories([])
        assert _custom_skill_categories == []

    def test_register_custom_categories_none(self):
        try:
            register_custom_categories(None)
            prompt = _build_extraction_prompt()
            assert "extra_categories" not in prompt
        finally:
            register_custom_categories([])
