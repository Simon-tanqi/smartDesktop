"""app.py Flask API 集成测试"""
import json
import pytest
from app import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


class TestIndexRoute:
    def test_index_returns_html(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert b"AI Agent" in response.data


class TestSessionsAPI:
    def test_get_sessions(self, client):
        response = client.get("/api/sessions")
        assert response.status_code == 200
        data = response.get_json()
        assert isinstance(data, list)

    def test_post_session(self, client):
        response = client.post("/api/sessions", json={
            "id": "test_session_001",
            "title": "测试会话"
        })
        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True

    def test_delete_session(self, client):
        # 先创建
        client.post("/api/sessions", json={"id": "to_delete", "title": "删除我"})
        # 再删除
        response = client.delete("/api/sessions/to_delete")
        assert response.status_code == 200

    def test_get_active_session(self, client):
        response = client.get("/api/sessions/active")
        assert response.status_code == 200
        data = response.get_json()
        assert "session_id" in data

    def test_get_session_messages(self, client):
        # 先创建会话
        client.post("/api/sessions", json={"id": "msg_test", "title": "消息测试"})
        response = client.get("/api/sessions/msg_test")
        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        assert "messages" in data


class TestModelsAPI:
    def test_get_models(self, client):
        response = client.get("/api/models")
        assert response.status_code == 200
        data = response.get_json()
        assert isinstance(data, list)
        assert len(data) >= 1
        for m in data:
            assert "display" in m
            assert "provider" in m
            assert "model" in m


class TestChatAPI:
    def test_chat_missing_session_id(self, client):
        response = client.post("/api/chat", json={
            "message": "hello"
        })
        assert response.status_code == 400

    def test_chat_empty_message(self, client):
        # 创建会话
        client.post("/api/sessions", json={"id": "chat_test", "title": "chat"})
        response = client.post("/api/chat", json={
            "message": "",
            "session_id": "chat_test"
        })
        # 空消息可能被拒绝或正常处理
        assert response.status_code in [200, 400]
