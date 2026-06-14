import os
import sys
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from memory import config as mem_config
from memory.stores import json_store
from memory import session as mem_session


ATTRS_TO_PATCH = {
    "MEMORY_DIR": "memory",
    "SESSION_DIR": "session",
    "LOGS_DIR": "logs",
    "MEMORY_FILE": "memory/user_memory.json",
    "COMPRESSED_FILE": "memory/compressed_memory.json",
    "SHORT_TERM_FILE": "memory/short_term_memory.json",
    "SESSION_LOG_FILE": "session/session_log.json",
    "CHAT_DB": "session/chat_history.db",
    "ACTIVE_SESSION_FILE": "session/active_session.txt",
    "USER_ID_FILE": "session/user_id.txt",
    "OPERATION_LOG_DB": "logs/operation_log.db",
    "TRACE_DB": "logs/agent_trace.db",
}


@pytest.fixture(autouse=True)
def isolated_memory(tmp_path, monkeypatch):
    """每个测试使用独立的临时目录"""
    for attr, rel_path in ATTRS_TO_PATCH.items():
        full_path = str(tmp_path / rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        # Patch config module
        monkeypatch.setattr(mem_config, attr, full_path)
        # Patch json_store module (imports MEMORY_FILE, COMPRESSED_FILE, etc.)
        if hasattr(json_store, attr):
            monkeypatch.setattr(json_store, attr, full_path)
        # Patch session module (imports ACTIVE_SESSION_FILE, LOGS_DIR)
        if hasattr(mem_session, attr):
            monkeypatch.setattr(mem_session, attr, full_path)

    yield


@pytest.fixture
def sample_user_memory():
    return {
        "user_info": {"name": "张三", "job_title": "开发", "company": "小米"},
        "core_knowledge": ["Python", "Flask"],
        "projects": ["[06-14] MyAgent"],
        "preferences": ["深色主题"],
        "facts": ["住深圳"]
    }


@pytest.fixture
def sample_messages():
    return [
        {"role": "user", "content": "帮我写一个冒泡排序"},
        {"role": "assistant", "content": json.dumps({"tool_name": "get_workspace_path", "arguments": {}})},
        {"role": "user", "content": '工具 get_workspace_path 返回：{"workspace": "/tmp/work_space"}'},
        {"role": "assistant", "content": json.dumps({"tool_name": "write_file", "arguments": {"file_path": "/tmp/work_space/bubble.py", "content": "def bubble_sort(arr): pass"}})},
        {"role": "user", "content": '工具 write_file 返回：{"success": true, "message": "写入成功"}'},
        {"role": "assistant", "content": json.dumps({"final_message": "已生成冒泡排序代码"})},
    ]
