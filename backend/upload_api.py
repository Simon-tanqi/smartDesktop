"""
文件上传 API — Blueprint。

POST /api/upload
  参数: session_id (form), file (multipart)
  返回: {success, file_path, file_name, size}

会话隔离上传目录: work_space/uploads/{session_id}/
文件命名: {uuid}_{原始文件名} → 防重名, 保留扩展名以供工具识别。
"""
import os
import uuid
from flask import Blueprint, request, jsonify

upload_bp = Blueprint("upload", __name__)

# 上传根目录: 项目 work_space/uploads/
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_ROOT = os.path.join(_BASE_DIR, "work_space", "uploads")

MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB
ALLOWED_EXTENSIONS = {".txt", ".pdf", ".docx", ".xlsx", ".pptx", ".csv",
                      ".json", ".xml", ".yaml", ".yml", ".md", ".log",
                      ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg",
                      ".mp4", ".avi", ".mov", ".mkv", ".webm",
                      ".mp3", ".wav", ".ogg", ".flac",
                      ".zip", ".rar", ".7z", ".tar", ".gz",
                      ".py", ".js", ".ts", ".html", ".css", ".c", ".cpp", ".h", ".java", ".go", ".rs",
                      ".exe", ".msi", ".apk", ".dmg", ".deb", ".rpm"}


@upload_bp.route("/api/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return jsonify({"success": False, "message": "未选择文件"}), 400

    file = request.files["file"]
    if not file.filename or file.filename.strip() == "":
        return jsonify({"success": False, "message": "文件名为空"}), 400

    session_id = request.form.get("session_id", "").strip()
    if not session_id:
        return jsonify({"success": False, "message": "缺少 session_id"}), 400

    # 扩展名白名单
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"success": False, "message": f"不支持的文件类型: {ext}"}), 400

    # 会话隔离目录
    session_dir = os.path.join(UPLOAD_ROOT, session_id)
    os.makedirs(session_dir, exist_ok=True)

    # UUID 防重名, 保留扩展名
    unique_name = f"{uuid.uuid4().hex}_{file.filename}"
    save_path = os.path.join(session_dir, unique_name)

    # 写入 + 大小限制
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > MAX_FILE_SIZE:
        return jsonify({"success": False, "message": f"文件过大 ({size / 1024 / 1024:.1f} MB), 上限 100 MB"}), 413

    file.save(save_path)

    return jsonify({
        "success": True,
        "file_path": os.path.abspath(save_path),
        "file_name": file.filename,
        "size": size,
    })


def cleanup_session_uploads(session_id: str):
    """删除指定会话的全部上传文件。"""
    import shutil
    session_dir = os.path.join(UPLOAD_ROOT, session_id)
    if os.path.isdir(session_dir):
        shutil.rmtree(session_dir, ignore_errors=True)
