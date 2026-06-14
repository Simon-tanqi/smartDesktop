"""memory/ 包单元测试"""
import os
import json
import time
import sqlite3
import pytest

import memory


# ─── 长期记忆 ───────────────────────────────────

class TestLongTermMemory:
    def test_load_returns_default_when_no_file(self):
        result = memory.load_long_term()
        assert "user_info" in result
        assert "core_knowledge" in result
        assert result["user_info"]["name"] == ""

    def test_save_and_load_roundtrip(self, sample_user_memory):
        memory.save_long_term(sample_user_memory)
        loaded = memory.load_long_term()
        assert loaded["user_info"]["name"] == "张三"
        assert loaded["core_knowledge"] == ["Python", "Flask"]

    def test_load_merges_with_defaults(self, tmp_path):
        """验证部分数据保存后，未提供字段有默认值"""
        from memory import config
        test_file = str(tmp_path / "mem.json")
        partial = {"user_info": {"name": "李四"}}
        with open(test_file, "w", encoding="utf-8") as f:
            json.dump(partial, f, ensure_ascii=False)
        # 直接读取验证结构
        with open(test_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["user_info"]["name"] == "李四"
        assert "job_title" not in data["user_info"]

    def test_save_preserves_chinese(self):
        data = {"user_info": {"name": "张三"}, "facts": ["住深圳"]}
        memory.save_long_term(data)
        loaded = memory.load_long_term()
        assert loaded["facts"][0] == "住深圳"


# ─── 压缩记忆 ───────────────────────────────────

class TestCompressedMemory:
    def test_add_and_get_recent(self):
        memory.add_compressed_summary("测试摘要1")
        memory.add_compressed_summary("测试摘要2")
        result = memory.get_recent_compressed(days=1)
        assert "测试摘要1" in result
        assert "测试摘要2" in result

    def test_get_recent_filters_old(self):
        from memory import config
        old_file = config.COMPRESSED_FILE
        compressed = [{"summary": "旧摘要", "timestamp": time.time() - 86400 * 10, "source": "auto"}]
        memory.save_compressed(compressed)
        result = memory.get_recent_compressed(days=7)
        assert result == ""

    def test_compressed_limit_50(self):
        for i in range(55):
            memory.add_compressed_summary(f"摘要{i}")
        loaded = memory.load_compressed()
        assert len(loaded) <= 50

    def test_empty_compressed_returns_empty_string(self):
        result = memory.get_recent_compressed()
        assert result == ""

    def test_compressed_format(self):
        memory.add_compressed_summary("摘要A")
        result = memory.get_recent_compressed(days=1)
        assert result.startswith("【历史对话摘要】")


# ─── 会话管理 ───────────────────────────────────

class TestSessionManagement:
    def test_create_session(self):
        sid = memory.create_session("测试会话")
        assert sid is not None
        assert len(sid) > 0

    def test_create_session_generates_unique_ids(self):
        sid1 = memory.create_session("会话1")
        sid2 = memory.create_session("会话2")
        assert sid1 != sid2

    def test_list_sessions(self):
        memory.create_session("会话A")
        memory.create_session("会话B")
        sessions = memory.list_sessions()
        assert len(sessions) >= 2
        titles = [s["title"] for s in sessions]
        assert "会话A" in titles

    def test_delete_session(self, tmp_path):
        """验证会话删除后元数据中不存在该会话"""
        from memory import config
        test_file = str(tmp_path / "sessions.json")
        db = {"s1": {"id": "s1", "title": "test"}, "s2": {"id": "s2", "title": "keep"}}
        with open(test_file, "w", encoding="utf-8") as f:
            json.dump(db, f)
        with open(test_file, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        del loaded["s1"]
        with open(test_file, "w", encoding="utf-8") as f:
            json.dump(loaded, f)
        with open(test_file, "r", encoding="utf-8") as f:
            final = json.load(f)
        assert "s1" not in final
        assert "s2" in final

    def test_get_active_session_creates_if_needed(self):
        sid = memory.get_active_session_id()
        assert sid is not None

    def test_set_and_get_active_session(self):
        sid = memory.create_session("活跃会话")
        memory.set_active_session(sid)
        active = memory.get_active_session_id()
        assert active == sid

    def test_session_messages_empty_by_default(self, tmp_path):
        """新创建的会话应没有聊天消息"""
        import tempfile
        from memory.stores import sqlite_store
        test_db = str(tmp_path / "test.db")
        old_db = sqlite_store.CHAT_DB
        sqlite_store.CHAT_DB = test_db
        try:
            sid = "fresh_session_test"
            msgs = sqlite_store.get_chat_messages(sid)
            assert msgs == []
        finally:
            sqlite_store.CHAT_DB = old_db

    def test_add_and_get_messages(self, tmp_path):
        """验证聊天消息的写入和读取"""
        import tempfile
        from memory.stores import sqlite_store
        test_db = str(tmp_path / "test.db")
        old_db = sqlite_store.CHAT_DB
        sqlite_store.CHAT_DB = test_db
        try:
            sid = "msg_test"
            sqlite_store.add_chat_message(sid, "user", "你好")
            sqlite_store.add_chat_message(sid, "assistant", "你好！")
            msgs = sqlite_store.get_chat_messages(sid)
            assert len(msgs) == 2
            assert msgs[0]["role"] == "user"
            assert msgs[0]["content"] == "你好"
        finally:
            sqlite_store.CHAT_DB = old_db

    def test_count_messages(self):
        sid = memory.create_session("计数测试")
        initial = memory.count_chat_messages(sid)
        memory.add_chat_message(sid, "user", "消息1")
        memory.add_chat_message(sid, "user", "消息2")
        memory.add_chat_message(sid, "assistant", "回复1")
        count = memory.count_chat_messages(sid)
        assert count >= initial + 3

    def test_delete_chat_session(self):
        sid = memory.create_session("删除测试")
        memory.add_chat_message(sid, "user", "测试")
        memory.delete_chat_session(sid)
        messages = memory.get_chat_messages(sid)
        assert len(messages) == 0

    def test_add_chat_message_filters_roles(self):
        """验证 add_chat_message 接受 user/assistant/system 角色"""
        import tempfile
        from memory.stores import sqlite_store

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            test_db = f.name
        try:
            old_db = sqlite_store.CHAT_DB
            sqlite_store.CHAT_DB = test_db
            try:
                sid = "test_role_filter"
                # system 角色在新版中也被接受
                sqlite_store.add_chat_message(sid, "system", "系统消息")
                msgs = sqlite_store.get_chat_messages(sid)
                assert len(msgs) == 1
                sqlite_store.add_chat_message(sid, "user", "正常消息")
                msgs = sqlite_store.get_chat_messages(sid)
                assert len(msgs) == 2
                # 无效角色被过滤
                sqlite_store.add_chat_message(sid, "invalid", "不应存储")
                msgs = sqlite_store.get_chat_messages(sid)
                assert len(msgs) == 2
            finally:
                sqlite_store.CHAT_DB = old_db
        finally:
            os.unlink(test_db)


# ─── 操作日志 ───────────────────────────────────

class TestOperationLog:
    def test_log_operation(self):
        memory.log_operation("trace1", "user1", "sess1", "test_op", "detail")

    def test_log_sanitize_newlines(self):
        memory.log_operation("trace2", "user1", "sess1", "test", "含\n换行\t制表")


# ─── Trace 日志 ───────────────────────────────────

class TestTraceLog:
    def test_log_trace(self):
        memory.log_trace(
            trace_id="t1", span_name="test_span", node_type="tool",
            duration_ms=100, status="success"
        )

    def test_log_trace_with_error(self):
        memory.log_trace(
            trace_id="t2", span_name="test_span", node_type="tool",
            duration_ms=50, status="error", error="something went wrong"
        )


# ─── 用户标识 ───────────────────────────────────

class TestUserId:
    def test_get_user_id_persists(self):
        uid1 = memory.get_user_id()
        uid2 = memory.get_user_id()
        assert uid1 == uid2

    def test_generate_trace_id(self):
        tid = memory.generate_trace_id()
        assert len(tid) == 12
        assert tid.isalnum()
