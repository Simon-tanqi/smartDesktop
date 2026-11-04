# -*- coding: utf-8 -*-
"""HTTP 边界回归：空消息、非法 session_id、上传路径穿越。

不纳入 CI（需要完整依赖），本地运行：
    python -m pytest tests/test_api_boundary.py -q
"""
import io
import sys
from pathlib import Path

import pytest
from flask import Flask

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.chat import chat_bp  # noqa: E402
import backend.upload_api as upload_api  # noqa: E402


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.config.update(TESTING=True)
    app.register_blueprint(chat_bp)
    app.register_blueprint(upload_api.upload_bp)
    return app.test_client()


def test_empty_message_rejected(client):
    resp = client.post("/api/chat", json={"message": "", "session_id": "qa-session"})
    assert resp.status_code == 400
    assert "不能为空" in resp.get_json()["message"]


def test_blank_message_rejected(client):
    resp = client.post("/api/chat", json={"message": "   ", "session_id": "qa-session"})
    assert resp.status_code == 400


def test_invalid_session_id_rejected(client):
    resp = client.post("/api/chat", json={"message": "hi", "session_id": "../evil"})
    assert resp.status_code == 400


def test_save_session_requires_id(client):
    resp = client.post("/api/sessions", json={"title": "no-id"})
    assert resp.status_code == 400


def test_upload_rejects_traversal_session_id(client, monkeypatch, tmp_path):
    monkeypatch.setattr(upload_api, "UPLOAD_ROOT", str(tmp_path))
    resp = client.post(
        "/api/upload",
        data={"session_id": "../evil", "file": (io.BytesIO(b"x"), "a.txt")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert not (tmp_path.parent / "evil").exists()


def test_upload_sanitizes_filename(client, monkeypatch, tmp_path):
    monkeypatch.setattr(upload_api, "UPLOAD_ROOT", str(tmp_path))
    resp = client.post(
        "/api/upload",
        data={"session_id": "s1", "file": (io.BytesIO(b"x"), r"..\..\pwn.txt")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["file_name"] == "pwn.txt"
    saved = Path(body["file_path"])
    assert tmp_path in saved.parents
    assert saved.name.endswith("_pwn.txt")


def test_upload_keeps_unicode_filename(client, monkeypatch, tmp_path):
    monkeypatch.setattr(upload_api, "UPLOAD_ROOT", str(tmp_path))
    resp = client.post(
        "/api/upload",
        data={"session_id": "s1", "file": (io.BytesIO(b"x"), "季度 报告.txt")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    assert resp.get_json()["file_name"] == "季度 报告.txt"
