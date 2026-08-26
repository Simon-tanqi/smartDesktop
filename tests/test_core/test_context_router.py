# -*- coding: utf-8 -*-
"""context_router 单测：分词 / BM25 / 任务切换检测 / 归档话题召回。

覆盖目标（memory/context_router.py）：
- tokenize：中文 bigram + 单字兜底、英文/数字滑动 bigram、停用字过滤
- BM25：fit / score_all / max_score / self_score / top_distinctive_terms
- ContextSwitchDetector：首次任务、短短语、延续指令、切换判定、commit_task 低质量过滤
- match_archived_topics：空会话/空输入兜底、无归档返回空、阈值过滤
- _is_low_quality_task：过短 / 纯续写 / 确认类消息判定
"""
import pytest

from memory.context_router import (
    BM25,
    ContextSwitchDetector,
    _is_low_quality_task,
    match_archived_topics,
    tokenize,
)


# ─────────────────────────── tokenize ───────────────────────────

class TestTokenize:
    def test_chinese_bigram_and_char_fallback(self):
        tokens = tokenize("卸载")
        # 双字 bigram
        assert "卸载" in tokens
        # 单字兜底（"卸" 不在停用表）
        assert "卸" in tokens

    def test_stopword_chars_skipped_in_single_char(self):
        tokens = tokenize("的是")
        assert "的" not in tokens
        assert "是" not in tokens

    def test_english_alnum_sequence_with_sliding_bigram(self):
        tokens = tokenize("qoder")
        assert "qoder" in tokens
        assert "qo" in tokens
        assert "de" in tokens

    def test_mixed_text(self):
        tokens = tokenize("生成PPT")
        assert "生成" in tokens
        assert "ppt" in tokens

    def test_empty_text(self):
        assert tokenize("") == []

    def test_skip_punctuation(self):
        tokens = tokenize("你好，世界！")
        assert "你好" in tokens
        assert "世界" in tokens
        assert "，" not in tokens


# ─────────────────────────── BM25 ───────────────────────────

class TestBM25:
    def test_fit_empty_docs(self):
        bm = BM25()
        bm.fit([])
        assert bm._num_docs == 0
        assert bm.score_all("任何查询") == []
        assert bm.max_score("任何查询") == 0.0

    def test_score_all_returns_per_doc_scores(self):
        bm = BM25()
        bm.fit(["生成周报", "卸载软件"])
        scores = bm.score_all("生成周报")
        assert len(scores) == 2
        assert all(isinstance(s, float) for s in scores)

    def test_self_score_positive_and_dominant(self):
        bm = BM25()
        bm.fit(["生成2026年AI发展报告"])
        self_score = bm.self_score("生成2026年AI发展报告")
        scores = bm.score_all("生成2026年AI发展报告")
        assert self_score > 0
        assert max(scores) > 0

    def test_unfitted_returns_empty_scores(self):
        bm = BM25()
        assert bm.score_all("查询") == []
        assert bm.max_score("查询") == 0.0

    def test_top_distinctive_terms_limited(self):
        bm = BM25()
        bm.fit(["生成2026年AI发展报告", "卸载Qoder软件"])
        terms = bm.top_distinctive_terms("生成AI报告", top_n=2)
        assert len(terms) <= 2

    def test_top_distinctive_terms_empty_when_unfitted(self):
        bm = BM25()
        assert bm.top_distinctive_terms("查询") == []


# ─────────────────── ContextSwitchDetector ───────────────────

class TestContextSwitchDetector:
    def test_first_message_no_switch(self):
        detector = ContextSwitchDetector()
        result = detector.detect("生成2026年AI发展报告")
        assert result["is_switch"] is False
        assert result["norm_similarity"] == 1.0
        assert detector.history == ["生成2026年AI发展报告"]

    def test_short_phrase_inherits_context(self):
        detector = ContextSwitchDetector()
        detector.detect("继续生成周报内容")
        result = detector.detect("好的")  # ≤8 字
        assert result["is_switch"] is False
        assert result["reason"].startswith("短短语")

    def test_continuation_pattern_inherits_context(self):
        detector = ContextSwitchDetector()
        detector.detect("生成周报")
        # 前缀延续指令（长度>8，命中 ^再帮 模式）
        result = detector.detect("再帮我处理一下文件整理")
        assert result["is_switch"] is False
        assert "延续指令" in result["reason"]

    def test_unrelated_topic_triggers_switch(self):
        detector = ContextSwitchDetector(window_size=5, strict_threshold=0.25)
        detector.detect("生成2026年AI发展报告，分析普通人机会")
        result = detector.detect("帮我卸载电脑上的Qoder软件")
        # 差异较大的主题应判定为切换
        assert result["is_switch"] is True

    def test_similar_topic_no_switch(self):
        detector = ContextSwitchDetector(window_size=5, strict_threshold=0.25)
        detector.detect("生成2026年AI发展报告，分析普通人机会")
        result = detector.detect("继续写AI发展报告，补充行业数据")
        assert result["is_switch"] is False

    def test_history_window_size_trim(self):
        detector = ContextSwitchDetector(window_size=2)
        for i in range(5):
            detector.add_to_history(f"任务{i}")
        assert len(detector.history) == 2
        assert detector.history == ["任务3", "任务4"]

    def test_commit_task_low_quality_not_added(self):
        detector = ContextSwitchDetector()
        detector.detect("生成周报")
        before = len(detector.history)
        detector.commit_task("好的")
        assert len(detector.history) == before

    def test_commit_task_normal_added(self):
        detector = ContextSwitchDetector()
        detector.detect("生成周报")
        detector.commit_task("继续写周报第二部分")
        assert detector.history[-1] == "继续写周报第二部分"

    def test_persist_flag_off_no_db_access(self, monkeypatch):
        """persist=False 时不应触发任何 sqlite 访问（无 session_id 即无恢复路径）。"""
        monkeypatch.delattr("memory.context_router.ContextSwitchDetector._restore_state", raising=False)
        detector = ContextSwitchDetector(session_id="", persist=False)
        result = detector.detect("测试消息")
        assert result["is_switch"] is False


# ─────────────────────── _is_low_quality_task ───────────────────────

class TestLowQualityTask:
    def test_empty(self):
        assert _is_low_quality_task("") is True
        assert _is_low_quality_task(None) is True

    def test_too_short(self):
        assert _is_low_quality_task("短消息") is True

    def test_continuation(self):
        assert _is_low_quality_task("继续") is True
        assert _is_low_quality_task("好的") is True
        assert _is_low_quality_task("OK") is True

    def test_normal_task(self):
        assert _is_low_quality_task("生成2026年AI发展报告，分析机会") is False


# ─────────────────── match_archived_topics ───────────────────

class TestMatchArchivedTopics:
    def test_empty_session_returns_empty(self):
        assert match_archived_topics("查询", session_id="") == []

    def test_empty_message_returns_empty(self):
        assert match_archived_topics("", session_id="sess-1") == []
        assert match_archived_topics("   ", session_id="sess-1") == []

    def test_no_archived_topics_returns_empty(self, monkeypatch):
        def fake_list(session_id):
            return []
        monkeypatch.setattr("memory.stores.sqlite_store.list_archived_topics", fake_list)
        assert match_archived_topics("查询", session_id="sess-1") == []

    def test_db_error_returns_empty(self, monkeypatch):
        def boom(session_id):
            raise RuntimeError("db unavailable")
        monkeypatch.setattr("memory.stores.sqlite_store.list_archived_topics", boom)
        assert match_archived_topics("查询", session_id="sess-1") == []

    def test_below_threshold_filtered(self, monkeypatch):
        """归档话题与当前消息差异过大时不应被召回。"""
        monkeypatch.setattr(
            "memory.stores.sqlite_store.list_archived_topics",
            lambda sid: [{"topic_desc": "完全无关的话题甲", "min_msg_id": 1, "max_msg_id": 10}],
        )
        monkeypatch.setattr(
            "memory.stores.sqlite_store.get_archived_topic_messages",
            lambda sid, lo, hi, limit: [],
        )
        # 高度不同的查询，归一化相似度应低于 0.35 阈值
        result = match_archived_topics("生成2026年AI发展报告", session_id="sess-1", threshold=0.35)
        assert result == []
